"""BE-01 回归：引用真实证据 ID 不等于事实得到支持。

反例来自审查报告：E1 是“CPU=12%”的真实来源证据，模型写“CPU=99%，OOM 根因已
确认”并引用 E1，旧实现仍把它当 supported。现在必须：

- 结构化观测（metric/value/unit）逐条对照原始 evidence 校验；
- supported 声明里的数值必须能在被引用证据中找到，否则降级；
- 因果结论一律保留为 hypothesis；
- 确定性数值由服务端渲染，不用模型文本。
"""

from __future__ import annotations

import pytest

from app.core.claims import (
    Observation,
    render_observation,
    verify_claims,
)

CPU_EVIDENCE = {
    "id": "E1",
    "tool": "query_cpu_metrics",
    "ok": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "source": "tencent_cloud",
        "metric": "cpu_usage",
        "unit": "percent",
        "statistics": {"avg": 12.0, "max": 14.0},
        "data_points": [
            {"timestamp": 1700000000, "value": 11.0},
            {"timestamp": 1700000060, "value": 12.0},
            {"timestamp": 1700000120, "value": 14.0},
        ],
        "time_range": {"start_time": 1699999000, "end_time": 1700001000},
    },
}

LOG_EVIDENCE = {
    "id": "E2",
    "tool": "search_service_logs",
    "ok": True,
    "source": "tencent_cloud",
    "target": "svc-a",
    "data": {
        "source": "tencent_cloud",
        "total": 2,
        "logs": [
            {"level": "ERROR", "message": "upstream timeout after 3000ms"},
            {"level": "ERROR", "message": "upstream timeout after 5000ms"},
        ],
        "time_range": {"start_time": 1699999000, "end_time": 1700001000},
    },
}

DEMO_EVIDENCE = {
    "id": "E3",
    "tool": "query_memory_metrics",
    "ok": True,
    "source": "demo",
    "target": "svc-a",
    "data": {"source": "demo", "demo_mode": True, "statistics": {"avg": 96.0}},
}

ALL = {item["id"]: item for item in (CPU_EVIDENCE, LOG_EVIDENCE, DEMO_EVIDENCE)}


def _claim(text: str, ids: list[str], status: str = "supported") -> dict:
    return {"claim": text, "status": status, "evidence_ids": ids}


def test_correct_numeric_claim_is_supported_and_server_rendered():
    claim = _claim("CPU 平均使用率 12%", ["E1"])
    observations = [Observation(evidence_id="E1", metric="cpu_usage", value=12.0,
                                unit="percent", statistic="avg")]

    checked, problems = verify_claims([claim], ALL, observations=observations)

    assert checked[0]["status"] == "supported"
    assert problems == []
    rendered = checked[0]["rendered"]
    assert "12" in rendered
    assert "avg" in rendered or "平均" in rendered


def test_wrong_number_is_demoted_even_with_real_evidence_id():
    claim = _claim("CPU 使用率 99%", ["E1"])
    checked, problems = verify_claims([claim], ALL)
    assert checked[0]["status"] == "hypothesis"
    assert any("99" in problem for problem in problems)


def test_wrong_metric_is_demoted():
    observations = [Observation(evidence_id="E1", metric="memory_usage", value=12.0,
                                unit="percent")]
    checked, problems = verify_claims([_claim("内存 12%", ["E1"])], ALL,
                                      observations=observations)
    assert checked[0]["status"] == "hypothesis"
    assert any("指标" in problem for problem in problems)


def test_wrong_target_is_demoted():
    observations = [Observation(evidence_id="E1", metric="cpu_usage", value=12.0,
                                unit="percent", target="svc-b")]
    checked, problems = verify_claims([_claim("svc-b CPU 12%", ["E1"])], ALL,
                                      observations=observations)
    assert checked[0]["status"] == "hypothesis"
    assert any("目标" in problem for problem in problems)


def test_out_of_window_value_is_demoted():
    observations = [Observation(evidence_id="E1", metric="cpu_usage", value=77.0,
                                unit="percent", at=1600000000)]
    checked, problems = verify_claims([_claim("CPU 77%", ["E1"])], ALL,
                                      observations=observations)
    assert checked[0]["status"] == "hypothesis"
    assert any("时间" in problem or "窗口" in problem for problem in problems)


def test_demo_evidence_cannot_support():
    checked, problems = verify_claims([_claim("内存 96%", ["E3"])], ALL)
    assert checked[0]["status"] == "hypothesis"
    assert any("demo" in problem.lower() or "演示" in problem for problem in problems)


def test_failed_evidence_cannot_support():
    failed = dict(CPU_EVIDENCE, id="E9", ok=False)
    checked, problems = verify_claims([_claim("CPU 12%", ["E9"])], {"E9": failed})
    assert checked[0]["status"] == "hypothesis"


def test_unknown_evidence_id_is_removed():
    checked, problems = verify_claims([_claim("CPU 12%", ["E1", "E999"])], ALL)
    assert checked[0]["evidence_ids"] == ["E1"]
    assert any("不存在" in problem for problem in problems)


def test_causal_language_is_always_hypothesis():
    claim = _claim("OOM 是根因，CPU 12%", ["E1"])
    checked, problems = verify_claims([claim], ALL)
    assert checked[0]["status"] == "hypothesis"
    assert any("因果" in problem for problem in problems)


@pytest.mark.parametrize(
    "text",
    [
        "根因是内存泄漏",
        "确认由连接池耗尽导致",
        "这是因为上游超时",
        "已确认故障原因为磁盘写满",
        "root cause is a memory leak",
    ],
)
def test_causal_phrases_are_detected(text):
    checked, _ = verify_claims([_claim(text, ["E2"])], ALL)
    assert checked[0]["status"] == "hypothesis"


def test_claim_without_evidence_is_hypothesis():
    checked, problems = verify_claims([_claim("CPU 12%", [])], ALL)
    assert checked[0]["status"] == "hypothesis"
    assert problems


def test_hypothesis_status_is_preserved():
    checked, _ = verify_claims([_claim("可能有 GC 压力", ["E1"], status="hypothesis")], ALL)
    assert checked[0]["status"] == "hypothesis"


def test_unavailable_is_preserved():
    checked, _ = verify_claims([_claim("无法获取进程列表", [], status="unavailable")], ALL)
    assert checked[0]["status"] == "unavailable"


def test_observation_value_must_exist_in_evidence():
    observations = [Observation(evidence_id="E1", metric="cpu_usage", value=55.0,
                                unit="percent")]
    checked, problems = verify_claims([_claim("CPU 55%", ["E1"])], ALL,
                                      observations=observations)
    assert checked[0]["status"] == "hypothesis"
    assert any("55" in problem for problem in problems)


def test_observation_with_unknown_evidence_is_rejected():
    observations = [Observation(evidence_id="E404", metric="cpu_usage", value=1.0,
                                unit="percent")]
    _, problems = verify_claims([_claim("CPU 1%", ["E1"])], ALL, observations=observations)
    assert any("E404" in problem for problem in problems)


def test_render_observation_is_deterministic_from_evidence():
    observations = [Observation(evidence_id="E2", metric="log_count", value=2.0,
                                unit="count")]
    text = render_observation(observations[0], ALL["E2"])
    assert "2" in text


def test_plain_text_without_numbers_can_stay_supported():
    checked, problems = verify_claims([_claim("日志中出现了上游超时", ["E2"])], ALL)
    assert checked[0]["status"] == "supported"
    assert problems == []


def test_percentage_and_unit_variants_are_accepted():
    for text in ("CPU 使用率 12%", "CPU usage 12 percent", "CPU 12.0%"):
        checked, _ = verify_claims([_claim(text, ["E1"])], ALL)
        assert checked[0]["status"] == "supported", text


def test_number_in_claim_must_be_attributed_to_referenced_evidence():
    """E2 里没有 CPU 数值；引用 E2 却写 CPU 99% 必须降级。"""
    checked, problems = verify_claims([_claim("CPU 99% 且日志有超时", ["E2"])], ALL)
    assert checked[0]["status"] == "hypothesis"
    assert problems
