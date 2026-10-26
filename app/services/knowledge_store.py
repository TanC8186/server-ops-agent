"""知识库元数据 / 作业存储（M2）。

设计边界（与 ``docs/claude-code-handoff/M2_API_CONTRACT.md`` §2 对齐）：

- **只用 JSON 文件**：默认 ``data/knowledge/store.json``，可用环境变量
  ``KNOWLEDGE_STORE_FILE`` 指到临时目录（测试）或其他部署路径。写入走
  “同目录临时文件 + ``os.replace``”，进程内用 ``threading.RLock`` 串行化，
  不引入数据库依赖。
- **作业状态机**：``queued → running → ready | failed | cancelled``。进程重启
  后遗留的 ``running`` 作业会被标记为 ``failed``（``error.code=stale``），
  历史记录保留，不会被悄悄删除。
- **有界后台 worker**：固定 ``KNOWLEDGE_INDEX_WORKERS``（默认 2）线程的线程池，
  请求只做校验 + 落内容 + 建作业，不在请求内做切分/embedding/写入。
  同一 ``document_id`` 的索引/激活/删除按文档级锁串行（``document_lock``）。
- **稳定 ID**：``document_id = doc-<sha256(规范化 source_uri)[:12]>``，与
  basename 无关，同名不同目录不会碰撞；``revision = rev-<sha256(内容)[:16]>``。

复用而非复制：

- 有界读取与 UTF-8/空内容校验用 ``vector_index_service.read_text``（导入路径）
  与 ``app/api/file.py`` 的 ``_read_bounded/_validate_text/_sanitize_filename/
  _is_reserved_device_name``（上传路径，由路由层调用）。
- 切分复用 ``document_splitter_service.split_document/split_text``，向量读写复用
  ``vector_store_manager.add_documents/delete_by_ids/delete_by_source/
  delete_by_document``。

为什么不直接调用 ``vector_index_service.index_single_file``：该函数不接受
``document_id`` / ``revision``，切分时会按 basename 派生 ``_document_id``，
与本模块“按 source_uri 派生 ID”的要求冲突，写入后 ``delete_by_document`` 会
找不到行。这里保留它同样的写入顺序（先插入新分片、再清理同来源旧分片、失败
按预生成 id 回滚），但显式传入 ``document_id``/``revision``。

检索可见性（A01）：

- ``active_revisions()`` 是检索侧唯一可信的“当前可检索版本”来源。检索适配器
  （``knowledge_service.VectorStoreRetriever``）按它过滤候选：``staged``、
  ``failed``、``superseded`` 的 revision 分片不会进入候选、rerank、模型上下文
  或引用。旧向量仍保留，用于回滚（``activate_revision``），但不再作为当前知识。
- 保留旧向量与“失败批次的残片清理”是两件事（A02）：新 revision 的写入失败必须
  按**全部预生成候选 ID** 幂等回滚，清理失败要如实记录并可在 retry 时重做。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.api import file as file_api
from app.services.document_splitter_service import document_splitter_service
from app.services.vector_embedding_service import (
    DescriptorMismatch,
    current_descriptor,
    read_descriptor,
)
from app.services.vector_index_service import vector_index_service
from app.services.vector_store_manager import (
    VectorStoreInvalidIndex,
    VectorStoreUnavailable,
    descriptor_path,
    metric_of,
    vector_store_manager,
)

#: 项目根目录（本文件在 app/services 下）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 默认存储文件：项目根的 data/knowledge/store.json。
DEFAULT_STORE_FILE = PROJECT_ROOT / "data" / "knowledge" / "store.json"

#: 环境变量覆盖（测试指向 tmp 目录；部署时可指到持久卷）。
STORE_FILE_ENV = "KNOWLEDGE_STORE_FILE"

#: 后台索引 worker 线程数（有界；请求线程不参与索引）。
INDEX_WORKERS = 2

#: 每批写入的分片数，用于推进 chunks_done 进度（不是性能调优参数）。
INDEX_BATCH_SIZE = 16

#: 作业状态机取值。
JOB_STATUSES = ("queued", "running", "ready", "failed", "cancelled")
_ACTIVE_JOB_STATUSES = ("queued", "running")

#: 列表分页上限。
MAX_PAGE_LIMIT = 100


# ── 异常：路由层据此映射 HTTP 状态码 ─────────────────────────


class KnowledgeStoreError(RuntimeError):
    """存储层通用错误。"""


class StoreUnavailable(KnowledgeStoreError):
    """向量库暂时不可用（连接失败/初始化失败）。"""


class NotFound(KnowledgeStoreError):
    """文档 / 作业 / revision 不存在。"""


class Conflict(KnowledgeStoreError):
    """状态冲突（例如重试一个非 failed 作业）。"""


class InvalidCursor(ValueError):
    """分页 cursor 非法（不是本服务生成的）。"""


# ── 小工具 ───────────────────────────────────────────────────


def store_path() -> Path:
    """存储文件路径：``KNOWLEDGE_STORE_FILE`` 优先，其次 ``config``，最后默认值。"""
    override = os.environ.get(STORE_FILE_ENV, "").strip()
    if not override:
        override = str(getattr(config, "knowledge_store_file", "") or "").strip()
    return Path(override) if override else DEFAULT_STORE_FILE


def _now() -> str:
    """ISO-8601 带时区的当前时间（契约 §0）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_source_uri(source_uri: str) -> str:
    """规范化 source_uri 的**展示形式**：统一正斜杠、去掉末尾斜杠与 URL fragment。

    ``document_id`` 的稳定性由 :func:`_identity_key` 负责（文件系统来源大小写
    不敏感）；这里只保证同一个来源只有一种写法可以落盘。
    """
    value = (source_uri or "").strip().replace("\\", "/")
    if not value:
        raise ValueError("source_uri 不能为空")
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        # URL：scheme/host 小写，去掉 fragment（同一文档的锚点不是新文档）。
        scheme, rest = value.split("://", 1)
        rest = rest.split("#", 1)[0]
        host, sep, path = rest.partition("/")
        value = f"{scheme.lower()}://{host.lower()}{sep}{path}"
    return value.rstrip("/") or value


def _identity_key(source_uri: str) -> str:
    """派生 document_id 用的身份键。

    - ``http(s)://``：只有 scheme/host 大小写不敏感（URL 路径本身区分大小写）；
    - 文件系统路径与 ``upload://`` / ``source://``：按 Windows 语义整体大小写
      归一，``C:\\docs\\A.md`` 与 ``c:/docs/a.md`` 是同一份文档。
    """
    normalized = normalize_source_uri(source_uri)
    if normalized.lower().startswith(("http://", "https://")):
        return normalized
    return normalized.casefold()


def document_id_for(source_uri: str) -> str:
    """由规范化 source_uri 派生 ``doc-<12 hex>``（不是 basename）。"""
    digest = hashlib.sha256(_identity_key(source_uri).encode("utf-8")).hexdigest()[:12]
    return f"doc-{digest}"


def revision_for(content: bytes) -> str:
    """内容 sha256 前 16 位，前缀 ``rev-``（契约样例风格）。"""
    return f"rev-{hashlib.sha256(content).hexdigest()[:16]}"


#: 文档级锁表（复用 vector_index_service.source_lock 的思路，但 key 是 document_id）。
_DOCUMENT_LOCKS: dict[str, threading.Lock] = {}
_DOCUMENT_LOCKS_GUARD = threading.Lock()

#: 锁表容量上限，避免长期运行的字典无限增长。
MAX_DOCUMENT_LOCKS = 512


def document_lock(document_id: str) -> threading.Lock:
    """返回某个 document_id 对应的进程内互斥锁（同一文档串行写入）。"""
    with _DOCUMENT_LOCKS_GUARD:
        lock = _DOCUMENT_LOCKS.get(document_id)
        if lock is None:
            lock = threading.Lock()
            _DOCUMENT_LOCKS[document_id] = lock
        if len(_DOCUMENT_LOCKS) > MAX_DOCUMENT_LOCKS:
            for key, candidate in list(_DOCUMENT_LOCKS.items()):
                if len(_DOCUMENT_LOCKS) <= MAX_DOCUMENT_LOCKS:
                    break
                if key != document_id and not candidate.locked():
                    _DOCUMENT_LOCKS.pop(key, None)
        return lock


# ── 主存储类 ─────────────────────────────────────────────────


class KnowledgeStore:
    """JSON 持久化的文档 / revision / job 存储 + 后台索引 worker。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else store_path()
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._data = self._load()
        self._recover_stale_jobs()
        _register_current_store(self)

    # ── 持久化 ────────────────────────────────────────────────

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "documents": {}, "jobs": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 损坏的旧文件保留副本（不删除、不静默丢历史），从空结构继续。
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            try:
                os.replace(self.path, backup)
                logger.error(f"知识库存储损坏，已备份到 {backup.name}: {exc}")
            except OSError as move_error:
                logger.error(f"知识库存储损坏且无法备份: {exc}; {move_error}")
            return self._empty()
        if not isinstance(payload, dict):
            return self._empty()
        data = self._empty()
        if isinstance(payload.get("documents"), dict):
            data["documents"] = payload["documents"]
        if isinstance(payload.get("jobs"), dict):
            data["jobs"] = payload["jobs"]
        return data

    def _save_locked(self) -> None:
        """临时文件 + ``os.replace`` 原子替换（调用方必须持有 ``self._lock``）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=".store-",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(temp_path, self.path)

    def _recover_stale_jobs(self) -> None:
        """进程重启：把遗留 running 作业标记为 failed（stale），保留历史。

        ``queued`` 作业没有遗留的 worker，保持 queued 等调用方 retry；不在这里
        自动重跑，避免导入时产生隐写的索引副作用。
        """
        recovered = 0
        with self._lock:
            for job in self._data["jobs"].values():
                if job.get("status") == "running":
                    job["status"] = "failed"
                    job["finished_at"] = _now()
                    job["error"] = {
                        "code": "stale",
                        "message": "服务重启，作业未完成，已标记为失败",
                        "retryable": True,
                    }
                    recovered += 1
            if recovered:
                self._save_locked()
        if recovered:
            logger.warning(f"已将 {recovered} 个中断的索引作业标记为 failed(stale)")

    # ── 内容落盘 ──────────────────────────────────────────────

    def content_dir(self) -> Path:
        return self.path.parent / "content"

    def _content_path(self, document_id: str, revision: str) -> Path:
        return self.content_dir() / document_id / f"{revision}.md"

    def _write_content(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=str(path.parent), prefix=".content-", suffix=".tmp", delete=False
        )
        temp_path = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(temp_path, path)

    # ── 作业提交 ──────────────────────────────────────────────

    def submit_document(
        self,
        *,
        source_uri: str,
        title: str,
        content: bytes,
        source_kind: str = "manual",
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """接收一份内容，创建（或复用）索引作业并交给后台 worker。

        幂等规则：同一 ``document_id`` + 同一 ``revision`` 已存在
        ``queued/running/ready`` 作业时直接返回它，不重复写入。
        """
        document_id = document_id_for(source_uri)
        revision = revision_for(content)
        meta = metadata or {}
        job: dict[str, Any] | None = None
        duplicate = False

        with self._lock:
            existing = self._find_job_locked(document_id, revision)
            if existing is not None:
                return self._job_response(existing, duplicate=True)

            content_path = self._content_path(document_id, revision)
            written = True
            if not content_path.exists():
                written = False

            doc = self._data["documents"].get(document_id)
            if doc is None:
                doc = {
                    "document_id": document_id,
                    "title": title or Path(source_uri).name,
                    "source_uri": normalize_source_uri(source_uri),
                    "source_kind": source_kind,
                    "active_revision": None,
                    "chunks": 0,
                    "updated_at": _now(),
                    "embedding_descriptor_hash": "",
                    "os_family": meta.get("os_family", ""),
                    "product": meta.get("product", ""),
                    "product_version": meta.get("product_version", ""),
                    "license_id": meta.get("license_id", ""),
                    "revisions": {},
                }
                self._data["documents"][document_id] = doc
            revision_record = doc["revisions"].get(revision)
            if revision_record is None:
                revision_record = {
                    "revision": revision,
                    "created_at": _now(),
                    "status": "staged",
                    "chunks": 0,
                    "content_path": str(content_path),
                    "chunk_ids": [],
                    "embedding_descriptor_hash": "",
                }
                doc["revisions"][revision] = revision_record

            # 先落内容再建作业：worker 只读已完成的文件，崩溃也不会读到半份。
            if not written:
                self._write_content(content_path, content)

            job = {
                "job_id": f"job-{uuid.uuid4().hex[:12]}",
                "document_id": document_id,
                "revision": revision,
                "status": "queued",
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "chunks_total": 0,
                "chunks_done": 0,
                "content_path": str(content_path),
                "source_uri": doc["source_uri"],
                "error": None,
                "attempts": 1,
            }
            self._data["jobs"][job["job_id"]] = job
            self._save_locked()

        # 先取 202 的快照再交给 worker：契约要求 status=queued，而 worker 可能
        # 在响应序列化前就已开始执行。
        accepted = self._job_response(job, duplicate=duplicate)
        self._submit(job["job_id"])
        return accepted

    def _find_job_locked(self, document_id: str, revision: str) -> dict[str, Any] | None:
        for job in self._data["jobs"].values():
            if (
                job.get("document_id") == document_id
                and job.get("revision") == revision
                and job.get("status") in ("queued", "running", "ready")
            ):
                return job
        return None

    def _submit(self, job_id: str) -> None:
        try:
            self._get_executor().submit(self._run_job, job_id)
        except Exception as exc:  # noqa: BLE001 - 线程池被关闭等
            logger.error(f"提交索引作业失败: {job_id}: {exc}")
            with self._lock:
                job = self._data["jobs"].get(job_id)
                if job is not None and job.get("status") == "queued":
                    job["status"] = "failed"
                    job["finished_at"] = _now()
                    job["error"] = {
                        "code": "worker_unavailable",
                        "message": f"后台 worker 不可用: {exc}",
                        "retryable": True,
                    }
                    self._save_locked()

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            with self._lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=INDEX_WORKERS, thread_name_prefix="knowledge-index"
                    )
        return self._executor

    def shutdown(self) -> None:
        """关闭后台线程池（测试/进程退出用；不影响已有持久化数据）。"""
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False)

    # ── worker 主体 ───────────────────────────────────────────

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._data["jobs"].get(job_id)
            if job is None or job.get("status") != "queued":
                return
            job["status"] = "running"
            job["started_at"] = _now()
            document_id = job["document_id"]
            revision = job["revision"]
            content_path = job["content_path"]
            source_uri = job["source_uri"]
            self._save_locked()

        lock = document_lock(document_id)
        #: 本次作业**全部预生成**的候选 ID（不是“已成功返回的 inserted”）。
        #: 批量写入可能写进一部分后才抛错，成功返回的列表拿不到这些残片；
        #: 回滚必须覆盖整个候选集合，否则失败批次的第一行会留在向量库里被检索到
        #: （A02 反例：``FAILED PARTIAL instructions``）。
        chunk_ids: list[str] = []
        #: 上一次尝试（retry 前）可能残留的分片；成功后清空。
        try:
            with lock:
                if not self._document_revision_exists(document_id, revision):
                    raise _JobCancelled("文档或 revision 在排队期间被删除")
                chunks = self._split(document_id, revision, content_path, source_uri)
                total = len(chunks)
                with self._lock:
                    job = self._data["jobs"].get(job_id)
                    if job is None or job.get("status") == "cancelled":
                        raise _JobCancelled("作业已取消")
                    job["chunks_total"] = total
                    job["pending_cleanup"] = []
                    self._save_locked()

                chunk_ids = [str(doc.metadata.get("_chunk_id") or "") for doc in chunks]
                # 重试时可能留下上次半批写入的行：先按稳定 id 清一遍（只影响本 revision）。
                cleaned, cleanup_error = _safe_delete_ids(chunk_ids)
                if not cleaned:
                    # 旧残片清不掉就继续写入，等于把“未知旧数据 + 新数据”一起当索引；
                    # 明确失败并记录待重做，避免用户以为新版本已就绪。
                    raise _CleanupFailed(cleanup_error or "清理旧分片失败")

                for start in range(0, len(chunks), INDEX_BATCH_SIZE):
                    batch = chunks[start:start + INDEX_BATCH_SIZE]
                    ids = chunk_ids[start:start + INDEX_BATCH_SIZE]
                    returned = vector_store_manager.add_documents(batch, ids=ids) or ids
                    with self._lock:
                        job = self._data["jobs"].get(job_id)
                        if job is None or job.get("status") == "cancelled":
                            raise _JobCancelled("作业已取消")
                        job["chunks_done"] = min(total, start + len(batch))
                        self._save_locked()

                self._mark_ready(job_id, document_id, revision, chunk_ids)
        except _JobCancelled as exc:
            self._rollback(chunk_ids, job_id)
            self._finish_failed(job_id, "cancelled", str(exc), retryable=True)
        except _CleanupFailed as exc:
            # 清理失败不是“索引失败”：旧残片可能仍在库里。记录可重试的清理待办。
            self._rollback(chunk_ids, job_id)
            self._finish_failed(job_id, "cleanup_failed", str(exc)[:500], retryable=True)
        except Exception as exc:  # noqa: BLE001 - 作业必须如实落到 failed
            self._rollback(chunk_ids, job_id)
            logger.error(f"索引作业失败: {job_id}: {exc}")
            self._finish_failed(job_id, "index_failed", str(exc)[:500], retryable=True)

    def _rollback(self, chunk_ids: list[str], job_id: str) -> None:
        """按全部候选 ID 回滚；清理失败写进作业，供 retry 重做（A02）。"""
        cleaned, error = _safe_delete_ids(chunk_ids)
        if cleaned:
            return
        message = f"索引失败后清理分片失败（将在重试时重做）: {error}"
        logger.error(f"{message} job={job_id}")
        with self._lock:
            job = self._data["jobs"].get(job_id)
            if job is not None:
                job.setdefault("pending_cleanup", list(chunk_ids))
                self._save_locked()

    def _document_revision_exists(self, document_id: str, revision: str) -> bool:
        with self._lock:
            doc = self._data["documents"].get(document_id)
            return bool(doc and revision in doc.get("revisions", {}))

    def _split(self, document_id: str, revision: str, content_path: str,
               source_uri: str):
        """读取 + 切分；显式传入 document_id/revision，避免 basename 派生碰撞。"""
        content = vector_index_service.read_text(content_path)
        documents = document_splitter_service.split_document(
            content, source_uri, document_id=document_id, revision=revision
        )
        if not documents:
            documents = document_splitter_service.split_text(
                content, source_uri, document_id=document_id, revision=revision
            )
        if not documents:
            raise ValueError("文件无法切分为任何分片")
        for index, document in enumerate(documents):
            # 稳定 id：同一 revision 重试/重复导入必须得到同一批 id（幂等）。
            document.metadata["_chunk_id"] = f"{document_id}:{revision}:{index + 1}"
            document.metadata["_document_id"] = document_id
            document.metadata["_revision"] = revision
            document.metadata["_source"] = source_uri
        return documents

    def _mark_ready(self, job_id: str, document_id: str, revision: str,
                    chunk_ids: list[str]) -> None:
        descriptor = current_descriptor()
        with self._lock:
            job = self._data["jobs"].get(job_id)
            doc = self._data["documents"].get(document_id)
            if job is None or doc is None:
                return
            record = doc["revisions"].get(revision)
            if record is None:
                return
            previous = doc.get("active_revision")
            if previous and previous != revision:
                old_record = doc["revisions"].get(previous)
                if old_record is not None and old_record.get("status") == "active":
                    old_record["status"] = "superseded"
            record["status"] = "active"
            record["chunks"] = len(chunk_ids)
            record["chunk_ids"] = list(chunk_ids)
            record["embedding_descriptor_hash"] = f"sha256:{descriptor.hash}"
            doc["active_revision"] = revision
            doc["chunks"] = len(chunk_ids)
            doc["updated_at"] = _now()
            doc["embedding_descriptor_hash"] = f"sha256:{descriptor.hash}"
            job["status"] = "ready"
            job["finished_at"] = _now()
            job["chunks_done"] = len(chunk_ids)
            job["error"] = None
            self._save_locked()

    def _finish_failed(self, job_id: str, code: str, message: str, *,
                       retryable: bool) -> None:
        with self._lock:
            job = self._data["jobs"].get(job_id)
            if job is None:
                return
            if job.get("status") == "cancelled":
                return
            job["status"] = "failed"
            job["finished_at"] = _now()
            job["error"] = {
                "code": code,
                "message": message,
                "retryable": retryable,
            }
            self._save_locked()

    # ── 作业查询 / 重试 ───────────────────────────────────────

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._data["jobs"].get(job_id)
            if job is None:
                raise NotFound(f"作业不存在: {job_id}")
            return self._job_response(job)

    def list_jobs(self, *, status: str | None = None, limit: int = 20) -> list[dict]:
        if status and status not in JOB_STATUSES:
            raise ValueError(f"未知作业状态: {status}")
        with self._lock:
            jobs = list(self._data["jobs"].values())
        jobs.sort(key=lambda item: (item.get("created_at") or "", item.get("job_id") or ""),
                  reverse=True)
        if status:
            jobs = [job for job in jobs if job.get("status") == status]
        return [self._job_response(job) for job in jobs[:limit]]

    def retry_job(self, job_id: str) -> dict[str, Any]:
        """重试失败作业：创建新 job，旧 job 原样保留为历史。"""
        with self._lock:
            old = self._data["jobs"].get(job_id)
            if old is None:
                raise NotFound(f"作业不存在: {job_id}")
            if old.get("status") != "failed":
                raise Conflict(f"只有 failed 作业可以重试，当前状态: {old.get('status')}")
            document_id = old["document_id"]
            revision = old["revision"]
            doc = self._data["documents"].get(document_id)
            if doc is None or revision not in doc.get("revisions", {}):
                raise NotFound(f"文档或 revision 已不存在: {document_id}")
            existing = self._find_job_locked(document_id, revision)
            if existing is not None and existing.get("status") == "ready":
                raise Conflict("该 revision 已索引成功，无需重试")
            content_path = old.get("content_path") or ""
            if not content_path or not Path(content_path).is_file():
                raise Conflict("原始内容文件已不存在，无法重试；请重新上传")
            job = {
                "job_id": f"job-{uuid.uuid4().hex[:12]}",
                "document_id": document_id,
                "revision": revision,
                "status": "queued",
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "chunks_total": 0,
                "chunks_done": 0,
                "content_path": content_path,
                "source_uri": old.get("source_uri") or doc.get("source_uri", ""),
                "error": None,
                "attempts": int(old.get("attempts") or 1) + 1,
            }
            self._data["jobs"][job["job_id"]] = job
            self._save_locked()
        self._submit(job["job_id"])
        return self._job_response(job)

    def _job_response(self, job: dict[str, Any], *, duplicate: bool = False) -> dict:
        """对外形状（契约 2.3）；不暴露 content_path 等内部字段。"""
        response = {
            "job_id": job.get("job_id", ""),
            "document_id": job.get("document_id", ""),
            "status": job.get("status", "queued"),
            "progress": {
                "chunks_total": int(job.get("chunks_total") or 0),
                "chunks_done": int(job.get("chunks_done") or 0),
            },
            "revision": job.get("revision", ""),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "error": job.get("error"),
        }
        if duplicate:
            response["duplicate"] = True
        return response

    # ── 文档列表 / revision ───────────────────────────────────

    def _pending_revision_locked(self, doc: dict[str, Any]) -> str | None:
        pending: dict[str, Any] | None = None
        for record in doc.get("revisions", {}).values():
            if record.get("status") != "staged":
                continue
            if pending is None or (record.get("created_at") or "") >= (
                pending.get("created_at") or ""
            ):
                pending = record
        return str(pending.get("revision")) if pending else None

    def _document_item_locked(self, doc: dict[str, Any]) -> dict[str, Any]:
        active = doc.get("active_revision")
        pending = self._pending_revision_locked(doc)
        active_record = doc.get("revisions", {}).get(active or "", {})
        return {
            "document_id": doc.get("document_id", ""),
            "title": doc.get("title", ""),
            "source_uri": doc.get("source_uri", ""),
            "source_kind": doc.get("source_kind", ""),
            "active_revision": active,
            "status": "active" if active else "pending",
            "chunks": int(active_record.get("chunks") or 0),
            "updated_at": doc.get("updated_at", ""),
            "embedding_descriptor_hash": doc.get("embedding_descriptor_hash", ""),
            "os_family": doc.get("os_family", ""),
            "product": doc.get("product", ""),
            "product_version": doc.get("product_version", ""),
            "license_id": doc.get("license_id", ""),
            "pending_revision": pending if pending != active else None,
        }

    def list_documents(self, *, limit: int = 20, cursor: str = "",
                       status: str | None = None) -> tuple[list[dict], str | None]:
        if status and status not in ("active", "pending"):
            raise ValueError(f"未知文档状态: {status}")
        with self._lock:
            items = [self._document_item_locked(doc)
                     for doc in self._data["documents"].values()]
        items.sort(key=lambda item: (item.get("updated_at") or "",
                                     item.get("document_id") or ""), reverse=True)
        if status:
            items = [item for item in items if item.get("status") == status]
        if cursor:
            key = _decode_cursor(cursor)
            items = [
                item for item in items
                if (item.get("updated_at") or "", item.get("document_id") or "") < key
            ]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            next_cursor = _encode_cursor(
                page[-1].get("updated_at") or "", page[-1].get("document_id") or ""
            )
        return page, next_cursor

    def list_revisions(self, document_id: str) -> dict[str, Any]:
        with self._lock:
            doc = self._data["documents"].get(document_id)
            if doc is None:
                raise NotFound(f"文档不存在: {document_id}")
            revisions = [
                {
                    "revision": record.get("revision", ""),
                    "created_at": record.get("created_at", ""),
                    "status": record.get("status", "staged"),
                    "chunks": int(record.get("chunks") or 0),
                }
                for record in doc.get("revisions", {}).values()
            ]
            revisions.sort(key=lambda item: item.get("created_at") or "", reverse=True)
            return {
                "revisions": revisions,
                "active_revision": doc.get("active_revision"),
            }

    def _revision_indexed(self, doc: dict[str, Any], revision: str) -> tuple[bool, str]:
        """某 revision 是否真的被索引过（有分片且作业成功）。

        失败/取消的作业会留下一个 ``staged``、``chunks=0`` 的 revision 记录。
        允许把这种 revision 设为 active，等于把“索引失败”标成“可检索”，
        文档列表与 ``documents.active`` 计数都会跟着骗人（FE-06 验收红线）。
        """
        record = (doc.get("revisions") or {}).get(revision) or {}
        chunks = int(record.get("chunks") or 0)
        chunk_ids = [value for value in record.get("chunk_ids") or [] if value]
        if chunks <= 0 or not chunk_ids:
            return False, f"revision {revision} 没有已索引的分片（chunks={chunks}）"
        for job in (self._data.get("jobs") or {}).values():
            if job.get("document_id") != doc.get("document_id"):
                continue
            if job.get("revision") != revision:
                continue
            if job.get("status") == "ready":
                return True, ""
        return False, f"revision {revision} 没有成功的索引作业，不能设为 active"

    def activate_revision(self, document_id: str, revision: str) -> dict[str, Any]:
        """切换到已有 revision（回滚）：删除其它 revision 的分片后切换指针。

        先删向量再改元数据：删除失败时保持原 active 不变，不产生“界面已切换、
        旧数据还在”的假成功。目标 revision 必须先通过“已索引”校验。
        """
        lock = document_lock(document_id)
        with lock:
            with self._lock:
                doc = self._data["documents"].get(document_id)
                if doc is None:
                    raise NotFound(f"文档不存在: {document_id}")
                target = doc.get("revisions", {}).get(revision)
                if target is None:
                    raise NotFound(f"revision 不存在: {revision}")
                ok, reason = self._revision_indexed(doc, revision)
                if not ok:
                    raise Conflict(reason)
                others: list[str] = []
                for other_revision, record in doc.get("revisions", {}).items():
                    if other_revision == revision:
                        continue
                    others.extend(str(value) for value in record.get("chunk_ids") or [])
            if others:
                try:
                    vector_store_manager.delete_by_ids(others)
                except Exception as exc:  # noqa: BLE001
                    raise StoreUnavailable(f"删除旧 revision 分片失败，未切换: {exc}") from exc
            with self._lock:
                doc = self._data["documents"].get(document_id)
                target = doc.get("revisions", {}).get(revision) if doc else None
                if doc is None or target is None:
                    raise NotFound(f"文档或 revision 已不存在: {document_id}")
                for other_revision, record in doc.get("revisions", {}).items():
                    if other_revision == revision:
                        record["status"] = "active"
                    elif record.get("status") == "active":
                        record["status"] = "superseded"
                    elif record.get("status") != "superseded":
                        record["status"] = "staged"
                    if other_revision != revision:
                        record["chunks"] = 0
                        record["chunk_ids"] = []
                doc["active_revision"] = revision
                doc["chunks"] = int(target.get("chunks") or 0)
                doc["updated_at"] = _now()
                doc["embedding_descriptor_hash"] = target.get(
                    "embedding_descriptor_hash", ""
                )
                self._save_locked()
                return {
                    "document_id": document_id,
                    "active_revision": revision,
                    "status": "active",
                }

    # ── 删除 ──────────────────────────────────────────────────

    def delete_document(self, document_id: str) -> int:
        """删除文档全部 revision 与向量，返回删除的分片数。

        向量删除失败时保持元数据不动（500/503 后可以重试），不做“只删记录”。
        """
        lock = document_lock(document_id)
        with lock:
            with self._lock:
                doc = self._data["documents"].get(document_id)
                if doc is None:
                    raise NotFound(f"文档不存在: {document_id}")
            try:
                deleted = int(vector_store_manager.delete_by_document(document_id) or 0)
            except Exception as exc:  # noqa: BLE001
                raise StoreUnavailable(f"删除向量失败，文档未删除: {exc}") from exc
            with self._lock:
                doc = self._data["documents"].pop(document_id, None)
                for job in self._data["jobs"].values():
                    if job.get("document_id") != document_id:
                        continue
                    if job.get("status") in _ACTIVE_JOB_STATUSES:
                        job["status"] = "cancelled"
                        job["finished_at"] = _now()
                        job["error"] = {
                            "code": "document_deleted",
                            "message": "文档已删除，作业被取消",
                            "retryable": False,
                        }
                self._save_locked()
            if doc is not None:
                self._remove_content_dir(document_id)
            return deleted

    def _remove_content_dir(self, document_id: str) -> None:
        directory = self.content_dir() / document_id
        try:
            if not directory.is_dir():
                return
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
            directory.rmdir()
        except OSError as exc:
            logger.warning(f"清理内容目录失败（忽略）: {directory}: {exc}")

    # ── 状态统计 ──────────────────────────────────────────────

    def document_counts(self) -> dict[str, int]:
        with self._lock:
            active = sum(1 for doc in self._data["documents"].values()
                         if doc.get("active_revision"))
            pending = sum(1 for doc in self._data["documents"].values()
                          if self._pending_revision_locked(doc))
        return {"active": active, "pending": pending}

    def active_revisions(self) -> dict[str, str]:
        """``document_id -> 当前 active revision`` 的一致快照（A01）。

        这是检索可见性的唯一来源：只有这里列出的 revision 允许进入候选。
        ``staged``（排队/运行中/失败）与 ``superseded`` 一律不在其中，因此即使
        旧向量仍在 collection 里（供回滚），也不会被当作当前知识返回。

        一次读取在同一把锁内完成：并发写入（激活/回滚/删除）只会得到“之前”或
        “之后”的完整快照，不会读到半切换状态。
        """
        with self._lock:
            result: dict[str, str] = {}
            for document_id, doc in self._data["documents"].items():
                active = doc.get("active_revision")
                if not active:
                    continue
                record = (doc.get("revisions") or {}).get(active) or {}
                if record.get("status") != "active":
                    # 指针与记录状态不一致时保守处理：不把不确定的版本当可见。
                    continue
                result[document_id] = str(active)
            return result

    def snapshot(self) -> dict[str, Any]:
        """只读快照（测试/诊断用）；不暴露给 HTTP。"""
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))


class _JobCancelled(Exception):
    """内部：作业在 worker 执行期间被取消/删除。"""


class _CleanupFailed(Exception):
    """内部：写入前清理旧分片失败（旧残片还在库里，不能继续写入）。"""


# ── cursor 编解码（不透明字符串） ────────────────────────────


def _encode_cursor(updated_at: str, document_id: str) -> str:
    raw = f"{updated_at}\x1f{document_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        updated_at, document_id = raw.split("\x1f", 1)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor("cursor 非法") from exc
    return updated_at, document_id


def _safe_delete_ids(ids: Iterable[str]) -> tuple[bool, str]:
    """按 id 清理本次可能写入的行。

    返回 ``(是否清理干净, 错误描述)``。清理失败不再只是一条日志：调用方必须据此
    把作业标成可重试的失败，并在 ``pending_cleanup`` 里留下待重做的 ID，否则
    下一次检索会把残片当成有效知识（A02）。
    """
    values = [value for value in ids if value]
    if not values:
        return True, ""
    try:
        vector_store_manager.delete_by_ids(values)
    except Exception as exc:  # noqa: BLE001 - 回滚是尽力而为，但必须如实上报
        logger.warning(f"清理索引分片失败（可能残留）: {exc}")
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


# ── 状态探测（契约 2.5） ────────────────────────────────────


def probe_status(store: KnowledgeStore | None = None) -> dict[str, Any]:
    """读取 Milvus 真实 metric/index + 当前描述符，返回契约 2.5 的形状。

    - Milvus 连不上 → ``unavailable``（不抛异常，不影响 ``/health``）；
    - collection 度量/描述符不一致 → 独立的 ``invalid_index``；
    - 其余可用 → ``ready``。
    """
    active_store = store or get_store()
    descriptor = current_descriptor()
    embedding = {
        "provider": descriptor.provider,
        "model": descriptor.model,
        "dimension": descriptor.dimension,
        "normalization": descriptor.normalization,
        "distance": descriptor.distance,
        "descriptor_hash": f"sha256:{descriptor.hash}",
    }
    documents = active_store.document_counts()

    started = time.perf_counter()
    try:
        vector_store_manager._ensure_initialized()
    except VectorStoreInvalidIndex as exc:
        return _status_payload(
            "invalid_index", 0, embedding, False, started, documents, str(exc)
        )
    except VectorStoreUnavailable as exc:
        return _status_payload(
            "unavailable", 0, embedding, False, started, documents, str(exc)
        )
    except DescriptorMismatch as exc:
        return _status_payload(
            "invalid_index", 0, embedding, False, started, documents, str(exc)
        )
    except Exception as exc:  # noqa: BLE001 - 任何初始化故障都要有确定状态
        text = str(exc).lower()
        status = (
            "invalid_index"
            if any(key in text for key in ("dimension", "descriptor", "metric"))
            else "unavailable"
        )
        return _status_payload(
            status, 0, embedding, False, started, documents, str(exc)
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    actual_metric = str(getattr(vector_store_manager, "metric_type", "") or "")
    score_kind = str(getattr(vector_store_manager, "score_kind", "") or "")
    try:
        chunks = int(milvus_manager.collection_count() or 0)
    except Exception:  # noqa: BLE001 - 统计失败退化为 0，由描述符判断兜底
        chunks = 0

    try:
        stored = read_descriptor(descriptor_path())
    except DescriptorMismatch as exc:
        return _status_payload(
            "invalid_index", chunks, embedding, True, started, documents, str(exc),
            latency_ms,
        )

    expected_metric = metric_of(descriptor.distance)
    if actual_metric and actual_metric != expected_metric:
        return _status_payload(
            "invalid_index",
            chunks,
            embedding,
            True,
            started,
            documents,
            f"collection 度量是 {actual_metric}（score_kind={score_kind or 'unknown'}），"
            f"当前描述符声明 {descriptor.distance}（{expected_metric}）；请显式重建索引",
            latency_ms,
        )
    if stored is None:
        if chunks:
            return _status_payload(
                "invalid_index",
                chunks,
                embedding,
                True,
                started,
                documents,
                f"collection 已有 {chunks} 条向量，但缺少 embedding 描述符；请显式重建索引",
                latency_ms,
            )
        return _status_payload(
            "degraded", 0, embedding, True, started, documents,
            "collection 为空，尚未建立索引", latency_ms,
        )
    if stored.dimension != descriptor.dimension:
        return _status_payload(
            "invalid_index",
            chunks,
            embedding,
            True,
            started,
            documents,
            f"描述符维度不一致：索引 {stored.dimension} 维，当前配置 "
            f"{descriptor.dimension} 维；请显式重建索引",
            latency_ms,
        )
    if stored.hash != descriptor.hash:
        return _status_payload(
            "invalid_index",
            chunks,
            embedding,
            True,
            started,
            documents,
            "embedding 描述符与 collection 不一致（provider/model/normalization 变化）；"
            "请显式重建索引",
            latency_ms,
        )
    return _status_payload(
        "ready", chunks, embedding, True, started, documents, "", latency_ms
    )


def _status_payload(status: str, chunks: int, embedding: dict[str, Any],
                    connected: bool, started: float, documents: dict[str, int],
                    reason: str, latency_ms: int | None = None) -> dict[str, Any]:
    if latency_ms is None:
        latency_ms = int((time.perf_counter() - started) * 1000)
    return {
        "status": status,
        "collection": getattr(vector_store_manager, "collection_name", "biz") or "biz",
        "chunks": chunks,
        "embedding": embedding,
        "milvus": {"connected": connected, "latency_ms": latency_ms},
        "documents": documents,
        "reason": reason or "",
    }


# ── 白名单目录导入 ───────────────────────────────────────────


def import_roots() -> list[tuple[str, Path]]:
    """可导入的根目录（与 ``app/api/file.py`` 的白名单保持一致）。

    ``uploads`` 目录从 ``file_api.UPLOAD_DIR`` 读取（测试会 monkeypatch），
    ``aiops-docs`` 固定在项目根。顺序即同名文件的查找顺序。
    """
    uploads = Path(getattr(file_api, "UPLOAD_DIR", PROJECT_ROOT / "uploads"))
    return [("aiops-docs", PROJECT_ROOT / "aiops-docs"), ("uploads", uploads)]


def resolve_import_source(source_id: str) -> tuple[str, Path]:
    """把 ``source_id`` 解析成 ``(规范化 source_uri, 文件路径)``。

    支持三种写法：

    - ``aiops-docs/disk_high_usage.md``：相对项目根的路径；
    - ``disk_high_usage.md``：在各自名单根下查找（必须唯一命中，否则 400）；
    - 绝对路径：解析后必须仍在白名单根内。

    返回的 source_uri 形如 ``source://aiops-docs/disk_high_usage.md``，不含
    真实绝对路径（避免把私有路径回显给前端），且不同目录下同名文件不同。
    """
    raw = (source_id or "").strip()
    if not raw:
        raise ValueError("source_id 不能为空")
    roots = import_roots()
    candidate = Path(raw)
    matches: list[tuple[str, Path, str]] = []

    def add_match(label: str, root: Path, path: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
            root_resolved = root.resolve()
        except (OSError, ValueError):
            return
        if not resolved.is_file():
            return
        if resolved != root_resolved and root_resolved not in resolved.parents:
            return
        if resolved.suffix.lower() not in (".md", ".txt"):
            return
        relative = resolved.relative_to(root_resolved).as_posix()
        matches.append((label, resolved, f"{label}/{relative}"))

    if candidate.is_absolute():
        for label, root in roots:
            add_match(label, root, candidate)
    else:
        parts = candidate.parts
        # ``aiops-docs/x.md`` / ``uploads/x.md``：前缀就是名单标签，直接从该根解析。
        label_roots = dict(roots)
        if len(parts) > 1 and parts[0].lower() in label_roots:
            label = parts[0].lower()
            add_match(label, label_roots[label], label_roots[label].joinpath(*parts[1:]))
        for label, root in roots:
            add_match(label, root, root.joinpath(*parts))
            add_match(label, root, PROJECT_ROOT.joinpath(*parts))

    unique: dict[str, tuple[str, Path]] = {canonical: (canonical, path)
                                          for _label, path, canonical in matches}
    if not unique:
        raise ValueError(
            f"source_id 不在白名单目录内或文件不存在: {raw}"
            f"（允许: {', '.join(label for label, _ in roots)}）"
        )
    if len(unique) > 1:
        raise ValueError(
            f"source_id 命中多个文件，请写成相对路径（如 aiops-docs/{Path(raw).name}）: {raw}"
        )
    canonical, path = next(iter(unique.values()))
    return f"source://{canonical}", path


# ── 全局单例 ─────────────────────────────────────────────────


_store: KnowledgeStore | None = None
#: 可重入：``get_store()`` 在持锁期间构造 ``KnowledgeStore``，而构造函数会经
#: ``_register_current_store`` 再次进入同一把锁。
_store_guard = threading.RLock()

#: 最近一次显式构造的存储实例（测试 / 探针直接 ``KnowledgeStore(path)`` 时，
#: 检索适配器也必须是同一份元数据；单例 ``get_store()`` 仅在从未显式构造时兜底）。
_recent_store: KnowledgeStore | None = None


def _register_current_store(store: KnowledgeStore) -> None:
    """记录最近构造的实例，作为检索可见性（active revision）的来源。"""
    global _recent_store
    with _store_guard:
        _recent_store = store


def current_store() -> KnowledgeStore:
    """检索侧使用的存储实例：优先最近显式构造的实例，其次进程单例。

    检索适配器不持有写权限，只调用 :meth:`KnowledgeStore.active_revisions` 读取
    一个一致快照；它不会触发任何索引、连接或写盘。
    """
    with _store_guard:
        recent = _recent_store
    if recent is not None:
        return recent
    return get_store()


def active_revisions() -> dict[str, str]:
    """当前可检索的 ``document_id -> active revision``（无存储时为空）。"""
    try:
        return current_store().active_revisions()
    except Exception as exc:  # noqa: BLE001 - 可见性读取失败不能伪造命中
        logger.warning(f"读取 active revision 快照失败: {type(exc).__name__}: {exc}")
        raise


def get_store() -> KnowledgeStore:
    """进程级单例；环境变量改变后用 :func:`reset_store` 重建。"""
    global _store
    if _store is None:
        with _store_guard:
            if _store is None:
                _store = KnowledgeStore()
    return _store


def reset_store() -> None:
    """测试/重配置用：关闭旧 worker 并清空单例（下一次按当前路径重建）。"""
    global _store
    with _store_guard:
        old = _store
        _store = None
    if old is not None:
        old.shutdown()


def wait_for_job(job_id: str, *, timeout: float = 10.0,
                 interval: float = 0.02) -> dict[str, Any]:
    """轮询作业直到终态（测试与同步调用方使用）。"""
    deadline = time.monotonic() + timeout
    while True:
        job = get_store().get_job(job_id)
        if job["status"] in ("ready", "failed", "cancelled"):
            return job
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待作业超时: {job_id}（当前 {job['status']}）")
        time.sleep(interval)
