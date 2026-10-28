"""trace 保留策略：后台调度、dry-run、并发协同删除、状态暴露（G3-1/2/5）。

全部使用 ``tmp_path`` 下的真实文件，并调用真实的 ``app.api.runs`` /
``app.observability`` 函数——只把 ``TRACE_DIR`` 指到临时目录。这样测的是真实的
加锁/删除/统计路径，而不是替身。

conftest.py 的 autouse fixture 已把 ``observability.TRACE_DIR`` 指到
``tmp_path/traces``；这里的 fixture 再把 ``runs.TRACE_DIR`` 指向**同一个**目录，
否则清理会去扫仓库里真实的 ``logs/traces``。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from app import observability
from app.api import runs

OLD_AGE_SECONDS = 10 * 86400


@pytest.fixture()
def trace_dir(tmp_path, monkeypatch):
    """让 runs 与 observability 指向同一个临时 trace 目录。"""
    directory = tmp_path / "traces"
    directory.mkdir(exist_ok=True)
    monkeypatch.setattr(observability, "TRACE_DIR", directory)
    monkeypatch.setattr(runs, "TRACE_DIR", directory)
    monkeypatch.setattr(runs, "_LAST_PURGE", None, raising=False)
    return directory


@pytest.fixture(autouse=True)
def reset_scheduler(monkeypatch):
    """每个用例前后清空调度器全局状态，避免用例之间互相影响。"""
    monkeypatch.setattr(runs, "_SCHEDULER_TASK", None, raising=False)
    yield
    monkeypatch.setattr(runs, "_SCHEDULER_TASK", None, raising=False)


def _run_id(index: int) -> str:
    return f"{index:032x}"


def _write_expired_trace(directory: Path, run_id: str, age: float = OLD_AGE_SECONDS,
                         events: list[dict] | None = None) -> Path:
    path = directory / f"{run_id}.jsonl"
    payload = events or [{"event": "run_start", "run_id": run_id}]
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in payload) + "\n",
        encoding="utf-8",
    )
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


# ---------------------------------------------------------------------------
# 1. 调度器：幂等启动/停止，保留关闭时不启动
# ---------------------------------------------------------------------------


async def test_scheduler_start_and_stop_are_idempotent(trace_dir, monkeypatch):
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "30")
    monkeypatch.setenv(runs.PURGE_INTERVAL_ENV, "3600")

    first = runs.start_trace_purge_scheduler()
    second = runs.start_trace_purge_scheduler()

    assert first is not None, "保留策略开启时必须真的启动任务"
    assert second is first, "重复启动不得叠加第二个循环"
    assert first.done() is False
    assert runs.purge_stats()["scheduler_running"] is True

    assert await runs.stop_trace_purge_scheduler() is True
    assert first.cancelled() or first.done()
    # 再停一次：没有存活任务，返回 False 且不抛异常
    assert await runs.stop_trace_purge_scheduler() is False
    assert runs.purge_stats()["scheduler_running"] is False


async def test_scheduler_is_disabled_when_retention_is_zero(trace_dir, monkeypatch):
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "0")

    assert runs.start_trace_purge_scheduler() is None
    assert runs.purge_stats()["scheduler_running"] is False
    # 关闭时不得写入"已运行"的假象
    stats = runs.purge_stats()
    assert stats["enabled"] is False
    assert stats["last_run_at"] is None


async def test_purge_loop_survives_a_raising_purge(trace_dir, monkeypatch):
    """单轮清理抛异常不得结束后台任务（启动后进程必须继续可用）。"""
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "30")
    monkeypatch.setenv(runs.PURGE_INTERVAL_ENV, "0.05")
    monkeypatch.setattr(runs, "PURGE_STARTUP_DELAY_SECONDS", 0.0)

    calls: list[int] = []

    def boom():
        calls.append(1)
        raise OSError("disk exploded")

    monkeypatch.setattr(runs, "purge_expired_traces", boom)

    task = runs.start_trace_purge_scheduler()
    assert task is not None
    await asyncio.sleep(0.3)

    assert task.done() is False, "清理失败后任务必须继续存活"
    assert len(calls) >= 2, "失败后应继续按间隔重试"
    await runs.stop_trace_purge_scheduler()


async def test_lifespan_starts_and_stops_scheduler_and_never_fails(trace_dir, monkeypatch):
    """main 的 lifespan：清理启动/停止失败都不能让应用起不来。"""
    from app import main

    monkeypatch.setattr(main, "startup_check", lambda: None)
    monkeypatch.setattr(main.milvus_manager, "connect", lambda: None)
    monkeypatch.setattr(main.milvus_manager, "close", lambda: None)
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "30")

    started = runs.start_trace_purge_scheduler
    async with main.lifespan(main.app):
        assert runs.purge_stats()["scheduler_running"] is True
    assert runs.purge_stats()["scheduler_running"] is False, "退出时必须停止后台任务"

    def broken_start():
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(runs, "start_trace_purge_scheduler", broken_start)
    # 清理起不来 → 应用仍必须能启动并正常退出
    async with main.lifespan(main.app):
        pass
    monkeypatch.setattr(runs, "start_trace_purge_scheduler", started)


# ---------------------------------------------------------------------------
# 2. dry-run：只统计，不删除
# ---------------------------------------------------------------------------


def test_dry_run_counts_but_does_not_delete(trace_dir, monkeypatch):
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.setenv(runs.DRY_RUN_ENV, "1")
    expired = _write_expired_trace(trace_dir, _run_id(1))
    fresh = _write_expired_trace(trace_dir, _run_id(2), age=0)

    stats = runs.purge_expired_traces()

    assert stats["dry_run"] is True
    assert stats["deleted"] == 0, "dry-run 绝不能删文件"
    assert stats["failed"] == 0
    assert stats["scanned"] == 2
    assert stats["would_delete"] == 1
    assert expired.exists() and fresh.exists()

    # 状态也要如实反映 dry-run 与其计数
    snapshot = runs.purge_stats()
    assert snapshot["dry_run"] is True
    assert snapshot["deleted"] == 0
    assert snapshot["scanned"] == 2
    assert snapshot["last_run_at"]


def test_dry_run_off_actually_deletes(trace_dir, monkeypatch):
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.delenv(runs.DRY_RUN_ENV, raising=False)
    expired = _write_expired_trace(trace_dir, _run_id(3))

    stats = runs.purge_expired_traces()

    assert stats["dry_run"] is False
    assert stats["deleted"] == 1
    assert not expired.exists()


# ---------------------------------------------------------------------------
# 3. 并发：正在追加的文件不被删除
# ---------------------------------------------------------------------------


def test_purge_skips_a_file_that_is_being_appended(trace_dir, monkeypatch):
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    path = _write_expired_trace(trace_dir, _run_id(4))

    lock = observability._path_lock(path)
    with lock:
        assert observability.is_file_locked(path) is True
        stats = runs.purge_expired_traces()

    assert stats["deleted"] == 0, "持锁期间不得删除"
    assert stats["failed"] == 0, "被锁跳过不是失败"
    assert stats["skipped"] == 1
    assert path.exists(), "正在追加的 trace 必须存活"

    # 释放后同一轮策略即可正常删除，证明跳过是暂时的而不是永久免疫
    after = runs.purge_expired_traces()
    assert after["deleted"] == 1
    assert not path.exists()


def test_remove_trace_file_respects_min_age_window(trace_dir, monkeypatch):
    """刚写过的文件即使已过保留期，也要受最小静默窗口保护。"""
    monkeypatch.setenv(observability.PURGE_MIN_AGE_ENV, "300")
    fresh = _write_expired_trace(trace_dir, _run_id(5), age=0)
    old = _write_expired_trace(trace_dir, _run_id(6))

    assert observability.remove_trace_file(fresh) is False
    assert fresh.exists(), "静默窗口内的文件不得删除"
    assert observability.remove_trace_file(old) is True
    assert not old.exists()
    # 不存在 / 已删除的文件：返回 False，不抛异常
    assert observability.remove_trace_file(old) is False


def test_remove_trace_file_returns_false_while_locked(trace_dir):
    path = _write_expired_trace(trace_dir, _run_id(7), age=0)
    with observability._path_lock(path):
        assert observability.remove_trace_file(path, min_age_seconds=0) is False
    assert path.exists()
    assert observability.remove_trace_file(path, min_age_seconds=0) is True


def test_dry_run_prediction_matches_real_run_under_locks(trace_dir, monkeypatch):
    """dry-run 的 would_delete 必须与真跑一致，不能把被锁文件算进预测。"""
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.setenv(runs.DRY_RUN_ENV, "1")
    monkeypatch.setenv(observability.PURGE_MIN_AGE_ENV, "0")
    free = _write_expired_trace(trace_dir, _run_id(20))
    busy = _write_expired_trace(trace_dir, _run_id(21))

    with observability._path_lock(busy):
        predicted = runs.purge_expired_traces()

    assert predicted["would_delete"] == 1, "被锁文件不得计入预测"
    assert predicted["skipped"] == 1
    assert free.exists() and busy.exists()

    monkeypatch.delenv(runs.DRY_RUN_ENV, raising=False)
    actual = runs.purge_expired_traces()
    assert actual["deleted"] == 2, "释放锁后两个过期文件都应被删除"


# ---------------------------------------------------------------------------
# 4. 状态暴露：purge_stats() 与 /health
# ---------------------------------------------------------------------------


def test_purge_stats_reports_last_run(trace_dir, monkeypatch):
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    assert runs.purge_stats()["last_run_at"] is None, "从未运行过时不得编造时间"

    _write_expired_trace(trace_dir, _run_id(8))
    runs.purge_expired_traces()

    snapshot = runs.purge_stats()
    assert set(snapshot) >= {
        "enabled", "dry_run", "interval_seconds", "last_run_at",
        "deleted", "failed", "scanned", "skipped", "scheduler_running",
    }
    assert snapshot["enabled"] is True
    assert snapshot["last_run_at"]
    assert snapshot["deleted"] == 1
    assert snapshot["scanned"] == 1


def test_health_exposes_trace_retention_block(trace_dir, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import health

    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)
    _write_expired_trace(trace_dir, _run_id(9))
    runs.purge_expired_traces()

    app = FastAPI()
    app.include_router(health.router)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()["data"]
    caps = data["capabilities"]
    # 既有能力项必须保留
    assert {"diagnosis_local", "diagnosis_cloud", "mcp", "rag", "tracing"} <= set(caps)

    retention = caps["trace_retention"]
    assert set(retention) == {
        "status", "reason", "last_run_at", "deleted", "failed",
        "scanned", "dry_run", "enabled",
    }
    assert retention["status"] in {"unconfigured", "ready", "degraded", "unavailable"}
    assert retention["status"] == "ready"
    assert retention["enabled"] is True
    assert retention["dry_run"] is False
    assert retention["deleted"] == 1
    assert retention["scanned"] == 1
    assert retention["last_run_at"]
    # 不得把 trace 内容/文件路径带进公开探针
    assert ".jsonl" not in json.dumps(data)


def test_health_retention_is_unconfigured_when_disabled(trace_dir, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import health

    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "0")
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)

    app = FastAPI()
    app.include_router(health.router)
    with TestClient(app) as client:
        retention = client.get("/health").json()["data"]["capabilities"]["trace_retention"]

    assert retention["status"] == "unconfigured"
    assert retention["enabled"] is False
    assert retention["reason"]


def test_health_retention_degrades_on_failed_deletes(trace_dir, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import health

    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)
    monkeypatch.setattr(
        runs, "_LAST_PURGE",
        {"last_run_at": "2026-10-27T00:00:00", "deleted": 1, "failed": 2,
         "scanned": 3, "skipped": 0, "dry_run": False, "enabled": True},
        raising=False,
    )

    app = FastAPI()
    app.include_router(health.router)
    with TestClient(app) as client:
        retention = client.get("/health").json()["data"]["capabilities"]["trace_retention"]

    assert retention["status"] == "degraded"
    assert retention["failed"] == 2
    assert retention["reason"]


def test_health_retention_degrades_when_scheduler_never_ran(trace_dir, monkeypatch):
    """开着保留策略却既没跑过、调度器也不在 → 必须可见（不能静默 ready）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import health

    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)

    app = FastAPI()
    app.include_router(health.router)
    with TestClient(app) as client:
        retention = client.get("/health").json()["data"]["capabilities"]["trace_retention"]

    assert retention["last_run_at"] is None
    assert retention["status"] == "degraded"
    assert "调度器" in retention["reason"]


async def test_health_retention_is_ready_while_scheduler_awaits_first_run(
    trace_dir, monkeypatch
):
    """调度器已起、首轮还没到：这是正常状态，不能报 degraded。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import health

    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "30")
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)
    task = runs.start_trace_purge_scheduler()
    assert task is not None
    try:
        app = FastAPI()
        app.include_router(health.router)
        with TestClient(app) as client:
            retention = client.get("/health").json()["data"]["capabilities"]["trace_retention"]

        assert retention["last_run_at"] is None
        assert retention["status"] == "ready"
        assert retention["reason"]
    finally:
        await runs.stop_trace_purge_scheduler()


# ---------------------------------------------------------------------------
# 5. 并发写入 + 清理：已写入的记录不丢失、文件不被腰斩
# ---------------------------------------------------------------------------


def test_concurrent_append_and_purge_never_corrupts_traces(trace_dir, monkeypatch):
    """50 个文件一边追加一边清理：幸存文件的每条记录都必须可解析。"""
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.setenv(observability.PURGE_MIN_AGE_ENV, "300")

    total_files = 50
    records_per_file = 20
    run_ids = [_run_id(100 + index) for index in range(total_files)]
    for run_id in run_ids:
        _write_expired_trace(trace_dir, run_id)

    stop = threading.Event()
    append_errors: list[BaseException] = []
    purged: list[dict] = []

    def writer(run_id: str) -> None:
        try:
            for index in range(records_per_file):
                observability.trace_event(run_id, "appended", index=index)
                time.sleep(0.002)
        except BaseException as exc:  # noqa: BLE001 - 记录后统一断言
            append_errors.append(exc)

    def purger() -> None:
        while not stop.is_set():
            try:
                purged.append(runs.purge_expired_traces())
            except BaseException as exc:  # noqa: BLE001
                append_errors.append(exc)
            time.sleep(0.005)

    purge_thread = threading.Thread(target=purger, name="purge")
    purge_thread.start()
    writers = [
        threading.Thread(target=writer, args=(run_id,), name=f"w-{run_id[:4]}")
        for run_id in run_ids
    ]
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join(timeout=60)
        assert not thread.is_alive(), "写入线程未在超时内结束"
    stop.set()
    purge_thread.join(timeout=60)
    assert not purge_thread.is_alive()

    assert append_errors == [], f"并发追加/清理抛异常: {append_errors}"
    assert purged, "清理循环至少应跑过一轮"

    # 所有文件都被写入过 → mtime 都是新的 → 最后一轮清理不得删除任何文件。
    final = runs.purge_expired_traces()
    assert final["deleted"] == 0, "刚被追加过的 trace 不得被清理"
    assert final["failed"] == 0

    for run_id in run_ids:
        path = trace_dir / f"{run_id}.jsonl"
        assert path.exists(), f"刚写入的 trace 被删除: {run_id}"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        parsed = []
        for line in lines:
            # 任何一行都必须是完整 JSON：半行（腰斩写入）会在这里失败。
            parsed.append(json.loads(line))
        appended = [record for record in parsed if record.get("event") == "appended"]
        assert len(appended) == records_per_file, (
            f"{run_id} 追加的记录丢失: {len(appended)}/{records_per_file}"
        )
        assert [record["index"] for record in appended] == list(range(records_per_file))


# ---------------------------------------------------------------------------
# 6. 跨进程 retention：别的进程正在写同一个 run 时不得删除
#
# 300 过期 + 10 未过期、单轮上限 200 的分轮删除用例放在
# ``tests/test_soak_concurrency.py``（那里同时覆盖 dry-run），这里只补本文件
# 缺少的**多进程**视角：进程内锁看不到别的 worker。
# ---------------------------------------------------------------------------


def _bulk_trace(directory: Path, run_id: str, age: float) -> Path:
    path = directory / f"{run_id}.jsonl"
    path.write_text(json.dumps({"run_id": run_id, "event": "run_start"}) + "\n",
                    encoding="utf-8")
    stamp = time.time() - age  # 构造的时间：不真的等待 10 天
    os.utime(path, (stamp, stamp))
    return path


def test_purge_skips_file_locked_by_another_process(trace_dir, monkeypatch):
    """另一个进程持有 ``<run>.jsonl.lock`` 时，本进程的清理必须跳过而不是删掉。

    进程内锁看不到别的进程；删除前必须再拿一次跨进程锁。这里用真实子进程持锁，
    验证 G5-E 要求的多进程 retention 安全（不是 mock）。
    """
    import subprocess
    import sys

    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.setenv(observability.PURGE_MIN_AGE_ENV, "0")
    run_id = _run_id(0xBEEF)
    path = _bulk_trace(trace_dir, run_id, OLD_AGE_SECONDS)

    repo_root = Path(observability.__file__).resolve().parents[1]
    child = """
import sys, time
from app import observability
lock = observability._cross_process_trace_lock(sys.argv[1], timeout=0)
lock.__enter__()
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child, str(path), "5"],
        cwd=str(repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        stats = runs.purge_expired_traces()
        assert stats["deleted"] == 0, "别的进程正在写，不得删除"
        assert stats["skipped"] >= 1
        assert path.exists()
    finally:
        process.kill()
        process.communicate(timeout=30)

    # 锁随进程退出释放，下一轮即可正常删除。
    after = runs.purge_expired_traces()
    assert after["deleted"] == 1
    assert not path.exists()


def test_expired_traces_beyond_the_scan_window_still_get_purged(trace_dir, monkeypatch):
    """目录里超过 200 个文件时，过期文件不能被"前 200 项"永久挤在外面。

    现状：``purge_expired_traces`` 先对 ``glob`` 结果切片 ``[:200]`` 再逐个判断
    是否过期，所以文件名/目录顺序排在第 200 项之后的过期 trace 每轮都扫不到，
    永远删不掉——``logs/traces`` 长期运行后必然进入这个状态。

    这里构造 250 个新鲜 + 50 个过期（过期文件名排在后面），连续跑多轮：修复后
    过期文件应当被删掉。本次不改 ``app/api/runs.py``（不在文件所有权内），用
    strict xfail 固定真实缺口；最小补丁见报告 cross_file_requests。
    """
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.setenv(observability.PURGE_MIN_AGE_ENV, "0")
    monkeypatch.delenv(runs.DRY_RUN_ENV, raising=False)
    fresh = [_bulk_trace(trace_dir, _run_id(index), 0) for index in range(250)]
    expired = [_bulk_trace(trace_dir, _run_id(0xF0000000 + index), OLD_AGE_SECONDS)
               for index in range(50)]

    for _ in range(3):
        stats = runs.purge_expired_traces()
        assert stats["failed"] == 0
        if not any(path.exists() for path in expired):
            break

    assert all(not path.exists() for path in expired), (
        "过期 trace 被扫描窗口挤在外面，永远轮不到删除"
    )
    assert all(path.exists() for path in fresh), "未过期 trace 必须保留"


