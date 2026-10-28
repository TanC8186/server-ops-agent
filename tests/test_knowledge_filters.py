"""G5：候选检索前的文档级过滤（ACL / 有效期 / OS / 产品 / 版本）。

真实缺陷（FINAL_ACCEPTANCE_REVIEW_2026-10-27「知识文档的 ACL、OS/产品/版本/
有效期候选过滤尚未形成完整策略」）：检索只按 active revision 过滤，任何已认证
调用方都能命中别人的私有文档；文档上的 os_family/product/product_version 字段
也从未参与候选。

本文件使用**真实**的 ``KnowledgeStore``（作业状态机、快照）、真实
``VectorStoreRetriever`` 与真实 chat 路由；只替换外部 I/O：

- 向量库：``RecordingVectors`` **不解析 expr**，返回全部行并记录收到的表达式。
  这是最坏情况（适配器忽略预过滤），用来证明后置核对本身足以挡住越权分片；
  同时断言表达式里确实写了允许/不允许的文档 ID（预过滤不是空话）。
- 切分：``OneChunkSplitter`` 一份文档一个分片，聚焦可见性而非切分细节。
- LLM：chat 用例注入 fake model，不连接真实模型。

如实标注：本文件**没有**真实 Milvus、真实 embedding 或真实 LLM；向量读写全部
来自上述替身，不能据此声称“真实检索链路已验证”。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.documents import Document

from app.api import chat as chat_api
from app.config import config
from app.core import principal as acl
from app.services import (
    knowledge_service as ksvc,
    knowledge_store as ks,
    vector_store_manager as vsm,
)
from app.services.knowledge_service import RetrievalFilters, VectorStoreRetriever

DEV = acl.DEV_PRINCIPAL_ID
ALICE = "principal:key-alice000000"


class RecordingVectors:
    """向量库替身：记录收到的 expr，但**不解析它**（返回全部行）。

    这样检索结果只能来自后置核对——如果后置核对失效，未授权分片会立刻出现在
    断言里，测试失败。``add_documents`` 的真实写入语义与 ``KnowledgeStore``
    的作业状态机配合（按预生成 chunk id 落行）。
    """

    score_kind = "similarity"
    metric_type = "COSINE"
    collection_name = "biz"

    def __init__(self) -> None:
        self.rows: dict[str, Document] = {}
        self.exprs: list[str | None] = []

    def _ensure_initialized(self) -> bool:
        return True

    def add_documents(self, documents, *, ids=None):
        row_ids = list(ids or [])
        for document, row_id in zip(documents, row_ids, strict=False):
            self.rows[row_id] = document
        return row_ids

    def delete_by_ids(self, ids) -> int:
        removed = 0
        for row_id in ids:
            if self.rows.pop(row_id, None) is not None:
                removed += 1
        return removed

    def delete_by_document(self, document_id: str) -> int:
        targets = [
            row_id for row_id, document in self.rows.items()
            if (document.metadata or {}).get("_document_id") == document_id
        ]
        for row_id in targets:
            self.rows.pop(row_id, None)
        return len(targets)

    def similarity_search_with_score(self, query, k=3, *, expr=None):
        self.exprs.append(expr)
        return [(document, 0.92) for document in list(self.rows.values())][:k]


class OneChunkSplitter:
    """一份内容一个分片；chunk id 含 revision，避免跨版本行互相覆盖。"""

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
def env(tmp_path, monkeypatch):
    """真实 store + 记录 expr 的向量替身；作业同步执行（保留状态机）。"""
    vectors = RecordingVectors()
    monkeypatch.setattr(ks, "vector_store_manager", vectors)
    monkeypatch.setattr(ks, "document_splitter_service", OneChunkSplitter())
    monkeypatch.setattr(config, "knowledge_tenant_mode", "single")
    store = ks.KnowledgeStore(tmp_path / "knowledge" / "store.json")
    store._submit = lambda _job_id: None

    def add(text: str, *, source: str, metadata: dict | None = None) -> dict:
        accepted = store.submit_document(
            source_uri=source,
            title=source.rsplit("/", 1)[-1],
            content=text.encode("utf-8"),
            source_kind="upload",
            metadata=metadata,
        )
        store._run_job(accepted["job_id"])
        assert store.get_job(accepted["job_id"])["status"] == "ready"
        return accepted

    yield {"store": store, "vectors": vectors, "add": add}
    store.shutdown()


def _texts(chunks) -> list[str]:
    return [chunk.text for chunk in chunks]


def _expr(vectors: RecordingVectors) -> str:
    assert vectors.exprs, "检索必须向向量库传 expr（候选前过滤）"
    return vectors.exprs[-1] or ""


# ── (a) 未授权文档不进入候选、不进入结果 ────────────────────


def test_unauthorized_document_is_excluded_before_and_after_candidates(env):
    env["add"]("ALLOWED runbook", source="upload://allowed.md")
    env["add"]("SECRET runbook", source="upload://secret.md")
    allowed_id = ks.document_id_for("upload://allowed.md")
    secret_id = ks.document_id_for("upload://secret.md")
    acl.claim_owner("knowledge_document", allowed_id, DEV)
    acl.claim_owner("knowledge_document", secret_id, ALICE)

    retriever = VectorStoreRetriever(env["vectors"], principal_id=DEV)
    chunks = retriever.search("runbook", 5)

    # 1) 候选前：表达式只允许可见文档，且不包含未授权文档 ID。
    expr = _expr(env["vectors"])
    assert allowed_id in expr
    assert secret_id not in expr, f"未授权文档不得出现在预过滤表达式: {expr}"
    # 2) 后置核对：即使适配器忽略 expr 返回全部行，未授权分片也不会进入结果。
    assert _texts(chunks) == ["ALLOWED runbook"]
    assert all(chunk.document_id != secret_id for chunk in chunks)
    assert "SECRET runbook" not in _texts(chunks)


def test_expr_combines_revision_and_document_allowlist(env):
    """单纯 revision 过滤不够：同一文档当前 active 但调用方无权读时也要排除。"""
    mine = env["add"]("mine", source="upload://mine.md")
    env["add"]("theirs", source="upload://theirs.md")
    acl.claim_owner("knowledge_document", ks.document_id_for("upload://mine.md"), DEV)
    acl.claim_owner(
        "knowledge_document", ks.document_id_for("upload://theirs.md"), ALICE
    )

    VectorStoreRetriever(env["vectors"], principal_id=DEV).search("q", 3)

    expr = _expr(env["vectors"])
    assert 'metadata["_revision"] in [' in expr
    assert 'metadata["_document_id"] in [' in expr
    assert mine["revision"] in expr


def test_empty_visibility_returns_no_candidates_without_querying_vectors(env):
    """没有任何可见文档时直接返回空，且不去向量库“试试看”。"""
    env["add"]("secret", source="upload://secret.md")
    acl.claim_owner(
        "knowledge_document", ks.document_id_for("upload://secret.md"), ALICE
    )

    chunks = VectorStoreRetriever(env["vectors"], principal_id=DEV).search("q", 3)

    assert chunks == []
    assert env["vectors"].exprs == [], "无可见文档时不应发起向量查询"


# ── (b) OS / 产品 / 版本 ────────────────────────────────────


def test_os_and_product_mismatch_is_excluded(env):
    rhel = env["add"](
        "RHEL 262 runbook", source="upload://rhel.md",
        metadata={"os_family": "RHEL", "product": "openEuler",
                  "product_version": "262"},
    )
    windows = env["add"](
        "Windows runbook", source="upload://win.md",
        metadata={"os_family": "Windows", "product": "Windows Server",
                  "product_version": "2022"},
    )

    chunks = VectorStoreRetriever(
        env["vectors"], principal_id=DEV,
        filters=RetrievalFilters(os_family="rhel", product="OPENEULER"),
    ).search("runbook", 5)

    assert _texts(chunks) == ["RHEL 262 runbook"]
    expr = _expr(env["vectors"])
    assert rhel["document_id"] in expr
    assert windows["document_id"] not in expr


def test_version_prefix_matches_numeric_only(env):
    exact = env["add"]("262", source="upload://v262.md",
                       metadata={"product_version": "262"})
    patch = env["add"]("262.1", source="upload://v2621.md",
                       metadata={"product_version": "262.1"})
    free_text = env["add"]("RHEL 9", source="upload://rhel9.md",
                           metadata={"product_version": "RHEL 9"})

    numeric = VectorStoreRetriever(
        env["vectors"], principal_id=DEV,
        filters=RetrievalFilters(product_version="262"),
    ).search("q", 10)
    assert sorted(_texts(numeric)) == ["262", "262.1"], "纯数字版本允许前缀匹配"

    text = VectorStoreRetriever(
        env["vectors"], principal_id=DEV,
        filters=RetrievalFilters(product_version="RHEL"),
    ).search("q", 10)
    assert _texts(text) == [], "自由文本只做相等匹配，不做语义推断"

    text_exact = VectorStoreRetriever(
        env["vectors"], principal_id=DEV,
        filters=RetrievalFilters(product_version="RHEL 9"),
    ).search("q", 10)
    assert _texts(text_exact) == ["RHEL 9"]
    assert {exact["document_id"], patch["document_id"], free_text["document_id"]}


def test_unannotated_document_does_not_match_a_filter(env):
    """文档未标注（空）不等于匹配：否则筛选形同虚设。"""
    env["add"]("annotated", source="upload://a.md", metadata={"os_family": "RHEL"})
    env["add"]("unannotated", source="upload://b.md")

    chunks = VectorStoreRetriever(
        env["vectors"], principal_id=DEV,
        filters=RetrievalFilters(os_family="RHEL"),
    ).search("q", 10)

    assert _texts(chunks) == ["annotated"]


def test_without_filters_all_authorized_documents_stay_visible(env):
    """请求不传筛选时行为不变：只按 ACL/active 过滤，不按 OS/产品/版本。"""
    env["add"]("rhel", source="upload://r.md", metadata={"os_family": "RHEL"})
    env["add"]("win", source="upload://w.md", metadata={"os_family": "Windows"})
    env["add"]("plain", source="upload://p.md")

    chunks = VectorStoreRetriever(env["vectors"], principal_id=DEV).search("q", 10)

    assert sorted(_texts(chunks)) == ["plain", "rhel", "win"]


# ── (c) 有效期 ──────────────────────────────────────────────


def test_expired_and_not_yet_valid_documents_are_excluded(env):
    env["add"]("expired", source="upload://old.md",
               metadata={"valid_until": "2026-01-01"})
    env["add"]("future", source="upload://future.md",
               metadata={"valid_from": "2027-01-01"})
    current = env["add"]("current", source="upload://now.md",
                         metadata={"valid_from": "2026-10-01",
                                   "valid_until": "2026-10-31"})

    chunks = VectorStoreRetriever(env["vectors"], principal_id=DEV,
                                  filters=RetrievalFilters(at_date="2026-10-27")
                                  ).search("q", 10)

    assert _texts(chunks) == ["current"]
    assert current["document_id"] in _expr(env["vectors"])


def test_validity_boundary_day_is_visible(env):
    """边界日当天必须可见：valid_from 与 valid_until 都是闭区间。"""
    env["add"]("boundary", source="upload://b.md",
               metadata={"valid_from": "2026-10-27", "valid_until": "2026-10-27"})

    for day in ("2026-10-27", "2026-10-26", "2026-10-28"):
        chunks = VectorStoreRetriever(
            env["vectors"], principal_id=DEV, filters=RetrievalFilters(at_date=day)
        ).search("q", 3)
        expected = ["boundary"] if day == "2026-10-27" else []
        assert _texts(chunks) == expected, day


def test_invalid_filter_date_is_rejected_not_ignored(env):
    env["add"]("doc", source="upload://d.md")

    with pytest.raises(ks.InvalidFilter):
        VectorStoreRetriever(
            env["vectors"], principal_id=DEV,
            filters=RetrievalFilters(at_date="2026-02-30"),
        ).search("q", 3)


def test_invalid_filter_maps_to_unavailable_retrieval_status(env):
    """过滤条件非法必须变成明确的检索状态，不能退化成“不过滤”。"""
    env["add"]("doc", source="upload://d.md")
    retriever = VectorStoreRetriever(
        env["vectors"], principal_id=DEV,
        filters=RetrievalFilters(at_date="27/10/2026"),
    )

    async def collect():
        return [event async for event in ksvc.answer_question(
            "问题", "session-1", retriever=retriever, model=None)]

    events = asyncio.run(collect())

    assert events[0]["status"] == "unavailable"
    assert events[-1]["outcome"] == "failed"
    assert env["vectors"].exprs == [], "条件非法时不得退化为不过滤的查询"


# ── (d) 单租户 demo vs 多租户 ───────────────────────────────


def test_ownerless_document_visibility_depends_on_tenant_mode(env, monkeypatch):
    env["add"]("legacy", source="upload://legacy.md")
    document_id = ks.document_id_for("upload://legacy.md")

    monkeypatch.setattr(config, "knowledge_tenant_mode", "single")
    single = env["store"].visible_documents(ALICE)
    assert list(single) == [document_id], "single 模式沿用旧可见性（无 owner 即可见）"

    monkeypatch.setattr(config, "knowledge_tenant_mode", "multi")
    assert env["store"].visible_documents(ALICE) == {}, "multi 模式对无 owner 文档 fail closed"
    # dev principal 仍可见：单机诊断/演示路径不能在 multi 下完全失明。
    assert document_id in env["store"].visible_documents(DEV)


def test_owned_document_is_only_visible_to_its_owner_in_both_modes(env):
    env["add"]("private", source="upload://private.md")
    document_id = ks.document_id_for("upload://private.md")
    acl.claim_owner("knowledge_document", document_id, ALICE)

    for mode in ("single", "multi"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(config, "knowledge_tenant_mode", mode)
            active = env["store"].active_revisions()[document_id]
            assert env["store"].visible_documents(DEV) == {}
            assert env["store"].visible_documents(ALICE) == {document_id: active}


# ── (e) allowlist 超上限必须明确失败 ────────────────────────


def test_oversized_allowlist_fails_instead_of_skipping_filter(env, monkeypatch):
    for index in range(3):
        env["add"](f"doc-{index}", source=f"upload://doc-{index}.md")
    monkeypatch.setattr(ks, "MAX_VISIBLE_DOCUMENTS", 2)

    with pytest.raises(ksvc.RetrievalUnavailable, match="超过过滤上限"):
        VectorStoreRetriever(env["vectors"], principal_id=DEV).search("q", 3)

    assert env["vectors"].exprs == [], "超上限时不得先查一次再说"


# ── (f) active revision 语义不被破坏 ────────────────────────


def test_superseded_revision_stays_invisible_after_document_filter(env):
    old = env["add"]("OLD instructions", source="upload://runbook.md")
    new = env["add"]("NEW instructions", source="upload://runbook.md")
    assert old["document_id"] == new["document_id"]

    chunks = VectorStoreRetriever(env["vectors"], principal_id=DEV).search("instructions", 10)

    assert _texts(chunks) == ["NEW instructions"]
    expr = _expr(env["vectors"])
    assert new["revision"] in expr
    assert old["revision"] not in expr, "superseded 版本不得进入预过滤允许列表"


# ── (g) /api/chat 把 filters 传到真实检索器 ─────────────────


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return SimpleNamespace(content="按手册处理 [K1]。")


@pytest.fixture()
def chat_client(tmp_path, monkeypatch, env):
    # chat 的默认检索器走 ``app.services.vector_store_manager.vector_store_manager``：
    # 只替换这一处外部 I/O，路由与 service 代码都是真实的。
    monkeypatch.setattr(vsm, "vector_store_manager", env["vectors"])
    monkeypatch.setenv("CHAT_HISTORY_DIR", str(tmp_path / "chat"))
    from app.services import chat_history

    monkeypatch.setattr(chat_history, "_history_dir", lambda: tmp_path / "chat")
    model = _FakeModel()
    monkeypatch.setattr(chat_api, "_chat_model", model)
    app = FastAPI()
    app.include_router(chat_api.router, prefix="/api")
    with TestClient(app) as client:
        yield client, model, env


def _events(response) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for line in response.text.splitlines() if line.startswith("data: ")]


def test_chat_filters_reach_retriever_and_narrow_candidates(chat_client):
    client, _model, env = chat_client
    rhel = env["add"]("RHEL runbook", source="upload://rhel.md",
                      metadata={"os_family": "RHEL"})
    win = env["add"]("Windows runbook", source="upload://win.md",
                     metadata={"os_family": "Windows"})

    response = client.post("/api/chat_stream", json={
        "Id": "session-f", "Question": "怎么排查",
        "filters": {"os_family": "RHEL"},
    })
    events = _events(response)

    assert events[0]["type"] == "retrieval"
    assert events[0]["status"] == "hit"
    done = events[-1]
    assert done["outcome"] == "answered"
    assert {chunk["document_id"] for chunk in events[0]["retrieval"]["chunks"]} == {
        rhel["document_id"]
    }
    expr = _expr(env["vectors"])
    assert rhel["document_id"] in expr
    assert win["document_id"] not in expr


def test_chat_without_filters_keeps_all_authorized_documents(chat_client):
    client, _model, env = chat_client
    env["add"]("RHEL runbook", source="upload://rhel.md",
               metadata={"os_family": "RHEL"})
    env["add"]("Windows runbook", source="upload://win.md",
               metadata={"os_family": "Windows"})

    events = _events(client.post("/api/chat_stream", json={
        "Id": "session-n", "Question": "怎么排查",
    }))

    documents = {chunk["document_id"]
                 for chunk in events[0]["retrieval"]["chunks"]}
    assert len(documents) == 2, "不传 filters 时不得按 OS/产品/版本过滤"


def test_chat_rejects_invalid_filter_date(chat_client):
    client, _model, _env = chat_client
    response = client.post("/api/chat_stream", json={
        "Id": "session-bad", "Question": "q",
        "filters": {"at_date": "2026-13-01"},
    })

    assert response.status_code == 422, response.text


# ── 配置开关来自 Settings，不来自请求 ───────────────────────


def test_tenant_mode_is_configured_and_validated(monkeypatch):
    """租户语义只来自配置：拼错的值必须显式失败，不能静默回落 single。"""
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.delenv("KNOWLEDGE_TENANT_MODE", raising=False)
    assert Settings(_env_file=None).knowledge_tenant_mode == "single"
    assert Settings(_env_file=None, knowledge_tenant_mode="MULTI").knowledge_tenant_mode == "multi"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, knowledge_tenant_mode="tenant")


def test_client_cannot_switch_tenant_mode_through_filters(chat_client):
    """客户端只能在 filters 里给事实（OS/产品/版本/日期），不能改租户语义。"""
    client, _model, env = chat_client
    env["add"]("legacy", source="upload://legacy.md")
    assert config.knowledge_tenant_mode == "single"

    events = _events(client.post("/api/chat_stream", json={
        "Id": "session-t", "Question": "q",
        "filters": {"tenant_mode": "multi", "principal_id": "principal:key-evil"},
    }))

    assert events[0]["type"] == "retrieval"
    # 未识别的 filter 字段被忽略，身份仍取自服务端：dev 模式下文档正常可见。
    assert config.knowledge_tenant_mode == "single"
    assert events[0]["status"] == "hit"


# ── ACL 列表：显式授权多个 principal ────────────────────────


def test_acl_principals_grant_extra_readers(env):
    """显式 ACL 可以授权给多人；不在列表里的 principal 仍然看不到。"""
    env["add"](
        "team runbook",
        source="upload://team.md",
        metadata={"acl_principals": [DEV, ALICE]},
    )
    document_id = ks.document_id_for("upload://team.md")
    stranger = "principal:key-carol0000000"

    assert list(env["store"].visible_documents(DEV)) == [document_id]
    assert list(env["store"].visible_documents(ALICE)) == [document_id]
    assert env["store"].visible_documents(stranger) == {}


def test_invalid_acl_principal_in_metadata_is_rejected(env):
    with pytest.raises(ks.InvalidFilter, match="principal"):
        env["add"](
            "bad acl",
            source="upload://bad-acl.md",
            metadata={"acl_principals": ["principal:ok", "everyone"]},
        )


def test_chat_non_streaming_also_applies_filters(chat_client):
    """非流式 /api/chat 与流式共用同一接线：filters 同样传到检索器。"""
    client, _model, env = chat_client
    rhel = env["add"]("RHEL runbook", source="upload://rhel-chat.md",
                      metadata={"os_family": "RHEL"})
    win = env["add"]("Windows runbook", source="upload://win-chat.md",
                     metadata={"os_family": "Windows"})

    response = client.post("/api/chat", json={
        "Id": "session-c", "Question": "怎么排查",
        "filters": {"os_family": "RHEL"},
    })
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["data"]["retrieval_status"] == "hit"
    assert body["data"]["citations"]
    assert {item["document_id"] for item in body["data"]["citations"]} == {
        rhel["document_id"]
    }
    expr = _expr(env["vectors"])
    assert rhel["document_id"] in expr
    assert win["document_id"] not in expr


def test_chat_passes_principal_so_private_documents_stay_out(chat_client):
    """chat 接线必须把服务端解析的 principal 传给检索器（不只是 filters）。"""
    client, _model, env = chat_client
    env["add"]("mine runbook", source="upload://mine-chat.md")
    env["add"]("alice runbook", source="upload://alice-chat.md")
    acl.claim_owner("knowledge_document", ks.document_id_for("upload://mine-chat.md"), DEV)
    acl.claim_owner(
        "knowledge_document", ks.document_id_for("upload://alice-chat.md"), ALICE
    )

    events = _events(client.post("/api/chat_stream", json={
        "Id": "session-p", "Question": "怎么排查",
    }))

    documents = {chunk["document_id"] for chunk in events[0]["retrieval"]["chunks"]}
    assert documents == {ks.document_id_for("upload://mine-chat.md")}
    assert ks.document_id_for("upload://alice-chat.md") not in _expr(env["vectors"])
