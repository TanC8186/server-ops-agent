"""统一事件信封：SSE、trace、回放共用同一份规范化实现。

约定（见 docs/claude-code-envelope）：

- 保留字段 ``schema_version/event_id/seq/run_id/session_id/execution_mode/type/ts``
  只能由服务端生成；模型或工具事件里同名的键一律被丢弃，不能覆盖。
- 规范化后的载荷既保留扁平字段（兼容现有前端 reducer），也放在 ``payload``
  下（新代码应通过它读取），避免两套协议分叉。
- 事件 ID 稳定：``{run_id}:{seq}``，同一 run 内按 ``seq`` 去重/排序。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SCHEMA_VERSION = "1"

#: 服务端独占字段；payload 中同名键会被忽略。
RESERVED_FIELDS: frozenset[str] = frozenset({
    "schema_version",
    "event_id",
    "seq",
    "run_id",
    "session_id",
    "execution_mode",
    "type",
    "event",       # trace 里的事件名，不能由 payload 顶替
    "ts",
    "timestamp",
})

#: trace 记录自身的键，写入时不得与载荷冲突。
TRACE_FIXED_FIELDS: frozenset[str] = frozenset({"ts", "run_id", "event"})


class EventNormalizationError(ValueError):
    """事件不是 JSON 对象时抛出，调用方应记为协议错误而不是静默丢弃。"""


def event_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """取出去掉保留字段后的业务载荷。"""
    return {key: value for key, value in raw.items() if key not in RESERVED_FIELDS}


def normalize_event(
    raw: dict[str, Any],
    *,
    run_id: str,
    session_id: str,
    execution_mode: str,
    seq: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把任意来源的事件规范成统一信封。

    参数全部由服务端提供（run 标识、会话、模式、序号），因此即使 generator
    里伪造了 ``run_id`` 也无法越权。返回值保持扁平以兼容旧前端，同时提供
    ``payload`` 供新代码使用。
    """
    if not isinstance(raw, dict):
        raise EventNormalizationError(f"事件必须是对象，实际为 {type(raw).__name__}")
    kind = raw.get("type") or raw.get("event")
    if not isinstance(kind, str) or not kind.strip():
        kind = "status"
    payload = event_payload(raw)
    timestamp = (now or datetime.now()).isoformat()
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"{run_id}:{seq}",
        "seq": seq,
        "run_id": run_id,
        "session_id": session_id,
        "execution_mode": execution_mode,
        "type": kind,
        "ts": timestamp,
        "payload": payload,
    }
    # 兼容层：旧 UI 直接读 event.step / event.tools / event.message。
    envelope.update(payload)
    return envelope


def trace_fields(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """把规范化事件拆成 trace 的 ``(事件名, 载荷)``。

    载荷已剔除保留字段，因此 ``trace_event(run_id, kind, **fields)`` 不可能再
    收到同名 ``run_id``。
    """
    kind = str(event.get("type") or "event")
    fields = {
        key: value
        for key, value in event.items()
        if key not in RESERVED_FIELDS and key not in TRACE_FIXED_FIELDS and key != "payload"
    }
    if event.get("seq") is not None:
        fields["seq"] = event["seq"]
    if event.get("event_id"):
        fields["event_id"] = event["event_id"]
    return kind, fields
