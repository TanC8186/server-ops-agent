"""单本地规划模型 + 确定性只读执行与证据报告；不构造云 LLM。"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from app.core.run_status import evaluate_quality
from app.observability import trace_event
from app.services.diagnostic_runtime import target_required_outcome

from .local_planner import try_local_plan
from .runtime_tools import TargetRequiredError, create_run_tools, create_tools_compat

_LOCAL_SLOT = asyncio.Semaphore(1)


def _record_cancel(run_id: str, tools: Any) -> None:
    """取消后如实记录：已发出的调用无法回收，必须先标记不再发起新调用。"""
    if tools is None:
        return
    try:
        tools.cancel()
        trace_event(
            run_id, "run_cancelled",
            calls=tools.calls,
            evidence=len(tools.evidence),
            note="取消后不再发起新工具调用；已发出的调用只能等待其返回",
        )
    except Exception as exc:  # noqa: BLE001 - 记录失败不能影响关闭
        trace_event(run_id, "run_cancelled", error=type(exc).__name__)


def render_local_report(service: str, result: dict, evidence: list[dict]) -> str:
    rows = ["# 本地运维诊断报告", "", f"目标服务：`{service}`", "",
            f"模型分诊建议：{result.get('severity', 'unknown')} / {result.get('category', 'unknown')}",
            "", "此报告根据工具结果生成，分诊建议不作为已确认根因。", "",
            "| 证据 | 工具 | 状态 | 来源 |", "|---|---|---|---|"]
    for item in evidence:
        rows.append(f"| {item['id']} | {item['tool']} | {'可用' if item['ok'] else '不可用'} | {item['source']} |")
    rows += ["", "## 查询结果", ""]
    for item in evidence:
        rows += [f"### [{item['id']}] {item['tool']}", "",
                 f"查询范围：{item['target'] or '服务映射表'}", ""]
        if item["error"]:
            rows += [f"证据缺口：{item['error']}", ""]
        else:
            raw = json.dumps(item["data"], ensure_ascii=False, indent=2, default=str)
            raw = raw.replace("```", "｀｀｀")
            rows += ["```json", raw[:2500], "```", ""]
            if len(raw) > 2500:
                rows += ["展示内容已截取，完整结果保存在本次运行的 evidence trace 中。", ""]
    if any("demo" in item["source"] for item in evidence):
        rows += ["**本次包含合成演示数据，不能据此判断生产服务状态。**", ""]
    if any(item["target"] == "configured_ssh_endpoint" for item in evidence):
        rows += ["SSH 结果来自配置的远程主机，尚未验证其与目标服务的实例映射。", ""]
    useful = [e for e in evidence if e["ok"] and e["tool"] not in {"list_all_services", "get_current_time"}]
    if not useful:
        rows += ["## 结论", "", "证据不足，无法确认服务健康状态或故障根因。请检查监控、日志及目标配置。"]
    else:
        rows += ["## 后续排查", "", "对照以上证据核查告警时段、异常指标与日志；根因仍需验证。"]
        if any(not e["ok"] for e in evidence):
            rows += ["部分查询失败，需补齐对应证据后再做最终判断。"]
    return "\n".join(rows)


def assess_local_quality(evidence: list[dict], *, target_bound: bool,
                         extra_gaps: list[str] | None = None) -> dict:
    """本地模式的诊断质量：与云端同一套判定，不再只有“报告已生成”。

    本地模式此前只发 ``complete``，没有 lifecycle/quality，于是“所有工具都
    失败”也会在运行历史里显示 ok —— 这正是 BE-05 的反例。
    ``extra_gaps`` 用于带上参数收敛/证据截断说明（A07）。
    """
    gaps = [
        f"{item.get('tool') or '工具'} 未获取到可用数据（{item.get('status')}）: {item.get('error')}"
        for item in evidence
        if item.get("status") in {"error", "timeout", "unavailable", "unsupported"}
    ]
    gaps.extend(str(gap) for gap in (extra_gaps or []) if str(gap).strip())
    report = evaluate_quality(evidence, gaps, target_bound=target_bound)
    degraded = report.quality != "sufficient"
    return {
        "lifecycle": str(report.lifecycle),
        "quality": str(report.quality),
        "status": "degraded" if degraded else "completed",
        "reason": report.reason,
        "gaps": list(report.gaps),
        "tools": report.summary.to_dict() if report.summary else None,
    }


def _chosen_target(service_name: str | None, target_choice: str | None) -> str | None:
    """候选卡片里的显式选择优先，其次才是输入框里的服务名。

    ``target_choice`` 只由“用户点击候选卡片”产生，是最近一次显式动作；输入框
    可能还留着上一次输入。旧实现反过来让旧的 ``service_name`` 静默压过用户的
    点击，于是点了 svc-b 仍然诊断 svc-a。
    """
    for candidate in (target_choice, service_name):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


async def run_local(user_input: str, session_id: str, run_id: str, *,
                    service_name: str | None = None,
                    target_choice: str | None = None,
                    target_candidate_id: str | None = None,
                    deadline: float | None = None,
                    budget=None):
    """本地诊断：plan → execute → report。

    ``deadline`` 是运行级硬截止（``time.monotonic()`` 秒），下传给每次工具调用；
    ``budget`` 与云端共用同一个 ``RequestBudget``，避免两条路径各算一套额度。
    流被关闭（客户端取消）时会把 ``RunTools.cancel()`` 标记上，让后续调用立即
    停止，并把“已经发出的调用”如实记进 trace。
    """
    queue: asyncio.Queue = asyncio.Queue()
    #: 自然结束（work 发出了 None）与“被提前关闭”要区分：只有后者才标记取消。
    holder: dict[str, Any] = {"tools": None, "finished": False}

    async def work():
        try:
            queue.put_nowait({"type": "status", "stage": "local_queue", "message": "等待本地诊断资源"})
            async with _LOCAL_SLOT:
                tools = await create_tools_compat(
                    create_run_tools, run_id, "local", queue.put_nowait,
                    deadline=deadline, budget=budget,
                )
                holder["tools"] = tools
                try:
                    target = await tools.discover_service(
                        _chosen_target(service_name, target_choice), user_input,
                        candidate=target_candidate_id)
                except TargetRequiredError as exc:
                    # 目标歧义不是失败：不调用 planner、不查询任何目标数据，
                    # 只发一条选择事件与唯一终态。
                    required, complete = target_required_outcome(exc.resolution, tools.evidence)
                    queue.put_nowait(required)
                    queue.put_nowait(complete)
                    return
                queue.put_nowait({"type": "agent_status", "agent": "local_planner", "status": "running",
                                  "message": "本地小模型正在制定排查计划"})
                result = await try_local_plan(user_input, target, list(tools.by_name.values()), run_id,
                                              allow_cloud_fallback=False)
                if result is None:
                    raise ValueError("本地模型不可用或计划校验失败；本地模式已停止，不会调用在线模型")
                if result.get("severity") not in {"P1", "P2", "P3", "P4"} or result.get("category") not in {
                    "cpu", "memory", "disk", "network", "service", "healthy", "unknown",
                }:
                    raise ValueError("本地模型分诊字段不合法，已停止诊断")
                steps = result["plan"]
                trace_event(run_id, "plan", steps=steps, execution_mode="local")
                queue.put_nowait({"type": "plan", "plan_steps": steps, "backend": "local"})
                queue.put_nowait({"type": "agent_status", "agent": "local_planner", "status": "complete",
                                  "message": "本地计划已通过工具校验"})
                for step in steps:
                    if step["kind"] != "tool":
                        continue
                    step_id = str(step.get("id") or "")
                    if step["tool"] == "list_all_services":
                        # 发现步骤在绑定目标时已经执行过；这里补一条带 step_id 的
                        # 完成事件，否则前端按 step_id 计完成度时这一步永远是 pending，
                        # 计划完成率到不了 100%。
                        discovery = next(
                            (item for item in tools.evidence
                             if item.get("tool") == "list_all_services"), None)
                        queue.put_nowait({
                            "type": "step_complete", "current_step": "list_all_services",
                            "tool": "list_all_services", "agent": "local_executor",
                            "step_id": step_id, "ok": bool(discovery and discovery.get("ok")),
                            "status": "ok" if discovery and discovery.get("ok") else "error",
                            "evidence_id": discovery["id"] if discovery else None,
                        })
                        continue
                    await tools.call(step["tool"], step["args"], agent="local_executor",
                                     step_id=step_id)
                report = render_local_report(target, result, tools.evidence)
                queue.put_nowait({"type": "report", "report": report})
                quality = assess_local_quality(
                    tools.evidence, target_bound=True,
                    extra_gaps=list(getattr(tools, "budget_notes", []) or []),
                )
                # 终态已产出：即使消费方在 complete 后立即关闭生成器，这也是一次
                # 正常结束，不能标记为“取消”。
                holder["finished"] = True
                queue.put_nowait({
                    "type": "complete", "response": report,
                    "lifecycle": quality["lifecycle"], "quality": quality["quality"],
                    "status": quality["status"], "gaps": quality["gaps"],
                    "tools": quality["tools"],
                })
        except Exception as exc:
            queue.put_nowait({"type": "error", "message": str(exc)})
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(work())
    try:
        while (event := await queue.get()) is not None:
            yield event
        holder["finished"] = True
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if not holder["finished"]:
            _record_cancel(run_id, holder.get("tools"))
