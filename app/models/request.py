"""请求数据模型

定义 API 请求的 Pydantic 模型
"""

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

#: 有效期/日期筛选只接受 ISO 日期（参见 knowledge_store 的同名规则）。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ChatFilters(BaseModel):
    """检索前的可选过滤条件（G5）。

    只接受调用方**明确提供**的 OS/产品/版本/日期：这些值用于在候选阶段排除
    不匹配的文档，不做任何语义推断（``RHEL 9`` 只做相等匹配）。全部可空，
    不传时行为不变。
    """

    os_family: str | None = Field(default=None, max_length=120)
    product: str | None = Field(default=None, max_length=120)
    product_version: str | None = Field(default=None, max_length=120)
    at_date: str | None = Field(
        default=None, max_length=10, description="有效期判定日期（YYYY-MM-DD）"
    )

    @field_validator("at_date")
    @classmethod
    def _validate_date(cls, value: str | None) -> str | None:
        """非法日期在请求解析阶段拒绝（422），不进入检索也不会被忽略。"""
        text = str(value or "").strip()
        if not text:
            return None
        if not _DATE_RE.match(text):
            raise ValueError("at_date 必须是 YYYY-MM-DD 格式的日期")
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"at_date 不是合法日期: {text}") from exc
        return text


class ChatRequest(BaseModel):
    """对话请求"""

    model_config = {"populate_by_name": True}

    id: str = Field(..., description="会话 ID", alias="Id")
    question: str = Field(..., description="用户问题", alias="Question")
    run_id: str | None = Field(
        default=None,
        description="追问某次诊断时的 run_id；服务端会校验它属于当前会话",
        max_length=64,
    )
    filters: ChatFilters | None = Field(
        default=None,
        description="可选检索过滤条件（OS/产品/版本/日期）；不传则不过滤",
    )


class ClearRequest(BaseModel):
    """清空会话请求"""

    model_config = {"populate_by_name": True}

    session_id: str = Field(..., description="会话 ID", alias="sessionId")
