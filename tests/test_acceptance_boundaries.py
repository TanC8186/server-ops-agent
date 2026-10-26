"""2026-10-27 独立验收反例的期望行为回归（A01 / A02 / A03 / A08）。

这些测试对应 ``docs/claude-code-handoff/ACCEPTANCE_REVIEW_2026-10-27.md`` 的
A01–A03、A08 反例。只替换外部 I/O（Milvus / embedding SDK），**保留真实的**
``KnowledgeStore`` 作业状态机、真实 ``VectorStoreRetriever``、真实
``VectorStoreManager`` 初始化流程。

版本化测试必须使用“先写一行再抛错”的 fake。只在调用前直接抛错的 fake 无法覆盖
“批量写入已落一部分行、但没有成功返回 inserted 列表”的真实竞态——那正是 A02
漏回滚的根因。
"""

from __future__ import annotations

import threading

import pytest
from langchain_core.documents import Document

from app.config import config
from app.services import knowledge_store as ks
from app.services import vector_embedding_service as emb
from app.services import vector_store_manager as vsm
from app.services.knowledge_service import VectorStoreRetriever


class RowWiseFailingVectors:
    """真实写入语义的向量存储替身：可选“写一行后抛错”。

    ``fail_after_rows`` 为 None 时正常写入；为整数 N 时写入 N 行后抛错，模拟
    Milvus 批量插入中途失败（此时调用方拿不到 inserted 列表）。
    """

    score_kind = "similarity"

    def __init__(self) -> None:
        self.rows: dict[str, Document] = {}
        self.fail_after_rows: int | None = None
        self.add_calls = 0
        self.delete_calls = 0
        self.delete_should_fail = False
        self._guard = threading.Lock()

    def add_documents(self, documents, *, ids):
        self.add_calls += 1
        written = 0
        for document, row_id in zip(documents, ids):
            with self._guard:
                self.rows[row_id] = document
            written += 1
            if self.fail_after_rows is not None and written >= self.fail_after_rows:
                # 关键：先落一行再抛错。inserted 列表永远不会包含这一行。
                raise RuntimeError("milvus insert failed after partial write")
        return list(ids)

    def delete_by_ids(self, ids):
        self.delete_calls += 1
        if self.delete_should_fail:
            raise RuntimeError("milvus delete failed")
        removed = 0
        with self._guard:
            for row_id in ids:
                if row_id in self.rows:
                    self.rows.pop(row_id)
                    removed += 1
        return removed

    def delete_by_document(self, document_id):
        removed = 0
        with self._guard:
            for row_id, document in list(self.rows.items()):
                if (document.metadata or {}).get("_document_id") == document_id:
                    self.rows.pop(row_id)
                    removed += 1
        return removed

    def similarity_search_with_score(self, query, k=3, **kwargs):
        with self._guard:
            rows = list(self.rows.values())
        return [(document, 0.9) for document in rows][:k]


class OneChunkSplitter:
    """把整段内容当作一个分片；真实切分逻辑另由 chunking 测试覆盖。"""

    def split_document(self, content, source, *, document_id, revision):
        return [Document(page_content=content, metadata={
            "_source": source,
            "_document_id": document_id,
            "_revision": revision,
            "_chunk_id": f"{document_id}:{revision}:1",
            "_line_start": 1,
            "_line_end": 1,
        })]


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    vectors = RowWiseFailingVectors()
    monkeypatch.setattr(ks, "vector_store_manager", vectors)
    monkeypatch.setattr(ks, "document_splitter_service", OneChunkSplitter())
    store = ks.KnowledgeStore(tmp_path / "store.json")
    # 同步执行 worker：去掉后台线程的不确定性，保留真实作业状态机。
    store._submit = lambda _job_id: None

    def import_text(text: str):
        accepted = store.submit_document(
            source_uri="upload://acceptance-runbook.md",
            title="acceptance",
            content=text.encode("utf-8"),
        )
        store._run_job(accepted["job_id"])
        return accepted

    yield {"store": store, "vectors": vectors, "import": import_text}
    store.shutdown()


def _texts(vectors) -> list[str]:
    return [chunk.text for chunk in VectorStoreRetriever(vectors).search("instructions", 10)]


# ── A01：检索可见性绑定 active revision ─────────────────────────


def test_superseded_revision_is_not_retrievable(store_env):
    """旧版本向量保留可回滚，但新版本激活后旧版不得作为当前知识被检索。"""
    old = store_env["import"]("OLD instructions")
    new = store_env["import"]("NEW instructions")

    assert store_env["store"].get_job(old["job_id"])["status"] == "ready"
    assert store_env["store"].get_job(new["job_id"])["status"] == "ready"
    assert store_env["store"].list_revisions(new["document_id"])["active_revision"] == new["revision"]

    texts = _texts(store_env["vectors"])

    assert texts == ["NEW instructions"], f"只能检索到 active 版本，实际: {texts}"


def test_rollback_makes_old_revision_visible_again(store_env):
    """回滚后可见性跟着 active 指针走，而不是跟写入顺序走。"""
    old = store_env["import"]("OLD instructions")
    new = store_env["import"]("NEW instructions")
    assert _texts(store_env["vectors"]) == ["NEW instructions"]

    store_env["store"].activate_revision(new["document_id"], old["revision"])

    assert _texts(store_env["vectors"]) == ["OLD instructions"]


def test_failed_revision_is_not_retrievable(store_env):
    """失败批次即使残留了行，身份不是 active，也不得进入候选。"""
    store_env["import"]("OLD instructions")
    store_env["vectors"].fail_after_rows = 1
    failed = store_env["import"]("FAILED instructions")

    assert store_env["store"].get_job(failed["job_id"])["status"] == "failed"
    assert "FAILED instructions" not in _texts(store_env["vectors"])


def test_active_revisions_snapshot_is_consistent_under_concurrency(store_env):
    """并发读取只看到“切换前”或“切换后”的完整快照，不会读到半切换状态。

    注：``activate_revision`` 按设计会删除其它 revision 的向量（回滚语义），因此
    同一文档只能回滚一次；这里并发读取的是一次切换的边界。
    """
    old = store_env["import"]("OLD instructions")
    new = store_env["import"]("NEW instructions")
    document_id = new["document_id"]
    seen: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()
    start = threading.Barrier(2)
    stop = threading.Event()

    def reader() -> None:
        try:
            start.wait(timeout=5)
            while not stop.is_set():
                snapshot = store_env["store"].active_revisions()
                with guard:
                    seen.append(snapshot.get(document_id, ""))
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    start.wait(timeout=5)
    store_env["store"].activate_revision(document_id, old["revision"])
    stop.set()
    thread.join(timeout=20)

    assert errors == []
    # 每个快照值都必须是两个合法 revision 之一（没有空/半截状态）。
    assert set(seen) <= {old["revision"], new["revision"]}, f"出现非法快照: {set(seen)}"
    assert old["revision"] in seen, "切换后的快照必须反映新的 active revision"
    assert store_env["store"].active_revisions()[document_id] == old["revision"]


# ── A02：部分写入失败必须按全部预生成 ID 回滚 ──────────────────


def test_partial_batch_failure_rolls_back_every_candidate_id(store_env):
    """写一行后抛错：残片必须被回滚，而不是只清理“成功返回的 inserted”。"""
    store_env["vectors"].fail_after_rows = 1

    failed = store_env["import"]("FAILED PARTIAL instructions")

    assert store_env["store"].get_job(failed["job_id"])["status"] == "failed"
    assert store_env["vectors"].rows == {}, (
        f"失败批次不得留下任何分片，实际残留: {list(store_env['vectors'].rows)}"
    )
    assert _texts(store_env["vectors"]) == []


def test_failed_new_revision_keeps_old_active_intact(store_env):
    """新版本失败不能破坏旧 active：(a) 旧行还在 (b) 仍可检索。"""
    old = store_env["import"]("OLD instructions")
    store_env["vectors"].fail_after_rows = 1
    failed = store_env["import"]("FAILED instructions")

    assert store_env["store"].get_job(failed["job_id"])["status"] == "failed"
    active = store_env["store"].active_revisions()
    assert active.get(failed["document_id"]) == old["revision"]
    assert _texts(store_env["vectors"]) == ["OLD instructions"]


def test_cleanup_failure_is_recorded_and_retry_succeeds(store_env):
    """清理失败要如实记成可重试失败（cleanup_failed），retry 后恢复。"""
    store_env["vectors"].fail_after_rows = 1
    store_env["vectors"].delete_should_fail = True

    failed = store_env["import"]("FAILED instructions")

    job = store_env["store"].get_job(failed["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "cleanup_failed"
    assert job["error"]["retryable"] is True
    snapshot = store_env["store"].snapshot()
    assert snapshot["jobs"][failed["job_id"]]["pending_cleanup"], (
        "清理失败必须留下待重做的分片 ID"
    )

    # 依赖恢复后 retry：先按稳定 ID 清掉残片，再写入成功。
    store_env["vectors"].delete_should_fail = False
    store_env["vectors"].fail_after_rows = None
    retried = store_env["store"].retry_job(failed["job_id"])
    store_env["store"]._run_job(retried["job_id"])

    assert store_env["store"].get_job(retried["job_id"])["status"] == "ready"
    assert _texts(store_env["vectors"]) == ["FAILED instructions"]
    assert store_env["store"].get_job(failed["job_id"])["status"] == "failed"


# ── A03：strict_local 真正约束 provider ────────────────────────


def test_strict_local_never_selects_remote_factory(monkeypatch):
    """配置成远端 provider 时，strict_local 请求也不得构造远端客户端。"""
    calls: list[dict] = []

    def fake_remote_factory(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(config, "embedding_provider", "openai")
    monkeypatch.setattr(config, "embedding_model", "audit-fake-model")
    monkeypatch.setattr(config, "embedding_api_key", "audit-fake-key")
    monkeypatch.setattr(config, "embedding_base_url", "https://audit.invalid/v1")
    monkeypatch.setattr(emb, "_create_openai_embeddings", fake_remote_factory)

    service = emb.create_embedding_service(strict_local=True)

    assert calls == [], "strict_local 不得调用远端 embedding 工厂"
    assert isinstance(service, emb.LazyLocalEmbeddings)
    # 构造阶段不加载模型（无 I/O、无网络）。
    assert service._inner is None


def test_strict_local_returns_no_remote_http_and_refuses_when_model_absent(monkeypatch):
    """本地模型不可用时明确失败，而不是悄悄改用远端。"""
    remote_calls: list[dict] = []
    monkeypatch.setattr(config, "embedding_provider", "dashscope")
    monkeypatch.setattr(config, "embedding_model", "audit-fake-model")
    monkeypatch.setattr(emb, "_create_openai_embeddings",
                        lambda **kwargs: remote_calls.append(kwargs))

    def boom(*args, **kwargs):
        raise RuntimeError("模型未预置")

    monkeypatch.setattr(emb, "_load_local_embeddings", boom)

    service = emb.create_embedding_service(strict_local=True)
    with pytest.raises(RuntimeError, match="本地 embedding 模型不可用"):
        service.embed_query("hello")

    assert remote_calls == [], "本地失败不得回退远端 provider"


# ── A08：transient 初始化恢复 / 永久 invalid_index ─────────────


def _manager(monkeypatch) -> vsm.VectorStoreManager:
    manager = vsm.VectorStoreManager()
    # 退避只为避免忙等；测试里把等待压到很小，逻辑不变。
    monkeypatch.setattr(vsm, "INIT_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(vsm, "INIT_RETRY_MAX_SECONDS", 0.0)
    return manager


def test_transient_initialization_failure_recovers_without_reset(monkeypatch):
    """依赖先停后启：同进程下一次调用重新连接并恢复，不需要手工 reset。"""
    manager = _manager(monkeypatch)
    attempts = {"count": 0}

    def flaky_connect():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("milvus down")
        return object()

    monkeypatch.setattr(vsm.milvus_manager, "connect", flaky_connect)
    # 连接之后的建库/描述符部分属于外部 I/O：用替身替换成“成功”。
    monkeypatch.setattr(manager, "_verify_descriptor", lambda: None)

    class FakeVectorStore:
        pass

    monkeypatch.setattr(vsm, "Milvus", lambda **kwargs: FakeVectorStore())
    monkeypatch.setattr(vsm, "get_embedding_service_lazy", lambda: None)
    monkeypatch.setattr(vsm.milvus_manager, "get_collection", lambda: None)

    with pytest.raises(vsm.VectorStoreUnavailable):
        manager._ensure_initialized()
    assert manager.init_failure_kind == "transient"

    assert manager._ensure_initialized() is True
    assert attempts["count"] == 2, "transient 失败后必须重新尝试连接"
    assert manager.init_failure_kind is None


def test_permanent_invalid_index_is_not_retried(monkeypatch):
    """描述符/度量不一致是永久错误：继续重连只会拖慢请求，必须不重试。"""
    manager = _manager(monkeypatch)
    attempts = {"count": 0}

    def connect():
        attempts["count"] += 1
        return object()

    monkeypatch.setattr(vsm.milvus_manager, "connect", connect)

    def fail_verify():
        raise vsm.VectorStoreInvalidIndex("维度不匹配")

    monkeypatch.setattr(manager, "_verify_descriptor", fail_verify)
    monkeypatch.setattr(vsm, "Milvus", lambda **kwargs: object())
    monkeypatch.setattr(vsm, "get_embedding_service_lazy", lambda: None)
    monkeypatch.setattr(vsm.milvus_manager, "get_collection", lambda: None)

    with pytest.raises(vsm.VectorStoreInvalidIndex):
        manager._ensure_initialized()
    assert manager.init_failure_kind == "permanent"

    with pytest.raises(vsm.VectorStoreInvalidIndex):
        manager._ensure_initialized()
    assert attempts["count"] == 1, "永久错误不得反复重连"


def test_concurrent_recovery_initializes_only_once(monkeypatch):
    """并发恢复不得重复初始化（单次 connect + 描述符校验）。"""
    manager = _manager(monkeypatch)
    calls = {"connect": 0}

    def connect():
        calls["connect"] += 1
        return object()

    monkeypatch.setattr(vsm.milvus_manager, "connect", connect)
    monkeypatch.setattr(manager, "_verify_descriptor", lambda: None)
    monkeypatch.setattr(manager, "_metric_type", "", raising=False)
    monkeypatch.setattr(vsm, "Milvus", lambda **kwargs: object())
    monkeypatch.setattr(vsm, "get_embedding_service_lazy", lambda: None)
    monkeypatch.setattr(vsm.milvus_manager, "get_collection", lambda: None)

    results: list[bool] = []
    errors: list[BaseException] = []
    start = threading.Barrier(6)

    def worker() -> None:
        try:
            start.wait(timeout=5)
            results.append(manager._ensure_initialized())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    assert results == [True] * 6
    assert calls["connect"] == 1, f"并发恢复只能连接一次，实际 {calls['connect']} 次"
