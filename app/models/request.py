"""请求数据模型

定义 API 请求的 Pydantic 模型
"""

from pydantic import BaseModel, Field


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


class ClearRequest(BaseModel):
    """清空会话请求"""

    model_config = {"populate_by_name": True}

    session_id: str = Field(..., description="会话 ID", alias="sessionId")
