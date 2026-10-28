"""G6 P1-2：诊断路径的知识检索必须带上调用方 principal。

这里要证明的不是“参数传下去了”，而是**两条路径看到同一份可见集合**：

- 诊断的内置工具 ``retrieve_knowledge``（``knowledge_wiring.knowledge_tools``）
  与 ``/api/chat`` 用的 ``VectorStoreRetriever`` 走同一个
  ``knowledge_store.visible_documents``，没有第二套过滤器；
- multi 租户下 A 的私有文档只有 A 能检索到，B 在两条路径上都看不到；
- single/dev 下行为完全不变（``retriever=None`` 的旧接线、本地模式的
  ``LocalOnlyEmbeddings`` 都保持原样）。

向量库替身刻意**不解析** ``expr``（把全部行都返回）：这样“B 检索不到 A 的
文档”只能由检索路径自己的可见性核对保证，而不是靠数据库碰巧过滤对了。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.documents import Document

from app.agent.aiops import knowledge_wiring, runtime_tools
from app.config import config
from app.core import principal as acl
from app.services import knowledge_service as ksvc
from app.services import knowledge_store as store_module

ALICE = "principal:key-alice0000"
BOB = "principal:key-bob00000"


class _AllRowsVectorStore:
    """返回全部行的向量库替身；``expr`` 只记录不执行。"""

    score_kind = "similarity"
    metric_type = "COSINE"
    collection_name = "biz"

    def __init__(self) -> None:
        self.rows: list[Document] = []
        self.exprs: list[str | None] = []

    def _ensure_initialized(self) -> bool:
        return True

    def add_documents(self, documents, *, ids=None):
        self.rows.extend(list(documents))
        return list(ids or [])

    def delete_by_ids(self, ids) -> int:
        return 0

    def delete_by_document(self, document_id: str) -> int:
        self.rows = [
            document for document in self.rows
            if (document.metadata or {}).get("_document_id") != document_id
        ]
        return 0

    def similarity_search_with_score(self, query, k=3, *, expr=None):
        self.exprs.append(expr)
        return [(document, 0.9) for document in self.rows][:k]


class _OneChunkSplitter:
    def split_document(self, content, source, *, document_id, revision):
        return [Document(page_content=content, metadata={
            "_source": source,
            "_document_id": document_id,
            "_revision": revision,
            "_chunk_id": f"{document_id}:{revision}:1",
        })]


@pytest.fixture()
def kb_env(tmp_path, monkeypatch):
    """真实 KnowledgeStore + 真实可见性规则；只替换向量 I/O 与切分。"""
    monkeypatch.setenv(store_module.STORE_FILE_ENV, str(tmp_path / "knowledge" / "store.json"))
    vectors = _AllRowsVectorStore()
    monkeypatch.setattr(store_module, "vector_store_manager", vectors)
    monkeypatch.setattr(store_module, "document_splitter_service", _OneChunkSplitter())
    # 诊断工具在 dev 单租户下不构造显式检索器（retriever=None），由工具调用时解析
    # “默认检索器”。这里把默认检索器的向量 I/O 指到同一份替身，两条路径才可比。
    monkeypatch.setattr(ksvc, "_default_store", lambda: vectors)
    store_module.reset_store()
    store = store_module.get_store()

    def upload(text: str, *, source: str, owner: str) -> str:
        accepted = store.submit_document(
            source_uri=source, title=source, content=text.encode("utf-8"),
        )
        store_module.wait_for_job(accepted["job_id"], timeout=10.0)
        if owner:
            acl.claim_owner("knowledge_document", accepted["document_id"], owner)
        return accepted["document_id"]

    yield {"store": store, "vectors": vectors, "upload": upload}
    store.shutdown()
    store_module.reset_store()


def _texts(chunks) -> list[str]:
    return [chunk.text for chunk in chunks]


def _tool_retriever(principal_id: str, *, execution_mode: str = "cloud_multi"):
    """诊断路径真正会用到的那个检索器（不是自己 new 一个）。

    dev 单租户下 ``knowledge_tools`` 刻意保持 ``retriever=None``；此时工具在调用
    时解析默认检索器，这里照同一条规则把它取出来，保证测的是生产接线。
    """
    tools = knowledge_wiring.knowledge_tools(
        execution_mode=execution_mode, principal_id=principal_id,
    )
    assert tools, "知识检索工具应当可用"
    retriever = tools[0]._retriever
    return retriever if retriever is not None else ksvc.VectorStoreRetriever()


# ── (a) 接线：owner 从 create_run_tools 一路传到检索器 ────────────────


def test_need_principal_scope_follows_identity_not_tenant_mode():
    """判据是"有没有身份"，不是租户模式：dev 也必须走同一套可见性规则。

    早先为"保持 dev 行为"在 single+dev 下关掉了 principal，结果同一个人在
    ``/api/chat``（一直传 principal）与诊断里看到不同集合——问答里看不见的
    文档能在诊断里当证据。这里锁定修正后的语义：
    """
    assert knowledge_wiring.need_principal_scope("") is False
    assert knowledge_wiring.need_principal_scope("   ") is False
    assert knowledge_wiring.need_principal_scope(acl.DEV_PRINCIPAL_ID) is True
    assert knowledge_wiring.need_principal_scope(ALICE) is True


def test_create_run_tools_passes_owner_into_knowledge_tools(monkeypatch):
    """``create_run_tools(owner=...)`` 必须把它交给 knowledge_tools。"""
    captured: dict = {}

    def fake_knowledge_tools(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(knowledge_wiring, "knowledge_tools", fake_knowledge_tools)

    async def fake_mcp():
        return []

    monkeypatch.setattr(runtime_tools, "get_mcp_tools_cached", fake_mcp)
    monkeypatch.setattr(config, "knowledge_tenant_mode", "multi")

    asyncio.run(runtime_tools.create_run_tools(
        "run-1", "cloud_multi", lambda event: None, owner=ALICE,
    ))
    assert captured.get("principal_id") == ALICE
    assert captured.get("execution_mode") == "cloud_multi"


def test_create_tools_compat_tolerates_legacy_signature_without_owner():
    """旧替身只接受 3 个位置参数时不能因为 owner 变成 TypeError。"""

    async def legacy(run_id, execution_mode, emit):
        return {"ok": True, "owner": None, "legacy": True}

    result = asyncio.run(runtime_tools.create_tools_compat(
        legacy, "run-1", "local", lambda event: None, owner=ALICE,
    ))
    assert result["legacy"] is True
    assert result["owner"] is None


def test_run_local_forwards_owner(monkeypatch):
    """run_local 必须把 owner 交给 create_run_tools（而不是只写进 trace）。"""
    import importlib

    local = importlib.import_module("app.agent.aiops.local_runtime")
    seen: dict = {}

    async def fake_create(run_id, execution_mode, emit, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("停止后续步骤：本用例只验证 owner 转发")

    monkeypatch.setattr(local, "create_run_tools", fake_create)
    monkeypatch.setattr(config, "local_llm_base_url", "http://127.0.0.1:9999/v1")

    async def collect():
        return [event async for event in local.run_local(
            "CPU 高", "session-1", "run-1", owner=ALICE,
        )]

    events = asyncio.run(collect())
    assert seen.get("owner") == ALICE
    assert events[-1]["type"] == "error"


# ── (b) multi 租户：A 可见、B 不可见，且两条路径结论一致 ──────────────


def test_multi_tenant_diagnosis_and_chat_share_visibility(kb_env, monkeypatch):
    monkeypatch.setattr(config, "knowledge_tenant_mode", "multi")
    alice_doc = kb_env["upload"]("ALICE 私有手册：磁盘满先清归档", source="upload://a.md",
                                 owner=ALICE)
    bob_doc = kb_env["upload"]("BOB 私有手册：重启服务前先摘流量", source="upload://b.md",
                               owner=BOB)

    # 诊断路径（工具用的检索器）
    alice_diag = _texts(_tool_retriever(ALICE).search("手册", 3))
    bob_diag = _texts(_tool_retriever(BOB).search("手册", 3))
    # 问答路径（/api/chat 的构造方式）
    alice_chat = _texts(ksvc.VectorStoreRetriever(principal_id=ALICE).search("手册", 3))
    bob_chat = _texts(ksvc.VectorStoreRetriever(principal_id=BOB).search("手册", 3))

    assert any("ALICE 私有手册" in text for text in alice_diag)
    assert not any("BOB 私有手册" in text for text in alice_diag)
    assert any("BOB 私有手册" in text for text in bob_diag)
    assert not any("ALICE 私有手册" in text for text in bob_diag)

    # 同一条可见性规则：两条路径的可见集合逐条相同（不是“都过滤了”就算）
    assert alice_diag == alice_chat
    assert bob_diag == bob_chat
    assert alice_doc != bob_doc

    # 未授权分片没有进入候选：expr 里出现的是本人文档的 revision。
    assert all("BOB" not in (expr or "") for expr in kb_env["vectors"].exprs)


def test_single_tenant_ownerless_document_stays_visible(kb_env, monkeypatch):
    """single/dev 行为不变：无 owner 文档在诊断与问答两条路径都仍可见。"""
    monkeypatch.setattr(config, "knowledge_tenant_mode", "single")
    kb_env["upload"]("历史遗留手册：无归属", source="upload://legacy.md", owner="")

    # 无归属的记录 claim 会失败，这里显式确认前提成立。
    assert acl.resource_owner(
        "knowledge_document", store_module.document_id_for("upload://legacy.md")
    ) in (None, "")

    diag = _texts(_tool_retriever(acl.DEV_PRINCIPAL_ID).search("手册", 3))
    chat = _texts(ksvc.VectorStoreRetriever(principal_id=acl.DEV_PRINCIPAL_ID).search("手册", 3))
    assert any("历史遗留手册" in text for text in diag)
    assert diag == chat


# ── (c) 没有身份时的旧行为，以及本地模式的 embedding 约束 ────────────


def test_cloud_multi_without_principal_keeps_default_retriever(monkeypatch):
    """没有身份（空 principal）时保持旧接线：由工具在调用时解析默认检索器。

    这是脚本/旧调用方的兼容路径；一旦拿到 principal（包括 dev）就必须构造
    显式检索器，否则诊断会绕过文档 ACL。
    """
    monkeypatch.setattr(config, "knowledge_tenant_mode", "single")
    tools = knowledge_wiring.knowledge_tools(execution_mode="cloud_multi", principal_id="")
    assert tools[0]._retriever is None

    with_dev = knowledge_wiring.knowledge_tools(
        execution_mode="cloud_multi", principal_id=acl.DEV_PRINCIPAL_ID,
    )
    retriever = with_dev[0]._retriever
    assert retriever is not None
    assert retriever._principal_id == acl.DEV_PRINCIPAL_ID


def test_local_mode_keeps_local_only_embeddings_with_principal(monkeypatch):
    """本地模式即使带上 principal，也必须仍是只允许本地的 embedding。"""
    from app.services import vector_store_manager as vsm

    monkeypatch.setattr(config, "knowledge_tenant_mode", "multi")
    tools = knowledge_wiring.knowledge_tools(execution_mode="local", principal_id=ALICE)
    retriever = tools[0]._retriever
    assert retriever._store is not vsm.vector_store_manager
    assert isinstance(getattr(retriever._store, "_embedding_function", None),
                      vsm.LocalOnlyEmbeddings)
    assert retriever._principal_id == ALICE
