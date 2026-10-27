"""运行上下文：把一次诊断的受控证据摘要提供给知识问答的追问（M4 / RAG-10）。

设计约束（对应审查 RAG-10）：

- **归属校验**：只允许同一 ``session_id`` 读取该 run；跨会话/跨用户一律返回
  ``None``，不串线。run_id 必须是服务端生成的 32 位十六进制，避免路径探测。
- **principal 归属（G3-3）**：``run_start`` 会写入 ``owner``（principal_id）。
  记录了 owner 时，除了 session 还要 ``principal_id`` 相符；旧 trace 没有
  owner 的，退化为“只校验 session”，不臆造归属。见
  :func:`run_owner` / :func:`owner_matches`。
- **受控摘要**：只暴露结论所需的字段（目标、时间窗、观测、假设、缺口、质量），
  不把整份 tool 原始 payload 塞回模型。
- **只读**：本模块不写 trace，也不修改 run。

trace 的结构由 ``app.observability`` 与 ``app.services.diagnostic_runtime`` 决定：
``run_start`` 带 session_id，``target_bound`` 带目标，``agent_result``（Reviewer）
带 lifecycle/quality，``run_complete`` 是终态。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from app.observability import TRACE_DIR

#: 服务端唯一生成的 run_id 形态。
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: 摘要里每类条目最多保留的数量（避免把整轮证据塞进 prompt）。
MAX_ITEMS = 8
#: 单条摘要文本的字符上限。
MAX_ITEM_CHARS = 300


def _safe_trace_path(run_id: str) -> Path | None:
    """run_id -> trace 文件；格式非法或越界时返回 None。"""
    if not RUN_ID_PATTERN.fullmatch(run_id or ""):
        return None
    try:
        base = Path(TRACE_DIR).resolve()
        candidate = (base / f"{run_id}.jsonl").resolve()
    except OSError:
        return None
    if candidate.parent != base:
        return None
    return candidate


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _owner_of(started: dict[str, Any]) -> str | None:
    """从 ``run_start`` 取 principal 归属；缺失/空/非字符串一律 ``None``。"""
    owner = started.get("owner") if isinstance(started, dict) else None
    if isinstance(owner, str) and owner.strip():
        return owner.strip()
    return None


def _clip(value: Any, limit: int = MAX_ITEM_CHARS) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit]


def _summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """从 trace 事件提炼受控摘要；缺失字段用空值而不是编造。"""
    started = next((e for e in events if e.get("event") == "run_start"), {})
    terminal = next(
        (e for e in reversed(events) if e.get("event") in {"run_complete", "run_error"}),
        {},
    )
    target: dict[str, Any] | None = None
    observations: list[str] = []
    hypotheses: list[str] = []
    gaps: list[str] = []
    time_window: dict[str, Any] | None = None

    for event in events:
        name = event.get("event")
        if name in {"target_bound", "status"} and isinstance(event.get("target"), dict):
            target = event["target"]
        if name == "run_start" and isinstance(event.get("target"), dict) and target is None:
            target = event["target"]
        if name == "evidence" and isinstance(event.get("evidence"), dict):
            data = event["evidence"].get("data")
            if isinstance(data, dict):
                window = data.get("time_range")
                if isinstance(window, dict) and window.get("start_time") is not None:
                    time_window = {
                        "start_time": window.get("start_time"),
                        "end_time": window.get("end_time"),
                    }
                for key in ("partial_errors", "errors"):
                    values = data.get(key)
                    if isinstance(values, list):
                        gaps.extend(_clip(item) for item in values[:MAX_ITEMS])
        if name == "agent_result" and event.get("agent") == "reviewer":
            observations.extend(_clip(item) for item in (event.get("observations") or [])[:MAX_ITEMS])
            hypotheses.extend(_clip(item) for item in (event.get("hypotheses") or [])[:MAX_ITEMS])
            gaps.extend(_clip(item) for item in (event.get("gaps") or [])[:MAX_ITEMS])
        if name == "runtime_error_event":
            gaps.append(_clip(event.get("message")))
        if name == "run_error":
            gaps.append(_clip(event.get("message")))

    quality = terminal.get("quality")
    lifecycle = terminal.get("lifecycle") or (
        "completed" if terminal.get("event") == "run_complete" else
        ("failed" if terminal else None)
    )
    return {
        "run_id": started.get("run_id") or terminal.get("run_id") or "",
        "session_id": started.get("session_id"),
        #: 创建该 run 的 principal（旧 trace 没有，为 None）。
        "owner": _owner_of(started),
        "execution_mode": started.get("execution_mode"),
        "target": target,
        "time_window": time_window,
        "observations": [item for item in observations if item][:MAX_ITEMS],
        "hypotheses": [item for item in hypotheses if item][:MAX_ITEMS],
        "gaps": list(dict.fromkeys(item for item in gaps if item))[:MAX_ITEMS],
        "quality": quality if isinstance(quality, str) else None,
        "lifecycle": lifecycle,
    }


def run_owner(run_id: str) -> str | None:
    """读取 trace 里 ``run_start`` 记录的 principal 归属；没有记录返回 ``None``。

    只读 ``run_start``：这是创建时写入的一次，不会被后续事件覆盖，也不会
    因为运行结束/清理而丢失（清理是把整份文件删掉，不是改 owner）。
    """
    path = _safe_trace_path(run_id)
    if path is None or not path.is_file():
        return None
    for event in _read_events(path):
        if event.get("event") == "run_start":
            return _owner_of(event)
    return None


def owner_matches(recorded_owner: str | None, principal_id: str | None) -> bool:
    """``recorded_owner`` 是否允许 ``principal_id`` 访问。

    没有记录 owner（旧数据）→ 允许（不臆造归属）；记录了就必须相等。
    """
    owner = str(recorded_owner or "").strip()
    if not owner:
        return True
    return owner == str(principal_id or "").strip()


def load_run_context(run_id: str, session_id: str,
                     principal_id: str | None = None) -> dict[str, Any] | None:
    """加载某次运行的受控摘要；归属不符或不存在时返回 ``None``。

    参数 ``session_id`` 是**当前请求**的会话；只有 trace 里记录的会话与它相同
    才允许读取。``principal_id`` 可选：trace 记录了 owner 时还必须相符，
    未记录时保持旧的“同会话即可”行为（存在明确边界，不假装有归属）。
    调用方不得把 ``None`` 当成“没有上下文”以外的含义。
    """
    path = _safe_trace_path(run_id)
    if path is None or not path.is_file():
        return None
    events = _read_events(path)
    if not events:
        return None
    summary = _summarize(events)
    owner = summary.get("session_id")
    if not owner or str(owner) != str(session_id or ""):
        return None
    if principal_id is not None and not owner_matches(summary.get("owner"), principal_id):
        return None
    summary["run_id"] = run_id
    return summary


async def load_run_context_async(run_id: str, session_id: str,
                                 principal_id: str | None = None
                                 ) -> dict[str, Any] | None:
    """异步包装：trace 读取放到线程，避免阻塞事件循环。"""
    return await asyncio.to_thread(load_run_context, run_id, session_id, principal_id)


def render_run_context(context: dict[str, Any] | None) -> str:
    """把摘要渲染成给模型看的短文本；没有上下文时返回空串。"""
    if not context:
        return ""
    lines = ["以下是用户追问的那次诊断的受控摘要（历史观测，不代表当前时刻）："]
    target = context.get("target") or {}
    if isinstance(target, dict) and target.get("service"):
        lines.append(f"- 目标服务: {target.get('service')}（实例 {target.get('instance_id') or '未声明'}）")
    if context.get("quality") or context.get("lifecycle"):
        lines.append(f"- 运行状态: {context.get('lifecycle') or '未知'} / 质量 {context.get('quality') or '未知'}")
    for label, key in (("观测", "observations"), ("假设", "hypotheses"), ("缺口", "gaps")):
        items = [item for item in (context.get(key) or []) if item]
        if not items:
            continue
        lines.append(f"- {label}:")
        lines.extend(f"  - {item}" for item in items[:MAX_ITEMS])
    lines.append("规则：这些是当时采集到的证据，不能证明此刻的状态；不要把它当作知识库手册内容。")
    return "\n".join(lines)
