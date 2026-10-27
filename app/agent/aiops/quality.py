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

**知识片段与观测的显式关联（G2.3 / BE-06）**：知识依据以前只有一句笼统免责
声明，读者无法判断某段手册到底支撑哪条观测。现在两边都要有名有姓：

- 知识行：``… 关联观测: E1, E3``（只列**被证实的实验观测**，不含假设）；
- 观测行：``知识依据: K1``（只列与它确实相关的知识片段）；
- 关联判定只用可复核的客观线索（共享指标族、显式指标名/命令词元重叠），
  一律不猜。没有命中时知识行必须写
  ``仅为背景依据（未关联到本轮观测）``，**绝不编造关联**。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from app.core.claims import (
    claim_metric_families,
    evidence_is_live,
    metric_family,
    verify_claims,
)
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
    # 只把知识片段挂到**被证实的观测**上：假设不能被手册“支撑”。
    links = link_knowledge(observed, knowledge, by_id)

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
        #: ``{知识 id: [观测 id, ...]}``；只含客观线索能对上的组合。
        "knowledge_links": links,
        #: 观测 id -> 与它相关的知识片段 id（由上面反向派生，只读）。
        "observation_links": _invert_links(links),
    }


def _invert_links(links: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """把 ``{知识 id: [观测 id]}`` 反置成 ``{观测 id: [知识 id]}``（保序去重）。"""
    inverted: dict[str, list[str]] = {}
    for knowledge_id, observation_ids in links.items():
        for observation_id in observation_ids:
            bucket = inverted.setdefault(str(observation_id), [])
            if knowledge_id and knowledge_id not in bucket:
                bucket.append(knowledge_id)
    return inverted


def _observation_line(item: Mapping[str, Any],
                      knowledge_ids: Iterable[str] = ()) -> str:
    """渲染一条“当前观测”。

    一行同时给出两样东西，缺一不可（BE-01 的原始缺陷是两者只出现一个）：

    - ``claim``：模型给出的结论（它之所以能到这里，是因为 ``verify_claims``
      已经核对了数值、指标、目标与时间窗）；
    - ``rendered``：服务端按证据逐字渲染的 ``E1 metric=12`` 口径，供人工复核，
      不依赖模型措辞。

    ``knowledge_ids`` 是与之建立了客观关联的知识片段（K 编号）。没有关联时
    不加任何后缀——宁可什么都不说，也不能暗示“有手册支持”。
    """
    rendered = str(item.get("rendered") or "").strip()
    claim = str(item.get("claim") or "").strip()
    refs = _refs(item)
    linked = ", ".join(str(value) for value in knowledge_ids if str(value))
    suffix = f"；知识依据: {linked}" if linked else ""
    if rendered and claim:
        return (f"- [{_STATUS_LABELS['supported']}] {claim} "
                f"（服务端核算: {rendered}；证据: {refs}{suffix}）")
    text = claim or rendered
    return f"- [{_STATUS_LABELS['supported']}] {text} （证据: {refs}{suffix}）"


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


# ── 知识片段 ↔ 观测的显式关联 ───────────────────────────────

#: 知识依据未关联到任何观测时必须出现的原文（回归见
#: tests/test_knowledge_observation_link.py）。
UNLINKED_KNOWLEDGE_NOTE = "仅为背景依据（未关联到本轮观测）"

#: 提取“词元”用：拉丁字母开头的标识符形态（maxclients、redis-cli、cpu_usage）。
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{3,}")

#: 过于通用、不构成关联证据的词元（避免把手册里任何一句都关联到观测）。
_GENERIC_TOKENS = frozenset({
    "this", "that", "with", "from", "have", "should", "when", "will", "then",
    "there", "their", "which", "value", "values", "data", "none", "null", "true",
    "false", "unknown", "default", "system", "info", "status", "step", "https",
    "http", "www", "html", "json", "text", "note", "notes",
})


def _knowledge_body(item: Mapping[str, Any]) -> tuple[str, str]:
    """取出知识片段的 (标题, 正文)，兼容 data 为 dict / 字符串两种形态。"""
    data = item.get("data")
    title = str(item.get("title") or "")
    text = ""
    if isinstance(data, Mapping):
        title = str(data.get("title") or data.get("name") or title)
        text = str(data.get("text") or data.get("content") or data.get("context") or "")
    elif isinstance(data, str):
        text = data
    return title, text


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN_RE.findall(text or "")
        if token.lower() not in _GENERIC_TOKENS
    }


def _families(text: str) -> set[str]:
    return claim_metric_families(text)


def _observation_signal(item: Mapping[str, Any],
                        by_id: Mapping[str, Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    """一条观测的 (指标族, 词元)。

    只使用**被引用证据**里可复核的字段：证据的 metric/name、工具名，以及
    声明文本本身。不读工具原始 payload 的自由文本，避免“日志里碰巧出现的词”
    把手册和观测绑在一起。
    """
    families: set[str] = set()
    tokens: set[str] = set()
    for evidence_id in item.get("evidence_ids") or ():
        evidence = by_id.get(str(evidence_id))
        if not isinstance(evidence, Mapping):
            continue
        data = evidence.get("data")
        if isinstance(data, Mapping):
            for key in ("metric", "metric_name", "name"):
                value = data.get(key)
                if value:
                    families.add(metric_family(str(value)))
                    tokens |= _tokens(str(value))
        tool = str(evidence.get("tool") or "")
        families.add(metric_family(tool))
        tokens |= _tokens(tool)
    text = f"{item.get('claim') or ''} {item.get('rendered') or ''}"
    families |= _families(text)
    tokens |= _tokens(str(item.get("claim") or ""))
    families.discard("")
    return families, tokens


def _related(families: set[str], tokens: set[str],
            other_families: set[str], other_tokens: set[str]) -> bool:
    """判定两个证据是否“可能相关”。

    有任一客观线索重合才算相关：共享指标族（CPU/内存/磁盘/网络/进程），或
    共享一个非通用的标识符词元（指标名、命令名、参数名、组件名）。
    """
    if families and other_families and (families & other_families):
        return True
    return bool(tokens & other_tokens)


def link_knowledge(observations: Iterable[Mapping[str, Any]],
                   knowledge: Iterable[Mapping[str, Any]],
                   by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """计算知识片段与观测的关联，返回 ``{知识片段 id: [证据 id, ...]}``。

    关联的“观测”用**主证据 id**（``E1``）表示，而不是内部序号：报告里可见的
    就是这个 id，读者能直接对上“当前观测”小节里的 ``证据: E1``。

    只建立**显式**关联：没有任何客观线索重合时返回空列表（渲染层据此写
    “仅为背景依据”），绝不因为“同一轮里只有一条知识”就把它挂到所有观测上。
    """
    signals = [
        (key, families, tokens)
        for key, families, tokens in (
            (observation_key(item),) + _observation_signal(item, by_id)
            for item in observations
        )
        if key
    ]
    links: dict[str, list[str]] = {}
    for chunk in knowledge:
        chunk_id = str(chunk.get("id") or "")
        title, text = _knowledge_body(chunk)
        chunk_families = _families(f"{title} {text}")
        chunk_tokens = _tokens(f"{title} {text}")
        links[chunk_id] = [
            key for key, families, tokens in signals
            if _related(chunk_families, chunk_tokens, families, tokens)
        ]
    return links


def observation_key(item: Mapping[str, Any]) -> str:
    """观测在报告里的稳定标识：主证据 id（``E1``），没有则退回证据 id 列表首项。

    一条 supported 观测可能引用多条证据；只取首个作为“主证据”，与
    ``verify_claims`` 渲染的 ``E1 metric=12`` 口径一致。
    """
    for evidence_id in item.get("evidence_ids") or ():
        text = str(evidence_id or "").strip()
        if text:
            return text
    return str(item.get("id") or "").strip()


def _knowledge_line(item: Mapping[str, Any],
                    observation_ids: Iterable[str] = ()) -> str:
    """渲染一条“知识依据”，并显式说明它与哪条观测相关。

    - 有客观关联：``… 关联观测: E1, E3``（只列实际被证实的观测）；
    - 没有任何关联：``… 仅为背景依据（未关联到本轮观测）``——不编造关联，
      也不把“同轮出现”当成“支持”；
    - ``（手册，不代表当前主机状态）`` 无论如何都保留。
    """
    title, text = _knowledge_body(item)
    source = str(item.get("source") or "manual")
    head = f"{title}：" if title else ""
    linked = ", ".join(str(value) for value in observation_ids if str(value))
    relation = f"关联观测: {linked}" if linked else UNLINKED_KNOWLEDGE_NOTE
    line = f"- {head}{text} {MANUAL_DISCLAIMER}（{relation}；来源: {source}）"
    return line.replace("  ", " ")


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
    observation_links = assessment.get("observation_links") or {}
    lines.extend(
        _observation_line(item, observation_links.get(observation_key(item), ()))
        for item in observations
    )
    if not observations:
        lines.append("- 本轮没有可用的真实观测；以下内容不能作为当前主机状态。")

    lines.extend(["", f"## {REPORT_SECTIONS[1]}"])
    hypotheses = assessment.get("hypotheses") or []
    lines.extend(_hypothesis_line(item) for item in hypotheses)
    if not hypotheses:
        lines.append("- 当前证据不足以提出可验证的原因假设。")

    lines.extend(["", f"## {REPORT_SECTIONS[2]}"])
    knowledge = assessment.get("knowledge") or []
    knowledge_links = assessment.get("knowledge_links") or {}
    lines.extend(
        _knowledge_line(item, knowledge_links.get(str(item.get("id") or ""), ()))
        for item in knowledge
    )
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
