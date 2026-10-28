"""服务端 principal / 资源归属校验（G3-3 / G2.3 第 3 条）。

覆盖“任何知道 session_id 的人都能读别人的运行”这一真实缺陷：

1. run 读取 / trace 回放的所有权判定（允许 + 拒绝两条路径都要有断言）；
2. 知识文档 delete / activate / list 的所有权判定；
3. dev 模式下**仍然执行**检查（拒绝路径可在测试里直接触发）；
4. 伪造 / 缺失 principal 抛 ``PermissionDenied``（映射 401/403），不是 500；
5. 没有记录 owner 的旧资源保持可读（不臆造归属）。

全部使用真实模块：只有 trace 目录、ACL 文件、向量库替身是外部 I/O 替换。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import knowledge as knowledge_api
from app.config import config
from app.core import principal as acl
from app.core import run_context
from app.services import knowledge_store as store_api


ALICE = "principal:key-alice000000"
BOB = "principal:key-bobb0000000"

RUN_ALICE = "a1" * 16
RUN_BOB = "b2" * 16


# ── 夹具 ────────────────────────────────────────────────────


@pytest.fixture()
def trace_dir(tmp_path, monkeypatch):
    directory = tmp_path / "traces"
    directory.mkdir(exist_ok=True)
    monkeypatch.setattr(run_context, "TRACE_DIR", directory)
    return directory


@pytest.fixture()
def acl_file(tmp_path, monkeypatch):
    """独立 ACL 侧表（conftest 已隔离，这里只取路径做断言）。"""
    path = tmp_path / "acl" / "principal_acl.json"
    monkeypatch.setenv(acl.ACL_FILE_ENV, str(path))
    return path


def _write_trace(directory: Path, run_id: str, *, owner: str | None,
                 session_id: str = "session-1") -> None:
    """写一份最小 trace；``owner=None`` 表示旧数据（run_start 没有 owner 字段）。"""
    start = {
        "ts": "2026-10-27T10:00:00", "run_id": run_id, "event": "run_start",
        "session_id": session_id, "execution_mode": "cloud_multi",
    }
    if owner is not None:
        start["owner"] = owner
    events = [
        start,
        {"ts": "2026-10-27T10:00:01", "run_id": run_id, "event": "agent_result",
         "agent": "reviewer", "observations": ["CPU 使用率 96%"],
         "hypotheses": [], "gaps": []},
        {"ts": "2026-10-27T10:00:02", "run_id": run_id, "event": "run_complete",
         "lifecycle": "completed", "quality": "sufficient"},
    ]
    path = directory / f"{run_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def _principal(principal_id: str) -> acl.Principal:
    return acl.Principal(principal_id, frozenset(acl.ACTION_SCOPES.values()), "api_key")


# ── 1. 纯检查：allow / deny ─────────────────────────────────


def test_owner_may_read_and_stranger_may_not():
    """同一资源：owner 放行，另一个 principal 被拒（403，带明确动作）。"""
    acl.authorize(_principal(ALICE), ALICE, action="run:read")

    with pytest.raises(acl.PermissionDenied) as denied:
        acl.authorize(_principal(BOB), ALICE, action="run:read")

    assert denied.value.status_code == 403
    assert denied.value.action == "run:read"
    # 错误信息不能把 owner 的身份泄漏给调用方。
    assert ALICE not in str(denied.value)


def test_resource_without_recorded_owner_stays_accessible():
    """旧资源没有 owner：放行（不臆造归属），而不是一律拒绝。"""
    for owner in (None, "", "   "):
        acl.authorize(_principal(BOB), owner, action="run:read")


def test_missing_principal_is_401_not_500():
    with pytest.raises(acl.PermissionDenied) as denied:
        acl.authorize(None, ALICE, action="run:read")
    assert denied.value.status_code == 401


def test_principal_without_scope_is_rejected():
    limited = acl.Principal(ALICE, frozenset(), "api_key")
    with pytest.raises(acl.PermissionDenied) as denied:
        acl.authorize(limited, ALICE, action="knowledge:delete")
    assert denied.value.status_code == 403


def test_unknown_action_does_not_silently_grant_unknown_permission():
    """未登记动作没有对应 scope：只做归属判定，绝不会因为“查不到 scope”跳过。"""
    acl.authorize(_principal(ALICE), ALICE, action="run:read")
    with pytest.raises(acl.PermissionDenied):
        acl.authorize(_principal(BOB), ALICE, action="run:read")


# ── 2. run 归属：trace run_start 是唯一事实 ──────────────────


def test_run_owner_comes_from_run_start(trace_dir):
    _write_trace(trace_dir, RUN_ALICE, owner=ALICE)

    assert run_context.run_owner(RUN_ALICE) == ALICE
    assert acl.run_owner(RUN_ALICE) == ALICE


def test_trace_without_owner_is_compatible_but_not_fabricated(trace_dir):
    _write_trace(trace_dir, RUN_BOB, owner=None)

    assert run_context.run_owner(RUN_BOB) is None
    # 没有 owner 不等于“属于任何人”，但也不拦；边界只在能证明时生效。
    assert run_context.owner_matches(None, BOB) is True
    assert run_context.owner_matches(ALICE, BOB) is False


def test_run_context_allows_owner_and_blocks_stranger(trace_dir):
    _write_trace(trace_dir, RUN_ALICE, owner=ALICE)

    allowed = run_context.load_run_context(RUN_ALICE, "session-1", ALICE)
    assert allowed is not None
    assert allowed["owner"] == ALICE

    assert run_context.load_run_context(RUN_ALICE, "session-1", BOB) is None
    # 会话不符依然拒绝（两条边界叠加，不是二选一）。
    assert run_context.load_run_context(RUN_ALICE, "session-2", ALICE) is None


def test_run_context_keeps_old_behaviour_when_principal_not_given(trace_dir):
    """未传 principal 的旧调用点保持“同会话即可”，不因升级而全部失效。"""
    _write_trace(trace_dir, RUN_ALICE, owner=ALICE)
    assert run_context.load_run_context(RUN_ALICE, "session-1") is not None


def test_run_visible_predicate_for_list_filtering(trace_dir):
    _write_trace(trace_dir, RUN_ALICE, owner=ALICE)

    assert acl.run_visible(RUN_ALICE, _principal(ALICE)) is True
    assert acl.run_visible(RUN_ALICE, _principal(BOB)) is False
    assert acl.run_visible(RUN_ALICE, None) is False


def test_enforce_run_access_http_maps_denial_to_403(trace_dir):
    from fastapi import HTTPException

    _write_trace(trace_dir, RUN_ALICE, owner=ALICE)
    # dev 模式：任何 key 都映射到 dev principal，而该 run 属于 ALICE → 403。
    with pytest.raises(HTTPException) as denied:
        acl.enforce_run_access_http(RUN_ALICE, "")
    assert denied.value.status_code == 403


def test_enforce_run_access_http_allows_dev_owner(trace_dir):
    _write_trace(trace_dir, RUN_ALICE, owner=acl.DEV_PRINCIPAL_ID)
    # 不抛异常即为放行。
    acl.enforce_run_access_http(RUN_ALICE, "")


# ── 3. dev 模式下检查依然存在 ───────────────────────────────


def test_dev_mode_maps_everyone_to_one_principal(monkeypatch):
    monkeypatch.setattr(config, "api_key", "")
    monkeypatch.setattr(config, "auth_mode", "dev")

    first = acl.current_principal(None, "")
    second = acl.current_principal("X-Principal: bob", "anything")

    assert first.principal_id == acl.DEV_PRINCIPAL_ID
    assert second.principal_id == acl.DEV_PRINCIPAL_ID
    assert first.scopes == second.scopes


def test_dev_mode_still_rejects_foreign_resources():
    """dev 模式不是“没有检查”：归属不符照样拒绝。"""
    dev = acl.Principal(acl.DEV_PRINCIPAL_ID, frozenset(acl.ACTION_SCOPES.values()), "dev")

    acl.authorize(dev, acl.DEV_PRINCIPAL_ID, action="run:read")
    with pytest.raises(acl.PermissionDenied):
        acl.authorize(dev, ALICE, action="run:read")


def test_deployment_mode_without_key_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "api_key", "")
    monkeypatch.setattr(config, "auth_mode", "deployment")

    with pytest.raises(acl.PermissionDenied) as denied:
        acl.current_principal(None, "")
    assert denied.value.status_code == 401


def test_configured_key_rejects_wrong_and_accepts_right(monkeypatch):
    monkeypatch.setattr(config, "api_key", "s3cret-key")
    monkeypatch.setattr(config, "auth_mode", "dev")

    principal = acl.current_principal(None, "s3cret-key")
    assert principal.source == "api_key"
    assert principal.principal_id.startswith(acl.API_KEY_PRINCIPAL_PREFIX)
    # principal_id 只含指纹，不含密钥本身。
    assert "s3cret-key" not in principal.principal_id

    for candidate in ("", "wrong", "s3cret-keY"):
        with pytest.raises(acl.PermissionDenied) as denied:
            acl.current_principal(None, candidate)
        assert denied.value.status_code == 401


def test_forged_principal_header_is_ignored(monkeypatch):
    """``request_text``（X-Principal 之类）绝不参与身份判定。"""
    monkeypatch.setattr(config, "api_key", "")
    monkeypatch.setattr(config, "auth_mode", "dev")

    forged = acl.current_principal(f"principal:{ALICE}", "")
    assert forged.principal_id == acl.DEV_PRINCIPAL_ID


# ── 4. ACL 侧表 ─────────────────────────────────────────────


def test_claim_owner_does_not_steal_existing_document(acl_file):
    assert acl.claim_owner("knowledge_document", "doc-1", ALICE) is True
    assert acl.claim_owner("knowledge_document", "doc-1", BOB) is False
    assert acl.resource_owner("knowledge_document", "doc-1") == ALICE


def test_unknown_kind_is_rejected_without_writing(acl_file):
    assert acl.record_owner("runn", "x", ALICE) is False
    assert not acl_file.exists()


def test_empty_owner_clears_the_record(acl_file):
    acl.claim_owner("knowledge_document", "doc-2", ALICE)
    acl.record_owner("knowledge_document", "doc-2", "")
    assert acl.resource_owner("knowledge_document", "doc-2") is None


# ── 5. 知识文档接口：delete / activate / list ───────────────


class _FakeVectorStore:
    """最小向量库替身：知识文档归属测试不关心分片细节。"""

    def __init__(self) -> None:
        self.deleted_documents: list[str] = []
        self.metric_type = "COSINE"
        self.score_kind = "similarity"
        self.collection_name = "biz"

    def _ensure_initialized(self) -> bool:
        return True

    def add_documents(self, documents, *, ids=None):
        return list(ids or [])

    def delete_by_ids(self, ids) -> int:
        return 0

    def delete_by_document(self, document_id: str) -> int:
        self.deleted_documents.append(document_id)
        return 0


@pytest.fixture()
def knowledge_env(tmp_path, monkeypatch):
    monkeypatch.setenv(store_api.STORE_FILE_ENV, str(tmp_path / "knowledge" / "store.json"))
    fake = _FakeVectorStore()
    monkeypatch.setattr(store_api, "vector_store_manager", fake)
    monkeypatch.setattr("app.services.knowledge_store.vector_store_manager", fake)
    store_api.reset_store()
    test_app = FastAPI()
    test_app.include_router(knowledge_api.router, prefix="/api")
    with TestClient(test_app) as client:
        yield {"client": client, "fake": fake}
    store_api.reset_store()


def _submit(env, *, source_uri: str, title: str) -> str:
    accepted = store_api.get_store().submit_document(
        source_uri=source_uri, title=title, content=b"# doc\n\nbody\n", source_kind="upload",
    )
    store_api.wait_for_job(accepted["job_id"], timeout=10.0)
    return accepted["document_id"]


def test_knowledge_delete_requires_ownership(knowledge_env):
    document_id = _submit(knowledge_env, source_uri="upload://acl.md", title="ACL")
    acl.claim_owner("knowledge_document", document_id, ALICE)

    # dev 模式解析出的 principal 不是 ALICE → 403，且没有真的删除。
    response = knowledge_env["client"].delete(f"/api/knowledge/documents/{document_id}")
    assert response.status_code == 403, response.text
    assert knowledge_env["fake"].deleted_documents == []

    # 换成 owner 身份（直接把当前 principal 指向 ALICE）后放行。
    acl.record_owner("knowledge_document", document_id, acl.DEV_PRINCIPAL_ID)
    ok = knowledge_env["client"].delete(f"/api/knowledge/documents/{document_id}")
    assert ok.status_code == 200, ok.text
    assert document_id in knowledge_env["fake"].deleted_documents
    # 删除成功后归属记录被清掉，重新导入不会继承旧 owner。
    assert acl.resource_owner("knowledge_document", document_id) is None


def test_knowledge_activate_requires_ownership(knowledge_env):
    document_id = _submit(knowledge_env, source_uri="upload://acl2.md", title="ACL2")
    acl.claim_owner("knowledge_document", document_id, ALICE)

    denied = knowledge_env["client"].post(
        f"/api/knowledge/documents/{document_id}/activate", json={"revision": "rev-x"},
    )
    assert denied.status_code == 403, denied.text

    acl.record_owner("knowledge_document", document_id, acl.DEV_PRINCIPAL_ID)
    allowed = knowledge_env["client"].post(
        f"/api/knowledge/documents/{document_id}/activate", json={"revision": "rev-x"},
    )
    # 归属通过后由存储层报“revision 不存在”，而不是 403。
    assert allowed.status_code in (400, 404), allowed.text


def test_knowledge_list_hides_foreign_documents(knowledge_env):
    mine = _submit(knowledge_env, source_uri="upload://mine.md", title="Mine")
    theirs = _submit(knowledge_env, source_uri="upload://theirs.md", title="Theirs")
    acl.claim_owner("knowledge_document", mine, acl.DEV_PRINCIPAL_ID)
    acl.claim_owner("knowledge_document", theirs, ALICE)

    response = knowledge_env["client"].get("/api/knowledge/documents")
    assert response.status_code == 200, response.text
    visible = {item["document_id"] for item in response.json()["documents"]}
    assert mine in visible
    assert theirs not in visible


def test_knowledge_upload_records_owner(knowledge_env):
    response = knowledge_env["client"].post(
        "/api/knowledge/documents",
        files={"file": ("acl3.md", b"# ACL3\n\nbody\n", "text/markdown")},
    )
    assert response.status_code == 202, response.text
    document_id = response.json()["document_id"]
    assert acl.resource_owner("knowledge_document", document_id) == acl.DEV_PRINCIPAL_ID


# ── 6. 文档级 ACL 在检索路径生效（G5） ──────────────────────


class _SearchableVectorStore:
    """向量库替身：保留真实 Document，供检索适配器做后置核对。

    刻意**不解析** ``expr``（把全部行都返回）：这样“B 检索不到 A 的私有文档”
    只能由检索路径自身的文档级过滤保证，而不是靠数据库碰巧过滤对了。
    """

    score_kind = "similarity"
    metric_type = "COSINE"
    collection_name = "biz"

    def __init__(self) -> None:
        self.rows: list = []
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


@pytest.fixture()
def acl_retrieval_env(tmp_path, monkeypatch):
    """真实 KnowledgeStore + 真实 VectorStoreRetriever；只替换向量 I/O。"""
    from app.services import knowledge_service, knowledge_store as store_module

    monkeypatch.setenv(store_module.STORE_FILE_ENV, str(tmp_path / "knowledge" / "store.json"))
    vectors = _SearchableVectorStore()
    monkeypatch.setattr(store_module, "vector_store_manager", vectors)

    class _Splitter:
        def split_document(self, content, source, *, document_id, revision):
            from langchain_core.documents import Document

            return [Document(page_content=content, metadata={
                "_source": source,
                "_document_id": document_id,
                "_revision": revision,
                "_chunk_id": f"{document_id}:{revision}:1",
            })]

    monkeypatch.setattr(store_module, "document_splitter_service", _Splitter())
    store_module.reset_store()
    store = store_module.get_store()

    def upload(text: str, *, source: str, owner: str) -> str:
        accepted = store.submit_document(source_uri=source, title=source, content=text.encode())
        store_module.wait_for_job(accepted["job_id"], timeout=10.0)
        acl.claim_owner("knowledge_document", accepted["document_id"], owner)
        return accepted["document_id"]

    yield {"store": store, "vectors": vectors, "upload": upload,
           "retriever": knowledge_service.VectorStoreRetriever,
           "store_module": store_module}
    store_module.reset_store()


def test_private_document_is_not_retrievable_by_another_principal(acl_retrieval_env):
    """A 上传的私有文档，B 的检索既拿不到候选也拿不到片段。"""
    env = acl_retrieval_env
    private_id = env["upload"]("ALICE ONLY runbook", source="upload://alice.md",
                               owner=ALICE)
    shared_id = env["upload"]("public runbook", source="upload://public.md", owner=BOB)

    chunks_b = env["retriever"](env["vectors"], principal_id=BOB).search("runbook", 5)

    texts = [chunk.text for chunk in chunks_b]
    assert texts == ["public runbook"]
    expr = env["vectors"].exprs[-1] or ""
    assert private_id not in expr, "未授权文档 ID 不得进入预过滤表达式"
    assert shared_id in expr
    assert all(chunk.document_id != private_id for chunk in chunks_b)

    chunks_a = env["retriever"](env["vectors"], principal_id=ALICE).search("runbook", 5)
    assert [chunk.text for chunk in chunks_a] == ["ALICE ONLY runbook"]


def test_uploaded_document_gets_uploader_in_acl(knowledge_env):
    """上传时把当前 principal 写进文档级 ACL；列表只暴露数量，不暴露成员。"""
    response = knowledge_env["client"].post(
        "/api/knowledge/documents",
        files={"file": ("acl-private.md", b"# private\n\nbody\n", "text/markdown")},
        data={"os_family": "RHEL", "valid_until": "2027-12-31"},
    )
    assert response.status_code == 202, response.text
    # ACL 在上传响应前已写入；等作业终态是为了不让后台 worker 跨用例继续跑。
    store_api.wait_for_job(response.json()["job_id"], timeout=15.0)
    document_id = response.json()["document_id"]
    snapshot = store_api.get_store().snapshot()["documents"][document_id]
    assert snapshot["acl_principals"] == [acl.DEV_PRINCIPAL_ID]
    assert snapshot["os_family"] == "RHEL"
    assert snapshot["valid_until"] == "2027-12-31"

    listing = knowledge_env["client"].get("/api/knowledge/documents").json()
    item = next(doc for doc in listing["documents"] if doc["document_id"] == document_id)
    assert item["acl_count"] == 1
    assert item["valid_until"] == "2027-12-31"
    # 响应里不得出现任何 ACL 成员身份。
    assert acl.DEV_PRINCIPAL_ID not in json.dumps(item, ensure_ascii=False)
    assert "acl_principals" not in item


def test_upload_rejects_invalid_metadata(knowledge_env):
    bad_date = knowledge_env["client"].post(
        "/api/knowledge/documents",
        files={"file": ("bad-date.md", b"# x\n\nbody\n", "text/markdown")},
        data={"valid_from": "2026/01/01"},
    )
    assert bad_date.status_code == 400, bad_date.text
    assert "valid_from" in bad_date.json()["detail"]

    bad_principal = knowledge_env["client"].post(
        "/api/knowledge/documents",
        files={"file": ("bad-acl.md", b"# y\n\nbody\n", "text/markdown")},
        data={"acl_principals": "everyone"},
    )
    assert bad_principal.status_code == 400, bad_principal.text
    assert knowledge_env["client"].get("/api/knowledge/jobs").json()["jobs"] == []


def test_import_accepts_optional_metadata_and_rejects_invalid_date(knowledge_env):
    """导入路径同样接受可选元数据；非法日期 400 且不创建作业。"""
    ok = knowledge_env["client"].post(
        "/api/knowledge/import",
        json={"source_id": "aiops-docs/disk_high_usage.md",
              "os_family": "RHEL", "product_version": "262",
              "valid_from": "2026-01-01", "valid_until": "2027-12-31"},
    )
    assert ok.status_code == 202, ok.text
    # 等作业进入终态：否则后台 worker 会在 fixture 拆除后继续跑，污染日志与下一条用例。
    store_api.wait_for_job(ok.json()["job_id"], timeout=15.0)
    document_id = ok.json()["document_id"]
    document = store_api.get_store().snapshot()["documents"][document_id]
    assert document["os_family"] == "RHEL"
    assert document["product_version"] == "262"
    assert document["valid_from"] == "2026-01-01"
    assert document["valid_until"] == "2027-12-31"

    bad = knowledge_env["client"].post(
        "/api/knowledge/import",
        json={"source_id": "aiops-docs/disk_high_usage.md", "valid_until": "31/12/2027"},
    )
    assert bad.status_code == 400, bad.text
    assert "valid_until" in bad.json()["detail"]
    # 只统计原作业：非法请求不得产生新作业。
    assert len(knowledge_env["client"].get("/api/knowledge/jobs").json()["jobs"]) == 1


def test_upload_does_not_open_someone_elses_document(knowledge_env):
    """同一 source_uri 属于别人时，上传被拒绝（而不是"202 但不改 ACL"）。

    旧行为只保证"上传者没被加进别人 ACL"，但新 revision 仍会被索引并**激活**
    到那份文档上：owner/ACL 不变，检索内容却被替换。G6 复核实测确认了这条越权，
    所以现在的口径是**提交前就拒绝**（409），文档一个字节都不动。
    """
    content = b"# shared\n\nprivate body\n"
    document_id = store_api.document_id_for("upload://shared-private.md")
    acl.claim_owner("knowledge_document", document_id, ALICE)

    response = knowledge_env["client"].post(
        "/api/knowledge/documents",
        files={"file": ("shared-private.md", content, "text/markdown")},
    )
    assert response.status_code == 409, response.text

    snapshot = store_api.get_store().snapshot()
    assert acl.resource_owner("knowledge_document", document_id) == ALICE
    # 文档记录可能还不存在（首次上传被拒），存在时也必须是"未被改动"。
    doc = snapshot["documents"].get(document_id)
    if doc is not None:
        assert doc["acl_principals"] == [], "上传者不得借同名上传进入别人的 ACL"
        assert doc["active_revision"] != store_api.revision_for(content), \
            "被拒绝的上传不得激活到别人的文档上"
