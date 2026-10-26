"""运行追踪与评测结果查询接口。

读取 `logs/traces/*.jsonl` 和 `eval/results/*.json`，让前端可以展示：
- 最近一次诊断的节点/工具/决策/token 明细
- 历史评测的指标表

安全边界：run_id 由服务端生成，格式固定为 32 位小写十六进制。任何不符合该
格式的值（编码反斜杠、点段、绝对路径、盘符、UNC）在拼接路径前就被拒绝；
拼接后再做一次 resolve 后的目录包含校验，防止符号链接/重解析点逃逸。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

ROOT = Path(__file__).resolve().parents[2]
TRACE_DIR = ROOT / "logs" / "traces"
RESULT_DIR = ROOT / "eval" / "results"

#: 服务端唯一生成格式：uuid4().hex
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: 单次回放的默认/最大事件数，避免把超大 trace 全量返回。
DEFAULT_EVENT_LIMIT = 2000
MAX_EVENT_LIMIT = 20000

#: 没有终态记录、且文件最后修改早于该秒数的运行判为 ``abandoned``。
#:
#: 为什么需要它：进程被强杀/容器被重启时，trace 里不会写 ``run_complete`` 或
#: ``run_error``，这次运行会永远停在 ``status=running``。把它如实标成
#: ``abandoned``（而不是继续显示“进行中”，也不是假装成功）比补一条编造的终态
#: 更诚实。阈值默认 1 小时，可用 ``RUN_ABANDONED_AFTER_SECONDS`` 覆盖。
DEFAULT_ABANDONED_AFTER_SECONDS = 3600
ABANDONED_AFTER_ENV = "RUN_ABANDONED_AFTER_SECONDS"


#: trace 保留天数；0 表示不自动清理。``TRACE_RETENTION_DAYS`` 可覆盖。
DEFAULT_TRACE_RETENTION_DAYS = 30
RETENTION_DAYS_ENV = "TRACE_RETENTION_DAYS"

#: 单次清理最多删除的文件数与每批字节上限（避免一次删太多影响磁盘）。
RETENTION_MAX_FILES_PER_RUN = 200


def _coerce_index(value: object, default: int = 0) -> int:
    """把查询参数收敛成正整数。

    直接调用 ``get_run(...)``（探针、脚本、内部调用）时，FastAPI 的
    ``Query(...)`` 默认对象不会被解析，``events[after_event:]`` 会因切片参数
    不是整数而 TypeError。HTTP 路径本来就已由 FastAPI 校验，这里只做一次
    "能当整数就用、否则用默认值"的兜底，让两种调用方式行为一致。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def abandoned_after_seconds() -> float:
    """多久没有终态的 run 判为 abandoned（秒）。非法值回退到默认。"""
    import os

    raw = os.environ.get(ABANDONED_AFTER_ENV, "").strip()
    if not raw:
        return float(DEFAULT_ABANDONED_AFTER_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_ABANDONED_AFTER_SECONDS)
    return value if value > 0 else float(DEFAULT_ABANDONED_AFTER_SECONDS)


def retention_days() -> float:
    """trace 保留天数；``0`` 表示关闭自动清理。非法值回退到默认。"""
    import os

    raw = os.environ.get(RETENTION_DAYS_ENV, "").strip()
    if not raw:
        return float(DEFAULT_TRACE_RETENTION_DAYS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TRACE_RETENTION_DAYS)
    return max(0.0, value)


def purge_expired_traces(*, now: float | None = None) -> dict:
    """按保留策略删除过期 trace 文件，返回清理统计（可审计、可重跑）。

    只删 `logs/traces/*.jsonl` 里通过 run_id 格式与包含性校验的普通文件；
    越界条目、非本目录文件、符号链接逃逸一律跳过。删除失败逐条记录，不抛异常
    （清理是运维动作，不能影响 API 可用性）。
    """
    import os

    days = retention_days()
    if days <= 0:
        return {"enabled": False, "deleted": 0, "failed": 0, "scanned": 0}
    cutoff = (now if now is not None else time.time()) - days * 86400
    deleted = failed = scanned = 0
    if not TRACE_DIR.exists():
        return {"enabled": True, "deleted": 0, "failed": 0, "scanned": 0}
    for candidate in list(TRACE_DIR.glob("*.jsonl"))[:RETENTION_MAX_FILES_PER_RUN]:
        if not is_valid_run_id(candidate.stem):
            continue
        resolved = resolve_trace_file(candidate)
        if resolved is None or not resolved.is_file():
            continue
        scanned += 1
        if _safe_mtime(resolved) >= cutoff:
            continue
        try:
            os.remove(resolved)
            deleted += 1
        except OSError:
            failed += 1
    return {"enabled": True, "deleted": deleted, "failed": failed, "scanned": scanned}


def is_valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_PATTERN.fullmatch(run_id or ""))


def resolve_trace_file(candidate: Path, base: Path | None = None) -> Path | None:
    """把候选路径解析为真实路径，并确认它仍直接位于 traces 目录内。

    越界（符号链接/junction 重解析点、`..`、盘符路径等）或解析失败时返回
    ``None``，调用方据此跳过或抛 404；不把底层异常暴露给客户端。
    """
    try:
        base_dir = (base if base is not None else TRACE_DIR).resolve()
        resolved = candidate.resolve()
    except OSError:
        return None
    # 必须直接落在 traces 内，不能借道任何子目录或重解析点逃逸。
    if resolved.parent != base_dir:
        return None
    return resolved


def trace_path(run_id: str) -> Path:
    """把 run_id 映射为 traces 目录内的文件；任何越界形式都返回 404。"""
    if not is_valid_run_id(run_id):
        # 不区分“格式非法”和“不存在”，避免把目录结构当成探测信号。
        raise HTTPException(status_code=404, detail="run not found")
    path = resolve_trace_file(TRACE_DIR / f"{run_id}.jsonl")
    if path is None:
        raise HTTPException(status_code=404, detail="run not found")
    return path


@router.get("/runs")
async def list_runs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """列出最近的诊断运行（按文件修改时间倒序）。"""
    if not TRACE_DIR.exists():
        return {"runs": []}
    files = []
    for candidate in TRACE_DIR.glob("*.jsonl"):
        if not is_valid_run_id(candidate.stem):
            continue
        # 与详情接口共用同一套包含性校验：越界条目直接跳过，而不是 500。
        resolved = resolve_trace_file(candidate)
        if resolved is None or not resolved.is_file():
            continue
        files.append(resolved)
    files.sort(key=_safe_mtime, reverse=True)
    files = files[:limit]

    runs = []
    for path in files:
        events = await asyncio.to_thread(_read_jsonl, path)
        runs.append(_summarize_run(path.stem, events, mtime=_safe_mtime(path)))
    return {"runs": runs}


#: trace 中没有终态记录时，运行列表用的占位状态（保持旧契约）。
_TERMINAL_EVENTS = frozenset({"run_complete", "run_error"})


def _summarize_run(run_id: str, events: list[dict],
                   mtime: float | None = None) -> dict:
    """把一次 run 的 trace 压缩成列表一行。

    ``lifecycle`` / ``quality`` 只从**终态记录**（run_complete / run_error）读取，
    没有终态时返回 null——不能用“报告生成成功”推断运行完成，也不能把
    ``success`` 拆成两套互相矛盾的布尔值（BE-05 / BE-08）。

    唯一例外是 **abandoned**：没有终态、且文件已静默超过保留阈值的运行，说明
    进程在写终态前就消失了。这时如实标成 ``abandoned``，而不是让列表永远显示
    “进行中”，也不是补一条编造的成功终态。

    ``has_report`` 是“报告正文是否落盘”的事实，与证据质量（quality）无关。
    """
    start = next((e for e in events if e.get("event") == "run_start"), {})
    end = next(
        (e for e in reversed(events) if e.get("event") in _TERMINAL_EVENTS),
        {},
    )
    plan = next((e for e in events if e.get("event") in {"plan", "agent_plan"}), {})
    route = next((e for e in events if e.get("event") == "planner_route"), {})
    steps = [e for e in events if e.get("event") == "tool_step"]
    response_chars = end.get("response_chars")
    has_report = (
        any(
            e.get("event") == "report" and str(e.get("result_preview") or "").strip()
            for e in events
        )
        or (isinstance(response_chars, int) and not isinstance(response_chars, bool)
            and response_chars > 0)
    )
    lifecycle = _lifecycle_of(end)
    abandoned = False
    if lifecycle is None and mtime is not None:
        quiet_for = time.time() - mtime
        if quiet_for >= abandoned_after_seconds():
            lifecycle = "abandoned"
            abandoned = True

    return {
        "run_id": run_id,
        "session_id": start.get("session_id"),
        "execution_mode": start.get("execution_mode"),
        "started_at": start.get("ts"),
        "duration_ms": end.get("duration_ms"),
        "lifecycle": lifecycle,
        "quality": _quality_of(end),
        "status": _status_of(end, lifecycle),
        "degraded": _degraded_of(end, lifecycle),
        #: 明确区分“进程在写终态前消失”与“仍在运行”。
        "abandoned": abandoned,
        "steps": len(steps),
        "plan_steps": len(plan.get("steps", []) or plan.get("assignments", [])),
        "target": _target_of(events),
        "has_report": has_report,
        "backend": route.get(
            "backend", "local" if start.get("execution_mode") == "local" else "cloud"
        ),
        "severity": route.get("severity") or "",
        "category": route.get("category") or "",
        "input_tokens": sum(e.get("input_tokens") or 0 for e in events),
        "output_tokens": sum(e.get("output_tokens") or 0 for e in events),
    }


def _lifecycle_of(end: dict) -> str | None:
    """终态记录 -> 生命周期；记录自带 lifecycle 时以它为准。"""
    if not end:
        return None
    recorded = end.get("lifecycle")
    if isinstance(recorded, str) and recorded.strip():
        return recorded
    if end.get("event") == "run_complete":
        return "completed"
    if end.get("event") == "run_error":
        message = str(end.get("message") or "")
        return "cancelled" if "取消" in message else "failed"
    return None


def _status_of(end: dict, lifecycle: str | None) -> str:
    """兼容字段 ``status``：由终态/生命周期推导，不另行计算。

    契约只允许 ``ok|error|cancelled|abandoned``。诊断质量不在这里表达——
    它有自己的 ``quality`` 字段与 ``degraded`` 布尔，混进 status 会让
    “跑完但证据不足”被读成失败。

    ``abandoned`` 不是终态记录推导出来的（它来自“无终态且长时间静默”），所以
    必须先看 ``lifecycle``，不能因为 ``end`` 为空就一律返回 ``running``。
    """
    if lifecycle == "abandoned":
        return "abandoned"
    if not end:
        return "running"
    return {
        "completed": "ok",
        "failed": "error",
        "cancelled": "cancelled",
        "abandoned": "abandoned",
    }.get(lifecycle or "", "ok")


def _degraded_of(end: dict, lifecycle: str | None) -> bool:
    """运行完成但质量不足以支撑结论时为 true（BE-05）。

    ``status`` 只回答“跑没跑完”，``degraded`` 回答“结论能不能用”。两者分开，
    前端/评测才不用猜。
    """
    if lifecycle != "completed":
        return False
    quality = _quality_of(end)
    return quality is not None and quality != "sufficient"


def _quality_of(end: dict) -> str | None:
    """诊断质量只能由服务端计算并写入终态；缺失就是未知（null）。"""
    if not end:
        return None
    quality = end.get("quality")
    return quality if isinstance(quality, str) and quality.strip() else None


def _target_of(events: list[dict]) -> dict | None:
    """取本次 run 绑定的目标；未绑定（如 target_required）返回 null。"""
    for event in events:
        if event.get("event") == "target_bound" or (
            event.get("event") == "status" and event.get("stage") == "target_bound"
        ):
            target = event.get("target")
            if isinstance(target, dict):
                return target
        # run_start 也可能带目标（由调用方显式指定）。
        if event.get("event") == "run_start":
            target = event.get("target")
            if isinstance(target, dict):
                return target
    return None


def _usable_seq(event: dict) -> int | None:
    """记录的续读用 ``seq``；非整数（含 bool、缺失、字符串）一律视为不可用。"""
    seq = event.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    return None


def _after_seq(events: list[dict], after_seq: int) -> list[dict]:
    """按位置续读：保留 after_seq 之后的所有记录，包括没有 seq 的。

    trace 里终态（``run_complete``）与部分过程记录不带 ``seq``，按“seq 大于
    after_seq”过滤会把它们永久丢掉——前端续读到最后永远看不到终态。这里改成
    按文件顺序找最后一个 ``seq <= after_seq`` 的位置，返回它之后的所有记录：
    没有 seq 的记录仍按真实写入顺序交付。
    """
    position = -1
    for index, event in enumerate(events):
        seq = _usable_seq(event)
        if seq is not None and seq <= after_seq:
            position = index
    return events[position + 1:]


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    after_event: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_EVENT_LIMIT, ge=1, le=MAX_EVENT_LIMIT),
) -> dict:
    path = trace_path(run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="run not found")
    events = await asyncio.to_thread(_read_jsonl, path)
    after_seq = _coerce_index(after_seq)
    after_event = _coerce_index(after_event)
    limit = _coerce_index(limit) or DEFAULT_EVENT_LIMIT
    #: 本次请求实际跳过的**绝对位置**。位置游标的续读必须从这里继续累加，
    #: 否则 seq→位置游标交接时会丢掉已跳过的距离，客户端在两页之间反复读取。
    total_events = len(events)
    skipped = 0
    if after_event:
        # 位置游标优先：它不依赖事件是否带 seq，是“无 seq 边界页”的唯一可靠续读方式。
        events = events[after_event:]
        skipped = min(after_event, total_events)
    elif after_seq:
        events = _after_seq(events, after_seq)
        # seq 游标切掉的前缀长度就是已跳过的绝对位置（按文件顺序）。
        skipped = total_events - len(events)
    selected = events[:limit]
    truncated = len(events) > limit
    next_after_seq: int | None = None
    next_after_event: int | None = None
    if truncated and _usable_seq(selected[-1]) is None:
        # 边界事件没有 seq，``next_after_seq`` 无从计算。两种收尾，都必须保证
        # “任意小分页能读到终态”，同时不能把整份 trace 一次性放大返回：
        # - 剩余量在 MAX_EVENT_LIMIT 以内：直接全部交付（truncated=false）；
        # - 否则：仍然截断，但改用**位置游标** ``after_event``（跳过前 N 条）。
        #   位置游标不依赖 seq，是这类页唯一可靠的续读方式。
        if len(events) <= MAX_EVENT_LIMIT:
            selected = events
            truncated = False
        else:
            selected = events[:MAX_EVENT_LIMIT]
            truncated = True
            # 绝对位置续读：本页末尾在**整份 trace** 中的位置是 skipped + 本页条数。
            # 必须带上 skipped，否则从 seq 游标切到位置游标的那一页会丢掉已读距离，
            # 客户端在两页之间反复读取（对抗复核实测 550k 事件、终态永不可达）。
            next_after_event = skipped + len(selected)
    elif truncated:
        # 边界事件带 seq：续读游标取本页最大的 seq（终态/错误记录可能不带 seq，
        # 用最后一条会返回 null，让前端误以为不能继续翻页）。
        seqs = [seq for seq in (_usable_seq(e) for e in selected) if seq is not None]
        next_after_seq = max(seqs) if seqs else None
        if next_after_seq is None:
            # 极端兜底：截断却没有可用 seq 游标时改用位置游标，绝不给死页面。
            next_after_event = skipped + len(selected)
    return {
        "run_id": path.stem,
        "events": selected,
        "truncated": truncated,
        "next_after_seq": next_after_seq,
        "next_after_event": next_after_event,
    }


@router.get("/eval/results")
async def list_eval_results(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    if not RESULT_DIR.exists():
        return {"results": []}
    files = sorted(
        RESULT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:limit]
    results = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # 新格式 {"manifest": {...}, "rows": [...]}；旧格式是裸列表。
        # manifest 让两份结果可以比较（换了模型/量化/场景子集），旧文件补 None。
        if isinstance(payload, dict):
            manifest = payload.get("manifest")
            rows = payload.get("rows")
        else:
            manifest = None
            rows = payload
        if not isinstance(rows, list):
            continue
        results.append(
            {
                "name": path.stem,
                "rows": rows,
                "manifest": manifest if isinstance(manifest, dict) else None,
                "mean_tool_f1": _mean([r.get("tool_f1") for r in rows]),
                "success_rate": _mean(
                    [1.0 if r.get("task_success") else 0.0 for r in rows]
                ),
                "total_tokens": sum(
                    (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
                    for r in rows
                ),
            }
        )
    return {"results": results}


def _safe_mtime(path: Path) -> float:
    """排序用的 mtime；文件在 glob 之后被删除时退化为 0 而不是抛异常。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_jsonl(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _mean(values: list) -> float:
    clean = [v for v in values if isinstance(v, (int, float))]
    return round(sum(clean) / len(clean), 3) if clean else 0.0
