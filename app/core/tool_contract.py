"""工具返回契约与结构化证据（BE-02 / BE-05 / BE-06 的基础）。

工具结果必须能被机械判定：

- ``status=ok``          拿到数据，且不是空集合
- ``status=empty``       查询成功但零命中（不是错误）
- ``status=partial``     部分子查询成功
- ``status=error``       执行失败
- ``status=timeout``     deadline 到期
- ``status=unavailable`` 依赖未配置/未接入
- ``status=unsupported`` 工具本身不支持该请求

``source_kind`` 另行表示来源真实性（tencent_cloud/ssh/manual/demo/...）。
demo 数据永远不能支撑真实观测结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

TOOL_STATUSES = frozenset({
    "ok", "empty", "partial", "error", "timeout", "unavailable", "unsupported",
})

NON_LIVE_SOURCES = frozenset({
    "", "unknown", "unspecified", "demo", "unavailable", "unsupported", "manual",
})

#: 文本型工具返回里表示"这次调用失败"的前缀。
#:
#: 进程内工具（`get_current_time` 等）返回字符串；仓库内所有文本型失败返回都
#: 以这些词开头（见 app/tools/*.py 的 `return f"获取xxx失败: ..."`）。用前缀
#: 而不是"包含关键词"，避免把正文里恰好提到"失败"的正常结果判成错误。
_TEXT_ERROR_PREFIXES = (
    "获取时间失败",
    "获取当前时间失败",
    "error",
    "Error",
    "ERROR",
)

#: 文本型失败的其他常见形态：``<动作>失败: <原因>``（冒号分隔）。
_TEXT_ERROR_MARKERS = ("失败:", "错误:", "异常:", "refused", "timed out", "timeout")


def _looks_like_text_error(text: str) -> bool:
    """文本是否像一次失败返回。

    判据是"简短 + 带失败标记"，两条同时满足才算：正常结果（时间戳、一段说明）
    通常不带这些标记，而 ``工具执行失败: connection refused`` 这类失败返回既短
    又带标记。宁可漏判成正常（下游还能看到原文），也不要误判成功为失败——
    误判会让证据表出现无理由的"不可用"，正是这次要修的问题。
    """
    if len(text) > 200:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _TEXT_ERROR_MARKERS)


@dataclass
class ToolResult:
    """工具调用的规范化结果。"""

    status: str
    source_kind: str = "unknown"
    data: Any = None
    error: Mapping[str, Any] | None = None
    target: str = ""
    time_range: Mapping[str, Any] | None = None
    subqueries: list[dict[str, Any]] = field(default_factory=list)
    demo: bool = False

    def __post_init__(self) -> None:
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"未知的工具状态: {self.status}")
        if self.demo and "demo" not in self.source_kind.split(","):
            self.source_kind = f"{self.source_kind},demo" if self.source_kind else "demo"

    @property
    def live(self) -> bool:
        """是否能支撑真实观测结论。"""
        return self.status in {"ok", "empty", "partial"} and not self.demo and not (
            set(self.source_kind.split(",")) & NON_LIVE_SOURCES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_kind": self.source_kind,
            "data": self.data,
            "error": dict(self.error) if self.error else None,
            "target": self.target,
            "time_range": dict(self.time_range) if self.time_range else None,
            "subqueries": list(self.subqueries),
            "demo": self.demo,
            "live": self.live,
        }


ErrorCode = str

#: 从底层 payload 抽取错误代码的已知键。
_ERROR_CODE_KEYS = ("code", "error_code", "ErrorCode", "Code")


def classify_payload(value: Any) -> ToolResult:
    """把 MCP 工具的原始返回归类成 ToolResult。

    原始缺陷（BE-02）：CLS 把底层 AccessDenied 包成 ``total=0, logs=[],
    "成功查询 0 条"``，上层因此把权限失败当成“没有错误日志”。
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ToolResult(status="empty", source_kind="unspecified", data=value)
        # 纯文本返回不一定是错误。进程内工具里有返回文本的正常工具
        # （`get_current_time` 返回 "2026-10-29 18:56:59"），旧实现把所有
        # 非空字符串都判成 error，于是时间戳被记成"工具不可用"、来源
        # unavailable，用户看到的证据表里就出现一条无理由的失败。
        #
        # 这里不靠猜语义，靠调用方已有的约定：MCP 工具一律返回 dict（ok/status/
        # source），纯文本只可能来自进程内工具，而它们的约定是"失败时返回带
        # 失败字样的文本"。因此只把明确像错误的文本判成 error——前缀匹配，
        # 覆盖本仓库所有文本型失败返回，不认识的一律当正常文本。
        if text.startswith(_TEXT_ERROR_PREFIXES) or _looks_like_text_error(text):
            return ToolResult(status="error", source_kind="unavailable",
                              data=value, error={"code": "text_error", "message": text[:300]})
        return ToolResult(status="ok", source_kind="unspecified", data=value)
    if not isinstance(value, dict):
        return ToolResult(status="ok" if value else "empty", data=value)

    explicit = value.get("status")
    source = _source(value)
    demo = _is_demo(value, source)

    if value.get("ok") is False or value.get("success") is False:
        return ToolResult(status="error", source_kind=source or "unavailable",
                          data=value, error=_error(value), demo=demo)
    if explicit in TOOL_STATUSES:
        return _result_for(explicit, value, source, demo)

    error = _error(value)
    if error and not _has_rows(value):
        # 有 error 且确实没有数据：这是失败，不是“零命中”。
        return ToolResult(status="error", source_kind=source or "unavailable",
                          data=value, error=error, demo=demo)
    if error:
        return ToolResult(status="partial", source_kind=source, data=value,
                          error=error, demo=demo,
                          subqueries=_subqueries(value))

    subqueries = _subqueries(value)
    if subqueries and any(not item.get("ok", True) for item in subqueries):
        return ToolResult(status="partial", source_kind=source, data=value,
                          error=_error(value) or {"code": "partial", "message": "部分子查询失败"},
                          demo=demo, subqueries=subqueries)
    if _is_empty_rows(value):
        return ToolResult(status="empty", source_kind=source or "unspecified",
                          data=value, demo=demo, subqueries=subqueries)
    if value.get("message") and _looks_like_unavailable(value):
        return ToolResult(status="unavailable", source_kind=source or "unavailable",
                          data=value, error={"code": "unavailable",
                                             "message": str(value.get("message"))[:300]})
    return ToolResult(status="ok", source_kind=source or "unspecified", data=value,
                      demo=demo, subqueries=subqueries)


def _result_for(status: str, value: dict, source: str, demo: bool) -> ToolResult:
    error = _error(value) if status in {"error", "partial", "timeout", "unavailable",
                                        "unsupported"} else None
    return ToolResult(status=status, source_kind=source or "unspecified", data=value,
                      error=error, demo=demo, subqueries=_subqueries(value))


def _source(value: dict) -> str:
    raw = value.get("source") or value.get("source_kind") or ""
    if isinstance(raw, (list, tuple, set)):
        return ",".join(str(item) for item in raw)
    return str(raw)


def _is_demo(value: dict, source: str) -> bool:
    if value.get("demo_mode") is True or value.get("demo") is True:
        return True
    return "demo" in {part.strip() for part in source.split(",")}


def _error(value: dict) -> dict[str, Any] | None:
    error = value.get("error")
    if isinstance(error, dict):
        payload = dict(error)
    elif error:
        payload = {"message": str(error)[:500]}
    else:
        return None
    payload.setdefault("code", _error_code(value) or "tool_error")
    payload.setdefault("retryable", False)
    return payload


def _error_code(value: dict) -> str:
    for key in _ERROR_CODE_KEYS:
        if value.get(key):
            return str(value[key])[:80]
    return ""


def _has_rows(value: dict) -> bool:
    for key in ("logs", "tickets", "data_points", "entries", "services", "topics", "rows"):
        items = value.get(key)
        if isinstance(items, list) and items:
            return True
    return False


def _is_empty_rows(value: dict) -> bool:
    """只在“确实做了查询且得到零条”时成立。"""
    for key in ("logs", "tickets", "data_points", "entries", "rows"):
        if key in value and value[key] == []:
            return True
    return False


def _subqueries(value: dict) -> list[dict[str, Any]]:
    raw = value.get("subqueries") or value.get("sub_queries") or []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _looks_like_unavailable(value: dict) -> bool:
    message = str(value.get("message") or "")
    return "未配置" in message or "不可用" in message or "unavailable" in message.lower()
