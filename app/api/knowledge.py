"""知识库管理接口（M2）。

路由与字段以 ``docs/claude-code-handoff/M2_API_CONTRACT.md`` 第 2 节为准：

- 列表 / 上传 / 导入 / 作业查询与重试 / revision 与回滚 / 删除 / collection 状态。
- 上传只做**校验 + 落盘 + 建作业**，索引交给 ``knowledge_store`` 的有界后台
  worker；请求内不做切分、embedding 或 Milvus 写入。
- 文件校验复用 ``app/api/file.py`` 的 ``_read_bounded/_validate_text/
  _sanitize_filename/_is_reserved_device_name``，不复制第二份规则。

错误约定（契约 §0）：统一 ``{"detail": "..."}`` + 非 2xx，不在 200 里塞
``success:false``。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

from app.api import file as file_api
from app.models.knowledge import (
    ActivateRequest,
    ActivateResponse,
    DeleteResponse,
    DocumentListResponse,
    ImportRequest,
    JobAccepted,
    JobItem,
    JobListResponse,
    KnowledgeStatusResponse,
    RevisionListResponse,
)
from app.services import knowledge_store as store_api
from app.services.knowledge_store import (
    Conflict,
    InvalidCursor,
    KnowledgeStore,
    NotFound,
    StoreUnavailable,
    get_store,
    probe_status,
)
from app.services.vector_index_service import vector_index_service

router = APIRouter()

#: 允许上传的扩展名（与现有上传入口一致，M2 暂不扩展格式）。
ALLOWED_EXTENSIONS = file_api.ALLOWED_EXTENSIONS


def _store() -> KnowledgeStore:
    """每次请求取当前单例（测试可 reset/替换存储路径）。"""
    return get_store()


def _map_error(exc: Exception) -> HTTPException:
    """存储层异常 -> 契约里的 HTTP 状态码。"""
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, Conflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidCursor):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, StoreUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail=f"知识库操作失败: {exc}")


def _title_for(filename: str, content: bytes) -> str:
    """标题优先取首个 Markdown 一级标题，其次文件名（不含扩展名）。"""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            if title:
                return title[:200]
        if stripped:
            break
    return Path(filename).stem[:200] or filename[:200]


# ── 文档列表 ─────────────────────────────────────────────────


@router.get("/knowledge/documents", response_model=DocumentListResponse)
async def list_documents(
    limit: int = Query(default=20, ge=1, le=store_api.MAX_PAGE_LIMIT),
    cursor: str = Query(default=""),
    status: str | None = Query(default=None),
) -> DocumentListResponse:
    """分页列出文档（按 updated_at 倒序，cursor 为不透明字符串）。"""
    try:
        documents, next_cursor = _store().list_documents(
            limit=limit, cursor=cursor, status=status
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return DocumentListResponse(documents=documents, next_cursor=next_cursor)


# ── 上传 / 导入 ──────────────────────────────────────────────


def _validate_upload_filename(filename: str | None) -> str:
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    safe_filename = file_api._sanitize_filename(filename)
    if file_api._is_reserved_device_name(safe_filename):
        raise HTTPException(
            status_code=400,
            detail=f"文件名使用了 Windows 保留设备名（如 CON/NUL/COM1），无法保存: {safe_filename}",
        )
    if file_api._get_file_extension(safe_filename) not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
        )
    return safe_filename


@router.post("/knowledge/documents", response_model=JobAccepted, status_code=202)
async def upload_document(file: UploadFile = File(...)):
    """接收上传并排队索引（202），返回 ``job_id/document_id/status=queued``。"""
    safe_filename = _validate_upload_filename(file.filename)
    try:
        content = await file_api._read_bounded(file, file_api.MAX_FILE_SIZE)
        file_api._validate_text(content)
        source_uri = f"upload://{safe_filename}"
        accepted = _store().submit_document(
            source_uri=source_uri,
            title=_title_for(safe_filename, content),
            content=content,
            source_kind="upload",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return JSONResponse(status_code=202, content=accepted)


@router.post("/knowledge/import", response_model=JobAccepted, status_code=202)
async def import_source(payload: ImportRequest):
    """从白名单目录导入单个文件；``url`` 导入暂不支持（400 并说明原因）。"""
    source_id = (payload.source_id or "").strip()
    url = (payload.url or "").strip()
    if url and not source_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "url 导入暂不支持：需要先做外网抓取、域名白名单与 SSRF 校验"
                "（拒绝 loopback/私网/metadata 目标），该能力不在本轮范围内。"
                "请先把文件放入白名单目录，再用 source_id 导入。"
            ),
        )
    if url and source_id:
        raise HTTPException(status_code=400, detail="source_id 与 url 只能二选一")
    if not source_id:
        raise HTTPException(status_code=400, detail="必须提供 source_id")

    try:
        source_uri, path = store_api.resolve_import_source(source_id)
        # 复用索引层的“有界读取 + UTF-8 严格解码 + 空内容拒绝”。
        vector_index_service.read_text(path)
        content = path.read_bytes()
        accepted = _store().submit_document(
            source_uri=source_uri,
            title=_title_for(path.name, content),
            content=content,
            source_kind="manual",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return JSONResponse(status_code=202, content=accepted)


# ── 作业 ─────────────────────────────────────────────────────


@router.get("/knowledge/jobs", response_model=JobListResponse)
async def list_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=store_api.MAX_PAGE_LIMIT),
) -> JobListResponse:
    """列出最近作业，可按状态过滤（契约 2.3）。"""
    try:
        jobs = _store().list_jobs(status=status, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return JobListResponse(jobs=jobs)


@router.get("/knowledge/jobs/{job_id}", response_model=JobItem)
async def get_job(job_id: str) -> JobItem:
    try:
        return JobItem(**_store().get_job(job_id))
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post("/knowledge/jobs/{job_id}/retry", response_model=JobAccepted, status_code=202)
async def retry_job(job_id: str):
    """重试失败作业：202 + 新 job_id，旧 job 保留为历史。"""
    try:
        job = _store().retry_job(job_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job["job_id"],
            "document_id": job["document_id"],
            "status": job["status"],
            "revision": job["revision"],
        },
    )


# ── revision / 删除 ─────────────────────────────────────────


@router.get(
    "/knowledge/documents/{document_id}/revisions",
    response_model=RevisionListResponse,
)
async def list_revisions(document_id: str) -> RevisionListResponse:
    try:
        return RevisionListResponse(**_store().list_revisions(document_id))
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post(
    "/knowledge/documents/{document_id}/activate",
    response_model=ActivateResponse,
)
async def activate_revision(document_id: str, payload: ActivateRequest) -> ActivateResponse:
    """切换到已有 revision（回滚）；删除其它 revision 的分片后切换指针。"""
    try:
        result = _store().activate_revision(document_id, payload.revision)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return ActivateResponse(**result)


@router.delete("/knowledge/documents/{document_id}", response_model=DeleteResponse)
async def delete_document(document_id: str) -> DeleteResponse:
    """删除文档全部 revision 与向量，返回实际删除的分片数。"""
    try:
        deleted = _store().delete_document(document_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    logger.info(f"已删除知识文档: {document_id}, 分片数={deleted}")
    return DeleteResponse(deleted_chunks=deleted)


# ── collection 状态 ─────────────────────────────────────────


@router.get("/knowledge/status", response_model=KnowledgeStatusResponse)
async def knowledge_status() -> KnowledgeStatusResponse:
    """读取 Milvus 真实 metric/index 与当前描述符；不可用/不一致都要有确定状态。"""
    try:
        payload = probe_status(_store())
    except Exception as exc:  # noqa: BLE001 - 状态探测本身不得 500
        logger.error(f"知识库状态探测失败: {exc}")
        payload = {
            "status": "unavailable",
            "collection": "",
            "chunks": 0,
            "embedding": {},
            "milvus": {"connected": False, "latency_ms": 0},
            "documents": {"active": 0, "pending": 0},
            "reason": f"状态探测失败: {exc}",
        }
    return KnowledgeStatusResponse(**payload)
