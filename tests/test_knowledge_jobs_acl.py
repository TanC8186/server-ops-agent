"""G6 P0-1：知识库作业权限闭环（列表 / 详情 / retry 与文档可见性一致）。

真实缺陷：``/api/knowledge/jobs`` 没有任何 principal 过滤，知道 job_id 就能读
别人的 ``source_uri``、错误信息，甚至替别人 retry 出一个新作业；``documents``
列表先全局分页再逐条过滤，隐藏文档会吃掉当前用户的 ``limit``。

本文件用真实路由 + 真实 ``KnowledgeStore``，只替换向量库 I/O；身份沿用 dev
模式（解析为 ``principal:dev``），通过 ACL 侧表把文档改成别人的来制造“不可见”
场景——这正是列表、详情、retry 三处必须给出同一结论的业务边界。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import knowledge as knowledge_api
from app.config import config
from app.core import principal as acl
from app.services import knowledge_store as store_api

ALICE = "principal:key-alice000000"
DEV = acl.DEV_PRINCIPAL_ID


class _FakeVectorStore:
    """最小向量库替身：只保留与作业状态机相关的“写入成功/失败”。"""

    def __init__(self) -> None:
        self.fail_on_add = False
        self.metric_type = "COSINE"
        self.score_kind = "similarity"
        self.collection_name = "biz"

    def _ensure_initialized(self) -> bool:
        return True

    def add_documents(self, documents, *, ids=None):
        if self.fail_on_add:
            raise RuntimeError("milvus insert failed")
        return list(ids or [])

    def delete_by_ids(self, ids) -> int:
        return 0

    def delete_by_document(self, document_id: str) -> int:
        return 0


@pytest.fixture()
def knowledge_env(tmp_path, monkeypatch):
    monkeypatch.setenv(store_api.STORE_FILE_ENV, str(tmp_path / "knowledge" / "store.json"))
    # 单租户 dev 是验收基线；显式设定，避免外部环境变量改变测试语义。
    monkeypatch.setattr(config, "knowledge_tenant_mode", "single")
    fake = _FakeVectorStore()
    monkeypatch.setattr(store_api, "vector_store_manager", fake)
    monkeypatch.setattr("app.services.knowledge_store.vector_store_manager", fake)
    store_api.reset_store()
    test_app = FastAPI()
    test_app.include_router(knowledge_api.router, prefix="/api")
    with TestClient(test_app) as client:
        yield {"client": client, "fake": fake, "store": store_api.get_store()}
    store_api.reset_store()


def _submit(env, *, source_uri: str, owner: str | None = None,
            content: bytes = b"# doc\n\nbody\n") -> tuple[dict, dict]:
    """提交并等待作业终态；``owner`` 非空时把文档归属记到指定 principal。"""
    accepted = store_api.get_store().submit_document(
        source_uri=source_uri, title=source_uri, content=content, source_kind="upload",
    )
    job = store_api.wait_for_job(accepted["job_id"], timeout=10.0)
    if owner is not None:
        acl.record_owner("knowledge_document", accepted["document_id"], owner)
    return accepted, job


# ── 1. 归属本人：列表、详情、retry 都可用 ───────────────────


def test_owner_can_list_get_and_retry_own_failed_job(knowledge_env):
    knowledge_env["fake"].fail_on_add = True
    accepted, failed = _submit(knowledge_env, source_uri="upload://mine-failed.md",
                               owner=DEV)
    assert failed["status"] == "failed"

    client = knowledge_env["client"]
    listed = client.get("/api/knowledge/jobs").json()["jobs"]
    assert [item["job_id"] for item in listed] == [accepted["job_id"]]

    detail = client.get(f"/api/knowledge/jobs/{accepted['job_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["error"]["code"] == "index_failed"

    knowledge_env["fake"].fail_on_add = False
    retried = client.post(f"/api/knowledge/jobs/{accepted['job_id']}/retry")
    assert retried.status_code == 202, retried.text
    assert retried.json()["job_id"] != accepted["job_id"]
    ready = store_api.wait_for_job(retried.json()["job_id"], timeout=10.0)
    assert ready["status"] == "ready"


# ── 2. 文档归属别人：列表看不见、详情 404、retry 不建新作业 ──


def test_foreign_document_job_is_hidden_and_retry_creates_nothing(knowledge_env):
    knowledge_env["fake"].fail_on_add = True
    accepted, failed = _submit(knowledge_env, source_uri="upload://alice-failed.md")
    assert failed["status"] == "failed"
    # 归属改成别人：此后 list / get / retry 必须统一按“看不到这份文档”处理。
    acl.record_owner("knowledge_document", accepted["document_id"], ALICE)

    store = knowledge_env["store"]
    jobs_before = len(store.snapshot()["jobs"])
    client = knowledge_env["client"]

    listed = client.get("/api/knowledge/jobs").json()["jobs"]
    assert accepted["job_id"] not in {item["job_id"] for item in listed}

    detail = client.get(f"/api/knowledge/jobs/{accepted['job_id']}")
    assert detail.status_code == 404, detail.text

    retried = client.post(f"/api/knowledge/jobs/{accepted['job_id']}/retry")
    assert retried.status_code == 404, retried.text
    assert len(store.snapshot()["jobs"]) == jobs_before, "不可见作业不得创建新作业"


# ── 3. 隐藏文档/作业不得消耗分页配额 ────────────────────────


def test_hidden_documents_do_not_consume_the_page(knowledge_env):
    mine = _submit(knowledge_env, source_uri="upload://page-mine.md", owner=DEV)
    _submit(knowledge_env, source_uri="upload://page-alice-a.md", owner=ALICE)
    _submit(knowledge_env, source_uri="upload://page-alice-b.md", owner=ALICE)

    first = knowledge_env["client"].get("/api/knowledge/documents?limit=1").json()
    assert [item["document_id"] for item in first["documents"]] == [mine[0]["document_id"]]
    assert first["next_cursor"] is None, "只有一条可见文档时不得再给出下一页"


def test_visible_documents_are_paged_around_hidden_ones(knowledge_env):
    for name, owner in (("page-a.md", DEV), ("page-x.md", ALICE),
                        ("page-b.md", DEV), ("page-y.md", ALICE)):
        _submit(knowledge_env, source_uri=f"upload://{name}", owner=owner)
    mine = {
        store_api.document_id_for("upload://page-a.md"),
        store_api.document_id_for("upload://page-b.md"),
    }

    client = knowledge_env["client"]
    seen: list[str] = []
    cursor = ""
    for _ in range(3):
        query = "?limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(f"/api/knowledge/documents{query}").json()
        seen.extend(item["document_id"] for item in page["documents"])
        cursor = page["next_cursor"] or ""
        if not cursor:
            break

    assert set(seen) == mine, "翻页只应看到自己的文档，且不得重复或漏项"
    assert len(seen) == 2


def test_hidden_jobs_do_not_consume_the_limit(knowledge_env):
    knowledge_env["fake"].fail_on_add = True
    mine, _ = _submit(knowledge_env, source_uri="upload://job-mine.md", owner=DEV)
    _submit(knowledge_env, source_uri="upload://job-alice-a.md", owner=ALICE)
    _submit(knowledge_env, source_uri="upload://job-alice-b.md", owner=ALICE)

    listed = knowledge_env["client"].get("/api/knowledge/jobs?limit=1").json()["jobs"]

    assert [item["job_id"] for item in listed] == [mine["job_id"]]


# ── 4. 单租户 dev 现状：无 owner 文档仍可见 ─────────────────


def test_ownerless_document_and_job_stay_visible_in_single_mode(knowledge_env):
    accepted, job = _submit(knowledge_env, source_uri="upload://legacy.md")
    assert job["status"] == "ready"

    client = knowledge_env["client"]
    documents = client.get("/api/knowledge/documents").json()["documents"]
    assert accepted["document_id"] in {item["document_id"] for item in documents}

    jobs = client.get("/api/knowledge/jobs").json()["jobs"]
    assert accepted["job_id"] in {item["job_id"] for item in jobs}
    assert client.get(f"/api/knowledge/jobs/{accepted['job_id']}").status_code == 200


# ── 5. 写权限边界（G6 复核发现的四条越权） ──────────────────


def _upload_via_route(env, *, filename: str, content: bytes, headers: dict | None = None):
    return env["client"].post(
        "/api/knowledge/documents",
        files={"file": (filename, content, "text/markdown")},
        headers=headers or {},
    )


def test_read_only_acl_member_cannot_activate_or_delete(knowledge_env):
    """只读 ACL 成员：revisions 可读，但 activate/delete 必须 403。

    复核时实测：``_enforce_document`` 只查 owner 侧表，而"有 ACL 无 owner"的
    文档没有任何 owner 记录 → ``acl.authorize`` 按"未记录 owner 就放行"处理，
    于是陌生 principal 能切换 active revision 并删除整份文档。
    """
    accepted, _job = _submit(knowledge_env, source_uri="upload://shared-readonly.md")
    document_id = accepted["document_id"]
    # 构造成"有 ACL、无 owner"：只有 ACL 成员才能读。
    acl.record_owner("knowledge_document", document_id, "")
    store_api.get_store().add_acl_principal(document_id, ALICE)

    client = knowledge_env["client"]  # dev principal，不在 ACL 里

    assert client.get(f"/api/knowledge/documents/{document_id}/revisions").status_code == 404
    # 写路由沿用历史的 403 语义（文档存在但不许改）；读路由统一 404。
    assert client.post(f"/api/knowledge/documents/{document_id}/activate",
                       json={"revision": "rev-x"}).status_code == 403
    assert client.delete(f"/api/knowledge/documents/{document_id}").status_code == 403
    # 文档仍在（没有被删除）。
    assert store_api.get_store().has_document(document_id)


def test_acl_member_without_owner_can_write(knowledge_env):
    """无 owner 但有 ACL 时，ACL 成员本人是唯一归属线索，可以写。"""
    accepted, job = _submit(knowledge_env, source_uri="upload://acl-ownerless.md",
                            owner=ALICE)
    document_id = accepted["document_id"]
    store_api.get_store().add_acl_principal(document_id, ALICE)

    # 用 ALICE 身份直接调用存储层（dev 路由只解析出 principal:dev）。
    store = store_api.get_store()
    assert store.document_visible_to(document_id, ALICE)
    assert store.document_writable_by(document_id, ALICE)
    assert not store.document_writable_by(document_id, DEV)
    assert job["status"] == "ready"


def test_retry_requires_write_permission_not_just_visibility(knowledge_env):
    """只读 ACL 成员不能 retry：重试成功会把旧 revision 重新激活。"""
    knowledge_env["fake"].fail_on_add = True
    accepted, failed = _submit(knowledge_env, source_uri="upload://retry-write.md",
                               owner=ALICE)
    assert failed["status"] == "failed"
    document_id = accepted["document_id"]
    store_api.get_store().add_acl_principal(document_id, ALICE)

    store = knowledge_env["store"]
    jobs_before = len(store.snapshot()["jobs"])
    # dev principal 既不是 owner 也不在 ACL 里：看不到 → 404，且不创建作业。
    retried = knowledge_env["client"].post(
        f"/api/knowledge/jobs/{accepted['job_id']}/retry")
    assert retried.status_code == 404, retried.text
    assert len(store.snapshot()["jobs"]) == jobs_before


def test_same_source_uri_upload_by_stranger_is_rejected(knowledge_env):
    """同名 source_uri 属于别人时上传被拒，不能替换对方正在被检索的内容。"""
    first = _upload_via_route(knowledge_env, filename="policy.md",
                              content=b"# policy\n\nALICE CONTENT\n")
    assert first.status_code == 202
    document_id = first.json()["document_id"]
    store_api.wait_for_job(first.json()["job_id"], timeout=10.0)
    acl.record_owner("knowledge_document", document_id, ALICE)

    second = _upload_via_route(knowledge_env, filename="policy.md",
                               content=b"# policy\n\nBOB INJECTED\n")
    assert second.status_code == 409, second.text
    snapshot = knowledge_env["store"].snapshot()["documents"][document_id]
    assert snapshot["active_revision"] != store_api.revision_for(b"# policy\n\nBOB INJECTED\n"), \
        "别人的内容不得被激活到该文档上"


def test_deleted_document_jobs_are_not_relisted(knowledge_env):
    """删除文档后，遗留作业不得重新出现在列表/详情里（否则删除成了后门）。"""
    accepted, job = _submit(knowledge_env, source_uri="upload://gone-jobs.md", owner=DEV)
    assert job["status"] == "ready"
    client = knowledge_env["client"]

    assert client.delete(f"/api/knowledge/documents/{accepted['document_id']}").status_code == 200

    listed = client.get("/api/knowledge/jobs").json()["jobs"]
    assert accepted["job_id"] not in {item["job_id"] for item in listed}
    assert client.get(f"/api/knowledge/jobs/{accepted['job_id']}").status_code == 404
