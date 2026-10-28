"""聊天流事件持久化与续读的回归测试（G5）。

只验证 ``app.services.chat_events`` 这一层的契约，不伪造真实 SSE/LLM：

- ``run_id`` 必须是 32 位小写十六进制，任何路径穿越尝试在拼路径前被拒绝；
- 缺 ``seq`` 的事件不能落盘（否则永远是读不到的尾部）；
- ``read_after`` 只返回 ``seq > after_seq`` 的事件，按落盘顺序，重复
  ``event_id`` 只保留一条；
- 分页：``truncated`` 为 true 时必须给出 ``next_after_seq``，保证客户端能翻到终态；
- 字节上限与 TTL 清理：超限停止追加并返回 false（不静默丢），过期文件被删除。

目录通过 ``CHAT_STREAM_DIR`` 指向 ``tmp_path``，不触碰仓库的 ``logs/``。
"""

from __future__ import annotations

import json
import os

import pytest

from app.services import chat_events

RUN_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(chat_events.CHAT_STREAM_DIR_ENV, str(tmp_path / "streams"))
    monkeypatch.delenv(chat_events.CHAT_STREAM_MAX_BYTES_ENV, raising=False)
    monkeypatch.delenv(chat_events.CHAT_STREAM_TTL_HOURS_ENV, raising=False)
    return tmp_path / "streams"


def _event(seq: int, text: str, *, run_id: str = RUN_ID) -> dict:
    return {
        "type": "content",
        "data": text,
        "seq": seq,
        "event_id": f"{run_id}:{seq}",
        "run_id": run_id,
    }


def test_run_id_shape_is_enforced_before_path_join(isolated_dir):
    store = chat_events.ChatEventStore()
    for bad in [
        "../../etc/passwd",
        "0123456789ABCDEF0123456789ABCDEF",
        "0123456789abcdef",
        "0123456789abcdef0123456789abcde/",
        "",
        "..",
    ]:
        assert store.append(bad, _event(1, "x")) is False
        assert store.exists(bad) is False
        with pytest.raises(ValueError):
            store.read_after(bad, 0, 10)
    # 目录里不能多出任何文件（写入在路径拼接前就被拒绝）。
    assert list(isolated_dir.glob("*")) == []


def test_append_requires_positive_seq_and_round_trips(isolated_dir):
    store = chat_events.ChatEventStore()
    assert store.append(RUN_ID, {"type": "content", "data": "no seq"}) is False
    assert store.append(RUN_ID, {"type": "content", "data": "zero", "seq": 0}) is False
    assert store.append(RUN_ID, {"type": "content", "data": "bool", "seq": True}) is False

    assert store.append(RUN_ID, _event(1, "磁盘")) is True
    assert store.append(RUN_ID, _event(2, "满了")) is True

    path = isolated_dir / f"{RUN_ID}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "每条事件必须单独一行，追加不重写整个文件"
    assert json.loads(lines[0])["seq"] == 1


def test_read_after_returns_only_later_events_in_order(isolated_dir):
    store = chat_events.ChatEventStore()
    for seq in (1, 2, 3, 4):
        store.append(RUN_ID, _event(seq, f"块{seq}"))

    events, truncated, next_after = store.read_after(RUN_ID, 0, 2)
    assert [item["seq"] for item in events] == [1, 2]
    assert truncated is True
    assert next_after == 2

    events, truncated, next_after = store.read_after(RUN_ID, next_after, 2)
    assert [item["seq"] for item in events] == [3, 4]
    assert truncated is False
    assert next_after is None

    # 游标已到末尾：没有新事件，也没有死页。
    events, truncated, _ = store.read_after(RUN_ID, 4, 10)
    assert events == []
    assert truncated is False


def test_duplicate_event_id_is_returned_once(isolated_dir):
    store = chat_events.ChatEventStore()
    store.append(RUN_ID, _event(1, "磁盘"))
    # 重连重放会写两次同一条事件（同 seq/event_id）：续读侧不能当两条内容。
    store.append(RUN_ID, _event(1, "磁盘"))
    store.append(RUN_ID, _event(2, "满了"))

    events, _, _ = store.read_after(RUN_ID, 0, 10)
    assert [item["data"] for item in events] == ["磁盘", "满了"]


def test_missing_run_raises_not_found(isolated_dir):
    store = chat_events.ChatEventStore()
    with pytest.raises(chat_events.ChatStreamNotFound):
        store.read_after(RUN_ID, 0, 10)
    assert store.exists(RUN_ID) is False


def test_byte_limit_stops_appending_instead_of_dropping_silently(isolated_dir):
    # 一条事件约 200 字节；上限 500 字节 => 前两条可写，第三条必须被拒绝。
    store = chat_events.ChatEventStore(max_bytes_limit=500)
    assert store.append(RUN_ID, _event(1, "x" * 50)) is True
    accepted = [store.append(RUN_ID, _event(seq, "y" * 50)) for seq in range(2, 20)]
    assert accepted[0] is True
    assert False in accepted, "达到字节上限后必须拒绝追加，而不是无限增长"
    path = isolated_dir / f"{RUN_ID}.jsonl"
    assert path.stat().st_size <= 500
    # 已写入的记录仍可读，且顺序完整（没有写坏的半行）。
    events, _, _ = store.read_after(RUN_ID, 0, 100)
    assert [item["seq"] for item in events] == list(range(1, len(events) + 1))


def test_purge_removes_only_expired_files(isolated_dir):
    store = chat_events.ChatEventStore()
    store.append(RUN_ID, _event(1, "旧"))
    old = isolated_dir / f"{RUN_ID}.jsonl"
    fresh_id = "fedcba9876543210fedcba9876543210"
    store.append(fresh_id, _event(1, "新"))
    fresh = isolated_dir / f"{fresh_id}.jsonl"

    # 只把旧文件的时间戳推回两天前；TTL 默认 24 小时。
    two_days_ago = fresh.stat().st_mtime - 2 * 24 * 3600
    os.utime(old, (two_days_ago, two_days_ago))

    removed = store.purge()
    assert removed == 1
    assert not old.exists()
    assert fresh.exists(), "未过期文件不能被清理"


def test_purge_disabled_when_ttl_zero(isolated_dir):
    store = chat_events.ChatEventStore(ttl=0)
    store.append(RUN_ID, _event(1, "x"))
    path = isolated_dir / f"{RUN_ID}.jsonl"
    os.utime(path, (0, 0))
    assert store.purge() == 0
    assert path.exists()


def test_record_helper_writes_through_singleton(isolated_dir):
    assert chat_events.record(RUN_ID, _event(1, "x")) is True
    events, _, _ = chat_events.chat_events.read_after(RUN_ID, 0, 10)
    assert len(events) == 1


# ── G6 P1：落地 session_id，续读按会话拒绝 ────────────────────────


def test_append_writes_explicit_session_id_overriding_event_field(isolated_dir):
    store = chat_events.ChatEventStore()
    # 事件字典里带别的会话值：服务端当前请求的显式参数必须覆盖它。
    event = {**_event(1, "x"), "session_id": "stale-session"}
    assert store.append(RUN_ID, event, session_id="session-current") is True

    events, _, _ = store.read_after(RUN_ID, 0, 10)
    assert events[0]["session_id"] == "session-current"

    # 不传显式参数时保留事件自带的值（兼容只写事件字典的调用方）。
    assert store.append(RUN_ID, {**_event(2, "y"), "session_id": "s2"}) is True
    events, _, _ = store.read_after(RUN_ID, 0, 10)
    assert events[1]["session_id"] == "s2"


def test_read_after_rejects_mismatched_session(isolated_dir):
    store = chat_events.ChatEventStore()
    store.append(RUN_ID, _event(1, "x"), session_id="session-1")
    store.append(RUN_ID, _event(2, "y"), session_id="session-1")

    events, _, _ = store.read_after(RUN_ID, 0, 10, session_id="session-1")
    assert [item["seq"] for item in events] == [1, 2]

    with pytest.raises(chat_events.ChatStreamNotFound):
        store.read_after(RUN_ID, 0, 10, session_id="session-2")
    # 不给 session_id 时保持原有行为（只按 seq 读）。
    events, _, _ = store.read_after(RUN_ID, 0, 10)
    assert len(events) == 2


def test_read_after_treats_missing_session_field_as_mismatch(isolated_dir):
    store = chat_events.ChatEventStore()
    store.append(RUN_ID, _event(1, "legacy"))  # 没有 session_id 字段
    with pytest.raises(chat_events.ChatStreamNotFound):
        store.read_after(RUN_ID, 0, 10, session_id="session-1")


def test_session_mismatch_is_rejected_regardless_of_cursor(isolated_dir):
    """会话不符不能因为 after_seq 越过别人的记录就变成“空页”。

    否则同一个已知 run 会用游标位置给出两种结论（空列表 vs 404），
    调用方据此就能判断“这条记录存在但对于我不可见”。
    """
    store = chat_events.ChatEventStore()
    store.append(RUN_ID, _event(1, "x"), session_id="session-1")
    store.append(RUN_ID, _event(2, "y"), session_id="session-1")

    with pytest.raises(chat_events.ChatStreamNotFound):
        store.read_after(RUN_ID, 2, 10, session_id="other-session")


def test_purge_expired_helper_removes_expired_files(isolated_dir):
    store = chat_events.ChatEventStore()
    store.append(RUN_ID, _event(1, "x"))
    path = isolated_dir / f"{RUN_ID}.jsonl"
    two_days_ago = path.stat().st_mtime - 2 * 24 * 3600
    os.utime(path, (two_days_ago, two_days_ago))

    assert chat_events.purge_expired() == 1
    assert not path.exists()
