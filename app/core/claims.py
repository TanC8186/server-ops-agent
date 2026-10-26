"""证据与事实校验（BE-01）。

审查报告中的反例：E1 是“CPU=12%”的真实来源证据，模型写“CPU=99%，OOM 根因已
确认”并引用 E1，旧实现只检查 ID 存在、``ok=true``、来源类型，于是误判为
supported。

这里的规则是可机械执行的部分：

1. 证据 ID 必须存在，且必须是**本轮**的可用真实证据（ok、非 demo、非独立 SSH）。
2. supported 声明里出现的数值，必须能在被引用证据的数值集合中找到。
3. 结构化观测（metric/value/unit/target/time）逐条回到原始证据核对。
4. 因果表述（根因/导致/因为/已确认……）一律保留为 hypothesis。
5. 确定性数值由服务端按证据渲染（``render_observation``），不采用模型文本。

无法机械校验的自由文本语义仍然需要人工复核——本模块不会声称自动验证了所有事实。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: 不能支撑真实观测结论的来源。
NON_LIVE_SOURCES = frozenset({
    "", "unknown", "unspecified", "demo", "unavailable", "unsupported", "manual",
})

CAUSAL_PATTERNS = (
    r"根因", r"根本原因", r"原因是", r"故障原因", r"导致", r"因为", r"由于",
    r"造成", r"已确认.{0,8}原因", r"确认由", r"证明", r"root\s*cause",
    r"caused\s+by", r"due\s+to", r"because",
)

# 前缀断言只排除 ASCII 字母/数字/下划线/点。用 ``(?<![\w.])`` 会把中文也算作
# 词字符，于是“CPU 使用率99%”里的 99 被整段跳过——这正是审查里的漏检反例。
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)\s*"
    r"(%|percent|ms|msec|millisecond|毫秒|秒|s\b|分钟|min\b|小时|h\b|"
    r"gb|g\b|mb|m\b|kb|k\b|count|次|个)?",
    re.IGNORECASE,
)
_EVIDENCE_ID_RE = re.compile(r"(?<![A-Za-z0-9])E\d+(?![A-Za-z0-9])")
_DURATION_UNITS = frozenset({
    "ms", "msec", "millisecond", "毫秒", "秒", "s", "分钟", "min", "小时", "h",
})
_PERCENT_UNITS = frozenset({"%", "percent", "百分比"})
#: 服务/实例名形态（用于识别声明里提到的“别的目标”）。
_SERVICE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)+")
#: ASCII 标识符（含数字的连字符/点分词）：其中的数字不是观测值。
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)+")
#: 这些 token 是通用技术词/单位，不是服务名，避免把 “cpu-usage” 当目标。
_SERVICE_TOKEN_STOPWORDS = frozenset({
    "cpu-usage", "memory-usage", "disk-usage", "high-cpu", "high-memory",
    "tencent-cloud", "tencentcloud", "http-status", "p95-latency",
})


#: 时长单位的规范族：毫秒 / 秒 / 分钟 / 小时。``12 秒`` 不能支撑 ``12ms``。
_DURATION_FAMILIES = {
    "ms": "ms", "msec": "ms", "millisecond": "ms", "毫秒": "ms",
    "s": "s", "秒": "s",
    "min": "min", "分钟": "min",
    "h": "h", "小时": "h",
}


def _unit_category(unit: str) -> str:
    """单位归类：``duration`` / ``percent`` / ``plain`` / ``other``。"""
    value = (unit or "").strip().lower()
    if not value:
        return "plain"
    if value in _PERCENT_UNITS:
        return "percent"
    if value in _DURATION_UNITS:
        return "duration"
    return "other"


def _duration_family(unit: str) -> str:
    """时长单位的规范族（ms/s/min/h）；非时长返回空串。"""
    return _DURATION_FAMILIES.get((unit or "").strip().lower(), "")


def claim_value_pairs(text: str) -> list[tuple[float, str]]:
    """抽取声明中的 ``(数值, 单位)`` 对（A06）。

    与旧的 :func:`claim_numbers` 不同，这里**保留单位**：同一个 12 在“12%”和
    “12ms”里含义完全不同。是否把某个数值当作观测值，由调用方结合被引用证据的
    单位决定（例如 CPU 证据里的“10 分钟”是查询窗口，而延迟证据里的“999ms”
    是被观测到的数值）。
    """
    cleaned = _EVIDENCE_ID_RE.sub(" ", text or "")
    cleaned = re.sub(r"\bv\d+(\.\d+)+\b", " ", cleaned, flags=re.IGNORECASE)
    # 标识符里的数字（``checkout-service-7d9``、``pod-3``、``sha256``）不是被
    # 观测到的数值。整段 ASCII 标识符先抹掉，避免把副本号当成指标读数。
    cleaned = _IDENTIFIER_RE.sub(" ", cleaned)
    pairs: list[tuple[float, str]] = []
    for match in _NUMBER_RE.finditer(cleaned):
        unit = (match.group(2) or "").strip().lower()
        try:
            pairs.append((float(match.group(1)), unit))
        except ValueError:
            continue
    return pairs

#: 浮点比较容差：0.05 或相对 0.1%。
_ABS_TOL = 0.05
_REL_TOL = 0.001


#: 指标别名 -> 规范族。用于“证据是 CPU、声明却写内存”这类错指标核验。
_METRIC_FAMILIES = {
    "cpu": ("cpu", "处理器", "processor"),
    "memory": ("memory", "内存", "ram", "oom"),
    "disk": ("disk", "磁盘", "inode", "存储", "容量"),
    "network": ("network", "网络", "带宽", "丢包", "延迟", "latency"),
    "process": ("process", "进程", "线程"),
}


def metric_family(name: str) -> str:
    """把指标名/自由文本片段归到规范族；无法归类时返回空串。"""
    text = str(name or "").strip().lower()
    if not text:
        return ""
    for family, aliases in _METRIC_FAMILIES.items():
        for alias in aliases:
            if alias in text:
                return family
    return ""


def claim_metric_families(text: str) -> set[str]:
    """声明文本里提到的指标族（只认词表内的已知指标）。"""
    families: set[str] = set()
    lowered = str(text or "").lower()
    for family, aliases in _METRIC_FAMILIES.items():
        if any(alias in lowered for alias in aliases):
            families.add(family)
    return families


@dataclass(frozen=True)
class Observation:
    """一条结构化观测，必须能回到原始证据。"""

    evidence_id: str
    metric: str = ""
    value: float = 0.0
    unit: str = ""
    statistic: str = ""
    target: str = ""
    at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id, "metric": self.metric, "value": self.value,
            "unit": self.unit, "statistic": self.statistic, "target": self.target,
            "at": self.at,
        }


def _numbers(value: Any) -> list[float]:
    """递归收集载荷里的所有数值。"""
    found: list[float] = []
    if isinstance(value, bool):
        return found
    if isinstance(value, (int, float)):
        found.append(float(value))
    elif isinstance(value, str):
        for match in re.finditer(r"-?\d+(?:\.\d+)?", value):
            try:
                found.append(float(match.group(0)))
            except ValueError:
                continue
    elif isinstance(value, Mapping):
        for item in value.values():
            found.extend(_numbers(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_numbers(item))
    return found


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= max(_ABS_TOL, abs(right) * _REL_TOL)


def _has_number(numbers: Iterable[float], target: float) -> bool:
    return any(_close(number, target) for number in numbers)


def claim_numbers(text: str) -> list[float]:
    """抽取声明中的数值（不做单位取舍；保留给旧调用方）。

    注意：只拿到裸数值无法区分“12%”与“12ms”。需要按单位核对的地方请用
    :func:`claim_value_pairs`。这里仍然保留，是因为 ``run_eval`` 等旧代码依赖
    它的形状。
    """
    return [value for value, _unit in claim_value_pairs(text)]


#: 指标名 -> 单位族（当 payload 没有显式 unit 时按指标语义推断）。
#:
#: 真实 MCP 返回值（``servers/monitor_server.py``）的形态是
#: ``{"metric_name": "...", "data_points": [{"value": ..., "unit": "%"}, ...],
#:    "statistics": {...}}`` —— **没有顶层 ``data.unit``**。只读 ``data.unit``
#: 会让 ``has_duration_evidence`` 永远为 False，于是“网络延迟 999ms”被当成查询
#: 窗口跳过（A06 第一个反例因此在接入真实 payload 后重新出现）。
_METRIC_UNIT_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("latency", "延迟", "rtt", "response_time", "duration"), "ms"),
    (("packet_loss", "丢包"), "%"),
    (("bandwidth", "带宽", "throughput", "rx_mbps", "tx_mbps"), "mbps"),
    (("cpu", "memory", "disk", "usage", "percent", "usage_percent"), "%"),
)


def _unit_from_metric_name(name: str) -> str:
    """按指标名推断单位（无显式 unit 时的兜底；认不出返回空串）。"""
    text = str(name or "").strip().lower()
    if not text:
        return ""
    for aliases, unit in _METRIC_UNIT_HINTS:
        if any(alias in text for alias in aliases):
            return unit
    return ""


def _evidence_metric_name(data: Mapping[str, Any]) -> str:
    for key in ("metric_name", "metric", "name", "metricName"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def evidence_value_pairs(evidence: Mapping[str, Any]) -> list[tuple[float, str]]:
    """从一条证据里抽取 ``(数值, 单位)`` 观测对。

    单位来源按优先级：
    1. 每个数据点自己的 ``unit``（真实 MCP 服务就是这么发的）；
    2. 顶层 ``data.unit``；
    3. 指标名推断（``latency``→ms、``packet_loss``→%、``cpu_usage_percent``→%）；
    4. 都没有 → 空单位。

    只认**结构化字段**（``statistics`` / ``data_points[].value`` / ``data.value``），
    不从日志正文里猜数字（那会把 "timeout after 3000ms" 当成被观测到的延迟值）。
    """
    data = _observed_value(evidence)
    if not isinstance(data, Mapping):
        return []
    fallback_unit = (
        str(data.get("unit") or "").strip().lower()
        or _unit_from_metric_name(_evidence_metric_name(data))
    )
    pairs: list[tuple[float, str]] = []

    points = data.get("data_points")
    if isinstance(points, list):
        for point in points:
            if not isinstance(point, Mapping):
                continue
            value = point.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                unit = str(point.get("unit") or "").strip().lower() or fallback_unit
                pairs.append((float(value), unit))

    statistics = data.get("statistics")
    if isinstance(statistics, Mapping):
        for key, value in statistics.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            # count 是条数，不是被观测到的指标读数。
            if str(key).lower() in {"count", "total", "n"}:
                continue
            pairs.append((float(value), fallback_unit))

    raw = data.get("value")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        pairs.append((float(raw), fallback_unit))
    return pairs


def _foreign_service_mentions(text: str, target: str) -> list[str]:
    """声明文本里出现的、**看起来是另一个目标服务**的 token。

    这个检查只为一个反例服务：证据属于 ``svc-a``，模型写 ``svc-b`` 却标成
    supported。它必须足够保守——误把 ``api-gateway``、``redis-cache``、
    ``www.example.com`` 这类通用组件/域名判成“换了个目标”，会把大量正常报告
    降级成假设，那比漏检更糟。所以：

    - 只认**与目标名同构**的 token：与目标共享同一个前缀族（如目标
      ``svc-a`` → ``svc-b``）或同一个命名空间分隔符形态；
    - 目标名本身是 token 的前缀时（``checkout-service-7d9``）视为同一目标；
    - 纯域名（含 ``.tld`` 形态）、带 scheme 的 URL、以及常见组件词一律跳过。
    """
    target_value = str(target or "").strip().lower()
    if not target_value:
        return []
    target_prefix = _service_prefix(target_value)
    found: list[str] = []
    for match in _SERVICE_TOKEN_RE.finditer(text or ""):
        token = match.group(0)
        lowered = token.lower()
        if lowered in _SERVICE_TOKEN_STOPWORDS:
            continue
        if lowered == target_value or lowered.startswith(target_value):
            continue
        if _looks_like_hostname(lowered):
            continue
        # 只在与目标同一“族”时才判为换目标：目标 svc-a 的族是 svc，
        # 于是 svc-b 命中；而 api-gateway 的族是 api，与 svc 无关，跳过。
        if target_prefix and _service_prefix(lowered) == target_prefix:
            found.append(token)
    return found


def _service_prefix(name: str) -> str:
    """服务名的“族前缀”：``svc-a`` / ``svc-b-01`` → ``svc``。"""
    head = name.split("-", 1)[0]
    return head if len(head) >= 3 else ""


def _looks_like_hostname(token: str) -> bool:
    """粗判域名/URL：含点且点后是被认作 TLD 的短字母段。"""
    if "://" in token:
        return True
    if "." not in token:
        return False
    tail = token.rsplit(".", 1)[-1]
    return tail.isalpha() and 2 <= len(tail) <= 6


def _verify_claim_value_pairs(
    text: str,
    evidence_with_pairs: list[tuple[str, list[tuple[float, str]]]],
) -> list[str]:
    """按 value/unit 逐对核对声明；返回问题描述列表（空表示通过）。

    规则（与 A06 验收一致）：

    - **duration 数值**：证据单位也是 duration → 必须是证据里存在的观测值；
      证据单位不是 duration → 视为查询窗口（如“最近 10 分钟”），跳过。
    - **percent 数值**：证据单位是 duration → 单位冲突（“12%”不能支撑 12ms）；
      其余情况按数值匹配。
    - **plain/other 数值**：按数值匹配（单位信息不足，只核对数值）。
    """
    issues: list[str] = []
    evidence_units = {unit for _id, pairs in evidence_with_pairs for _v, unit in pairs}
    has_duration_evidence = any(_unit_category(unit) == "duration" for unit in evidence_units)
    all_values = [value for _id, pairs in evidence_with_pairs for value, _unit in pairs]

    for value, unit in claim_value_pairs(text):
        category = _unit_category(unit)
        if category == "duration":
            if not has_duration_evidence:
                # 查询窗口（“最近 10 分钟”），不是被观测到的数值。
                continue
            claim_family = _duration_family(unit)
            matching = [
                (candidate, evidence_unit)
                for _id, pairs in evidence_with_pairs
                for candidate, evidence_unit in pairs
                if _unit_category(evidence_unit) == "duration"
                and _duration_family(evidence_unit) == claim_family
                and _close(candidate, value)
            ]
            if matching:
                continue
            same_family_values = [
                candidate
                for _id, pairs in evidence_with_pairs
                for candidate, evidence_unit in pairs
                if _duration_family(evidence_unit) == claim_family
            ]
            if same_family_values and not _has_number(same_family_values, value):
                issues.append(
                    f"声明中的数值 {value:g}{unit or ''} 无法在引用证据中找到"
                )
            elif not same_family_values:
                # 证据时长单位与声明时长单位不在同一族（如证据 ms、声明 12 秒）。
                issues.append(
                    f"声明中的数值 {value:g}{unit or ''} 的单位与被引用证据的时长单位"
                    "不一致，无法在引用证据中找到同单位观测"
                )
            continue
        if category == "percent" and has_duration_evidence:
            issues.append(
                f"声明中的数值 {value:g}{unit} 的单位与被引用证据的单位不一致"
                f"（证据为 duration），无法在引用证据中找到同单位观测"
            )
            continue
        if not _has_number(all_values, value):
            issues.append(f"声明中的数值 {value:g}{unit or ''} 无法在引用证据中找到")
    return issues


def is_causal(text: str) -> bool:
    return any(re.search(pattern, text or "", re.IGNORECASE) for pattern in CAUSAL_PATTERNS)


def evidence_is_live(evidence: Mapping[str, Any]) -> bool:
    if evidence.get("ok") is not True:
        return False
    if evidence.get("demo") is True:
        return False
    sources = {part.strip().lower() for part in str(evidence.get("source", "")).split(",")}
    if sources & NON_LIVE_SOURCES:
        return False
    return evidence.get("target") != "configured_ssh_endpoint"


def _observed_value(evidence: Mapping[str, Any]) -> Any:
    data = evidence.get("data")
    return data if data is not None else evidence


def _metric_of(evidence: Mapping[str, Any]) -> str:
    data = _observed_value(evidence)
    if isinstance(data, Mapping):
        for key in ("metric", "metric_name", "name"):
            if data.get(key):
                return str(data[key])
    return ""


def _time_window(evidence: Mapping[str, Any]) -> tuple[float, float] | None:
    data = _observed_value(evidence)
    if not isinstance(data, Mapping):
        return None
    window = data.get("time_range")
    if not isinstance(window, Mapping):
        return None
    start, end = window.get("start_time"), window.get("end_time")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        return float(start), float(end)
    return None


def verify_observation(observation: Observation,
                       evidence_by_id: Mapping[str, Mapping[str, Any]]) -> str:
    """校验一条结构化观测；返回问题描述，空串表示通过。"""
    return "; ".join(verify_observation_issues(observation, evidence_by_id))


def verify_observation_issues(observation: Observation,
                              evidence_by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """逐项校验，返回全部问题（不只第一个），便于报告完整缺口。"""
    evidence = evidence_by_id.get(observation.evidence_id)
    if evidence is None:
        return [f"观测引用了不存在的证据: {observation.evidence_id}"]
    if not evidence_is_live(evidence):
        return [f"证据 {observation.evidence_id} 不是可用的真实观测（demo/unavailable/SSH）"]

    issues: list[str] = []
    values = _numbers(_observed_value(evidence))
    if not _has_number(values, observation.value):
        issues.append(
            f"证据 {observation.evidence_id} 中不存在数值 {observation.value:g}"
            f"{observation.unit}"
        )
    metric = _metric_of(evidence)
    if observation.metric and metric and observation.metric != metric:
        issues.append(
            f"观测指标 {observation.metric} 与证据 {observation.evidence_id} "
            f"的指标 {metric} 不一致"
        )
    if observation.target and evidence.get("target") and observation.target != evidence.get("target"):
        issues.append(
            f"观测目标 {observation.target} 与证据 {observation.evidence_id} "
            f"的目标 {evidence.get('target')} 不一致"
        )
    if observation.at is not None:
        window = _time_window(evidence)
        if window is None:
            issues.append(
                f"证据 {observation.evidence_id} 没有可核对的时间窗，无法确认观测时间"
            )
        elif not (window[0] <= float(observation.at) <= window[1]):
            issues.append(
                f"观测时间 {observation.at} 不在证据 {observation.evidence_id} "
                f"的时间窗 {window[0]}~{window[1]} 内"
            )
    return issues


def render_observation(observation: Observation,
                       evidence: Mapping[str, Any]) -> str:
    """由服务端按证据渲染确定性描述，不使用模型生成的数值文本。"""
    data = _observed_value(evidence)
    value = observation.value
    unit = observation.unit or ""
    if isinstance(data, Mapping):
        statistics = data.get("statistics")
        if isinstance(statistics, Mapping) and observation.statistic:
            raw = statistics.get(observation.statistic)
            if isinstance(raw, (int, float)) and _close(float(raw), value):
                return (f"{observation.evidence_id} {observation.metric or 'metric'} "
                        f"{observation.statistic}={raw:g}{unit}")
        values = data.get("data_points")
        if isinstance(values, list):
            points = [item for item in values if isinstance(item, Mapping)]
            for point in points:
                try:
                    point_value = float(point.get("value", float("nan")))
                except (TypeError, ValueError):
                    continue
                if _close(point_value, value):
                    timestamp = point.get("timestamp")
                    return (f"{observation.evidence_id} {observation.metric or 'metric'}"
                            f"={value:g}{unit} @ {timestamp}")
    target = evidence.get("target") or observation.target or "未绑定"
    return (f"{observation.evidence_id} {observation.metric or 'metric'}="
            f"{value:g}{unit} target={target}")


def verify_claims(claims: Iterable[Mapping[str, Any]],
                  evidence_by_id: Mapping[str, Mapping[str, Any]], *,
                  observations: Iterable[Observation] | None = None,
                  hypotheses: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """校验模型声明，返回 (规范化声明, 问题列表)。"""
    problems: list[str] = []
    observation_problems: dict[str, str] = {}
    observation_index: dict[str, Observation] = {}
    for observation in observations or ():
        observation_index.setdefault(observation.evidence_id, observation)
        issues = verify_observation_issues(observation, evidence_by_id)
        if issues:
            problems.extend(issues)
            observation_problems[observation.evidence_id] = "; ".join(issues)

    checked: list[dict[str, Any]] = []
    for claim in claims:
        item = dict(claim)
        text = str(item.get("claim") or "")
        ids = [str(value) for value in item.get("evidence_ids") or []]
        unknown = [value for value in ids if value not in evidence_by_id]
        if unknown:
            problems.append("模型引用了不存在的证据: " + ", ".join(sorted(unknown)))
            ids = [value for value in ids if value in evidence_by_id]
            item["status"] = "hypothesis"
        item["evidence_ids"] = ids

        candidates = [evidence_by_id[value] for value in ids]
        live = [evidence for evidence in candidates if evidence_is_live(evidence)]

        if hypotheses:
            item["status"] = "hypothesis"
        elif item.get("status") == "unavailable":
            item["status"] = "unavailable"
        elif item.get("status") == "hypothesis":
            item["status"] = "hypothesis"
        elif item.get("status") == "supported":
            if not live:
                reasons = [
                    "结论缺少可用真实证据，已保留为假设: " + text
                ]
                non_live_sources = {
                    str(evidence.get("source") or "unknown")
                    for evidence in candidates
                }
                if non_live_sources:
                    reasons.append(
                        "引用的证据来源不可用于真实观测（"
                        + ", ".join(sorted(non_live_sources)) + "）"
                    )
                problems.extend(reasons)
                item["status"] = "hypothesis"
            elif is_causal(text):
                problems.append("因果解释不能标记为已证实，已保留为假设: " + text)
                item["status"] = "hypothesis"
            else:
                # A06：按 (数值, 单位) 逐个核对，并把声明里提到的“别的目标”视为
                # 张冠李戴。旧的裸数值比较会漏掉“12% vs 12ms”“999ms”“svc-b”。
                pairs = [
                    (str(evidence.get("id") or ""), evidence_value_pairs(evidence))
                    for evidence in live
                ]
                pair_issues = _verify_claim_value_pairs(text, pairs)
                claimed_families = claim_metric_families(text)
                evidence_families = {
                    metric_family(_metric_of(evidence)) for evidence in live
                } - {""}
                wrong_metric = (
                    bool(claimed_families) and bool(evidence_families)
                    and not (claimed_families & evidence_families)
                )
                live_targets = {
                    str(evidence.get("target") or "").strip() for evidence in live
                } - {""}
                foreign = sorted({
                    token
                    for target in live_targets
                    for token in _foreign_service_mentions(text, target)
                })
                if pair_issues:
                    problems.extend(f"{issue} — {text}" for issue in pair_issues)
                    item["status"] = "hypothesis"
                elif foreign:
                    problems.append(
                        "声明提到的目标与被引用证据的目标不一致（声明出现: "
                        + ", ".join(foreign)
                        + "；证据目标: " + ", ".join(sorted(live_targets) or ["未绑定"])
                        + "）: " + text
                    )
                    item["status"] = "hypothesis"
                elif wrong_metric:
                    # 反例：证据 E1 是 CPU=12，声明“内存 12%”。数值对得上，
                    # 指标却是另一个，不能算 supported。
                    problems.append(
                        "声明中的指标与被引用证据不一致（声明: "
                        + ", ".join(sorted(claimed_families))
                        + "；证据: " + ", ".join(sorted(evidence_families)) + "）: " + text
                    )
                    item["status"] = "hypothesis"
                elif any(value in observation_problems for value in ids):
                    problems.append(
                        "引用的证据存在未通过的观测校验: " + text
                    )
                    item["status"] = "hypothesis"
                else:
                    item["status"] = "supported"

        if item["status"] == "supported" and live:
            primary = live[0]
            structured = observation_index.get(str(primary.get("id", "")))
            if structured is not None:
                item["rendered"] = render_observation(structured, primary)
            else:
                item["rendered"] = render_observation(
                    Observation(
                        evidence_id=str(primary.get("id", "")),
                        metric=_metric_of(primary),
                        value=next(iter(claim_numbers(text)), 0.0),
                        unit="",
                        target=str(primary.get("target") or ""),
                    ),
                    primary,
                )
        checked.append(item)
    return checked, problems
