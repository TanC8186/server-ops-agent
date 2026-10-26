"""A06：数值/单位/目标/时间必须回到结构化证据核对（2026-10-27 验收反例）。

原始反例：证据 ``latency.avg=12, unit=ms, target=svc-a``，以下文本曾全部被当成
``supported``：

- ``网络延迟 999ms``            数值 999 不在证据里
- ``网络延迟 12%``              数值对上但单位（%）与证据（ms）冲突
- ``网络延迟 12ms，svc-b在昨天的结果`` 目标 svc-b 与证据 svc-a 不一致

正确的对照 ``网络延迟 12ms`` 必须仍然是 supported，且“最近 10 分钟”这类查询窗口
不能被误当成观测值。全部使用真实 ``verify_claims`` / ``quality.assess``。
"""

from __future__ import annotations

import pytest

from app.agent.aiops import quality
from app.core.claims import verify_claims

LATENCY_EVIDENCE = {
    "id": "E1",
    "tool": "query_network_metrics",
    "ok": True,
    "status": "ok",
    "live": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "metric": "latency",
        "statistics": {"avg": 12},
        "unit": "ms",
        "time_range": {"start_time": 100, "end_time": 200},
    },
}

CPU_EVIDENCE = {
    "id": "E2",
    "tool": "query_cpu_metrics",
    "ok": True,
    "status": "ok",
    "live": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "metric": "cpu_usage",
        "statistics": {"avg": 12.0, "max": 14.0},
        "unit": "percent",
        "time_range": {"start_time": 100, "end_time": 200},
    },
}

EVIDENCE = {"E1": LATENCY_EVIDENCE, "E2": CPU_EVIDENCE}


def _observation(claim: str, evidence_id: str = "E1") -> dict:
    return {"claim": claim, "status": "supported", "evidence_ids": [evidence_id]}


def _assess(claim: str, evidence_id: str = "E1") -> dict:
    return quality.assess(
        [LATENCY_EVIDENCE if evidence_id == "E1" else CPU_EVIDENCE],
        [],
        {
            "observations": [_observation(claim, evidence_id)],
            "hypotheses": [],
            "evidence_gaps": [],
            "recommendations": [],
        },
        target_bound=True,
    )


@pytest.mark.parametrize(
    "claim",
    [
        "网络延迟 999ms",
        "网络延迟 12%",
        "网络延迟 12ms，svc-b在昨天的结果",
    ],
)
def test_counterexample_claims_are_demoted_to_hypothesis(claim):
    assessment = _assess(claim)

    assert assessment["observations"] == [], f"{claim} 不应作为当前观测"
    assert any(item["claim"] == claim for item in assessment["hypotheses"]), (
        f"{claim} 应降为待验证假设"
    )


def test_counterexample_gaps_name_the_offending_value_or_target():
    _, problems = verify_claims([_observation("网络延迟 999ms")], EVIDENCE)
    assert any("999" in problem for problem in problems)

    _, problems = verify_claims([_observation("网络延迟 12%")], EVIDENCE)
    assert any("单位" in problem for problem in problems)

    _, problems = verify_claims([_observation("网络延迟 12ms，svc-b在昨天的结果")], EVIDENCE)
    assert any("svc-b" in problem or "目标" in problem for problem in problems)


def test_correct_latency_claim_stays_supported():
    assessment = _assess("网络延迟 12ms")

    assert [item["claim"] for item in assessment["observations"]] == ["网络延迟 12ms"]
    assert assessment["hypotheses"] == []


def test_correct_latency_claim_is_rendered_by_server():
    checked, problems = verify_claims([_observation("网络延迟 12ms")], EVIDENCE)

    assert problems == []
    assert checked[0]["status"] == "supported"
    assert "12" in str(checked[0].get("rendered") or "")


def test_percent_claim_against_percent_evidence_stays_supported():
    """回归：CPU 证据是 percent，12% 必须仍然成立。"""
    checked, problems = verify_claims(
        [_observation("CPU 使用率 12%", "E2")], EVIDENCE
    )

    assert checked[0]["status"] == "supported", problems


def test_query_window_duration_is_not_treated_as_observation():
    """“最近 10 分钟”是查询窗口，不能因为证据是 percent 就判数值不符。"""
    checked, problems = verify_claims(
        [_observation("CPU 最近10分钟使用率 12%", "E2")], EVIDENCE
    )

    assert checked[0]["status"] == "supported", problems


def test_duration_unit_mismatch_against_latency_evidence_is_rejected():
    """证据是 ms，声明写“12 秒”：单位族不同，不能算支持。"""
    checked, problems = verify_claims([_observation("网络延迟 12秒")], EVIDENCE)

    assert checked[0]["status"] == "hypothesis"
    assert problems


def test_numberless_claim_still_supported():
    """回归：没有数值的定性描述不受影响。"""
    logs = {
        "id": "E3",
        "tool": "search_service_logs",
        "ok": True,
        "status": "ok",
        "live": True,
        "source": "tencent_cloud",
        "target": "svc-a",
        "data": {"logs": [{"level": "ERROR", "message": "upstream timeout"}]},
    }
    checked, problems = verify_claims(
        [_observation("日志中出现了上游超时", "E3")], {"E3": logs}
    )

    assert checked[0]["status"] == "supported", problems


def test_target_mentioning_the_bound_target_is_not_a_conflict():
    """声明里明确写下目标本身（svc-a）不是张冠李戴。"""
    checked, problems = verify_claims([_observation("svc-a 网络延迟 12ms")], EVIDENCE)

    assert checked[0]["status"] == "supported", problems


@pytest.mark.parametrize(
    "claim",
    [
        "CPU 使用率 12%，上游 api-gateway 出现超时",
        "CPU 使用率 12%，参考 www.example.com 文档",
        "CPU 使用率 12%，pod checkout-service-7d9 正常",
        "CPU 使用率 12%，日志里 redis-cache 连接失败",
    ],
)
def test_common_component_names_are_not_mistaken_for_other_targets(claim):
    """保守性回归：通用组件/域名/副本号不能被当成“换了个目标”。

    证据目标是 checkout-service；上面这些文本里的 token 与目标同族无一致性，
    也不构成“同族但不同的服务”，所以必须保持 supported，否则大量正常报告会被
    无谓降级。
    """
    evidence = {
        "id": "E1",
        "tool": "query_cpu_metrics",
        "ok": True,
        "status": "ok",
        "live": True,
        "source": "tencent_cloud",
        "target": "checkout-service",
        "data": {"metric": "cpu_usage", "statistics": {"avg": 12.0}, "unit": "percent"},
    }
    checked, problems = verify_claims(
        [{"claim": claim, "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": evidence},
    )

    assert checked[0]["status"] == "supported", problems


def test_same_family_different_service_is_still_rejected():
    """与目标同族的另一个服务（svc-a 证据 vs svc-b 声明）仍必须拒绝。"""
    evidence = dict(
        LATENCY_EVIDENCE,
        target="svc-a",
    )
    checked, problems = verify_claims(
        [{"claim": "svc-b 的网络延迟 12ms", "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": evidence},
    )

    assert checked[0]["status"] == "hypothesis"
    assert any("svc-b" in problem or "目标" in problem for problem in problems)


def test_identifier_digits_are_not_treated_as_observed_values():
    """``checkout-service-7d9`` 里的 7 是副本号，不是被测到的指标数值。"""
    evidence = {
        "id": "E1",
        "tool": "query_cpu_metrics",
        "ok": True,
        "status": "ok",
        "live": True,
        "source": "tencent_cloud",
        "target": "checkout-service",
        "data": {"metric": "cpu_usage", "statistics": {"avg": 12.0}, "unit": "percent"},
    }
    checked, problems = verify_claims(
        [{"claim": "CPU 12%，pod checkout-service-7d9 正常",
          "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": evidence},
    )

    assert checked[0]["status"] == "supported", problems


# ─────────────────────────────────────────────────────────────────────────────
# 真实 MCP payload 形态（servers/monitor_server.py）：单位在 data_points[].unit，
# 字段名是 metric_name，**没有顶层 data.unit**。这是对抗复核发现的回归点：
# 只读 data.unit 会让 duration 证据永远识别不出来，于是“999ms”被当成查询窗口。
# ─────────────────────────────────────────────────────────────────────────────

REAL_NETWORK_EVIDENCE = {
    "id": "E1",
    "tool": "query_network_metrics",
    "ok": True,
    "status": "ok",
    "live": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "metric_name": "network_latency",
        "data_points": [
            {"timestamp": 1, "value": 12, "unit": "ms"},
            {"timestamp": 2, "value": 13, "unit": "ms"},
        ],
        "statistics": {"avg": 12.5, "max": 13, "min": 12, "p95": 13, "count": 2},
    },
}

REAL_CPU_EVIDENCE = {
    "id": "E2",
    "tool": "query_cpu_metrics",
    "ok": True,
    "status": "ok",
    "live": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "metric_name": "cpu_usage_percent",
        "data_points": [{"value": 12, "unit": "%"}],
        "statistics": {"avg": 12.0, "max": 14.0, "count": 2},
    },
}

UNITLESS_DISK_EVIDENCE = {
    "id": "E3",
    "tool": "query_disk_metrics",
    "ok": True,
    "status": "ok",
    "live": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "metric_name": "disk_usage",
        "data_points": [{"value": 71.5}],
        "statistics": {"avg": 71.5},
    },
}


@pytest.mark.parametrize(
    "claim",
    [
        "网络延迟 999ms",
        "网络延迟 12%",
        "网络延迟 999s",
        "网络延迟 50分钟",
    ],
)
def test_review_counterexamples_are_rejected_on_real_payload_shape(claim):
    """单位来自 data_points[].unit（真实服务形态），不是假设的 data.unit。"""
    checked, problems = verify_claims(
        [{"claim": claim, "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": REAL_NETWORK_EVIDENCE},
    )

    assert checked[0]["status"] == "hypothesis", f"{claim} 不应为 supported: {problems}"
    assert problems


def test_correct_value_on_real_payload_stays_supported():
    checked, problems = verify_claims(
        [{"claim": "网络延迟 12.5ms", "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": REAL_NETWORK_EVIDENCE},
    )

    assert checked[0]["status"] == "supported", problems


def test_percent_metric_on_real_payload():
    good, good_problems = verify_claims(
        [{"claim": "CPU 使用率 12%", "status": "supported", "evidence_ids": ["E2"]}],
        {"E2": REAL_CPU_EVIDENCE},
    )
    bad, _ = verify_claims(
        [{"claim": "CPU 使用率 99%", "status": "supported", "evidence_ids": ["E2"]}],
        {"E2": REAL_CPU_EVIDENCE},
    )

    assert good[0]["status"] == "supported", good_problems
    assert bad[0]["status"] == "hypothesis"


def test_unit_inferred_from_metric_name_when_payload_has_no_unit():
    """payload 完全没有单位时，按指标名推断（disk_usage → %）。"""
    good, good_problems = verify_claims(
        [{"claim": "磁盘使用率 71.5%", "status": "supported", "evidence_ids": ["E3"]}],
        {"E3": UNITLESS_DISK_EVIDENCE},
    )
    bad, _ = verify_claims(
        [{"claim": "磁盘使用率 12%", "status": "supported", "evidence_ids": ["E3"]}],
        {"E3": UNITLESS_DISK_EVIDENCE},
    )

    assert good[0]["status"] == "supported", good_problems
    assert bad[0]["status"] == "hypothesis"


def test_statistics_count_is_not_treated_as_an_observed_value():
    """statistics.count=2 是条数，不是被测到的读数值。"""
    checked, problems = verify_claims(
        [{"claim": "网络延迟 2ms", "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": REAL_NETWORK_EVIDENCE},
    )

    assert checked[0]["status"] == "hypothesis", "count 不能被当成延迟读数"
    assert problems


# ── 第二轮对抗复核：duration 声明挂在非 duration 证据上 ──────────────

REAL_CPU_PERCENT = {
    "id": "E1",
    "tool": "query_cpu_metrics",
    "ok": True,
    "status": "ok",
    "live": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "metric_name": "cpu_usage_percent",
        "data_points": [{"value": 12, "unit": "%"}],
        "statistics": {"avg": 12.0, "count": 2},
    },
}

REAL_TRAFFIC_MBPS = {
    "id": "E2",
    "tool": "query_network_metrics",
    "ok": True,
    "status": "ok",
    "live": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "metric_name": "network_traffic_mbps",
        "data_points": [{"value": 12.5, "unit": "mbps"}],
        "statistics": {"avg": 12.5},
    },
}


@pytest.mark.parametrize(
    ("evidence", "claim"),
    [
        (REAL_CPU_PERCENT, "CPU 使用率 999ms"),
        (REAL_CPU_PERCENT, "CPU 使用率 999秒"),
        (REAL_CPU_PERCENT, "CPU 使用率 12%，接口耗时 999ms"),
        (REAL_TRAFFIC_MBPS, "网络延迟 999ms"),
    ],
)
def test_duration_claim_on_non_duration_evidence_is_rejected(evidence, claim):
    """证据没有任何时长单位时，声明里的时长是“读数”而不是查询窗口，必须拒绝。

    对抗复核实测：旧实现把这类 duration 一律当查询窗口跳过，于是任意
    “999ms/999秒” 都能挂在 CPU/流量证据上冒充 supported。
    """
    checked, problems = verify_claims(
        [{"claim": claim, "status": "supported", "evidence_ids": [evidence["id"]]}],
        {evidence["id"]: evidence},
    )

    assert checked[0]["status"] == "hypothesis", f"{claim} 不应为 supported"
    assert problems


@pytest.mark.parametrize(
    "claim",
    [
        "CPU 最近10分钟使用率 12%",
        "过去5秒内 CPU 使用率 12%",
        "近一小时 CPU 平均 12%",
    ],
)
def test_query_window_is_still_not_validated(claim):
    """带明确范围线索的时长是查询窗口，不能因为证据无 duration 单位就被判错。"""
    checked, problems = verify_claims(
        [{"claim": claim, "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": REAL_CPU_PERCENT},
    )

    assert checked[0]["status"] == "supported", problems


def test_correct_percent_and_traffic_readings_stay_supported():
    cpu_checked, cpu_problems = verify_claims(
        [{"claim": "CPU 使用率 12%", "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": REAL_CPU_PERCENT},
    )
    net_checked, net_problems = verify_claims(
        [{"claim": "网络流量 12.5mbps", "status": "supported", "evidence_ids": ["E2"]}],
        {"E2": REAL_TRAFFIC_MBPS},
    )

    assert cpu_checked[0]["status"] == "supported", cpu_problems
    assert net_checked[0]["status"] == "supported", net_problems


@pytest.mark.parametrize(
    "claim",
    [
        "延迟 九百九十九毫秒",
        "延迟 十二秒",
        "延迟 一百ms",
    ],
)
def test_chinese_numerals_are_parsed_and_checked(claim):
    """中文数字不能成为绕过数值校验的通道（对抗复核发现）。"""
    checked, problems = verify_claims(
        [{"claim": claim, "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": REAL_NETWORK_EVIDENCE},
    )

    assert checked[0]["status"] == "hypothesis", f"{claim} 不应为 supported"
    assert problems


def test_chinese_numeral_correct_reading_stays_supported():
    """正确的中文数字读数仍应通过（不因引入解析而误伤）。"""
    checked, problems = verify_claims(
        [{"claim": "网络延迟 十二毫秒", "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": REAL_NETWORK_EVIDENCE},
    )

    # 证据里没有正好 12 的观测（avg=12.5、data_points 有 12）；12 存在 -> 通过。
    assert checked[0]["status"] == "supported", problems


@pytest.mark.parametrize(
    "claim",
    [
        "CPU 使用率 999ms，最近10分钟",
        "过去5分钟 CPU 使用率 8888ms 峰值",
        "最近5分钟内出现过 999ms 的卡顿，CPU 使用率 12%",
        "disk usage 999ms since deploy",
    ],
)
def test_window_word_cannot_launder_a_duration_reading(claim):
    """句尾补一个“最近10分钟”不能把 999ms 洗成查询窗口（终验复核反例）。

    旧实现全句搜范围线索；只要句子里任何地方出现“最近/过去”，该时长就被
    当成窗口跳过。现在范围线索必须紧贴数值前面。
    """
    checked, problems = verify_claims(
        [{"claim": claim, "status": "supported", "evidence_ids": ["E1"]}],
        {"E1": REAL_CPU_PERCENT},
    )

    assert checked[0]["status"] == "hypothesis", f"{claim} 不应为 supported"
    assert problems


def test_server_rendered_value_comes_from_evidence_not_from_window_number():
    """服务端核算行不能把查询窗口的 10/5 当成读数渲染出去。"""
    checked, problems = verify_claims(
        [{"claim": "CPU 最近10分钟使用率 12%", "status": "supported",
          "evidence_ids": ["E1"]}],
        {"E1": REAL_CPU_PERCENT},
    )

    assert checked[0]["status"] == "supported", problems
    rendered = str(checked[0].get("rendered") or "")
    assert "12" in rendered, rendered
    assert "=10" not in rendered and "=5" not in rendered, rendered


def test_numberless_evidence_does_not_render_a_fabricated_zero():
    """证据没有数值读数时，不得渲染 metric=0 这种不存在的核算值。"""
    logs = {
        "id": "E9", "tool": "search_service_logs", "ok": True, "status": "ok",
        "live": True, "source": "tencent_cloud", "target": "svc-a",
        "data": {"logs": [{"level": "ERROR", "message": "upstream timeout"}]},
    }
    checked, problems = verify_claims(
        [{"claim": "日志中出现了上游超时", "status": "supported", "evidence_ids": ["E9"]}],
        {"E9": logs},
    )

    assert checked[0]["status"] == "supported", problems
    rendered = str(checked[0].get("rendered") or "")
    assert "=0" not in rendered, rendered
    assert "无数值读数" in rendered, rendered
