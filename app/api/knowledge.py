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

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

from app.api import file as file_api
from app.core import principal as acl
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


def _principal(x_api_key: str | None) -> acl.Principal:
    """解析调用方 principal；认证失败映射成 401（不是 500）。"""
    try:
        return acl.current_principal(None, x_api_key or "")
    except acl.PermissionDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


def _enforce_document_read(document_id: str, principal: acl.Principal) -> None:
    """读文档（revisions）：不可见统一 404，不用 403 暴露“它存在”。

    读权限来自存储层的 ``document_visible_to``（owner / 文档级 ACL / 租户模式
    同一份规则），不在这里另写一套。
    """
    store = _store()
    if not store.has_document(document_id):
        raise HTTPException(status_code=404, detail=f"文档不存在: {document_id}")
    if not store.document_visible_to(document_id, principal.principal_id):
        raise HTTPException(status_code=404, detail=f"文档不存在: {document_id}")


def _enforce_document_write(document_id: str, principal: acl.Principal, *,
                            hidden_status: int = 403) -> None:
    """写文档（activate / delete / 覆盖上传 / retry）：只有 owner，或"无 owner 时的 ACL 成员"。

    与读分开的原因：只读 ACL 成员能看见文档，但让他们改 active revision 或删除，
    等于"共享阅读"顺手拿到了"回滚/删除"能力。可见即可写是这里最危险的退化。

    ``hidden_status`` 决定"文档存在但调用方完全看不见"时返回什么：文档路由沿用
    历史的 403；作业路由传 404，与作业详情/列表保持同一口径（不给 job_id 当存在性
    探针）。
    """
    store = _store()
    if not store.has_document(document_id):
        raise HTTPException(status_code=404, detail=f"文档不存在: {document_id}")
    if not store.document_visible_to(document_id, principal.principal_id):
        # 完全看不见的文档：按调用方选择的语义返回，不泄漏"它存在"。
        raise HTTPException(status_code=hidden_status,
                            detail=f"文档不存在或无权访问: {document_id}")
    if not store.document_writable_by(document_id, principal.principal_id):
        raise HTTPException(
            status_code=403,
            detail=f"无权修改不属于本 principal 的知识文档（{principal.principal_id}）",
        )


def _ensure_no_foreign_document(source_uri: str, principal: acl.Principal) -> None:
    """上传/导入前检查同名文档：已存在且不可写时拒绝，避免内容被他人覆盖。

    ``document_id`` 由 ``source_uri`` 派生，所以"同名文件"就是"同一份文档"。
    没有这道检查时，B 用同名 source_uri 上传会把新 revision 索引并**激活**到
    A 的文档上（A 的 owner/ACL 不变，但检索内容被替换）——一次上传即可注入
    错误的处置步骤。文档不存在时保持原行为（首次创建）。
    """
    store = _store()
    document_id = store_api.document_id_for(source_uri)
    if not store.has_document(document_id):
        return
    if not store.document_writable_by(document_id, principal.principal_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "同名文档已存在且不属于当前 principal；新 revision 会覆盖它正在"
                "被检索的内容，已拒绝。请换一个文件名，或由文档所有者上传。"
            ),
        )


def _claim_uploaded_document(document_id: str, principal: acl.Principal) -> None:
    """**提交之前**声明归属：claim owner（只占空位），再把本人写进文档级 ACL 由
    :func:`_seal_uploaded_document` 在提交成功后补。

    为什么提前：``submit_document`` 会立刻落盘、排队并可能开始索引；等它返回
    再 claim 时，一旦 claim 失败（同名文档已属于别人 / ACL 侧表不可写），路由
    已经返回 202，调用方却在自己的列表与作业详情里**看不到**这份刚上传的文档
    （前端把它当查询失败并清空 pendingJob）。提前 claim 把"要么拿到归属、要么
    在创建任何状态前被拒绝"变成同一步。

    失败时抛 409，而不是只打一条 warning：这是"同名文档属于别人"，与
    :func:`_ensure_no_foreign_document` 的拒绝口径一致。
    """
    if not acl.claim_owner("knowledge_document", document_id, principal.principal_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "该 source_uri 派生的文档已归属其它 principal，已拒绝上传；"
                "请换一个文件名，或由文档所有者上传。"
            ),
        )
    if acl.resource_owner("knowledge_document", document_id) != principal.principal_id:
        raise HTTPException(
            status_code=409,
            detail="文档归属记录写入后校验不一致，已停止（请检查 PRINCIPAL_ACL_FILE）",
        )


def _seal_uploaded_document(document_id: str, principal: acl.Principal) -> None:
    """提交成功后的收尾：把上传者写进文档级 ACL（幂等）。

    这一步失败不影响已排队的索引（只记 ERROR）：owner 侧表已经记录了归属，
    读取/写入判定都能正常工作；ACL 只是"显式成员名单"的补充。
    """
    try:
        _store().add_acl_principal(document_id, principal.principal_id)
    except Exception as exc:  # noqa: BLE001 - ACL 补充失败不能回滚已排队的索引
        logger.error(
            f"写入文档级 ACL 失败（不影响索引）: {document_id}: {type(exc).__name__}: {exc}"
        )


def _metadata_from_form(form: dict[str, object]) -> dict[str, object]:
    """从 multipart 的可选 Form 字段组装元数据；未出现的字段不传给存储层。"""
    fields = ("os_family", "product", "product_version", "valid_from", "valid_until")
    provided: dict[str, object] = {}
    for field in fields:
        value = form.get(field)
        if value is not None:
            provided[field] = value
    acl_value = form.get("acl_principals")
    if acl_value is not None:
        provided["acl_principals"] = acl_value
    return provided


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
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> DocumentListResponse:
    """分页列出文档（按 updated_at 倒序，cursor 为不透明字符串）。

    **只列出调用方拥有的文档**：记录了 owner 且不是自己的一律不出现（不是
    报 403——列表里“看不见”比“看见了说无权”更不泄漏存在性）。没有 owner 的
    旧文档保持可见，兼容迁移期。

    可见性在存储层分页**之前**过滤：路由先全局分页再筛会把隐藏文档也算进
    ``limit``，出现“一页少几条”甚至空页，``next_cursor`` 也会跳过可见文档。
    """
    principal = _principal(x_api_key)
    try:
        documents, next_cursor = _store().list_documents(
            limit=limit, cursor=cursor, status=status,
            principal_id=principal.principal_id,
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
async def upload_document(
    file: UploadFile = File(...),
    os_family: str | None = Form(default=None),
    product: str | None = Form(default=None),
    product_version: str | None = Form(default=None),
    valid_from: str | None = Form(default=None),
    valid_until: str | None = Form(default=None),
    acl_principals: list[str] | None = Form(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """接收上传并排队索引（202），返回 ``job_id/document_id/status=queued``。

    可选 Form 字段是**文档级标注**（OS/产品/版本/有效期/ACL）。不传时行为与
    之前完全一致；非法日期/非法 principal 值由存储层统一校验并映射成 400
    （不会静默丢弃）。
    """
    principal = _principal(x_api_key)
    safe_filename = _validate_upload_filename(file.filename)
    metadata = _metadata_from_form({
        "os_family": os_family,
        "product": product,
        "product_version": product_version,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "acl_principals": acl_principals,
    })
    try:
        content = await file_api._read_bounded(file, file_api.MAX_FILE_SIZE)
        file_api._validate_text(content)
        source_uri = f"upload://{safe_filename}"
        # 同名文档属于别人时先拒绝：否则新 revision 会被索引并激活到别人的
        # 文档上，实质是用一次上传替换他人知识库内容。
        _ensure_no_foreign_document(source_uri, principal)
        document_id = store_api.document_id_for(source_uri)
        _claim_uploaded_document(document_id, principal)
        accepted = _store().submit_document(
            source_uri=source_uri,
            title=_title_for(safe_filename, content),
            content=content,
            source_kind="upload",
            metadata=metadata,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    _seal_uploaded_document(str(accepted.get("document_id") or ""), principal)
    return JSONResponse(status_code=202, content=accepted)


@router.post("/knowledge/import", response_model=JobAccepted, status_code=202)
async def import_source(payload: ImportRequest,
                        x_api_key: str | None = Header(default=None,
                                                      alias="X-API-Key")):
    """从白名单目录导入单个文件；``url`` 导入暂不支持（400 并说明原因）。"""
    principal = _principal(x_api_key)
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
        # 同 upload：同名来源已属于别人时不允许把新 revision 激活上去。
        _ensure_no_foreign_document(source_uri, principal)
        document_id = store_api.document_id_for(source_uri)
        _claim_uploaded_document(document_id, principal)
        accepted = _store().submit_document(
            source_uri=source_uri,
            title=_title_for(path.name, content),
            content=content,
            source_kind="manual",
            metadata=payload.metadata(),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    _seal_uploaded_document(str(accepted.get("document_id") or ""), principal)
    return JSONResponse(status_code=202, content=accepted)


# ── 作业 ─────────────────────────────────────────────────────


def _enforce_job_visible(job_id: str, principal: acl.Principal) -> None:
    """单条作业的鉴权：作业不存在或其文档对调用方不可见，一律 404。

    用 404 而不是 403：列表里看不见的作业，详情里也不该泄漏“它存在”。鉴权在
    读取详情/创建新作业**之前**调用，避免仅凭 job_id 探测 ``source_uri``、
    错误信息，或对别人的作业发起 retry。
    """
    store = _store()
    document_id = store.job_document_id(job_id)
    if not store.document_visible_to(document_id, principal.principal_id):
        raise HTTPException(status_code=404, detail=f"作业不存在: {job_id}")


@router.get("/knowledge/jobs", response_model=JobListResponse)
async def list_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=store_api.MAX_PAGE_LIMIT),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> JobListResponse:
    """列出最近作业，可按状态过滤（契约 2.3）。

    只返回当前 principal 可见文档的作业；过滤在 ``jobs[:limit]`` 之前完成，
    别人的作业不消耗当前用户的 ``limit``。
    """
    principal = _principal(x_api_key)
    try:
        jobs = _store().list_jobs(
            status=status, limit=limit, principal_id=principal.principal_id
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return JobListResponse(jobs=jobs)


@router.get("/knowledge/jobs/{job_id}", response_model=JobItem)
async def get_job(
    job_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> JobItem:
    principal = _principal(x_api_key)
    try:
        _enforce_job_visible(job_id, principal)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    try:
        return JobItem(**_store().get_job(job_id))
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post("/knowledge/jobs/{job_id}/retry", response_model=JobAccepted, status_code=202)
async def retry_job(
    job_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """重试失败作业：202 + 新 job_id，旧 job 保留为历史。

    先鉴权再判状态：不可见作业直接 404，调用方拿不到“它不是 failed”之类的
    状态探测信号；鉴权通过后 409 等状态错误才有意义。
    """
    principal = _principal(x_api_key)
    try:
        document_id = _store().job_document_id(job_id)
        # retry 成功会把该 revision 置为 active：这是**写**操作，必须按
        # 写权限判定，不能只看"可见"（只读 ACL 成员能看不能改）。
        _enforce_document_write(document_id, principal, hidden_status=404)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
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
async def list_revisions(document_id: str,
                         x_api_key: str | None = Header(default=None,
                                                       alias="X-API-Key")
                         ) -> RevisionListResponse:
    _enforce_document_read(document_id, _principal(x_api_key))
    try:
        return RevisionListResponse(**_store().list_revisions(document_id))
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post(
    "/knowledge/documents/{document_id}/activate",
    response_model=ActivateResponse,
)
async def activate_revision(document_id: str, payload: ActivateRequest,
                            x_api_key: str | None = Header(default=None,
                                                          alias="X-API-Key")
                            ) -> ActivateResponse:
    """切换到已有 revision（回滚）；删除其它 revision 的分片后切换指针。"""
    _enforce_document_write(document_id, _principal(x_api_key))
    try:
        result = _store().activate_revision(document_id, payload.revision)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return ActivateResponse(**result)


@router.delete("/knowledge/documents/{document_id}", response_model=DeleteResponse)
async def delete_document(document_id: str,
                          x_api_key: str | None = Header(default=None,
                                                        alias="X-API-Key")
                          ) -> DeleteResponse:
    """删除文档全部 revision 与向量，返回实际删除的分片数。"""
    principal = _principal(x_api_key)
    _enforce_document_write(document_id, principal)
    try:
        deleted = _store().delete_document(document_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    # 删除成功后清掉归属记录，避免 document_id 被重新导入时继承旧 owner。
    acl.record_owner("knowledge_document", document_id, "")
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
