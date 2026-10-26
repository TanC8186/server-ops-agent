"""BE-05：把“运行完成”和“诊断质量”拆开，并渲染可复核的在线多 Agent 报告。

在线模式曾经只有一个 ``success``：只要 Reviewer 生成了文本就当作诊断成功。
这里改为服务端计算：

- ``lifecycle``：运行本身是否走完（见 ``app.core.run_status.Lifecycle``）；
- ``quality``：本轮证据是否足以支撑结论（sufficient/partial/insufficient）；
- ``status``：向后兼容字段，由前两者派生——``quality=insufficient`` 时必为
  ``degraded``，绝不采用模型文本里的自我评价。

声明校验复用 ``app.core.claims.verify_claims``：数值必须能在被引用证据中找到，
因果解释一律保留为假设；demo / SSH / 不可用证据会被降级并写入证据缺口。

报告的结构固定为：当前观测 → 待验证假设 → 知识依据 → 证据缺口 →
后续检查建议 → 原始证据索引。知识片段只能出现在“知识依据”，并且必须标注
“（手册，不代表当前主机状态）”——手册描述的是某个版本的行为，不是这台主机
此刻的状态。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.core.claims import evidence_is_live, verify_claims
from app.core.run_status import (
    DiagnosisQuality,
    Lifecycle,
    QualityReport,
    evaluate_quality,
)

#: 报告必须出现的 H2 小节，顺序固定（回归见 tests/test_cloud_multi_quality.py）。
REPORT_SECTIONS = (
    "当前观测",
    "待验证假设",
    "知识依据",
    "证据缺口",
    "后续检查建议",
    "原始证据索引",
)

#: 知识证据的免责声明：手册不等于当前主机状态。
MANUAL_DISCLAIMER = "（手册，不代表当前主机状态）"

#: 只提供知识依据、不能作为当前观测的工具。
KNOWLEDGE_TOOLS = frozenset({"retrieve_knowledge"})

_STATUS_LABELS = {"supported": "证据观察", "hypothesis": "待验证假设", "unavailable": "未获取"}


def _as_lifecycle(value: Any) -> Lifecycle:
    if isinstance(value, Lifecycle):
        return value
    try:
        return Lifecycle(str(value))
    except ValueError:
        return Lifecycle.COMPLETED


def _normalize(item: Mapping[str, Any]) -> dict[str, Any]:
    """补齐报告需要的字段，不修改调用方的证据字典。

    兼容旧证据字典：只有 ``ok`` 而没有 ``status``/``live`` 时按同一套契约推导，
    不能让“缺字段”被错误当成失败或成功。
    """
    evidence = dict(item)
    tool = str(evidence.get("tool") or "")
    kind = str(evidence.get("kind") or "")
    if not kind:
        kind = "knowledge" if tool in KNOWLEDGE_TOOLS else "observation"
    evidence["kind"] = kind
    evidence.setdefault("source", "unknown")
    evidence.setdefault("target", "")
    evidence.setdefault("error", "")
    if "demo" not in evidence:
        evidence["demo"] = "demo" in {
            part.strip() for part in str(evidence.get("source") or "").split(",")
        }
    if "status" not in evidence:
        evidence["status"] = (
            "ok" if evidence.get("ok") is True
            else "error" if evidence.get("ok") is False else "unknown"
        )
    if "live" not in evidence:
        evidence["live"] = evidence_is_live(evidence)
    return evidence


def is_knowledge(item: Mapping[str, Any]) -> bool:
    return str(item.get("kind") or "") == "knowledge" or str(item.get("tool") or "") in KNOWLEDGE_TOOLS


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _is_supported(item: Mapping[str, Any]) -> bool:
    return str(item.get("status") or "") == "supported"


def assess(runtime_evidence: Iterable[Mapping[str, Any]],
           specialist_results: Iterable[Mapping[str, Any]] | None,
           review_data: Mapping[str, Any] | None, *,
           lifecycle: Lifecycle | str = Lifecycle.COMPLETED,
           target_bound: bool) -> dict[str, Any]:
    """汇总证据、复核声明并计算生命周期/诊断质量。

    参数：
        runtime_evidence: 本轮全部工具证据（``RunTools.evidence`` 的结构）。
        specialist_results: 每个专科的 ``{agent, findings, errors, ...}``。
        review_data: Reviewer 输出的 observations/hypotheses/recommendations/
            evidence_gaps；``reviewer_failed=True`` 表示复核阶段不可用。
        lifecycle: 运行生命周期；非 completed 一律 insufficient。
        target_bound: 是否绑定了可信诊断目标。

    返回字典包含 ``lifecycle``/``quality``/``status``/``reason``/``gaps``/
    ``tools``（工具摘要）以及渲染所需的分类结果。
    """
    evidence = [_normalize(item) for item in runtime_evidence or ()]
    by_id = {str(item["id"]): item for item in evidence if item.get("id")}
    review = dict(review_data or {})
    results = list(specialist_results or ())

    gaps = list(review.get("evidence_gaps") or ())
    specialist_errors: list[str] = []
    for result in results:
        specialist_errors.extend(str(error) for error in result.get("errors") or () if str(error).strip())
    gaps.extend(specialist_errors)

    raw_observations = list(review.get("observations") or ())
    raw_hypotheses = list(review.get("hypotheses") or ())

    observations, observation_problems = verify_claims(raw_observations, by_id)
    hypotheses, hypothesis_problems = verify_claims(raw_hypotheses, by_id, hypotheses=True)
    gaps.extend(observation_problems)
    gaps.extend(hypothesis_problems)

    # 未被证实（含 demo/SSH/数值不符/因果）的“观测”只能进待验证假设。
    unconfirmed = [item for item in observations if not _is_supported(item)]
    observed = [item for item in observations if _is_supported(item)]
    pending = unconfirmed + hypotheses

    lifecycle_value = _as_lifecycle(lifecycle)
    quality_report: QualityReport = evaluate_quality(
        evidence, gaps, target_bound=target_bound, lifecycle=lifecycle_value,
    )
    all_gaps = _dedupe(list(quality_report.gaps))

    degraded = (
        quality_report.quality == DiagnosisQuality.INSUFFICIENT
        or lifecycle_value != Lifecycle.COMPLETED
        or bool(specialist_errors)
        or bool(review.get("reviewer_failed"))
    )
    status = "degraded" if degraded else "completed"

    knowledge = [item for item in evidence if is_knowledge(item)]
    indexed = [item for item in evidence if not is_knowledge(item)]

    return {
        "lifecycle": str(lifecycle_value),
        "quality": str(quality_report.quality),
        "status": status,
        "reason": quality_report.reason,
        "gaps": all_gaps,
        "tools": quality_report.summary.to_dict() if quality_report.summary else None,
        "observations": observed,
        "hypotheses": pending,
        "knowledge": knowledge,
        "recommendations": [str(item) for item in review.get("recommendations") or ()],
        "evidence": indexed,
    }


def _observation_line(item: Mapping[str, Any]) -> str:
    """渲染一条“当前观测”。

    一行同时给出两样东西，缺一不可（BE-01 的原始缺陷是两者只出现一个）：

    - ``claim``：模型给出的结论（它之所以能到这里，是因为 ``verify_claims``
      已经核对了数值、指标、目标与时间窗）；
    - ``rendered``：服务端按证据逐字渲染的 ``E1 metric=12`` 口径，供人工复核，
      不依赖模型措辞。
    """
    rendered = str(item.get("rendered") or "").strip()
    claim = str(item.get("claim") or "").strip()
    refs = _refs(item)
    if rendered and claim:
        return (f"- [{_STATUS_LABELS['supported']}] {claim} "
                f"（服务端核算: {rendered}；证据: {refs}）")
    text = claim or rendered
    return f"- [{_STATUS_LABELS['supported']}] {text} （证据: {refs}）"


def _hypothesis_line(item: Mapping[str, Any]) -> str:
    """待验证假设：保留模型文本，但同样附上服务端渲染的依据（若有）。"""
    claim = str(item.get("claim") or "").strip()
    rendered = str(item.get("rendered") or "").strip()
    refs = _refs(item)
    if rendered and rendered not in claim:
        return (f"- [{_STATUS_LABELS['hypothesis']}] {claim} "
                f"（服务端核算: {rendered}；证据: {refs}；需进一步验证）")
    return f"- [{_STATUS_LABELS['hypothesis']}] {claim} （证据: {refs}；需进一步验证）"


def _refs(item: Mapping[str, Any]) -> str:
    ids = [str(value) for value in item.get("evidence_ids") or ()]
    return ", ".join(ids) if ids else "无可用证据引用"


def _knowledge_line(item: Mapping[str, Any]) -> str:
    data = item.get("data")
    title = str(item.get("title") or "")
    text = ""
    if isinstance(data, Mapping):
        title = str(data.get("title") or data.get("name") or title)
        text = str(data.get("text") or data.get("content") or data.get("context") or "")
    elif isinstance(data, str):
        text = data
    source = str(item.get("source") or "manual")
    head = f"{title}：" if title else ""
    return f"- {head}{text} {MANUAL_DISCLAIMER}（来源: {source}）".replace("  ", " ")


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(assessment: Mapping[str, Any]) -> str:
    """渲染固定结构的 Markdown 报告；确定性部分全部由服务端生成。"""
    lifecycle = str(assessment.get("lifecycle") or "completed")
    quality = str(assessment.get("quality") or "insufficient")
    tools = assessment.get("tools") or {}
    lines = [
        "# 在线 API 多 Agent 运维诊断报告",
        "",
        f"生命周期: {lifecycle}｜诊断质量: {quality}",
        "诊断质量只评价本轮证据是否足以支撑结论，与运行是否完成无关；"
        "insufficient 表示报告已生成但证据不足，不能据此执行变更。",
    ]
    if tools:
        lines.append(
            f"工具结果: 共 {tools.get('total', 0)} 次，成功 {tools.get('succeeded', 0)}，"
            f"失败 {tools.get('failed', 0)}，空结果 {tools.get('empty', 0)}，"
            f"演示 {tools.get('demo', 0)}。"
        )
    lines.extend(["", f"## {REPORT_SECTIONS[0]}"])
    observations = assessment.get("observations") or []
    lines.extend(_observation_line(item) for item in observations)
    if not observations:
        lines.append("- 本轮没有可用的真实观测；以下内容不能作为当前主机状态。")

    lines.extend(["", f"## {REPORT_SECTIONS[1]}"])
    hypotheses = assessment.get("hypotheses") or []
    lines.extend(_hypothesis_line(item) for item in hypotheses)
    if not hypotheses:
        lines.append("- 当前证据不足以提出可验证的原因假设。")

    lines.extend(["", f"## {REPORT_SECTIONS[2]}"])
    knowledge = assessment.get("knowledge") or []
    lines.extend(_knowledge_line(item) for item in knowledge)
    if not knowledge:
        lines.append("- 本轮没有检索到知识库依据。")

    lines.extend(["", f"## {REPORT_SECTIONS[3]}"])
    gaps = assessment.get("gaps") or []
    lines.extend(f"- {gap}" for gap in gaps)
    if not gaps:
        lines.append("- 本轮没有记录到查询错误；原因假设仍需人工验证。")

    lines.extend(["", f"## {REPORT_SECTIONS[4]}"])
    recommendations = assessment.get("recommendations") or []
    lines.extend(f"- {item}" for item in recommendations)
    if not recommendations:
        lines.append("- 核对失败查询、时间窗和服务映射，再补充证据。")

    lines.extend([
        "", f"## {REPORT_SECTIONS[5]}", "",
        "| ID | 工具 | 状态 | 来源 | 目标范围 |",
        "| --- | --- | --- | --- | --- |",
    ])
    evidence = assessment.get("evidence") or []
    for item in evidence:
        cells = [
            item.get("id", ""), item.get("tool", ""),
            "成功" if item.get("ok") is True else "未获取",
            item.get("source", "unknown"), item.get("target", "未绑定"),
        ]
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    if not evidence:
        lines.append("| — | — | 未获取证据 | unavailable | 未绑定 |")
    return "\n".join(lines)
