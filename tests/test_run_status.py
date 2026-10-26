"""BE-05 回归：生命周期、诊断质量、工具结果必须是三个独立状态。"""

from __future__ import annotations

import pytest

from app.core.run_status import (
    DiagnosisQuality,
    Lifecycle,
    evaluate_quality,
    summarize_tool_results,
)


def _tool(status: str, *, live: bool = True, demo: bool = False, target: str = "svc-a") -> dict:
    return {"status": status, "live": live, "demo": demo, "target": target}


def test_lifecycle_and_quality_are_separate_fields():
    result = evaluate_quality(
        tool_results=[_tool("error"), _tool("unavailable", live=False)],
        evidence_gaps=["CPU 查询失败"],
        target_bound=True,
    )
    assert result.lifecycle == Lifecycle.COMPLETED
    assert result.quality == DiagnosisQuality.INSUFFICIENT
    payload = result.to_dict()
    assert payload["lifecycle"] == "completed"
    assert payload["quality"] == "insufficient"


def test_zero_evidence_is_insufficient_not_success():
    result = evaluate_quality(tool_results=[], evidence_gaps=[], target_bound=True)
    assert result.quality == DiagnosisQuality.INSUFFICIENT
    assert result.reason


def test_all_tools_failed_is_insufficient():
    result = evaluate_quality(
        tool_results=[_tool("error"), _tool("timeout")], evidence_gaps=[], target_bound=True,
    )
    assert result.quality == DiagnosisQuality.INSUFFICIENT


def test_partial_success_is_partial():
    result = evaluate_quality(
        tool_results=[_tool("ok"), _tool("error")], evidence_gaps=[], target_bound=True,
    )
    assert result.quality == DiagnosisQuality.PARTIAL


def test_full_success_with_observations_is_sufficient():
    result = evaluate_quality(
        tool_results=[_tool("ok"), _tool("empty")], evidence_gaps=[], target_bound=True,
    )
    assert result.quality == DiagnosisQuality.SUFFICIENT


def test_demo_only_evidence_cannot_be_sufficient():
    result = evaluate_quality(
        tool_results=[_tool("ok", live=False, demo=True)], evidence_gaps=[], target_bound=True,
    )
    assert result.quality == DiagnosisQuality.INSUFFICIENT
    assert any("演示" in gap or "demo" in gap for gap in result.gaps)


def test_ssh_evidence_outside_target_scope_is_a_gap():
    result = evaluate_quality(
        tool_results=[_tool("ok"), _tool("ok", target="configured_ssh_endpoint")],
        evidence_gaps=[], target_bound=True,
    )
    assert any("SSH" in gap for gap in result.gaps)
    assert result.quality in {DiagnosisQuality.PARTIAL, DiagnosisQuality.SUFFICIENT}


def test_discovery_only_evidence_is_not_sufficient():
    """只有服务发现、时间或知识依据，不能算当前诊断证据充分。"""
    result = evaluate_quality(
        tool_results=[
            {"status": "ok", "live": True, "demo": False, "target": "",
             "tool": "list_all_services"},
            {"status": "ok", "live": True, "demo": False, "target": "",
             "tool": "get_current_time"},
        ],
        evidence_gaps=[], target_bound=True,
    )
    assert result.quality == DiagnosisQuality.INSUFFICIENT


def test_knowledge_only_evidence_is_not_sufficient():
    result = evaluate_quality(
        tool_results=[{"status": "ok", "live": True, "demo": False,
                       "target": "", "kind": "knowledge", "tool": "retrieve_knowledge"}],
        evidence_gaps=[], target_bound=True,
    )
    assert result.quality == DiagnosisQuality.INSUFFICIENT


def test_unbound_target_is_flagged():
    result = evaluate_quality(tool_results=[_tool("ok")], evidence_gaps=[],
                              target_bound=False)
    assert result.quality != DiagnosisQuality.SUFFICIENT
    assert any("目标" in gap for gap in result.gaps)


@pytest.mark.parametrize("status", ["error", "timeout", "unavailable", "unsupported"])
def test_failure_statuses_count_as_failures(status):
    summary = summarize_tool_results([_tool(status), _tool("ok")])
    assert summary.failed == 1
    assert summary.succeeded == 1
    assert summary.total == 2


def test_empty_result_is_not_a_failure_but_not_evidence_of_absence():
    summary = summarize_tool_results([_tool("empty")])
    assert summary.failed == 0
    assert summary.empty == 1
    assert summary.succeeded == 0


def test_summary_counts_demo_separately():
    summary = summarize_tool_results([_tool("ok", live=False, demo=True), _tool("ok")])
    assert summary.demo == 1
    assert summary.live_successes == 1


def test_quality_never_claims_success_for_aborted_lifecycle():
    result = evaluate_quality(tool_results=[_tool("ok")], evidence_gaps=[],
                              target_bound=True, lifecycle=Lifecycle.CANCELLED)
    assert result.lifecycle == Lifecycle.CANCELLED
    assert result.quality != DiagnosisQuality.SUFFICIENT
