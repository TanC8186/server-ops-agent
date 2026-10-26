"""M4 / RAG-10 回归：追问上下文与知识问答链路的真实接线。

覆盖三件事：

1. ``load_run_context`` 只允许同会话读取，跨会话/非法 run_id 返回 None；
2. ``/api/chat_stream`` 走 ``knowledge_service.answer_question``（不是旧 RAG Agent），
   事件带服务端分配的引用；
3. 追问携带的 run_id 归属不符时明确说明，不把别的运行串进来。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import observability
from app.api import chat as chat_api
from app.core import run_context
from app.services import chat_history
from app.services import knowledge_service as ks


RUN_ID = "a" * 32


@pytest.fixture()
def trace_dir(tmp_path, monkeypatch):
    # conftest 的 autouse fixture 已把 observability.TRACE_DIR 指到 tmp_path/traces。
    directory = tmp_path / "traces"
    directory.mkdir(exist_ok=True)
    monkeypatch.setattr(run_context, "TRACE_DIR", directory)
    monkeypatch.setattr(observability, "TRACE_DIR", directory)
    return directory


def _write_run(directory: Path, *, run_id: str = RUN_ID, session_id: str = "session-1",
               quality: str = "partial") -> Path:
    events = [
        {"ts": "2026-10-26T12:00:00", "run_id": run_id, "event": "run_start",
         "session_id": session_id, "execution_mode": "cloud_multi"},
        {"ts": "2026-10-26T12:00:01", "run_id": run_id, "event": "target_bound",
         "target": {"service": "svc-a", "instance_id": "ins-a"}},
        {"ts": "2026-10-26T12:00:02", "run_id": run_id, "event": "evidence",
         "evidence": {"id": "E1", "tool": "query_cpu_metrics",
                      "data": {"time_range": {"start_time": 1, "end_time": 2},
                               "partial_errors": ["E2 权限不足"]}}},
        {"ts": "2026-10-26T12:00:03", "run_id": run_id, "event": "agent_result",
         "agent": "reviewer", "observations": ["CPU 使用率 12%"],
         "hypotheses": ["可能存在上游超时"], "gaps": ["缺少磁盘证据"]},
        {"ts": "2026-10-26T12:00:04", "run_id": run_id, "event": "run_complete",
         "lifecycle": "completed", "quality": quality, "duration_ms": 4000},
    ]
    path = directory / f"{run_id}.jsonl"
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                    encoding="utf-8")
    return path


def test_same_session_run_is_loaded(trace_dir):
    _write_run(trace_dir)

    context = run_context.load_run_context(RUN_ID, "session-1")

    assert context is not None
    assert context["target"]["service"] == "svc-a"
    assert context["quality"] == "partial"
    assert context["lifecycle"] == "completed"
    assert any("12%" in item for item in context["observations"])
    assert context["time_window"] == {"start_time": 1, "end_time": 2}


def test_cross_session_run_is_not_exposed(trace_dir):
    _write_run(trace_dir, session_id="session-1")

    assert run_context.load_run_context(RUN_ID, "session-2") is None


@pytest.mark.parametrize("bad", ["../escape", "A" * 32, "x", "a" * 31])
def test_invalid_run_id_is_rejected(trace_dir, bad):
    _write_run(trace_dir)
    assert run_context.load_run_context(bad, "session-1") is None


def test_render_context_states_it_is_historical():
    rendered = run_context.render_run_context({
        "target": {"service": "svc-a"}, "quality": "partial",
        "lifecycle": "completed", "observations": ["CPU 12%"],
        "hypotheses": [], "gaps": [],
    })
    assert "svc-a" in rendered
    assert "不能证明此刻的状态" in rendered


class _FakeRetriever:
    def __init__(self, chunks):
        self.chunks = chunks

    def search(self, query, k):
        return list(self.chunks)


class _FakeModel:
    def __init__(self, answer="先执行 df -h [K1]。"):
        self.answer = answer
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.answer)


def _chunk():
    return ks.make_chunk(
        chunk_id="doc-disk:rev-1:1", document_id="doc-disk", revision="rev-1",
        text="磁盘满时先看 df -h 与 inode。", title="磁盘容量排查",
        heading_path=("运维手册", "磁盘"), source_uri="https://example.invalid/manual#disk",
        line_start=10, line_end=15, score=0.91,
    )


@pytest.fixture()
def client(trace_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_HISTORY_DIR", str(tmp_path / "chat"))
    monkeypatch.setattr(chat_history, "_history_dir", lambda: tmp_path / "chat")
    monkeypatch.setattr(chat_api, "_retriever", lambda: _FakeRetriever([_chunk()]))
    model = _FakeModel()
    monkeypatch.setattr(chat_api, "_chat_model", model)

    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client, model


def _events(response) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for line in response.text.splitlines() if line.startswith("data: ")]


def test_chat_stream_uses_structured_knowledge_pipeline(client):
    http, model = client
    response = http.post("/api/chat_stream",
                         json={"Id": "session-1", "Question": "磁盘满了怎么办"})

    events = _events(response)
    kinds = [event["type"] for event in events]
    assert kinds[0] == "retrieval"
    assert events[0]["status"] == "hit"
    assert events[0]["retrieval"]["chunks"][0]["chunk_id"] == "doc-disk:rev-1:1"
    assert kinds[-1] == "done"
    done = events[-1]
    assert done["outcome"] == "answered"
    assert done["citations"][0]["document_id"] == "doc-disk"
    assert done["tool_calls"] == []
    assert done["run_context"] is None
    assert len(model.calls) == 1


def test_chat_stream_includes_owned_run_context(client, trace_dir):
    http, model = client
    _write_run(trace_dir, session_id="session-1")

    response = http.post("/api/chat_stream",
                         json={"Id": "session-1", "Question": "那次 CPU 12% 说明什么",
                               "run_id": RUN_ID})
    done = _events(response)[-1]

    assert done["run_context"] is not None
    assert done["run_context"]["target"]["service"] == "svc-a"
    prompt = json.dumps(model.calls[0], ensure_ascii=False)
    assert "svc-a" in prompt and "不能证明此刻的状态" in prompt


def test_chat_stream_rejects_foreign_run(client, trace_dir):
    http, model = client
    _write_run(trace_dir, session_id="someone-else")

    response = http.post("/api/chat_stream",
                         json={"Id": "session-1", "Question": "追问", "run_id": RUN_ID})
    events = _events(response)
    done = events[-1]

    assert done["run_context"] is None
    assert "不属于当前会话" in (done.get("reason") or "")
    assert "svc-a" not in json.dumps(model.calls[0], ensure_ascii=False)


def test_history_records_turn_and_respects_clear(client):
    http, _ = client
    http.post("/api/chat_stream", json={"Id": "session-h", "Question": "磁盘满"})

    info = http.get("/api/chat/session/session-h").json()
    assert info["message_count"] == 2
    assert info["history"][0]["role"] == "user"
    assert info["history"][1]["role"] == "assistant"
    assert info["history"][1]["citations"][0]["id"] == "K1"

    cleared = http.post("/api/chat/clear", json={"sessionId": "session-h"})
    assert cleared.status_code == 200
    assert http.get("/api/chat/session/session-h").json()["message_count"] == 0


def test_history_trimming_keeps_pairs(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_DIR", str(tmp_path))
    store = chat_history.ChatHistoryStore(ttl_seconds=3600, max_turns=2, max_chars=10000)

    for index in range(5):
        store.append_turn("s", question=f"q{index}", answer=f"a{index}")

    messages = store.load("s")
    assert len(messages) == 4, "只保留最近 2 轮（4 条）"
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "q3"
    assert messages[-1]["role"] == "assistant"


def test_history_ttl_expiry_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_DIR", str(tmp_path))
    store = chat_history.ChatHistoryStore(ttl_seconds=0.01)
    store.append_turn("expired", question="q", answer="a")

    asyncio.run(asyncio.sleep(0.05))
    assert store.load("expired") == []


def test_session_id_cannot_escape_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_DIR", str(tmp_path))
    store = chat_history.ChatHistoryStore()
    store.append_turn("../../evil", question="q", answer="a")

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path
    assert ".." not in files[0].name
