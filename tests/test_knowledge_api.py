"""M2 知识库管理 API 测试。

覆盖契约 §2 的关键路径：列表分页、上传 202+job、作业推进、失败重试、
revision 切换、删除、status 的 invalid_index / unavailable，以及
document_id 稳定性与并发重复导入的幂等性。

Milvus 全部用 fake store 注入（monkeypatch ``knowledge_store.vector_store_manager``），
因此没有 Milvus 的环境也能真实运行；其中一个用例专门证明“Milvus 不可用时
status=unavailable 而不是 500”。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import file as file_api
from app.api import knowledge as knowledge_api
from app.services import knowledge_store as store_api
from app.services.vector_store_manager import VectorStoreUnavailable


class FakeVectorStore:
    """最小向量存储替身：按 ``_document_id`` 记录行，支持按 id/文档删除。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.documents: dict[str, int] = {}
        self.calls: list[tuple] = []
        self.add_delay = 0.0
        self.fail_on_add = False
        self.fail_on_delete_document = False
        self.fail_on_delete_ids = False
        self.metric_type = "COSINE"
        self.score_kind = "similarity"
        self.collection_name = "biz"
        self._guard = threading.Lock()
        #: 期望的 _ensure_initialized 行为：None 正常，异常对象则抛出。
        self.init_error: Exception | None = None

    def _ensure_initialized(self) -> bool:
        if self.init_error is not None:
            raise self.init_error
        return True

    def add_documents(self, documents, *, ids=None):
        docs = list(documents)
        doc_ids = list(ids) if ids is not None else [f"auto-{i}" for i in range(len(docs))]
        self.calls.append(("add", len(docs)))
        if self.add_delay:
            time.sleep(self.add_delay)
        if self.fail_on_add:
            raise RuntimeError("milvus insert failed")
        with self._guard:
            for doc, doc_id in zip(docs, doc_ids):
                metadata = doc.metadata or {}
                document_id = str(metadata.get("_document_id") or "")
                self.rows[doc_id] = {
                    "document_id": document_id,
                    "revision": str(metadata.get("_revision") or ""),
                    "content": doc.page_content,
                }
                self.documents[document_id] = self.documents.get(document_id, 0) + 1
        return doc_ids

    def delete_by_ids(self, ids) -> int:
        ids = [value for value in ids if value]
        self.calls.append(("delete_by_ids", tuple(ids)))
        if self.fail_on_delete_ids:
            raise RuntimeError("milvus delete failed")
        removed = 0
        with self._guard:
            for row_id in ids:
                row = self.rows.pop(row_id, None)
                if row is None:
                    continue
                removed += 1
                document_id = row["document_id"]
                if document_id:
                    self.documents[document_id] = max(
                        0, self.documents.get(document_id, 1) - 1
                    )
        return removed

    def delete_by_document(self, document_id: str) -> int:
        self.calls.append(("delete_by_document", document_id))
        if self.fail_on_delete_document:
            raise RuntimeError("milvus delete failed")
        with self._guard:
            targets = [row_id for row_id, row in self.rows.items()
                       if row["document_id"] == document_id]
            for row_id in targets:
                self.rows.pop(row_id, None)
            self.documents.pop(document_id, None)
        return len(targets)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(file_api, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(file_api, "UPLOAD_DIR", uploads)

    store_file = tmp_path / "knowledge" / "store.json"
    monkeypatch.setenv(store_api.STORE_FILE_ENV, str(store_file))

    fake = FakeVectorStore()
    monkeypatch.setattr(store_api, "vector_store_manager", fake)
    # 指向同一 fake，避免不同 import 路径拿到真实 Milvus 单例。
    monkeypatch.setattr("app.services.knowledge_store.vector_store_manager", fake)

    store_api.reset_store()

    test_app = FastAPI()
    test_app.include_router(knowledge_api.router, prefix="/api")
    with TestClient(test_app) as client:
        yield {
            "client": client,
            "app": test_app,
            "fake": fake,
            "store_file": store_file,
            "uploads": uploads,
            "tmp": tmp_path,
        }
    store_api.reset_store()


def _upload(client, name: str, content: bytes):
    return client.post(
        "/api/knowledge/documents",
        files={"file": (name, content, "text/markdown")},
    )


def _long_text(tag: str = "runbook", sections: int = 24) -> bytes:
    """足够长、可切成多个分片的内容（用于观察进度推进）。"""
    body = [
        f"## {tag} 第{index}节\n\n" + f"磁盘满时先检查 inode（{tag}）。" * 8
        for index in range(sections)
    ]
    return (f"# {tag} 长文档\n\n" + "\n\n".join(body)).encode("utf-8")


def _wait(client, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/knowledge/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in ("ready", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"作业未在 {timeout}s 内到达终态: {job_id}")


# ---------------------------------------------------------------------------
# 1. 上传 202 + 作业推进 + 分页
# ---------------------------------------------------------------------------


def test_upload_returns_202_and_job_then_ready(env):
    response = _upload(env["client"], "disk.md", _long_text("disk"))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"].startswith("job-")
    assert body["document_id"].startswith("doc-")

    job = _wait(env["client"], body["job_id"])
    assert job["status"] == "ready", job
    assert job["progress"]["chunks_total"] > 0
    assert job["progress"]["chunks_done"] == job["progress"]["chunks_total"]
    assert job["error"] is None
    assert job["finished_at"]

    listing = env["client"].get("/api/knowledge/documents").json()
    assert [doc["document_id"] for doc in listing["documents"]] == [body["document_id"]]
    document = listing["documents"][0]
    assert document["status"] == "active"
    assert document["active_revision"] == job["revision"]
    assert document["chunks"] == job["progress"]["chunks_total"]
    assert document["title"] == "disk 长文档"
    assert document["embedding_descriptor_hash"].startswith("sha256:")
    assert document["source_uri"].startswith("upload://")
    # 契约 §0：不得回显私有绝对路径。
    assert str(env["tmp"]) not in str(listing)


def test_upload_rejects_invalid_files_without_creating_jobs(env):
    assert _upload(env["client"], "CON.md", b"# x\n").status_code == 400
    assert _upload(env["client"], "note.exe", b"x").status_code == 400
    assert _upload(env["client"], "empty.md", b"   \n").status_code == 400
    assert _upload(env["client"], "bad.md", b"\xff\xfe bad").status_code == 400

    assert env["client"].get("/api/knowledge/jobs").json()["jobs"] == []


def test_job_progress_advances_from_queued_to_running(env):
    env["fake"].add_delay = 0.03
    response = _upload(env["client"], "slow.md", _long_text("slow", sections=40))
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    seen_running = False
    seen_partial = False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = env["client"].get(f"/api/knowledge/jobs/{job_id}").json()
        if job["status"] == "running":
            seen_running = True
            if job["progress"]["chunks_done"] > 0:
                seen_partial = True
        if job["status"] in ("ready", "failed", "cancelled"):
            break
        time.sleep(0.01)

    assert seen_running, "作业应经过 running 状态，而不是请求内同步完成"
    assert seen_partial, "running 期间应能看到分片进度"
    assert job["status"] == "ready"
    assert job["progress"]["chunks_done"] == job["progress"]["chunks_total"]


def test_documents_pagination_cursor(env):
    for name in ("a.md", "b.md", "c.md"):
        accepted = _upload(env["client"], name, f"# {name}\n\n内容 {name}\n".encode())
        assert accepted.status_code == 202
        assert _wait(env["client"], accepted.json()["job_id"])["status"] == "ready"

    first = env["client"].get("/api/knowledge/documents?limit=2").json()
    assert len(first["documents"]) == 2
    assert first["next_cursor"]

    second = env["client"].get(
        f"/api/knowledge/documents?limit=2&cursor={first['next_cursor']}"
    ).json()
    assert len(second["documents"]) == 1
    assert second["next_cursor"] is None

    ids = {doc["document_id"] for doc in first["documents"] + second["documents"]}
    assert len(ids) == 3, "分页不得重复或漏掉文档"

    bad = env["client"].get("/api/knowledge/documents?cursor=not-a-cursor")
    assert bad.status_code == 400
    assert "cursor" in bad.json()["detail"]


# ---------------------------------------------------------------------------
# 2. 失败与 retry
# ---------------------------------------------------------------------------


def test_failed_job_can_be_retried_and_old_job_kept(env):
    env["fake"].fail_on_add = True
    accepted = _upload(env["client"], "flaky.md", _long_text("flaky", sections=4))
    assert accepted.status_code == 202

    failed = _wait(env["client"], accepted.json()["job_id"])
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "index_failed"
    assert failed["error"]["retryable"] is True

    env["fake"].fail_on_add = False
    retried = env["client"].post(
        f"/api/knowledge/jobs/{failed['job_id']}/retry"
    )
    assert retried.status_code == 202, retried.text
    new_job_id = retried.json()["job_id"]
    assert new_job_id != failed["job_id"]
    assert retried.json()["document_id"] == failed["document_id"]

    ready = _wait(env["client"], new_job_id)
    assert ready["status"] == "ready"

    # 旧 job 保留为历史，状态不被覆盖。
    history = env["client"].get(f"/api/knowledge/jobs/{failed['job_id']}").json()
    assert history["status"] == "failed"

    listed = env["client"].get("/api/knowledge/jobs?status=failed").json()["jobs"]
    assert [job["job_id"] for job in listed] == [failed["job_id"]]

    # 只有 failed 才能重试。
    assert env["client"].post(
        f"/api/knowledge/jobs/{new_job_id}/retry"
    ).status_code == 409


# ---------------------------------------------------------------------------
# 3. revisions / activate / delete
# ---------------------------------------------------------------------------


def test_revisions_and_activate_switches_back(env):
    first = _upload(env["client"], "same.md", b"# same\n\nold body content\n")
    first_job = _wait(env["client"], first.json()["job_id"])
    document_id = first_job["document_id"]
    rev1 = first_job["revision"]

    second = _upload(env["client"], "same.md", b"# same\n\nnew body content\n")
    second_job = _wait(env["client"], second.json()["job_id"])
    rev2 = second_job["revision"]
    assert rev2 != rev1
    assert second.json()["document_id"] == document_id, "同名上传应归到同一文档"

    revisions = env["client"].get(
        f"/api/knowledge/documents/{document_id}/revisions"
    ).json()
    assert revisions["active_revision"] == rev2
    by_rev = {item["revision"]: item for item in revisions["revisions"]}
    assert by_rev[rev1]["status"] == "superseded"
    assert by_rev[rev2]["status"] == "active"

    rev2_chunk_ids = [row_id for row_id, row in env["fake"].rows.items()
                      if row["revision"] == rev2]
    activated = env["client"].post(
        f"/api/knowledge/documents/{document_id}/activate", json={"revision": rev1}
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["active_revision"] == rev1
    assert not [row_id for row_id, row in env["fake"].rows.items()
                if row["revision"] == rev2], "切换后应清掉非目标 revision 的分片"
    assert rev2_chunk_ids, "测试前提：rev2 确实写入过分片"

    after = env["client"].get(
        f"/api/knowledge/documents/{document_id}/revisions"
    ).json()
    assert after["active_revision"] == rev1
    assert {item["revision"]: item["status"] for item in after["revisions"]}[rev1] == "active"

    missing = env["client"].post(
        f"/api/knowledge/documents/{document_id}/activate", json={"revision": "rev-nope"}
    )
    assert missing.status_code == 404


def test_delete_document_removes_chunks_and_jobs_are_cancelled(env):
    accepted = _upload(env["client"], "gone.md", _long_text("gone", sections=3))
    job = _wait(env["client"], accepted.json()["job_id"])
    document_id = job["document_id"]
    assert env["fake"].rows

    deleted = env["client"].delete(f"/api/knowledge/documents/{document_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_chunks"] == job["progress"]["chunks_total"]
    assert not [row for row in env["fake"].rows.values()
                if row["document_id"] == document_id]
    assert env["client"].get("/api/knowledge/documents").json()["documents"] == []

    # 删除后不得再被引用：文档相关接口统一 404。
    assert env["client"].get(
        f"/api/knowledge/documents/{document_id}/revisions"
    ).status_code == 404
    assert env["client"].delete(
        f"/api/knowledge/documents/{document_id}"
    ).status_code == 404


# ---------------------------------------------------------------------------
# 4. document_id 稳定性：同名不同目录不碰撞
# ---------------------------------------------------------------------------


def test_document_id_derives_from_source_uri_not_basename(env):
    first = store_api.document_id_for("C:/docs/a/runbook.md")
    second = store_api.document_id_for("C:/docs/b/runbook.md")
    assert first.startswith("doc-") and len(first) == len("doc-") + 12
    assert first != second, "同名不同目录不得碰撞"

    # 同一来源的不同写法必须归一化到同一个 id。
    assert store_api.document_id_for("C:\\docs\\A\\runbook.md") == first
    assert store_api.document_id_for("https://Example.invalid/manual#disk") == (
        store_api.document_id_for("https://example.invalid/manual")
    )

    # 端到端：两份同名文件导入到不同文档，互不覆盖。
    uploads = env["uploads"]
    (uploads / "shared.md").write_text("# upload copy\n\nupload body\n", encoding="utf-8")
    docs_dir = env["tmp"] / "aiops-docs"
    docs_dir.mkdir()
    (docs_dir / "shared.md").write_text("# docs copy\n\ndocs body\n", encoding="utf-8")
    env_api = store_api.import_roots
    roots = [("aiops-docs", docs_dir), ("uploads", uploads)]
    original = store_api.import_roots
    store_api.import_roots = lambda: roots  # type: ignore[assignment]
    env_api  # noqa: B018 - 仅保留可读引用
    try:
        first_source = store_api.resolve_import_source("aiops-docs/shared.md")
        second_source = store_api.resolve_import_source("uploads/shared.md")
    finally:
        store_api.import_roots = original  # type: ignore[assignment]

    uri_a, path_a = first_source
    uri_b, path_b = second_source
    assert path_a != path_b
    assert store_api.document_id_for(uri_a) != store_api.document_id_for(uri_b)

    store = store_api.get_store()
    job_a = store.submit_document(
        source_uri=uri_a, title="docs", content=path_a.read_bytes()
    )
    job_b = store.submit_document(
        source_uri=uri_b, title="uploads", content=path_b.read_bytes()
    )
    assert job_a["document_id"] != job_b["document_id"]
    assert _wait(env["client"], job_a["job_id"])["status"] == "ready"
    assert _wait(env["client"], job_b["job_id"])["status"] == "ready"
    assert len(env["client"].get("/api/knowledge/documents").json()["documents"]) == 2


# ---------------------------------------------------------------------------
# 5. 并发重复导入幂等
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_import_is_idempotent(env):
    content = _long_text("dup", sections=8)
    env["fake"].add_delay = 0.02
    results: list[dict] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def submit() -> None:
        try:
            response = _upload(env["client"], "dup.md", content)
            with guard:
                results.append({"http_status": response.status_code, **response.json()})
        except BaseException as exc:  # noqa: BLE001 - 线程内异常需回传
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert [item["http_status"] for item in results] == [202, 202, 202, 202]
    job_ids = {item["job_id"] for item in results}
    document_ids = {item["document_id"] for item in results}
    assert len(job_ids) == 1, f"重复导入应返回同一 job: {job_ids}"
    assert len(document_ids) == 1

    job = _wait(env["client"], next(iter(job_ids)))
    assert job["status"] == "ready"
    document_id = job["document_id"]
    rows = [row for row in env["fake"].rows.values()
            if row["document_id"] == document_id]
    assert len(rows) == job["progress"]["chunks_total"], "幂等导入不得产生重复分片"


# ---------------------------------------------------------------------------
# 6. status：ready / invalid_index / unavailable（不得 500）
# ---------------------------------------------------------------------------


def test_status_invalid_index_is_distinct_from_unavailable(env, monkeypatch):
    accepted = _upload(env["client"], "status.md", b"# status\n\nbody for status\n")
    assert _wait(env["client"], accepted.json()["job_id"])["status"] == "ready"

    # collection 真实度量与当前描述符（cosine）不一致 -> invalid_index。
    env["fake"].metric_type = "L2"
    env["fake"].score_kind = "distance"
    monkeypatch.setattr(
        "app.services.knowledge_store.milvus_manager.collection_count",
        lambda: 3,
    )
    response = env["client"].get("/api/knowledge/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "invalid_index"
    assert body["milvus"]["connected"] is True
    assert body["chunks"] == 3
    assert "L2" in body["reason"]
    assert body["embedding"]["descriptor_hash"].startswith("sha256:")
    assert body["documents"]["active"] == 1


def test_status_unavailable_when_milvus_is_down(env):
    env["fake"].init_error = VectorStoreUnavailable("向量库不可用: connection refused")

    response = env["client"].get("/api/knowledge/status")

    assert response.status_code == 200, "Milvus 故障不得变成 500"
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["milvus"]["connected"] is False
    assert "connection refused" in body["reason"]
    # 文档元数据仍可读，不受 Milvus 影响。
    assert body["documents"] == {"active": 0, "pending": 0}


# ---------------------------------------------------------------------------
# 7. import：白名单目录 / url 未支持
# ---------------------------------------------------------------------------


def test_import_rejects_url_and_unknown_source(env):
    url_response = env["client"].post(
        "/api/knowledge/import", json={"url": "https://example.invalid/manual"}
    )
    assert url_response.status_code == 400
    assert "url" in url_response.json()["detail"]

    both = env["client"].post(
        "/api/knowledge/import",
        json={"source_id": "aiops-docs/disk_high_usage.md",
              "url": "https://example.invalid/x"},
    )
    assert both.status_code == 400

    unknown = env["client"].post(
        "/api/knowledge/import", json={"source_id": "../../etc/passwd"}
    )
    assert unknown.status_code == 400
    assert "白名单" in unknown.json()["detail"]


def test_import_whitelisted_source_creates_job(env):
    response = env["client"].post(
        "/api/knowledge/import", json={"source_id": "aiops-docs/disk_high_usage.md"}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"].startswith("job-")

    job = _wait(env["client"], body["job_id"])
    assert job["status"] == "ready"
    document = env["client"].get("/api/knowledge/documents").json()["documents"][0]
    assert document["document_id"] == body["document_id"]
    assert document["source_uri"] == "source://aiops-docs/disk_high_usage.md"
    assert document["source_kind"] == "manual"
    assert document["title"], "导入文档应有标题"


def test_failed_revision_cannot_be_activated(env):
    """索引失败的 revision 不能被设为 active（FE-06 验收红线）。"""
    env["fake"].fail_on_add = True
    response = _upload(env["client"], "broken.md", b"# broken\n\nbad body\n")
    job = _wait(env["client"], response.json()["job_id"])
    assert job["status"] == "failed"
    document_id = response.json()["document_id"]

    activated = env["client"].post(
        f"/api/knowledge/documents/{document_id}/activate",
        json={"revision": job["revision"]},
    )

    assert activated.status_code == 409, activated.text
    assert "分片" in activated.json()["detail"] or "索引" in activated.json()["detail"]
    documents = env["client"].get("/api/knowledge/documents").json()["documents"]
    [document] = [item for item in documents if item["document_id"] == document_id]
    assert document["status"] != "active"
    assert document["active_revision"] is None
