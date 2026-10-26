"""trace 可靠性回归：失败可见、脱敏、限长、单 run 配额、并发与信封写入。

这些测试只依赖标准库 fixture（tmp_path/monkeypatch），不需要 pytest-mock。
conftest.py 的 autouse fixture 已把 ``observability.TRACE_DIR`` 指到临时目录。
"""

import json
import threading

from loguru import logger

from app import observability

_HEALTH_KEYS = {"writes", "failures", "last_error", "last_error_at", "dropped"}


def _path(trace_dir, run_id):
    return trace_dir / f"{run_id}.jsonl"


def _lines(trace_dir, run_id):
    path = _path(trace_dir, run_id)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _records(trace_dir, run_id):
    return [json.loads(line) for line in _lines(trace_dir, run_id)]


def _counters() -> dict:
    health = observability.get_trace_health()
    return {key: health[key] for key in ("writes", "failures", "dropped")}


def _delta(before: dict) -> dict:
    after = _counters()
    return {key: after[key] - before[key] for key in before}


def test_get_trace_health_reports_structured_counters():
    health = observability.get_trace_health()
    assert set(health) == _HEALTH_KEYS
    assert isinstance(health["writes"], int)
    assert isinstance(health["failures"], int)
    assert isinstance(health["dropped"], int)


def test_write_failure_is_counted_logged_without_payload_and_never_raises(
    monkeypatch, isolated_trace_dir
):
    """open 失败：计数 + 告警（只带异常类型），但绝不把异常抛给诊断主流程。"""
    before = _counters()
    logs: list[str] = []
    sink = logger.add(lambda message: logs.append(str(message)), level="WARNING")

    def broken_open(*args, **kwargs):
        raise PermissionError("access denied")

    try:
        monkeypatch.setattr("builtins.open", broken_open)
        observability.trace_event(
            "run-fail", "boom", password="hunter2", api_key="sk-live-should-not-leak"
        )
    finally:
        logger.remove(sink)

    health = observability.get_trace_health()
    delta = _delta(before)
    assert delta["failures"] == 1
    assert delta["writes"] == 0
    assert "PermissionError" in health["last_error"]
    assert health["last_error_at"]
    assert any("PermissionError" in message for message in logs)
    assert all("hunter2" not in message for message in logs)
    assert all("sk-live-should-not-leak" not in message for message in logs)


def test_empty_run_id_is_counted_as_dropped_without_creating_files(isolated_trace_dir):
    """没有 run_id 的事件无法归档，但丢弃必须可见（dropped 计数）。"""
    before = _counters()
    observability.trace_event("", "ignored", value=1)
    assert _delta(before) == {"writes": 0, "failures": 0, "dropped": 1}
    assert list(isolated_trace_dir.glob("*.jsonl")) == []


def test_redacts_secret_keys_and_values_in_nested_payload(isolated_trace_dir):
    observability.trace_event(
        "run-redact",
        "usage",
        api_key="abc123",
        password="p@ss",
        token="plain-token",
        nested={
            "Authorization": "Bearer whatever",
            "X-Api-Key": "zzz",
            "items": [{"secret": "s3cr3t"}, "sk-live-abcdef123456"],
        },
        input_tokens=12,
        output_tokens=34,
    )

    record = _records(isolated_trace_dir, "run-redact")[0]
    assert record["api_key"] == observability.REDACTED
    assert record["password"] == observability.REDACTED
    assert record["token"] == observability.REDACTED
    assert record["nested"]["Authorization"] == observability.REDACTED
    assert record["nested"]["X-Api-Key"] == observability.REDACTED
    assert record["nested"]["items"][0]["secret"] == observability.REDACTED
    assert record["nested"]["items"][1] == observability.REDACTED
    # token 用量是正常的观测指标，不能被“token”关键词误伤
    assert record["input_tokens"] == 12
    assert record["output_tokens"] == 34

    raw = _path(isolated_trace_dir, "run-redact").read_text(encoding="utf-8")
    for leaked in ("abc123", "p@ss", "plain-token", "Bearer whatever", "zzz", "s3cr3t"):
        assert leaked not in raw
    assert "sk-live-abcdef123456" not in raw


def test_oversized_record_is_truncated_with_marker(isolated_trace_dir):
    body = "A" * 200_000
    observability.trace_event("run-big", "tool_result", body=body, note="keep-me")

    path = _path(isolated_trace_dir, "run-big")
    raw = path.read_bytes()
    assert len(raw) <= observability.MAX_RECORD_BYTES + 1  # 允许行尾换行
    record = json.loads(raw.decode("utf-8"))
    assert record["truncated"] is True
    assert observability.TRUNCATION_MARKER in record["body"]
    assert record["note"] == "keep-me"
    assert record["event"] == "tool_result"


def test_per_run_size_cap_writes_final_marker_then_refuses(monkeypatch, isolated_trace_dir):
    monkeypatch.setattr(observability, "MAX_RUN_BYTES", 500)
    run_id = "run-cap"
    before = _counters()

    for _ in range(20):
        observability.trace_event(run_id, "step", pad="x" * 120)

    records = _records(isolated_trace_dir, run_id)
    assert records[-1]["event"] == "trace_truncated"
    assert records[-1]["truncated"] is True
    assert records[-1]["limit_bytes"] == 500
    written_lines = len(records)
    assert written_lines < 20

    # 触顶后继续写：文件不再增长，只累加 dropped，且不抛异常
    observability.trace_event(run_id, "step", pad="y" * 10)
    observability.trace_event(run_id, "step", pad="z" * 10)
    assert len(_lines(isolated_trace_dir, run_id)) == written_lines

    delta = _delta(before)
    assert delta["dropped"] >= 3
    assert delta["writes"] == written_lines
    record = json.loads(_lines(isolated_trace_dir, run_id)[-1])
    assert record["event"] == "trace_truncated"


def test_concurrent_appends_do_not_interleave_lines(isolated_trace_dir):
    run_id = "run-thread"
    blob = "x" * 3000
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for seq in range(50):
                observability.trace_event(run_id, "tick", worker=index, seq=seq, blob=blob)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常要带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    lines = _lines(isolated_trace_dir, run_id)
    assert len(lines) == 100
    parsed = [json.loads(line) for line in lines]
    assert len({(item["worker"], item["seq"]) for item in parsed}) == 100


def test_serialization_failure_is_counted_and_swallowed(isolated_trace_dir):
    class Exploding:
        def __str__(self) -> str:
            raise RuntimeError("cannot serialize")

    before = _counters()
    observability.trace_event("run-serialize", "bad", payload=Exploding())

    delta = _delta(before)
    assert delta["failures"] == 1
    assert delta["writes"] == 0
    assert not _path(isolated_trace_dir, "run-serialize").exists()


def test_caller_cannot_override_timestamp_field(isolated_trace_dir):
    """调用方误传 ts 时不能覆盖服务端时间戳；run_id/event 是位置参数，无法重复传入。"""
    observability.trace_event("run-fixed", "real_event", ts="1999-01-01T00:00:00")

    record = _records(isolated_trace_dir, "run-fixed")[0]
    assert record["run_id"] == "run-fixed"
    assert record["event"] == "real_event"
    assert record["ts"] != "1999-01-01T00:00:00"


def test_run_id_cannot_escape_trace_dir(isolated_trace_dir):
    """run_id 直接拼文件名，必须挡住 ../ 与路径分隔符。"""
    observability.trace_event("../evil/run", "escape", value=1)

    outside = isolated_trace_dir.parent / "evil"
    assert not outside.exists()
    written = list(isolated_trace_dir.glob("*.jsonl"))
    assert len(written) == 1
    assert written[0].parent == isolated_trace_dir
    assert "/" not in written[0].name and "\\" not in written[0].name


def test_trace_envelope_redacts_secret_payload(isolated_trace_dir):
    from app.core.events import normalize_event

    envelope = normalize_event(
        {"type": "agent_llm_error", "agent": "supervisor", "api_key": "sk-live-secret-123",
         "detail": {"authorization": "Bearer abc"}},
        run_id="run-env-secret",
        session_id="s",
        execution_mode="cloud_multi",
        seq=3,
    )

    observability.trace_envelope("run-env-secret", envelope)

    raw = _path(isolated_trace_dir, "run-env-secret").read_text(encoding="utf-8")
    assert "sk-live-secret-123" not in raw
    assert "Bearer abc" not in raw
    record = _records(isolated_trace_dir, "run-env-secret")[0]
    assert record["payload"]["api_key"] == observability.REDACTED
    assert record["payload"]["detail"]["authorization"] == observability.REDACTED


def test_trace_envelope_rejects_non_dict_without_raising(isolated_trace_dir):
    before = _counters()
    observability.trace_envelope("run-bad-env", ["not", "a", "dict"])  # type: ignore[arg-type]

    assert _delta(before)["failures"] == 1
    assert not _path(isolated_trace_dir, "run-bad-env").exists()


def test_trace_envelope_writes_normalized_event_with_seq(isolated_trace_dir):
    from app.core.events import normalize_event

    envelope = normalize_event(
        {"type": "tool_step", "tool": "query_metrics", "message": "ok"},
        run_id="run-env",
        session_id="session-1",
        execution_mode="cloud_multi",
        seq=7,
    )

    observability.trace_envelope("run-env", envelope)

    record = _records(isolated_trace_dir, "run-env")[0]
    assert record["event"] == "tool_step"
    assert record["seq"] == 7
    assert record["event_id"] == "run-env:7"
    assert record["run_id"] == "run-env"
    assert record["session_id"] == "session-1"
    assert record["execution_mode"] == "cloud_multi"
    assert record["payload"]["tool"] == "query_metrics"
