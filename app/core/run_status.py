"""生命周期与诊断质量（BE-05）。

三个互相独立的概念，不能再用一个 ``success`` 字段表示：

- ``Lifecycle``：运行本身是否走完（queued/running/completed/failed/cancelled/abandoned）
- ``DiagnosisQuality``：这次诊断的证据是否足以支撑结论
  （sufficient/partial/insufficient）
- 工具结果：逐个调用的 status（见 app.core.tool_contract）

“报告生成成功”不等于“证据充分”。只有服务发现、时间、demo 或知识片段的运行
一律是 insufficient。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

#: 只证明“系统还能被查询”，不能证明目标服务状态。
NON_OBSERVATION_TOOLS = frozenset({
    "list_all_services", "get_current_time", "get_current_timestamp",
    "get_region_code_by_name", "search_topic_by_service_name", "get_topic_info_by_name",
    "retrieve_knowledge",
})

FAILURE_STATUSES = frozenset({"error", "timeout", "unavailable", "unsupported"})


class Lifecycle(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class DiagnosisQuality(StrEnum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class ToolSummary:
    total: int
    succeeded: int
    failed: int
    empty: int
    partial: int
    demo: int
    live_successes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total, "succeeded": self.succeeded, "failed": self.failed,
            "empty": self.empty, "partial": self.partial, "demo": self.demo,
            "live_successes": self.live_successes,
        }


@dataclass(frozen=True)
class QualityReport:
    lifecycle: Lifecycle
    quality: DiagnosisQuality
    reason: str
    gaps: list[str] = field(default_factory=list)
    summary: ToolSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle": str(self.lifecycle),
            "quality": str(self.quality),
            "reason": self.reason,
            "gaps": list(self.gaps),
            "tools": self.summary.to_dict() if self.summary else None,
        }


def summarize_tool_results(tool_results: Iterable[Mapping[str, Any]]) -> ToolSummary:
    total = succeeded = failed = empty = partial = demo = live = 0
    for item in tool_results:
        total += 1
        status = str(item.get("status") or "unknown")
        if status == "ok":
            succeeded += 1
        elif status == "empty":
            empty += 1
        elif status == "partial":
            partial += 1
        elif status in FAILURE_STATUSES:
            failed += 1
        if item.get("demo"):
            demo += 1
        if status == "ok" and item.get("live"):
            live += 1
    return ToolSummary(total=total, succeeded=succeeded, failed=failed, empty=empty,
                       partial=partial, demo=demo, live_successes=live)


def evaluate_quality(tool_results: Iterable[Mapping[str, Any]],
                     evidence_gaps: Iterable[str], *, target_bound: bool,
                     lifecycle: Lifecycle = Lifecycle.COMPLETED) -> QualityReport:
    """由服务端计算质量，模型不能自行宣布成功。"""
    results = list(tool_results)
    summary = summarize_tool_results(results)
    gaps = [str(gap) for gap in evidence_gaps if str(gap).strip()]

    if lifecycle not in {Lifecycle.COMPLETED}:
        return QualityReport(
            lifecycle=lifecycle, quality=DiagnosisQuality.INSUFFICIENT,
            reason=f"运行未正常完成（{lifecycle}），不评价诊断质量",
            gaps=gaps, summary=summary,
        )

    if not target_bound:
        gaps.append("未绑定可信诊断目标，结论不能归属到具体服务")

    observations = []
    for item in results:
        tool = str(item.get("tool") or "")
        kind = str(item.get("kind") or "observation")
        if tool in NON_OBSERVATION_TOOLS or kind != "observation":
            continue
        if item.get("demo"):
            gaps.append(f"{tool or '工具'} 仅返回演示数据，不能支撑真实观测结论")
            continue
        if item.get("target") == "configured_ssh_endpoint":
            gaps.append("SSH 证据属于独立配置的主机，尚未证明与目标服务的实例映射")
        if item.get("status") in {"ok", "empty", "partial"} and item.get("live"):
            observations.append(item)
        elif item.get("status") in FAILURE_STATUSES:
            gaps.append(f"{tool or '工具'} 未获取到可用数据（{item.get('status')}）")

    if summary.total == 0:
        return QualityReport(lifecycle=lifecycle, quality=DiagnosisQuality.INSUFFICIENT,
                             reason="本次没有任何工具结果", gaps=gaps, summary=summary)

    if not observations:
        reason = ("只有服务发现/时间/知识类结果，或全部查询失败；"
                  "不能据此做出当前诊断")
        return QualityReport(lifecycle=lifecycle, quality=DiagnosisQuality.INSUFFICIENT,
                             reason=reason, gaps=gaps, summary=summary)

    if summary.failed or summary.partial or not target_bound:
        return QualityReport(lifecycle=lifecycle, quality=DiagnosisQuality.PARTIAL,
                             reason="部分观测成功，结论需补齐缺口后确认",
                             gaps=gaps, summary=summary)

    if any(item.get("target") == "configured_ssh_endpoint" for item in observations):
        return QualityReport(lifecycle=lifecycle, quality=DiagnosisQuality.PARTIAL,
                             reason="目标服务的观测与独立 SSH 主机证据混合",
                             gaps=gaps, summary=summary)

    return QualityReport(lifecycle=lifecycle, quality=DiagnosisQuality.SUFFICIENT,
                         reason="目标范围内有可用的真实观测", gaps=gaps, summary=summary)
