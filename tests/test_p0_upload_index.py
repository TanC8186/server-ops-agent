"""P0-04 回归：同名上传/重建失败不得先破坏旧文件与旧向量。

本文件只覆盖 M0 的“不丢数据”保证：临时写入 + 校验通过后才替换，索引在
staging 分片完成、删除旧向量成功之后才写入。完整 revision 作业
(queued/running/ready/failed、staging→activate、撤销) 属 M2，不在此声称完成。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import file as file_api
from app.services import vector_index_service as vis_module
from app.services import vector_store_manager as vsm_module
from app.services.vector_index_service import VectorIndexService


class FakeStore:
    """记录调用顺序与行数据的向量存储替身。

    ``rows`` 模拟真实集合：只有 add 成功落地的 id 才会存在，delete_by_source
    只删除同来源且不在 keep_ids 中的行，因此可以检查“孤儿行”是否残留。
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.rows: dict[str, dict] = {}
        self.fail_on_delete = False
        self.fail_on_add = False
        #: None 表示整批成功；整数表示最多插入 N 行后抛错（模拟批量中途失败）。
        self.fail_after_n_inserts: int | None = None
        self.add_delay = 0.0
        self.last_add_ids: list[str] = []
        self._active_adds = 0
        self.max_concurrent_adds = 0
        self._active_guard = threading.Lock()

    def delete_by_source(self, path: str, *, keep_ids=None) -> int:
        keep = set(keep_ids or ())
        self.calls.append(("delete", path, tuple(keep)))
        if self.fail_on_delete:
            raise RuntimeError("milvus delete failed")
        removed = [
            row_id
            for row_id, row in self.rows.items()
            if row["source"] == path and row_id not in keep
        ]
        for row_id in removed:
            self.rows.pop(row_id, None)
        return len(removed)

    def delete_by_ids(self, ids) -> int:
        ids = list(ids)
        self.calls.append(("delete_by_ids", tuple(ids)))
        removed = 0
        for row_id in ids:
            if self.rows.pop(row_id, None) is not None:
                removed += 1
        return removed

    def add_documents(self, docs, *, ids=None) -> list[str]:
        docs = list(docs)
        doc_ids = list(ids) if ids is not None else [f"id-{i}" for i in range(len(docs))]
        self.last_add_ids = doc_ids
        self.calls.append(("add", len(docs)))
        with self._active_guard:
            self._active_adds += 1
            self.max_concurrent_adds = max(self.max_concurrent_adds, self._active_adds)
        try:
            if self.add_delay:
                time.sleep(self.add_delay)
            if self.fail_on_add:
                raise RuntimeError("milvus insert failed")
            limit = (
                len(docs)
                if self.fail_after_n_inserts is None
                else min(len(docs), self.fail_after_n_inserts)
            )
            for doc, row_id in list(zip(docs, doc_ids))[:limit]:
                source = (doc.metadata or {}).get("_source", "")
                self.rows[row_id] = {"source": source, "content": doc.page_content}
            if self.fail_after_n_inserts is not None:
                raise RuntimeError("milvus insert failed mid-batch")
        finally:
            with self._active_guard:
                self._active_adds -= 1
        return doc_ids

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(file_api, "UPLOAD_DIR", uploads)

    store = FakeStore()
    service = VectorIndexService()
    service.upload_path = str(uploads)
    monkeypatch.setattr(vis_module, "vector_store_manager", store)
    monkeypatch.setattr(file_api, "vector_index_service", service)

    test_app = FastAPI()
    test_app.include_router(file_api.router)
    with TestClient(test_app) as client:
        yield {
            "client": client,
            "app": test_app,
            "uploads": uploads,
            "store": store,
            "service": service,
        }


def _upload(client, name: str, content: bytes):
    return client.post(
        "/upload",
        files={"file": (name, content, "text/markdown")},
    )


def _seed(uploads: Path, name: str = "runbook.md") -> Path:
    path = uploads / name
    path.write_text("# 既有 runbook\n\n磁盘满时先检查 inode。\n", encoding="utf-8")
    return path


def _long_text(tag: str = "runbook") -> bytes:
    """生成可切成多个分片的长文本（chunk_size=180，min_size=80）。"""
    sections = [
        f"## {tag} 第{index}节\n\n" + f"磁盘满时先检查 inode（{tag}）。" * 8
        for index in range(6)
    ]
    return (f"# {tag} 长文档\n\n" + "\n\n".join(sections)).encode("utf-8")


def _seed_long(uploads: Path, name: str = "runbook.md") -> Path:
    """写入足够长、可切成多个分片的内容，用于模拟批量插入中途失败。"""
    path = uploads / name
    path.write_bytes(_long_text())
    return path


def test_oversized_upload_keeps_previous_file(env):
    old = _seed(env["uploads"])
    before = old.read_bytes()

    response = _upload(env["client"], "runbook.md", b"x" * (file_api.MAX_FILE_SIZE + 1))

    assert response.status_code == 400
    assert old.exists()
    assert old.read_bytes() == before
    assert env["store"].calls == []


def test_non_utf8_upload_keeps_previous_file(env):
    old = _seed(env["uploads"])
    before = old.read_bytes()

    response = _upload(env["client"], "runbook.md", b"\xff\xfe\x00bad bytes")

    assert response.status_code == 400
    assert old.read_bytes() == before


def test_empty_upload_is_rejected_without_damage(env):
    old = _seed(env["uploads"])
    before = old.read_bytes()

    response = _upload(env["client"], "runbook.md", b"   \n\n")

    assert response.status_code == 400
    assert old.read_bytes() == before


def test_failed_index_restores_previous_file(env):
    old = _seed(env["uploads"])
    before = old.read_bytes()
    env["store"].fail_on_add = True

    response = _upload(env["client"], "runbook.md", b"# new\n\nnew content\n")

    assert response.status_code == 202
    body = response.json()
    assert body["data"]["indexed"] is False
    assert body["data"]["index_error"]
    assert old.read_bytes() == before, "索引失败不得替换已生效的文件"


def test_successful_upload_replaces_file_once(env):
    old = _seed(env["uploads"])

    response = _upload(env["client"], "runbook.md", b"# new\n\nnew content\n")

    assert response.status_code == 200
    assert response.json()["data"]["indexed"] is True
    assert old.read_text(encoding="utf-8").strip() == "# new\n\nnew content"
    # 先插新分片、再删旧分片：旧数据在新数据落盘前始终可检索。
    assert env["store"].names() == ["add", "delete"]
    assert len(list(env["uploads"].glob("runbook*"))) == 1


def test_no_temp_files_left_after_failure(env):
    _seed(env["uploads"])
    env["store"].fail_on_add = True

    _upload(env["client"], "runbook.md", b"# new\n")

    leftovers = [p.name for p in env["uploads"].iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_index_single_file_does_not_delete_before_split(env, monkeypatch):
    path = _seed(env["uploads"])

    def boom(*args, **kwargs):
        raise RuntimeError("split failed")

    monkeypatch.setattr(
        vis_module.document_splitter_service, "split_document", boom
    )

    with pytest.raises(RuntimeError):
        env["service"].index_single_file(str(path))

    assert env["store"].calls == [], "分割失败时不得删旧向量"
    assert path.exists()


def test_index_single_file_deletes_only_after_insert(env):
    path = _seed(env["uploads"])

    env["service"].index_single_file(str(path))

    assert env["store"].names() == ["add", "delete"]


def test_insert_failure_never_touches_old_vectors(env):
    path = _seed(env["uploads"])
    env["store"].fail_on_add = True

    with pytest.raises(RuntimeError):
        env["service"].index_single_file(str(path))

    state = env["service"].last_index_state
    assert state is not None
    assert state["ok"] is False
    assert state["inserted_ids"] == []
    assert "delete" not in env["store"].names(), "新分片写入失败时旧向量必须保持原样"


def test_cleanup_failure_rolls_back_new_chunks(env):
    path = _seed(env["uploads"])
    env["store"].fail_on_delete = True

    with pytest.raises(RuntimeError):
        env["service"].index_single_file(str(path))

    names = env["store"].names()
    assert names == ["add", "delete", "delete_by_ids"], names
    state = env["service"].last_index_state
    assert state is not None
    assert state["rolled_back"] is True
    assert state["duplicates_possible"] is True


def test_directory_index_reports_per_file_failure(env, monkeypatch):
    good = _seed(env["uploads"], "good.md")
    bad = env["uploads"] / "bad.md"
    bad.write_bytes(b"\xff\xfe broken")

    result = env["service"].index_directory(str(env["uploads"]))

    assert result.total_files == 2
    assert result.success_count == 1
    assert result.fail_count == 1
    assert any("bad.md" in name for name in result.failed_files)
    assert good.exists()


# ---------------------------------------------------------------------------
# 1. 批量插入中途失败：不得留下孤儿行，旧行保持原样
# ---------------------------------------------------------------------------


def test_partial_insert_rolls_back_exact_ids_without_orphans(env):
    path = _seed_long(env["uploads"])
    env["service"].index_single_file(str(path))
    old_rows = dict(env["store"].rows)
    assert old_rows, "第一次索引应写入若干旧行"

    env["store"].calls.clear()
    env["store"].fail_after_n_inserts = 2  # 只写入 2 行就中断，其余分片丢失

    with pytest.raises(RuntimeError):
        env["service"].index_single_file(str(path))

    assert env["store"].rows == old_rows, "旧行必须原样保留，且不得残留半批孤儿行"
    state = env["service"].last_index_state
    assert state is not None
    assert state["rolled_back"] is True
    assert state["duplicates_possible"] is False, "按预生成 id 精确清理后不存在重复风险"
    assert "delete" not in env["store"].names(), "插入失败时不得走到删旧逻辑"

    rollback_calls = [call for call in env["store"].calls if call[0] == "delete_by_ids"]
    assert len(rollback_calls) == 1
    assert set(rollback_calls[0][1]) == set(env["store"].last_add_ids)


# ---------------------------------------------------------------------------
# 2. 并发同名上传 / 同来源索引必须串行化
# ---------------------------------------------------------------------------


async def test_concurrent_same_name_uploads_end_in_one_consistent_state(env):
    store = env["store"]
    store.add_delay = 0.15
    first = _long_text("alpha")
    second = _long_text("beta")

    transport = httpx.ASGITransport(app=env["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async def post(content: bytes):
            return await client.post(
                "/upload", files={"file": ("runbook.md", content, "text/markdown")}
            )

        responses = await asyncio.gather(post(first), post(second))

    assert [r.status_code for r in responses] == [200, 200]
    assert all(r.json()["data"]["indexed"] is True for r in responses)

    files = [p for p in env["uploads"].iterdir() if p.name.startswith("runbook")]
    assert len(files) == 1, "同名并发上传最终只能有一个文件"
    final = files[0].read_text(encoding="utf-8")

    marker = "alpha" if "alpha" in final else "beta"
    other = "beta" if marker == "alpha" else "alpha"
    assert store.rows, "最终应保留一套索引行"
    assert all(marker in row["content"] for row in store.rows.values())
    assert all(other not in row["content"] for row in store.rows.values()), (
        "行数据必须与最终文件属于同一版本"
    )
    assert {row["source"] for row in store.rows.values()} == {
        files[0].resolve().as_posix()
    }
    assert store.max_concurrent_adds == 1, "同一来源的索引不得并发写入"


def test_directory_index_serializes_same_source(env):
    path = _seed_long(env["uploads"])
    store = env["store"]
    store.add_delay = 0.15
    errors: list[BaseException] = []

    def worker():
        try:
            env["service"].index_single_file(str(path))
        except BaseException as exc:  # noqa: BLE001 - 线程内异常需回传
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert store.max_concurrent_adds == 1, "同一 source 的索引必须串行"


# ---------------------------------------------------------------------------
# 3. Milvus 表达式转义
# ---------------------------------------------------------------------------


def test_delete_by_source_escapes_quote_and_backslash(env, monkeypatch):
    class RecordingCollection:
        def __init__(self):
            self.expressions: list[str] = []

        def delete(self, expr):
            self.expressions.append(expr)

            class Result:
                delete_count = 0

            return Result()

    collection = RecordingCollection()
    manager = vsm_module.VectorStoreManager()
    manager._initialized = True
    manager.vector_store = object()  # 非 None 即可跳过懒加载
    monkeypatch.setattr(
        vsm_module.milvus_manager, "get_collection", lambda: collection
    )

    hostile = 'C:\\docs\\a"b.md'
    deleted = manager.delete_by_source(hostile, keep_ids=["id-1"])

    assert deleted == 0
    assert len(collection.expressions) == 1, "必须仍然尝试删除"
    expr = collection.expressions[0]
    assert expr == (
        'metadata["_source"] == "C:\\\\docs\\\\a\\"b.md" and id not in ["id-1"]'
    ), expr


# ---------------------------------------------------------------------------
# 4. 文件名加固：UNIX/Windows 设备名、结尾点空格、空格冲突、目录包含
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["CON.txt", "NUL.md", "COM1.md", "lpt9.txt", "aux.MD"])
def test_reserved_device_names_are_rejected(env, name):
    response = _upload(env["client"], name, b"# device\n")

    assert response.status_code == 400
    assert "保留" in response.json()["detail"]
    assert list(env["uploads"].iterdir()) == []


def test_similar_but_non_reserved_names_are_accepted(env):
    for name in ("console.md", "auxiliary.md"):
        response = _upload(env["client"], name, b"# ok\n")
        assert response.status_code == 200, name


def test_trailing_dots_and_spaces_are_trimmed(env):
    response = _upload(env["client"], "runbook.md. ", b"# new\n")

    assert response.status_code == 200
    assert response.json()["data"]["filename"] == "runbook.md"
    assert (env["uploads"] / "runbook.md").exists()


def test_space_and_underscore_filenames_collide_by_design(env):
    """已知限制：空格会被替换为下划线，因此 a b.md 与 a_b.md 指向同一文件。"""
    first = _upload(env["client"], "a b.md", b"# one\n")
    second = _upload(env["client"], "a_b.md", b"# two\n")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["filename"] == "a_b.md"
    assert second.json()["data"]["filename"] == "a_b.md"
    assert [p.name for p in env["uploads"].iterdir()] == ["a_b.md"]
    assert (env["uploads"] / "a_b.md").read_text(encoding="utf-8") == "# two\n"


def test_upload_target_stays_inside_upload_dir(env):
    response = _upload(env["client"], "../evil.md", b"# evil\n")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["filename"] == "_evil.md"
    assert (env["uploads"] / "_evil.md").exists()
    assert not (env["uploads"].parent / "evil.md").exists()


def test_resolve_upload_target_rejects_escaping_path(env):
    with pytest.raises(file_api.HTTPException):
        file_api._resolve_upload_target("../evil.md")


# ---------------------------------------------------------------------------
# 5. staging 残留清理（有界、只清过期）
# ---------------------------------------------------------------------------


def test_upload_cleans_stale_staging_but_keeps_in_flight_file(env):
    uploads = env["uploads"]
    stale = uploads / ".staging-stale.tmp"
    fresh = uploads / ".staging-fresh.tmp"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    old_time = time.time() - 7200
    os.utime(stale, (old_time, old_time))

    response = _upload(env["client"], "runbook.md", b"# new\n")

    assert response.status_code == 200
    assert not stale.exists(), "超过 1 小时的 staging 残留应被清理"
    assert fresh.exists(), "仍在使用中的 staging 文件不得删除"


def test_stale_staging_cleanup_is_bounded(env):
    uploads = env["uploads"]
    old_time = time.time() - 7200
    for index in range(3):
        path = uploads / f".staging-{index}.tmp"
        path.write_bytes(b"old")
        os.utime(path, (old_time, old_time))

    removed = file_api._cleanup_stale_staging(limit=2)

    assert removed == 2
    assert len(list(uploads.glob(".staging-*.tmp"))) == 1

