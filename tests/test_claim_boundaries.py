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
