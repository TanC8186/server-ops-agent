"""请求级模式路由：不修改进程全局模型配置或复用其他运行的状态。

生命周期不变式（回归见 ``tests/test_p0_cloud_pipeline.py``）：

- 每次 run 在 trace 中恰好有一条终态生命周期记录：``run_complete`` 与
  ``run_error`` 互斥，不会同时出现。其它记录（``runtime_error_event``、
  ``source_close_error`` 等）都是过程记录，不参与终态判定。
- 入站 ``type=error`` 事件改名记为 ``runtime_error_event``，只保留错误上下文；
  入站事件名若与终态同名（``run_complete`` / ``run_error``）也统一加
  ``runtime_`` 前缀。终态与对外错误详情只由本模块的外层 ``except`` 写出，
  避免同一次失败被计两次。
- 事件源的 ``aclose()`` 失败只记 ``source_close_error`` 并吞掉异常，绝不替换
  或复制已经交付给消费者的终态事件。取消（``CancelledError``）仍照常传播。
- 目标歧义（``target_required``）不是失败：两种执行模式都发一条
  ``target_required``（候选 + 原因）后用 ``complete`` 结束，``status`` 为
  ``target_required``、lifecycle=completed、quality=insufficient。终态语义
  集中在本模块，避免 local / cloud_multi 各写一份而分叉（BE-03）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from typing import Any

from app.config import config
from app.core.budgets import AdmissionController, AdmissionRejected, RequestBudget
from app.core.events import normalize_event, trace_fields
from app.core.run_status import evaluate_quality
from app.core.targets import TargetResolution, target_candidate
from app.observability import trace_event

#: 进程级准入闸门：所有诊断请求共享同一个实例（配置在首次使用时读取）。
_ADMISSION: AdmissionController | None = None


def admission_controller() -> AdmissionController:
    """返回进程级准入控制器（懒建，读取 Settings 当前值）。"""
    global _ADMISSION
    if _ADMISSION is None:
        _ADMISSION = AdmissionController(
            config.max_concurrent_diagnostics,
            config.admission_queue_timeout_seconds,
        )
    return _ADMISSION


def reset_admission_controller() -> None:
    """测试用：清空单例，让下一次调用按新配置重建。"""
    global _ADMISSION
    _ADMISSION = None

#: 终态生命周期记录名；一次 run 只允许出现其中一个且仅一条。
TERMINAL_TRACE_EVENTS = frozenset({"run_complete", "run_error"})

#: ``target_required`` 事件最多携带的候选数；超出部分不发给前端，避免事件膨胀。
MAX_TARGET_CANDIDATES = 10


def target_choice_value(holder: Any) -> str | None:
    """可选地读取 ``target_choice``（UI 选择后重跑时携带）；缺失时返回 None。

    支持三种形态：字符串本身、带 ``target_choice`` 属性的请求对象、包含该键
    的映射（图状态）。这里只保证“没有这个字段”时按目标歧义处理，不抛异常。
    """
    if isinstance(holder, str):
        value: Any = holder
    else:
        value = getattr(holder, "target_choice", None)
        if value is None and isinstance(holder, Mapping):
            value = holder.get("target_choice")
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def target_candidates(resolution: TargetResolution, *, limit: int = MAX_TARGET_CANDIDATES) -> list[dict]:
    """把候选目标转成事件载荷：最多 ``limit`` 个，带稳定候选 ID。

    A04：候选必须保留“同名多实例”的每一个，并带上服务端签发的 ``target_id``；
    只给服务名会让 UI 无法把用户的选择绑回具体实例。
    """
    return [target_candidate(target) for target in list(resolution.candidates)[:limit]]


def render_target_required(resolution: TargetResolution) -> str:
    """目标歧义的 Markdown 说明：用户没有做错，也不是诊断失败。"""
    candidates = target_candidates(resolution)
    reason = resolution.reason or "需要选择诊断目标"
    lines = [
        "# 需要选择诊断目标",
        "",
        f"本次请求没有指定唯一的服务：{reason}。",
        "",
        "为避免把其他服务的证据归到目标服务，**本次没有调用任何指标或日志查询工具**。"
        "这不是故障；选择目标后重新发起诊断即可。",
        "",
        "## 候选服务",
        "",
        "| 服务 | 实例 | 地域 |",
        "| --- | --- | --- |",
    ]
    for item in candidates:
        cells = [
            item.get("service") or "",
            item.get("instance_id") or "未声明",
            item.get("region") or "未声明",
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    if not candidates:
        lines.append("| — | — | — |")
    if len(resolution.candidates) > len(candidates):
        lines += ["", f"仅显示前 {len(candidates)} 个候选，可通过服务名精确指定。"]
    lines += [
        "",
        "## 如何继续",
        "",
        "请选择本次诊断的目标服务后重新发起诊断；诊断只会读取该服务及其绑定的实例、"
        "地域和日志主题。",
    ]
    return "\n".join(lines)


def target_required_outcome(
    resolution: TargetResolution, evidence: list[dict],
) -> tuple[dict, dict]:
    """构造 ``target_required`` 事件与唯一的 ``complete`` 终态事件。

    终态选择（两种模式一致）：发 ``complete`` 并带 ``status="target_required"``，
    而不是 ``error``——用户没有做错任何事，歧义只差一次选择；协议仍要求每次
    run 恰好一个终态，因此把说明放在 ``response`` 里，并用
    ``app.core.run_status.evaluate_quality`` 给出 lifecycle=completed、
    quality=insufficient（未绑定目标，证据不足以支撑结论）。
    """
    candidates = target_candidates(resolution)
    reason = resolution.reason or "需要选择诊断目标"
    quality = evaluate_quality(evidence, [reason], target_bound=False)
    required = {
        "type": "target_required",
        "stage": "target_selection",
        "message": reason,
        "candidates": candidates,
        "reason": reason,
    }
    complete = {
        "type": "complete",
        "stage": "target_required",
        "status": "target_required",
        "lifecycle": str(quality.lifecycle),
        "quality": str(quality.quality),
        "candidates": candidates,
        "reason": reason,
        "response": render_target_required(resolution),
    }
    return required, complete


class _Done:
    """事件源结束标记（``None`` 也可能是合法事件，不能用它判断）。"""


_DONE = _Done()


async def _bounded(source, deadline: float):
    """按 deadline 截断事件源；超时抛 TimeoutError，由外层转成唯一终态。

    ``asyncio.timeout`` 只能中断 await 点，如果运行器把工作丢进无法取消的同步
    线程，外层 timeout 会一直等到线程结束。这里把等待切成按剩余时间的
    ``wait_for``，让超时至少能在事件边界上生效，并把预算一并暴露给运行器。
    """
    iterator = source.__aiter__()

    async def _next():
        try:
            return await iterator.__anext__()
        except StopAsyncIteration:
            return _DONE

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("诊断达到时间预算")
        try:
            item = await asyncio.wait_for(_next(), remaining)
        except TimeoutError as exc:
            raise TimeoutError("诊断达到时间预算") from exc
        if item is _DONE:
            return
        yield item


def _incoming_trace_name(kind: str) -> str:
    """入站事件落到 trace 时的名字。

    入站 error 与任何与终态同名的入站事件都必须改名：否则伪造的
    ``type=run_error`` 会与本模块写出的终态记录一起，让一次失败被计两次。
    """
    if kind == "error":
        return "runtime_error_event"
    if kind in TERMINAL_TRACE_EVENTS:
        return f"runtime_{kind}_event"
    return kind


async def _close_source(source, run_id: str, execution_mode: str) -> None:
    """关闭事件源；失败只落一条 trace，不能覆盖或复制已交付的终态事件。"""
    try:
        await source.aclose()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 关闭失败不能让终态事件变形
        trace_event(run_id, "source_close_error", error=type(exc).__name__,
                    message=str(exc)[:500], execution_mode=execution_mode)


async def run_diagnostic(user_input: str, session_id: str, execution_mode: str,
                         *, service_name: str | None = None,
                         target_choice: str | None = None,
                         target_candidate_id: str | None = None,
                         request_context: dict | None = None):
    """执行一次诊断并流式产出事件。

    ``target_choice`` 是用户在 ``target_required`` 候选卡片里的显式选择；它与
    ``service_name`` 指向同一个目标目录绑定字段（后者优先），因此这里原样转发
    给两种运行器，由它们在绑定前统一归一化。``request_context`` 只是协议预留，
    其中的 ``target_choice`` 会在传入的 ``target_choice`` 为空时兜底。
    """
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    # 硬 deadline 同时给运行器和工具：模型/SSH 子调用不能超过它。
    deadline = started + float(config.diagnostic_timeout_seconds)
    budget = RequestBudget.from_config(config)
    trace_event(run_id, "run_start", session_id=session_id, execution_mode=execution_mode,
                input_chars=len(user_input), deadline_s=config.diagnostic_timeout_seconds)
    # 显式形参优先；请求上下文里的同名字段只做兜底，避免两处选择冲突。
    choice = target_choice_value(target_choice) or target_choice_value(request_context)
    candidate = target_choice_value(target_candidate_id) or target_choice_value(
        request_context.get("target_candidate_id") if isinstance(request_context, Mapping) else None
    )

    def envelope(event: dict, seq: int) -> dict:
        return normalize_event(event, run_id=run_id, session_id=session_id,
                               execution_mode=execution_mode, seq=seq)

    source = None
    seq = 0
    try:
        if execution_mode == "local":
            if not config.local_llm_base_url.strip():
                raise ValueError("本地模型尚未配置，请设置 LOCAL_LLM_BASE_URL 并启动模型服务")
            from app.agent.aiops.local_runtime import run_local
            source = run_local(user_input, session_id, run_id, service_name=service_name,
                               target_choice=choice, target_candidate_id=candidate,
                               deadline=deadline, budget=budget)
        elif execution_mode == "cloud_multi":
            if not config.llm_api_key.strip() or config.llm_api_key.startswith("your-"):
                raise ValueError("在线模型尚未配置，请在服务端设置 LLM_API_KEY")
            from app.agent.aiops.cloud_multi import run_cloud_multi
            source = run_cloud_multi(user_input, session_id, run_id, service_name=service_name,
                                     target_choice=choice, target_candidate_id=candidate,
                                     deadline=deadline, budget=budget)
        else:
            raise ValueError("不支持的诊断模式")

        seq += 1
        yield envelope({"type": "status", "stage": "run_start", "message": "诊断已开始"}, seq)
        complete = False
        async with admission_controller():
            async for event in _bounded(source, deadline):
                seq += 1
                incoming = envelope(event, seq)
                kind = incoming["type"]
                if kind == "complete":
                    if not str(incoming.get("response", "")).strip():
                        raise ValueError("诊断未生成报告，已按失败处理")
                    complete = True
                    duration = round((time.monotonic() - started) * 1000)
                    incoming["duration_ms"] = duration
                    incoming["payload"] = {**incoming["payload"], "duration_ms": duration}
                    trace_event(run_id, "run_complete", duration_ms=duration,
                                response_chars=len(str(incoming["response"])),
                                execution_mode=execution_mode,
                                lifecycle=str(incoming.get("lifecycle") or "completed"),
                                quality=incoming.get("quality"),
                                budget=budget.snapshot())
                elif kind == "report":
                    trace_event(run_id, "report", result_preview=str(incoming.get("report", "")),
                                execution_mode=execution_mode)
                elif kind == "error":
                    # 入站 error 只是过程记录，改名避免与终态 run_error 重复计数；
                    # 唯一的终态生命周期记录由外层 except 写出。
                    _, fields = trace_fields(incoming)
                    trace_event(run_id, "runtime_error_event", **fields)
                    raise ValueError(str(incoming.get("message") or "诊断执行失败"))
                else:
                    _, fields = trace_fields(incoming)
                    trace_event(run_id, _incoming_trace_name(kind), **fields)
                yield incoming
                if complete:
                    break
        if not complete:
            raise ValueError("诊断提前结束，未收到完成事件")
    except asyncio.CancelledError:
        trace_event(run_id, "run_error", message="客户端取消诊断", execution_mode=execution_mode,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    lifecycle="cancelled")
        raise
    except AdmissionRejected as exc:
        # 排队超限是资源约束，不是诊断失败：保留 retry_after 供调用方退避。
        trace_event(run_id, "run_error", message=str(exc), execution_mode=execution_mode,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    lifecycle="failed", retry_after=exc.retry_after)
        seq += 1
        yield envelope({"type": "error", "message": str(exc),
                        "retryable": True, "retry_after": exc.retry_after,
                        "code": "admission_rejected"}, seq)
    except Exception as exc:
        message = "诊断达到时间预算，已停止" if isinstance(exc, TimeoutError) else str(exc)
        trace_event(run_id, "run_error", message=message, execution_mode=execution_mode,
                    duration_ms=round((time.monotonic() - started) * 1000))
        seq += 1
        yield envelope({"type": "error", "message": message}, seq)
    finally:
        if source is not None:
            # 关闭失败只落 trace：此时终态事件可能已经交付给消费者，
            # 不能再抛出异常替换它或让 Web 层追加第二个 error 事件。
            await _close_source(source, run_id, execution_mode)
