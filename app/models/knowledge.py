"""知识库管理（M2）请求/响应模型。

字段名与状态值以 ``docs/claude-code-handoff/M2_API_CONTRACT.md`` 第 2 节为准：
不要在这里发明新的状态（例如把 ``invalid_index`` 并进 ``unavailable``），
也不要向前端泄露内部路径。内部记录（content_path / live_path 等）只存在于
``app/services/knowledge_store.py``，不进入这里的响应模型。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: job 状态机：queued → running → ready | failed | cancelled
JobStatus = Literal["queued", "running", "ready", "failed", "cancelled"]

#: 文档在列表里的状态（``pending`` 表示只有待生效 revision）
DocumentStatus = Literal["active", "pending"]

#: revision 状态：只有被激活的那个是 active
RevisionStatus = Literal["active", "superseded", "staged"]

#: collection 级状态。invalid_index 必须独立于 unavailable（契约 2.5）。
IndexStatus = Literal["ready", "degraded", "unavailable", "invalid_index"]


class DocumentItem(BaseModel):
    """文档列表中的一行（契约 2.1）。"""

    document_id: str
    title: str = ""
    source_uri: str = ""
    source_kind: str = ""
    active_revision: str | None = None
    status: DocumentStatus = "pending"
    chunks: int = 0
    updated_at: str = ""
    embedding_descriptor_hash: str = ""
    os_family: str = ""
    product: str = ""
    product_version: str = ""
    license_id: str = ""
    pending_revision: str | None = None


class DocumentListResponse(BaseModel):
    """文档列表响应；``next_cursor`` 为 null 表示没有下一页。"""

    documents: list[DocumentItem] = Field(default_factory=list)
    next_cursor: str | None = None


class JobProgress(BaseModel):
    chunks_total: int = 0
    chunks_done: int = 0


class JobError(BaseModel):
    code: str = ""
    message: str = ""
    retryable: bool = False


class JobItem(BaseModel):
    """作业状态（契约 2.3），不包含内部文件路径。"""

    job_id: str
    document_id: str
    status: JobStatus
    progress: JobProgress = Field(default_factory=JobProgress)
    revision: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: JobError | None = None


class JobListResponse(BaseModel):
    jobs: list[JobItem] = Field(default_factory=list)


class JobAccepted(BaseModel):
    """202 响应：已接收，尚未可检索。"""

    job_id: str
    document_id: str
    status: str = "queued"


class RevisionItem(BaseModel):
    revision: str
    created_at: str = ""
    status: RevisionStatus = "staged"
    chunks: int = 0


class RevisionListResponse(BaseModel):
    revisions: list[RevisionItem] = Field(default_factory=list)
    active_revision: str | None = None


class ImportRequest(BaseModel):
    """导入请求：``source_id`` 与 ``url`` 二选一。

    当前仅支持白名单目录内的 ``source_id``；``url`` 会返回 400 并说明原因
    （需要外网抓取与 SSRF 校验，不在本模块的所有权范围内）。
    """

    source_id: str | None = None
    url: str | None = None


class ActivateRequest(BaseModel):
    """切换到已有 revision（回滚）。"""

    revision: str


class ActivateResponse(BaseModel):
    document_id: str
    active_revision: str
    job_id: str | None = None
    status: str = "queued"


class DeleteResponse(BaseModel):
    deleted_chunks: int = 0


class EmbeddingInfo(BaseModel):
    provider: str = ""
    model: str = ""
    dimension: int = 0
    normalization: str = ""
    distance: str = ""
    descriptor_hash: str = ""


class MilvusInfo(BaseModel):
    connected: bool = False
    latency_ms: int = 0


class DocumentCounts(BaseModel):
    active: int = 0
    pending: int = 0


class KnowledgeStatusResponse(BaseModel):
    """collection 描述符状态（契约 2.5）。"""

    status: IndexStatus
    collection: str = ""
    chunks: int = 0
    embedding: EmbeddingInfo = Field(default_factory=EmbeddingInfo)
    milvus: MilvusInfo = Field(default_factory=MilvusInfo)
    documents: DocumentCounts = Field(default_factory=DocumentCounts)
    reason: str = ""
