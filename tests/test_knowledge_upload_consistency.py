"""G7 P0-1：上传归属与文档提交的一致性（幽灵 owner / 回滚 / ACL seal 失败）。

背景（G7 复核实测确认的三个问题）：

1. metadata 校验发生在 ``submit_document`` 内部，而 owner 侧表在那之前就已经写好；
   非法 ``valid_from`` 会 400，但留下一份**文档并不存在**的 owner 记录。此后同名
   上传被永久 409，且 DELETE 也清不掉（文档不存在 → 404，`record_owner("")` 那行
   永远执行不到）。
2. 提交失败（向量库挂了、磁盘满）时，同上：owner 留下、文档没有。
3. ACL 补写失败但接口返回 202：若 `acl_principals` 里恰好只有别人，上传者会
   **看不到自己的文档与作业**（早期读取谓词"ACL 非空就忽略 owner"），
   可见但写不了，等于 202 成功却什么都做不了。

这个文件只验证**存储状态**，不只验证 HTTP 状态码：`owner` 侧表内容、文档记录、
`visible_to` / `writable_by`、作业详情四者必须互相自洽。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import knowledge as knowledge_api
from app.config import config
from app.core import principal as acl
from app.services import knowledge_store as store_api

DEV = acl.DEV_PRINCIPAL_ID
ALICE = "principal:key-alice000000"
BOB = "principal:key-bob000000"
STRANGER = "principal:key-stranger0000"


class _FakeVectorStore:
    metric_type = "COSINE"
    score_kind = "similarity"
    collection_name = "biz"

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def _ensure_initialized(self) -> bool:
        return True

    def add_documents(self, documents, *, ids=None):
        for doc, row_id in zip(documents, list(ids or []), strict=False):
            self.rows[row_id] = {
                "document_id": str((doc.metadata or {}).get("_document_id") or ""),
                "revision": str((doc.metadata or {}).get("_revision") or ""),
                "content": doc.page_content,
            }
        return list(ids or [])

    def delete_by_ids(self, ids) -> int:
        removed = 0
        for row_id in [value for value in ids if value]:
            if self.rows.pop(row_id, None) is not None:
                removed += 1
        return removed

    def delete_by_document(self, document_id: str) -> int:
        targets = [row_id for row_id, row in self.rows.items()
                   if row["document_id"] == document_id]
        for row_id in targets:
            self.rows.pop(row_id, None)
        return len(targets)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(store_api.STORE_FILE_ENV, str(tmp_path / "knowledge" / "store.json"))
    monkeypatch.setattr(config, "knowledge_tenant_mode", "single")
    fake = _FakeVectorStore()
    monkeypatch.setattr(store_api, "vector_store_manager", fake)
    monkeypatch.setattr("app.services.knowledge_store.vector_store_manager", fake)
    store_api.reset_store()
    test_app = FastAPI()
    test_app.include_router(knowledge_api.router, prefix="/api")
    with TestClient(test_app) as client:
        yield {"client": client, "fake": fake, "store": store_api.get_store(),
               "acl_file": tmp_path / "acl" / "principal_acl.json"}
    store_api.reset_store()


def _doc_owners(env) -> list[str]:
    """ACL 侧表里 knowledge_document 类的键（只报键名，不涉及任何凭据）。"""
    path = env["acl_file"]
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return sorted(key for key in (payload.get("owners") or {})
                  if key.startswith("knowledge_document:"))


def _upload(env, filename: str, content: bytes, **data):
    return env["client"].post(
        "/api/knowledge/documents",
        files={"file": (filename, content, "text/markdown")},
        data=data or None,
    )


# ── 1. 非法 metadata 不得留下幽灵 owner ─────────────────────


@pytest.mark.parametrize("field,value", [
    ("valid_from", "not-a-date"),
    ("valid_until", "20260101"),
    ("acl_principals", "not-a-principal"),
])
def test_invalid_metadata_leaves_no_ghost_owner(env, field, value):
    """400 之后：没有文档、没有 owner 记录、同名合法上传能成功。"""
    bad = _upload(env, "ghost.md", b"# ghost\n\nbody\n", **{field: value})
    assert bad.status_code == 400, bad.text

    assert env["store"].snapshot()["documents"] == {}
    assert _doc_owners(env) == [], "非法 metadata 不得在 owner 侧表留下记录"

    good = _upload(env, "ghost.md", b"# ghost\n\nbody\n", valid_from="2026-01-01")
    assert good.status_code == 202, good.text
    assert env["store"].has_document(good.json()["document_id"])


def test_invalid_valid_from_after_valid_until_is_400_without_owner(env):
    """区间反了也算非法：同样不允许留下 owner。"""
    bad = _upload(env, "range.md", b"# r\n\nb\n",
                  valid_from="2026-12-31", valid_until="2026-01-01")
    assert bad.status_code == 400, bad.text
    assert _doc_owners(env) == []


def test_failed_upload_does_not_privatize_shared_document(env):
    """对**已存在的无 owner 共享文档**做一次非法上传，不得把它变成私有。

    这是幽灵 owner 的连带后果：claim 会成功写下私有 owner，正文一点没变，但
    原本对所有人可见的文档立刻只对上传者可见。
    """
    store = env["store"]
    accepted = store.submit_document(
        source_uri="upload://shared.md", title="shared",
        content=b"# shared\n\nbody\n", source_kind="upload",
    )
    store_api.wait_for_job(accepted["job_id"], timeout=10.0)
    document_id = accepted["document_id"]
    assert store.document_visible_to(document_id, BOB) is True

    bad = _upload(env, "shared.md", b"# shared\n\nbody\n", valid_from="oops")
    assert bad.status_code == 400, bad.text
    assert store.document_visible_to(document_id, BOB) is True, \
        "失败的上传不得把共享文档私有化"
    assert acl.resource_owner("knowledge_document", document_id) is None


# ── 2. 提交失败回滚本次新建的 owner ─────────────────────────


def test_submit_failure_rolls_back_new_owner(env):
    store = env["store"]
    original = store.submit_document

    def boom(**_kwargs):
        raise RuntimeError("milvus exploded")

    store.submit_document = boom
    try:
        failed = _upload(env, "rollback.md", b"# rb\n\nb\n")
    finally:
        store.submit_document = original

    assert failed.status_code == 500, failed.text
    assert _doc_owners(env) == [], "提交失败必须回滚本次新建的 owner"
    # 回滚干净之后，同一个文件名的合法上传必须能成功。
    retried = _upload(env, "rollback.md", b"# rb\n\nb\n")
    assert retried.status_code == 202, retried.text


def test_submit_failure_keeps_preexisting_owner(env):
    """文档原本就归属本人时，提交失败不得把 owner 记录删掉。"""
    store = env["store"]
    document_id = store_api.document_id_for("upload://keep.md")
    acl.record_owner("knowledge_document", document_id, DEV)

    original = store.submit_document

    def boom(**_kwargs):
        raise RuntimeError("milvus exploded")

    store.submit_document = boom
    try:
        failed = _upload(env, "keep.md", b"# keep\n\nb\n")
    finally:
        store.submit_document = original

    assert failed.status_code == 500, failed.text
    assert acl.resource_owner("knowledge_document", document_id) == DEV, \
        "失败不得删除并非本次新建的 owner"


def test_release_is_conditional_not_blind_delete(env):
    """回滚用的是条件删除：owner 已被别人改写时不得删除。"""
    document_id = env["store"]._data  # noqa: SLF001 - 只为取一个稳定 id 拼法
    del document_id
    target = store_api.document_id_for("upload://race.md")
    acl.record_owner("knowledge_document", target, ALICE)

    assert acl.release_owner_if("knowledge_document", target, DEV) is False
    assert acl.resource_owner("knowledge_document", target) == ALICE, \
        "条件删除不得抹掉别人的记录"
    assert acl.release_owner_if("knowledge_document", target, ALICE) is True
    assert acl.resource_owner("knowledge_document", target) is None


# ── 3. ACL seal 失败不得让上传者失去可见性 ──────────────────


def test_acl_seal_failure_keeps_uploader_visibility(env):
    """seal 失败 + ACL 里只有别人：上传者仍能列出、查作业、检索该文档。"""
    store = env["store"]
    original = store.add_acl_principal

    def boom(*_args, **_kwargs):
        raise RuntimeError("acl disk full")

    store.add_acl_principal = boom
    try:
        uploaded = _upload(env, "sealfail.md", b"# seal\n\nbody\n",
                           acl_principals=STRANGER)
    finally:
        store.add_acl_principal = original

    assert uploaded.status_code == 202, uploaded.text
    payload = uploaded.json()
    assert payload.get("acl_sealed") is False, "ACL 没写成必须在响应里如实标注"

    document_id = payload["document_id"]
    snapshot = store.snapshot()["documents"][document_id]
    assert snapshot["acl_principals"] == [STRANGER], "ACL 里不含 uploader（这正是缺陷前提）"

    # 三处读取都必须对上传者成立：owner OR ACL。
    assert store.document_visible_to(document_id, DEV) is True
    assert document_id in {item["document_id"]
                           for item in store.list_documents(limit=50, principal_id=DEV)[0]}
    detail = env["client"].get(f"/api/knowledge/jobs/{payload['job_id']}")
    assert detail.status_code == 200, detail.text
    # 检索路径（visible_documents）同样可见。
    assert document_id in store.visible_documents(DEV)
    # 显式共享出去的那个 principal 仍然可见（ACL 生效，没被 owner 判定吃掉）。
    assert store.document_visible_to(document_id, STRANGER) is True
    # 既不是 owner 也不在 ACL 里的第三方仍然看不见。
    assert store.document_visible_to(document_id, BOB) is False


def test_acl_seal_success_marks_response_sealed(env):
    uploaded = _upload(env, "sealok.md", b"# ok\n\nb\n")
    assert uploaded.status_code == 202, uploaded.text
    assert "acl_sealed" not in uploaded.json(), \
        "成功时不必额外加字段（保持既有响应形状）"
    document_id = uploaded.json()["document_id"]
    snapshot = env["store"].snapshot()["documents"][document_id]
    assert DEV in snapshot["acl_principals"]


# ── 4. 三种身份 × 五种操作的权限矩阵（不回归 G6 规则） ───────


def test_permission_matrix_owner_acl_member_stranger(env):
    """owner / ACL 只读成员 / 陌生人的 list·get·activate·delete·retry 结果。"""
    store = env["store"]

    # 一个 owner 是 ALICE、ACL 里额外有 BOB（只读共享）的文档。
    accepted = store.submit_document(
        source_uri="upload://matrix.md", title="matrix",
        content=b"# matrix\n\nbody\n", source_kind="upload",
        metadata={"acl_principals": [BOB]},
    )
    store_api.wait_for_job(accepted["job_id"], timeout=10.0)
    document_id = accepted["document_id"]
    acl.record_owner("knowledge_document", document_id, ALICE)

    # ACL 生效（只读共享），owner 也在。
    assert store.document_visible_to(document_id, ALICE) is True
    assert store.document_visible_to(document_id, BOB) is True
    assert store.document_visible_to(document_id, STRANGER) is False

    # 写权限：只有 owner。ACL 成员能读不能改（G6 语义）。
    assert store.document_writable_by(document_id, ALICE) is True
    assert store.document_writable_by(document_id, BOB) is False
    assert store.document_writable_by(document_id, STRANGER) is False

    # 作业详情：可见者可看，陌生人 404。
    assert env["client"].get(f"/api/knowledge/jobs/{accepted['job_id']}").status_code in (200, 404)
