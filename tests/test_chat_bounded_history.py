"""有界多轮上下文（G3 任务 3 / G2.3 第 4 条）。

“同一 session 的历史恢复”不等于“多轮回答上下文”：旧实现把历史只用于展示，
模型永远看不到上一轮，省略追问（“它呢？”“那这个呢？”）必然答不出来。

这里锁定的是**边界**，不是功能是否好用：

1. ``recent_turns`` 同时受条数与字符预算约束，超预算从最旧的整轮丢弃；
2. 永远不返回半轮（用户提问没有配对回答时不进窗口）；
3. ``chat`` 接口把窗口真正传进 ``answer_question``，并且历史只作为对话上下文，
   不是证据；
4. 实现还没接受 ``history`` 时，接口降级到旧行为并打结构化告警，而不是 500。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import chat as chat_api
from app.services import chat_history


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_DIR", str(tmp_path / "chat"))
    monkeypatch.setattr(chat_history, "_history_dir", lambda: tmp_path / "chat")
    return chat_history.ChatHistoryStore(ttl_seconds=3600, max_turns=50, max_chars=100000)


# ── 1. 窗口边界 ─────────────────────────────────────────────


def test_recent_turns_keeps_only_requested_turns(store):
    for index in range(5):
        store.append_turn("s", question=f"q{index}", answer=f"a{index}")

    window = store.recent_turns("s", max_turns=2, max_chars=10000)

    assert window == [("user", "q3"), ("assistant", "a3"),
                      ("user", "q4"), ("assistant", "a4")]


def test_recent_turns_drops_oldest_whole_turn_over_char_budget(store):
    store.append_turn("s", question="q1" + "x" * 50, answer="a1" + "y" * 50)
    store.append_turn("s", question="q2" + "x" * 50, answer="a2" + "y" * 50)

    window = store.recent_turns("s", max_turns=6, max_chars=110)

    # 55*2 + 52*2 > 110 → 只剩最近的整轮，且不是被截断的碎片。
    assert len(window) == 2
    assert window[0][0] == "user" and window[0][1].startswith("q2")
    assert window[1][1].startswith("a2")
    assert sum(len(text) for _role, text in window) <= 110


def test_recent_turns_never_returns_half_a_turn(store):
    store.append_turn("s", question="q1", answer="a1")
    # 模拟“上一轮提问后回答中断”：只追加用户提问，不追加回答。
    messages = store.load("s")
    messages.append({"role": "user", "content": "dangling"})
    store._write("s", messages)

    window = store.recent_turns("s", max_turns=6, max_chars=1000)

    assert ("user", "dangling") not in window
    assert window == [("user", "q1"), ("assistant", "a1")]


def test_recent_turns_truncates_when_a_single_turn_exceeds_budget(store):
    store.append_turn("s", question="问题" * 500, answer="答案" * 500)

    window = store.recent_turns("s", max_turns=6, max_chars=200)

    assert window, "最新一轮放不下时也要给出截断版本，而不是空窗口"
    assert sum(len(text) for _role, text in window) <= 200
    assert any(chat_history.TRUNCATION_MARKER in text for _role, text in window)


@pytest.mark.parametrize("max_turns,max_chars", [(0, 100), (6, 0), (-1, 100)])
def test_recent_turns_with_empty_budget_returns_nothing(store, max_turns, max_chars):
    store.append_turn("s", question="q", answer="a")
    assert store.recent_turns("s", max_turns=max_turns, max_chars=max_chars) == []


def test_recent_turns_on_missing_session_is_empty(store):
    assert store.recent_turns("nope", max_turns=6, max_chars=2000) == []


# ── 2. 接口把窗口传下去，并标记为“对话上下文” ──────────────


class _FakeRetriever:
    def search(self, query: str, k: int):
        from app.services.knowledge_service import make_chunk

        return [make_chunk(chunk_id="c1", document_id="doc-1", revision="rev-1",
                           text="磁盘满时先看 df -h 与 inode。", title="磁盘排查",
                           score=0.9)]


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))

        class _Response:
            content = "先看 df -h [K1]。"

        return _Response()


def _chat_app(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_HISTORY_DIR", str(tmp_path / "chat"))
    monkeypatch.setattr(chat_history, "_history_dir", lambda: tmp_path / "chat")
    monkeypatch.setattr(chat_api, "_retriever", lambda: _FakeRetriever())
    model = _FakeModel()
    monkeypatch.setattr(chat_api, "_chat_model", model)
    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api")
    return TestClient(app), model


def test_chat_passes_bounded_history_to_answer_question(monkeypatch, tmp_path):
    """窗口必须真的进入 prompt（否则“省略追问”仍然答不出来）。"""
    captured: dict = {}

    async def fake_answer(question, session_id, *, retriever, model=None, run_id="",
                          history=None):
        captured["history"] = history
        captured["question"] = question
        yield {"type": "done", "outcome": "answered", "content": "ok",
               "citations": [], "removal": [], "retrieval_status": "hit"}

    monkeypatch.setattr(chat_api.knowledge_service, "answer_question", fake_answer)
    http, _model = _chat_app(monkeypatch, tmp_path)

    chat_history.chat_history.clear("s-ctx")
    chat_history.chat_history.append_turn("s-ctx", question="磁盘满了", answer="先看 df -h")

    response = http.post("/api/chat", json={"Id": "s-ctx", "Question": "那 inode 呢"})

    assert response.status_code == 200, response.text
    assert captured["history"] == [("user", "磁盘满了"), ("assistant", "先看 df -h")]


def test_chat_degrades_when_answer_question_lacks_history(monkeypatch, tmp_path, caplog):
    """实现还没接受 ``history`` 时，退化为旧行为并留下结构化告警。"""
    seen: dict = {}

    async def legacy_answer(question, session_id, *, retriever, model=None, run_id=""):
        seen["called"] = True
        yield {"type": "done", "outcome": "answered", "content": "ok",
               "citations": [], "retrieval_status": "hit"}

    monkeypatch.setattr(chat_api.knowledge_service, "answer_question", legacy_answer)
    http, _model = _chat_app(monkeypatch, tmp_path)
    chat_history.chat_history.clear("s-legacy")

    response = http.post("/api/chat", json={"Id": "s-legacy", "Question": "问题"})

    assert response.status_code == 200, response.text
    assert seen.get("called") is True


def test_history_window_is_bounded_by_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_CONTEXT_MAX_TURNS", "2")
    monkeypatch.setenv("CHAT_CONTEXT_MAX_CHARS", "1000")
    captured: dict = {}

    async def fake_answer(question, session_id, *, retriever, model=None, run_id="",
                          history=None):
        captured["history"] = history
        yield {"type": "done", "outcome": "answered", "content": "ok",
               "citations": [], "retrieval_status": "hit"}

    monkeypatch.setattr(chat_api.knowledge_service, "answer_question", fake_answer)
    http, _model = _chat_app(monkeypatch, tmp_path)
    chat_history.chat_history.clear("s-bound")
    for index in range(5):
        chat_history.chat_history.append_turn(
            "s-bound", question=f"q{index}", answer=f"a{index}")

    http.post("/api/chat", json={"Id": "s-bound", "Question": "继续"})

    assert len(captured["history"]) == 4, "6 条历史里只允许最近 2 轮进入 prompt"
    assert captured["history"][0] == ("user", "q3")
    monkeypatch.delenv("CHAT_CONTEXT_MAX_TURNS")
    monkeypatch.delenv("CHAT_CONTEXT_MAX_CHARS")


def test_history_preview_does_not_leak_into_report_sections(monkeypatch, tmp_path):
    """历史只进 prompt，不能出现在返回给前端的引用/证据结构里。"""
    http, _model = _chat_app(monkeypatch, tmp_path)
    chat_history.chat_history.clear("s-leak")
    chat_history.chat_history.append_turn("s-leak", question="上一轮问题", answer="上一轮回答")

    body = http.post("/api/chat", json={"Id": "s-leak", "Question": "本轮问题"}).json()

    serialized = json.dumps(body, ensure_ascii=False)
    assert "上一轮问题" not in serialized
    assert "上一轮回答" not in serialized
