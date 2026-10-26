"""
AIOps 请求和响应模型
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Literal, Union
from pydantic import BaseModel, Field, field_validator, model_validator


#: 单次诊断允许回溯的最长时间窗。
MAX_TIME_WINDOW = timedelta(days=7)


def _parse_moment(value: Union[str, int, float]) -> datetime:
    """把 ISO-8601 字符串或 epoch 秒解析为带时区的 datetime。

    无时区的 ISO 字符串按 UTC 处理，避免不同机器本地时区导致边界漂移；
    bool 是 int 的子类，必须显式排除，否则 True 会被当成 1970-01-01。
    """
    if isinstance(value, bool):
        raise ValueError("时间必须是 ISO-8601 字符串或 epoch 秒")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("时间不能为空")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("时间必须是 ISO-8601 字符串或 epoch 秒") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment
    raise ValueError("时间必须是 ISO-8601 字符串或 epoch 秒")


class TimeWindow(BaseModel):
    """诊断回溯时间窗：闭开区间 ``[start, end)``，最长 7 天。"""

    start: Union[str, int, float] = Field(description="ISO-8601 字符串或 epoch 秒")
    end: Union[str, int, float] = Field(description="ISO-8601 字符串或 epoch 秒")

    @model_validator(mode="after")
    def _check_range(self) -> "TimeWindow":
        start = _parse_moment(self.start)
        end = _parse_moment(self.end)
        if end <= start:
            raise ValueError("time_window.end 必须晚于 start")
        if end - start > MAX_TIME_WINDOW:
            raise ValueError("time_window 不能超过 7 天")
        return self


class AIOpsRequest(BaseModel):
    """AIOps 诊断请求"""

    session_id: Optional[str] = Field(
        default="default",
        description="会话ID，用于追踪诊断历史"
    )
    execution_mode: Literal["local", "cloud_multi"] = "local"
    symptom: Optional[str] = Field(default=None, max_length=4000)
    service_name: Optional[str] = Field(default=None, max_length=120)
    target_choice: Optional[str] = Field(
        default=None,
        max_length=120,
        description="用户在 target_required 候选卡片中显式选择的服务名",
    )
    target_candidate_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description=(
            "用户点击候选卡片时由服务端签发的稳定候选 ID；同名多实例必须用它"
            "绑定具体实例/地域，仅在未提供时才回退到 target_choice 服务名"
        ),
    )
    time_window: Optional[TimeWindow] = None

    @field_validator("target_choice")
    @classmethod
    def _strip_choice(cls, value: Optional[str]) -> Optional[str]:
        """空白选择视同未选择，避免把空串当成一次显式绑定。"""
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("target_candidate_id")
    @classmethod
    def _strip_candidate(cls, value: Optional[str]) -> Optional[str]:
        """只接受服务端候选 ID 的形态（``tgt-<hex>``）；其他值视为未选择。"""
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not re.fullmatch(r"tgt-[0-9a-f]{8,64}", text):
            raise ValueError("target_candidate_id 必须是服务端签发的候选 ID")
        return text

    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "session-123"
            }
        }


class AlertInfo(BaseModel):
    """告警信息"""
    alertname: str
    severity: str
    instance: str
    duration: str
    description: Optional[str] = None


class DiagnosisResponse(BaseModel):
    """诊断响应（非流式）"""
    
    code: int = 200
    message: str = "success"
    data: Dict[str, Any]
    
    class Config:
        json_schema_extra = {
            "example": {
                "code": 200,
                "message": "success",
                "data": {
                    "status": "completed",
                    "target_alert": {
                        "alertname": "HighCPUUsage",
                        "severity": "critical"
                    },
                    "diagnosis": {
                        "root_cause": "数据库连接池耗尽",
                        "recommendations": ["扩容数据库连接池", "优化SQL查询"]
                    }
                }
            }
        }
