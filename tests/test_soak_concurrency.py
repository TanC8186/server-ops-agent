"""长时间/并发收尾（G5-E）：跨进程 trace 单写者、retention 上限、job/ACL/active revision。

每个用例都对应一条真实用户流程或数据一致性约束：

- **跨进程 trace 单写者**：多 worker 部署时同一个 run 的事件由多个进程追加。
  进程内 ``threading.Lock`` 管不到别的进程，Windows 上并发 ``open(..., "a")``
  会出现整行丢失/两行粘成半行。这里用**真实子进程**（不是 mock、不是线程）验证
  2000 条事件一条不少、每行都是合法 JSON。
- **retention 上限**：一次清理最多删 ``RETENTION_MAX_FILES_PER_RUN`` 个文件，
  必须能"分轮删完"而不是永远卡在原地；dry-run 只预测不删。
- **abandoned**：无终态且静默超过阈值的运行如实标 abandoned，且不会被 30 天
  保留策略提前清掉（否则用户回放历史时看不到"这次没跑完"）。
- **job / active revision 元数据**：并发提交同一文档的不同内容后，``active``
  指针与分片数必须一致；并发重试同一个 failed 作业不得产生重复作业；
  ``activate_revision`` 与 ``list_documents`` 并发时不得读到"active 指向不存在的
  revision"。
- **ACL 侧表**：并发 ``claim_owner(overwrite=False)`` 只允许一个成功、文件仍是
  合法 JSON 且只有一个 owner。

时间控制：过期条件一律用 ``os.utime`` 改文件 mtime 构造（24 小时/10 天前），
**不真的等待**；跨进程用例是真实进程，不是替身。Milvus 用 fake store 注入，
与 ``tests/test_knowledge_api.py`` 的既有约定一致（本文件不声称有真实 Milvus）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import observability
from app.api import runs
from app.core import principal as acl

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 单写者吞吐上限（秒）。只用来抓"每条事件都重新打开锁文件 + 死等"这类**数量级**
#: 退化，不是基准测试。本机实测 2000 条 3.5–4.0s，但并行跑其它用例时观测到
#: 6.3s、全量跑 7.1s（磁盘争用，不是实现退化），所以取一个不会被机器负载误伤的
#: 上界；真要退化（每条约 1ms 以上）仍然会被抓住。
SINGLE_WRITER_BUDGET_SECONDS = 30.0


def _python() -> str:
    """当前解释器路径：子进程必须用同一个 venv，避免 import 不到依赖。"""
    return sys.executable


# 子进程入口：把 TRACE_DIR 指到父进程的临时目录，然后追加 count 条事件。
_CHILD_TRACE_WRITER = """
import sys
from pathlib import Path
from app import observability

observability.TRACE_DIR = Path(sys.argv[1])
run_id = sys.argv[2]
worker = int(sys.argv[3])
base = int(sys.argv[4])
count = int(sys.argv[5])
for index in range(count):
    observability.trace_event(run_id, "soak", seq=base + index, worker=worker)
health = observability.get_trace_health()
print("health", health["writes"], health["failures"], health["dropped"])
"""


# ── 1. 跨进程 trace 单写者 ──────────────────────────────────


def test_four_processes_append_to_one_run_without_loss_or_interleaving(tmp_path):
    """4 个真实进程各追加 500 条：事件数正好 2000、每行合法、seq 不重复。"""
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(exist_ok=True)  # conftest 的 autouse fixture 已建好
    run_id = "0123456789abcdef0123456789abcdef"
    workers, per_worker = 4, 500

    processes = []
    for worker in range(workers):
        processes.append(subprocess.Popen(
            [_python(), "-c", _CHILD_TRACE_WRITER,
             str(trace_dir), run_id, str(worker), str(worker * per_worker),
             str(per_worker)],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ))
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, f"写入子进程失败: {stderr[-2000:]}"
        # 子进程自己报的事实：本进程写的条数 + 失败/丢弃数。锁等不到或写盘失败
        # 都会计入 failures，这里必须看到 0 0——不能"悄悄丢事件"。
        _, writes, failures, dropped = stdout.strip().splitlines()[-1].split()
        assert (writes, failures, dropped) == (str(per_worker), "0", "0"), stdout

    lines = [line for line in
             (trace_dir / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == workers * per_worker, f"事件丢失或粘行: {len(lines)}"

    records = [json.loads(line) for line in lines]  # 任何半行都会在这里抛错
    seqs = [record["seq"] for record in records]
    assert len(set(seqs)) == workers * per_worker, "seq 出现重复（写入串号）"
    assert set(seqs) == set(range(workers * per_worker))
    assert {record["worker"] for record in records} == set(range(workers))
    assert all(record["event"] == "soak" for record in records)


def test_lock_file_is_a_zero_byte_sibling_and_not_a_trace(tmp_path, monkeypatch):
    """锁文件必须与 trace 同目录、0 字节，且不会被 retention 的 ``*.jsonl`` 扫到。"""
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(exist_ok=True)  # conftest 的 autouse fixture 已建好
    monkeypatch.setattr(observability, "TRACE_DIR", trace_dir)

    observability.trace_event("run-lock", "hello")

    lock_path = trace_dir / "run-lock.jsonl.lock"
    assert lock_path.exists()
    assert lock_path.stat().st_size == 0, "锁文件不得写入 pid/内容（崩溃后靠 OS 释放）"
    assert list(trace_dir.glob("*.jsonl")) == [trace_dir / "run-lock.jsonl"]


def test_lock_timeout_is_bounded_and_visible(tmp_path, monkeypatch):
    """另一个进程持锁时：有界等待后抛明确错误、事件不写入、失败计数可见。"""
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(exist_ok=True)  # conftest 的 autouse fixture 已建好
    monkeypatch.setattr(observability, "TRACE_DIR", trace_dir)
    path = trace_dir / "run-busy.jsonl"
    monkeypatch.setenv(observability.TRACE_LOCK_TIMEOUT_ENV, "0.3")
    before = observability.get_trace_health()

    errors: list[BaseException] = []

    def waiter() -> None:
        try:
            observability.trace_event("run-busy", "blocked")
        except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程
            errors.append(exc)

    holder = observability._cross_process_trace_lock(path)
    holder.__enter__()
    try:
        thread = threading.Thread(target=waiter)
        started = time.monotonic()
        thread.start()
        thread.join(timeout=10)
        elapsed = time.monotonic() - started
        assert not thread.is_alive(), "等待必须有上限，不能死等"
    finally:
        holder.__exit__(None, None, None)

    # trace_event 契约是"绝不抛给调用方"，所以异常不会穿出线程；它会计入 failures。
    assert errors == []
    health = observability.get_trace_health()
    assert health["failures"] - before["failures"] == 1
    assert "TraceLockTimeout" in (health["last_error"] or "")
    assert elapsed < 5.0, f"锁超时应有界，实际 {elapsed:.1f}s"
    assert not path.exists() or path.read_text(encoding="utf-8") == "", "等锁期间不得写入"


def test_lock_is_not_reentrant_in_the_same_process(tmp_path, monkeypatch):
    """同一进程再次加同一把锁会等到超时（明确拒绝），而不是悄悄"重入成功"。

    进程内已有 ``_path_lock`` 串行化线程，正常路径不会嵌套；这里把"不可重入"
    这个取舍固定下来，避免有人以后把它当成可重入锁来用。
    """
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(exist_ok=True)  # conftest 的 autouse fixture 已建好
    path = trace_dir / "run-reentrant.jsonl"
    monkeypatch.setenv(observability.TRACE_LOCK_TIMEOUT_ENV, "0.2")

    with observability._cross_process_trace_lock(path):
        with pytest.raises(observability.TraceLockTimeout):
            with observability._cross_process_trace_lock(path):
                pytest.fail("同进程重入不应成功")

    # 释放后必须能立刻再拿到：不存在"锁一直被挂着"的残留。
    with observability._cross_process_trace_lock(path, timeout=0):
        pass


def test_run_file_cap_still_truncates_once_across_processes(tmp_path):
    """触顶行为保留：写 ``trace_truncated`` 后丢弃后续事件，且多个进程只写一个标记。"""
    source = """
import sys
from pathlib import Path
from app import observability

observability.TRACE_DIR = Path(sys.argv[1])
observability.MAX_RUN_BYTES = 600
for index in range(30):
    observability.trace_event("run-cap", "step", seq=index, pad="x" * 120)
health = observability.get_trace_health()
print(health["writes"], health["failures"], health["dropped"])
"""
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(exist_ok=True)  # conftest 的 autouse fixture 已建好
    processes = [
        subprocess.Popen(
            [_python(), "-c", source, str(trace_dir)],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    writes = 0
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr[-2000:]
        child_writes, child_failures, _ = stdout.strip().splitlines()[-1].split()
        assert child_failures == "0", stdout
        writes += int(child_writes)

    lines = (trace_dir / "run-cap.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    markers = [record for record in records if record["event"] == "trace_truncated"]
    assert len(markers) == 1, f"标记应只写一次，实际 {len(markers)}"
    assert records[-1]["event"] == "trace_truncated"
    assert records[-1]["limit_bytes"] == 600
    assert len(records) < 60, "触顶后不得继续追加"
    # 每个被计为写入的事件都必须在文件里：触顶后的事件只进 dropped，不能算写入。
    assert writes == len(records)


def test_lock_does_not_survive_a_crashed_process(tmp_path, monkeypatch):
    """进程拿到锁后 ``os._exit`` 崩溃：OS 释放锁，别的进程立刻能写入，不留死锁。"""
    child = """
import os, sys
from app import observability

lock = observability._cross_process_trace_lock(sys.argv[1], timeout=0)
lock.__enter__()
print("locked", flush=True)
os._exit(3)
"""
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(exist_ok=True)  # conftest 的 autouse fixture 已建好
    path = trace_dir / "run-crash.jsonl"

    result = subprocess.run(
        [_python(), "-c", child, str(path)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 3
    assert "locked" in result.stdout

    monkeypatch.setattr(observability, "TRACE_DIR", trace_dir)
    before = observability.get_trace_health()
    started = time.monotonic()
    observability.trace_event("run-crash", "after_crash")
    assert time.monotonic() - started < 2.0, "崩溃后不应再等满锁超时"
    # failures 是进程级累计值（前面用例可能已制造过超时），只看本次增量。
    assert observability.get_trace_health()["failures"] == before["failures"]
    assert json.loads((trace_dir / "run-crash.jsonl").read_text(encoding="utf-8"))["event"] \
        == "after_crash"


# ── 2. 单写者吞吐不明显退化 ─────────────────────────────────


def test_single_writer_throughput_stays_within_budget(tmp_path, monkeypatch):
    """2000 条事件在本机宽松上限内写完：跨进程锁不能把单写者拖成 O(秒/条)。

    只取**两批中较快的一批**：这是数量级守卫（例如"每条都 open/close 锁文件 +
    固定 sleep"），不是基准测试。同机并行跑其它测试时单批会被磁盘争用拖慢，
    取最小值可以避免把机器负载误判成实现退化；真退化是两批都慢。
    """
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(exist_ok=True)  # conftest 的 autouse fixture 已建好
    monkeypatch.setattr(observability, "TRACE_DIR", trace_dir)
    before = observability.get_trace_health()

    batches = 2000
    best = float("inf")
    for attempt in range(2):
        started = time.monotonic()
        for index in range(batches):
            observability.trace_event(
                f"run-bench-{attempt}", "step", seq=index, pad="x" * 20
            )
        best = min(best, time.monotonic() - started)

    after = observability.get_trace_health()
    assert after["writes"] - before["writes"] == batches * 2
    # failures/dropped 是进程级累计值（别的用例制造过超时），只看本次增量。
    assert after["failures"] == before["failures"]
    assert after["dropped"] == before["dropped"]
    assert best < SINGLE_WRITER_BUDGET_SECONDS, f"2000 条最快耗时 {best:.1f}s 超预算"


# ── 3. retention：300 过期 + 10 未过期，按上限分轮删完 ──────


@pytest.fixture()
def trace_dirs(tmp_path, monkeypatch):
    """让 runs 与 observability 指向同一个临时 trace 目录。"""
    directory = tmp_path / "traces"
    directory.mkdir(exist_ok=True)  # conftest 已建好
    monkeypatch.setattr(observability, "TRACE_DIR", directory)
    monkeypatch.setattr(runs, "TRACE_DIR", directory)
    monkeypatch.setattr(runs, "_LAST_PURGE", None, raising=False)
    monkeypatch.setenv(observability.PURGE_MIN_AGE_ENV, "0")
    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "1")
    monkeypatch.delenv(runs.DRY_RUN_ENV, raising=False)
    return directory


def _make_trace(directory: Path, run_id: str, age_seconds: float) -> Path:
    path = directory / f"{run_id}.jsonl"
    path.write_text(json.dumps({"run_id": run_id, "event": "run_start"}) + "\n",
                    encoding="utf-8")
    stamp = time.time() - age_seconds  # 构造的时间：不真的等待
    os.utime(path, (stamp, stamp))
    return path


def test_retention_caps_each_round_and_drains_only_expired(trace_dirs, monkeypatch):
    """dry-run 不删；真实清理每轮 ≤200，最终只删过期文件、未过期全部保留。"""
    expired = [_make_trace(trace_dirs, f"{index:032x}", 10 * 86400) for index in range(300)]
    fresh = [_make_trace(trace_dirs, f"{0xAAA00000 + index:032x}", 0) for index in range(10)]

    monkeypatch.setenv(runs.DRY_RUN_ENV, "1")
    dry = runs.purge_expired_traces()
    assert dry["dry_run"] is True
    assert dry["deleted"] == 0
    assert 0 < dry["would_delete"] <= runs.RETENTION_MAX_FILES_PER_RUN
    assert all(path.exists() for path in expired + fresh), "dry-run 绝不能删文件"

    monkeypatch.delenv(runs.DRY_RUN_ENV, raising=False)
    rounds: list[dict] = []
    for _ in range(6):
        stats = runs.purge_expired_traces()
        rounds.append(stats)
        assert stats["deleted"] <= runs.RETENTION_MAX_FILES_PER_RUN
        assert stats["failed"] == 0
        if stats["deleted"] == 0:
            break

    assert len(rounds) >= 2, "300 个过期文件不可能一轮删完（每轮上限 200）"
    assert sum(item["deleted"] for item in rounds) == 300
    assert all(not path.exists() for path in expired)
    assert all(path.exists() for path in fresh), "未过期 trace 必须保留"
    # 删完后再跑一轮：无事可做，不能报失败（否则 /health 会误报 degraded）。
    idle = runs.purge_expired_traces()
    assert idle["deleted"] == 0 and idle["failed"] == 0


def test_abandoned_run_survives_default_retention_window(trace_dirs, monkeypatch):
    """静默 24 小时的 abandoned run 仍在列表里；30 天保留策略不会提前清掉它。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv(observability.PURGE_MIN_AGE_ENV, "300")
    run_id = "deadbeefdeadbeefdeadbeefdeadbeef"
    path = trace_dirs / f"{run_id}.jsonl"
    path.write_text(
        json.dumps({"event": "run_start", "run_id": run_id, "ts": "t"}) + "\n"
        + json.dumps({"event": "tool_step", "step": "a"}) + "\n",
        encoding="utf-8",
    )
    stamp = time.time() - 24 * 3600  # 构造的时间：不是真的睡了 24 小时
    os.utime(path, (stamp, stamp))

    test_app = FastAPI()
    test_app.include_router(runs.router, prefix="/api")
    with TestClient(test_app) as client:
        run = client.get("/api/runs").json()["runs"][0]

    assert run["run_id"] == run_id
    assert run["abandoned"] is True
    assert run["status"] == "abandoned"

    monkeypatch.setenv(runs.RETENTION_DAYS_ENV, "30")
    monkeypatch.delenv(runs.DRY_RUN_ENV, raising=False)
    stats = runs.purge_expired_traces()
    assert stats["deleted"] == 0
    assert path.exists(), "24 小时前的 abandoned trace 不在 30 天保留期内"


# ── 4. knowledge_store：job / active revision 元数据 ────────


class FakeVectorStore:
    """最小向量库替身：只按稳定 id 记录行，用于校验"元数据与分片一致"。

    这是 fake（没有连接真实 Milvus），本文件不把它的行为当作真实向量库证据。
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.guard = threading.Lock()
        self.metric_type = "COSINE"
        self.score_kind = "similarity"
        self.collection_name = "biz"

    def _ensure_initialized(self) -> bool:
        return True

    def add_documents(self, documents, *, ids=None):
        docs = list(documents)
        doc_ids = list(ids) if ids is not None else [f"auto-{i}" for i in range(len(docs))]
        with self.guard:
            for doc, doc_id in zip(docs, doc_ids, strict=False):
                metadata = dict(doc.metadata or {})
                self.rows[doc_id] = {
                    "document_id": str(metadata.get("_document_id") or ""),
                    "revision": str(metadata.get("_revision") or ""),
                }
        return doc_ids

    def delete_by_ids(self, ids) -> int:
        removed = 0
        with self.guard:
            for row_id in [value for value in ids if value]:
                if self.rows.pop(row_id, None) is not None:
                    removed += 1
        return removed

    def delete_by_document(self, document_id: str) -> int:
        with self.guard:
            targets = [row_id for row_id, row in self.rows.items()
                       if row["document_id"] == document_id]
            for row_id in targets:
                self.rows.pop(row_id, None)
        return len(targets)

    def rows_of(self, document_id: str, revision: str) -> int:
        with self.guard:
            return sum(1 for row in self.rows.values()
                       if row["document_id"] == document_id and row["revision"] == revision)


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """真实的 KnowledgeStore + fake 向量库（文件落盘、线程池、作业状态机都是真的）。"""
    from app.services import knowledge_store as store_api

    store_file = tmp_path / "knowledge" / "store.json"
    monkeypatch.setenv(store_api.STORE_FILE_ENV, str(store_file))
    fake = FakeVectorStore()
    monkeypatch.setattr(store_api, "vector_store_manager", fake)
    store_api.reset_store()
    store = store_api.KnowledgeStore(store_file)
    try:
        yield {"store": store, "fake": fake, "store_api": store_api}
    finally:
        store.shutdown()
        store_api.reset_store()


def _content(tag: str, sections: int = 3) -> bytes:
    """可切分成多个分片的文档内容（不同 tag → 不同 revision）。"""
    body = "\n\n".join(
        f"## {tag}-{index}\n\n" + f"磁盘满时先检查 inode（{tag}-{index}）。" * 6
        for index in range(sections)
    )
    return f"# {tag}\n\n{body}\n".encode()


def _wait_job(store, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if job["status"] in ("ready", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"作业未在 {timeout}s 内到达终态: {job_id}")


def test_concurrent_submits_leave_one_consistent_active_revision(store_env):
    """8 线程并发提交同一文档的不同内容：只有一个 active，chunks 与分片数一致。"""
    from app.services import knowledge_store as store_api

    store, fake = store_env["store"], store_env["fake"]
    source_uri = "upload://soak/disk.md"
    document_id = store_api.document_id_for(source_uri)

    results: list[dict] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def submit(index: int) -> None:
        try:
            job = store.submit_document(
                source_uri=source_uri, title="disk", content=_content(f"v{index}")
            )
            final = _wait_job(store, job["job_id"])
            with guard:
                results.append(final)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads), "提交线程未在超时内结束"
    assert errors == [], f"并发提交抛异常: {errors}"
    assert len(results) == 8

    documents, _ = store.list_documents(limit=10)
    item = next(doc for doc in documents if doc["document_id"] == document_id)
    snapshot = store.snapshot()["documents"][document_id]
    active = item["active_revision"]

    assert active, "并发提交后文档必须有 active revision"
    active_states = [record for record in snapshot["revisions"].values()
                     if record.get("status") == "active"]
    assert len(active_states) == 1, "同一时刻只能有一个 active revision"
    assert active_states[0]["revision"] == active
    # 元数据与向量库事实一致：chunks 必须等于该 revision 在库里的行数。
    assert item["chunks"] == active_states[0]["chunks"] > 0
    assert item["chunks"] == fake.rows_of(document_id, active)
    assert store.active_revisions() == {document_id: active}


def test_concurrent_retry_of_one_failed_job_creates_one_new_job(store_env, monkeypatch):
    """8 线程并发 retry 同一个 failed 作业：只应产生一个新的重试作业。

    现状：``KnowledgeStore.retry_job`` 只对 ``ready`` 抛 Conflict，已存在的
    ``queued/running`` 重试作业既不返回也不拒绝，于是并发 retry 各建一个 job、
    对同一 revision 重复索引。最小补丁见报告的 cross_file_requests（本次不改
    ``knowledge_store.py``）。

    为了让失败**确定性**而不依赖 worker 快慢，这里把 ``_submit`` 换成不执行的
    桩：新作业停在 queued，8 次 retry 都会走到"新建 job"分支，可稳定重现。
    """
    store = store_env["store"]
    job = store.submit_document(
        source_uri="upload://soak/retry.md", title="retry", content=_content("retry")
    )
    final = _wait_job(store, job["job_id"])
    assert final["status"] == "ready"

    # 构造"失败"状态：模拟一次真实失败留下的 failed 作业（不改存储实现）。
    with store._lock:
        store._data["jobs"][job["job_id"]]["status"] = "failed"
        store._data["jobs"][job["job_id"]]["error"] = {
            "code": "index_failed", "message": "模拟失败", "retryable": True,
        }
        store._data["jobs"][job["job_id"]]["finished_at"] = "2026-10-27T00:00:00"
        store._save_locked()
    monkeypatch.setattr(store, "_submit", lambda job_id: None)

    outcomes: list[str] = []
    guard = threading.Lock()

    def retry() -> None:
        try:
            new_job = store.retry_job(job["job_id"])
            outcome = f"job:{new_job['job_id']}"
        except Exception as exc:  # noqa: BLE001 - Conflict 是允许的结果（幂等/409）
            outcome = f"error:{type(exc).__name__}"
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=retry) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    created = {outcome for outcome in outcomes if outcome.startswith("job:")}
    assert len(created) == 1, f"并发重试产生了 {len(created)} 个新作业: {created}"
    assert len(outcomes) == 8




def test_activate_and_list_documents_never_expose_dangling_active(store_env):
    """并发 activate + list_documents/active_revisions：active 永远指向存在的 revision。"""
    store = store_env["store"]
    from app.services import knowledge_store as store_api

    documents: list[tuple[str, str, str]] = []
    for tag in ("alpha", "beta"):
        source_uri = f"upload://soak/{tag}.md"
        first = store.submit_document(
            source_uri=source_uri, title=tag, content=_content(f"{tag}-1")
        )
        _wait_job(store, first["job_id"])
        second = store.submit_document(
            source_uri=source_uri, title=tag, content=_content(f"{tag}-2")
        )
        _wait_job(store, second["job_id"])
        # 第二个 revision 已成为 active；回滚到第一个（它仍保有分片）。
        documents.append((
            store_api.document_id_for(source_uri), first["revision"], second["revision"]
        ))

    violations: list[str] = []
    errors: list[BaseException] = []
    stop = threading.Event()

    def activate(document_id: str, revision: str) -> None:
        try:
            store.activate_revision(document_id, revision)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        """只断言"同一份快照内部"的不变量。

        跨两次调用的比较是伪命题：``list_documents`` 与随后的 ``snapshot`` 之间
        activate 可能已经发生（把旧 revision 的 chunks 归零），那不是中间态而是
        两个不同时刻的事实。真正要守的是：任意一份快照里，``active_revision``
        都必须指向一个存在且 ``status=active`` 的 revision 记录。
        """
        try:
            while not stop.is_set():
                snapshot = store.snapshot()["documents"]
                for document_id, doc in snapshot.items():
                    active = doc.get("active_revision")
                    if not active:
                        continue
                    record = (doc.get("revisions") or {}).get(active)
                    if record is None:
                        violations.append(f"{document_id}: active 指向不存在的 revision {active}")
                    elif record.get("status") != "active":
                        violations.append(
                            f"{document_id}: active {active} 的状态是 {record.get('status')}"
                        )
                    elif int(record.get("chunks") or 0) != int(doc.get("chunks") or 0):
                        violations.append(
                            f"{document_id}: 文档 chunks 与 active 记录不一致"
                        )
                # list_documents 的 active 指针必须能在快照里找到（只比较存在性）。
                for item in store.list_documents(limit=10)[0]:
                    active = item["active_revision"]
                    if active and active not in store.snapshot()["documents"][
                        item["document_id"]
                    ]["revisions"]:
                        violations.append(f"list_documents 返回了悬空 active: {active}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(6)]
    writers = [threading.Thread(target=activate, args=(document_id, revision))
               for document_id, revision, _ in documents]
    for thread in readers + writers:
        thread.start()
    for thread in writers:
        thread.join(timeout=60)
    stop.set()
    for thread in readers:
        thread.join(timeout=60)

    assert errors == [], f"并发 activate/list 抛异常: {errors}"
    assert violations == [], f"读到中间态: {violations[:5]}"
    for document_id, revision, _ in documents:
        assert store.active_revisions().get(document_id) == revision


# ── 5. ACL 侧表并发 claim_owner ─────────────────────────────


def test_concurrent_claim_owner_allows_exactly_one_winner():
    """8 线程同时 claim 同一资源（overwrite=False）：只有一个 True、JSON 仍合法。

    ``conftest.py`` 已把 ``PRINCIPAL_ACL_FILE`` 指到临时文件，这里操作的是真实
    的 JSON 原子写路径（临时文件 + ``os.replace``），不是内存替身。
    """
    kind, resource_id = "knowledge_document", "doc-soak-race"
    outcomes: list[bool] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def claim(index: int) -> None:
        try:
            result = acl.claim_owner(kind, resource_id, f"principal:key-w{index}")
            with guard:
                outcomes.append(result)
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], f"并发 claim 抛异常: {errors}"
    assert len(outcomes) == 8
    assert outcomes.count(True) == 1, f"只允许一个 owner，实际 {outcomes.count(True)}"
    assert outcomes.count(False) == 7

    path = acl._acl_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    owners = payload["owners"]
    key = f"{kind}:{resource_id}"
    assert payload["version"] == 1
    assert key in owners
    assert len([value for value in owners.values() if value]) == 1
    assert acl.resource_owner(kind, resource_id) == owners[key]
    # 落盘目录里不应残留临时文件（原子写要么替换成功、要么不留半份）。
    leftovers = [item.name for item in path.parent.iterdir() if item.name.endswith(".tmp")]
    assert leftovers == []
