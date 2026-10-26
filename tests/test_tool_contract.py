"""BE-02 回归：工具返回必须区分成功/零命中/部分失败/权限不足/demo。"""

from __future__ import annotations

import pytest

from app.core.tool_contract import ToolResult, classify_payload


def test_access_denied_wrapped_as_zero_rows_is_an_error():
    """原始缺陷：CLS 把 AccessDenied 包成 total=0 / logs=[] / 成功查询 0 条。"""
    payload = {
        "total": 0,
        "logs": [],
        "error": "API 错误: [TencentCloudSDKException] code:AuthFailure.UnauthorizedOperation",
        "message": "成功查询 0 条日志",
    }

    result = classify_payload(payload)

    assert result.status == "error"
    assert result.error is not None
    assert "AuthFailure" in result.error["message"] + result.error["code"]
    assert not result.live


def test_genuine_zero_hits_is_empty_not_error():
    payload = {"total": 0, "logs": [], "message": "成功查询 0 条日志",
               "time_range": {"start_time": 1, "end_time": 2}, "source": "tencent_cloud"}

    result = classify_payload(payload)

    assert result.status == "empty"
    assert result.error is None
    assert result.live is True


def test_demo_data_can_never_support_live_observation():
    payload = {"ok": True, "source": "demo", "demo_mode": True, "total": 3,
               "logs": [{"message": "synthetic"}]}

    result = classify_payload(payload)

    assert result.demo is True
    assert result.live is False


def test_mixed_demo_source_is_still_demo():
    result = classify_payload({"source": "demo,tencent_cloud", "logs": [{"a": 1}]})
    assert result.demo is True
    assert result.live is False


def test_unavailable_payload_is_not_success():
    payload = {"ok": False, "source": "unavailable", "total": 0, "logs": [],
               "error": "未配置腾讯云凭证且未开启 DEMO_MODE"}

    result = classify_payload(payload)

    assert result.status == "error"
    assert result.live is False


def test_unsupported_tool_reports_unsupported():
    result = classify_payload({"status": "unsupported", "message": "需要 TAT"})
    assert result.status == "unsupported"
    assert result.live is False


def test_partial_subquery_failure_is_visible():
    payload = {
        "total": 4,
        "logs": [{"message": "a"}],
        "subqueries": [
            {"topic_id": "t1", "ok": True, "count": 4},
            {"topic_id": "t2", "ok": False, "error": "timeout"},
        ],
    }

    result = classify_payload(payload)

    assert result.status == "partial"
    assert [item["topic_id"] for item in result.subqueries] == ["t1", "t2"]
    assert result.error is not None


def test_error_with_rows_is_partial_not_ok():
    payload = {"logs": [{"message": "a"}], "error": "部分主题查询失败"}
    result = classify_payload(payload)
    assert result.status == "partial"
    assert result.error["message"].startswith("部分主题")


def test_plain_text_error_string():
    result = classify_payload("工具执行失败: connection refused")
    assert result.status == "error"
    assert result.error["code"] == "text_error"


def test_empty_string_is_empty():
    assert classify_payload("").status == "empty"


def test_non_empty_metric_points_are_ok():
    payload = {"source": "tencent_cloud", "data_points": [{"value": 12.0}],
               "statistics": {"avg": 12.0, "max": 12.0}}
    result = classify_payload(payload)
    assert result.status == "ok"
    assert result.live is True


def test_explicit_status_wins():
    result = classify_payload({"status": "timeout", "logs": []})
    assert result.status == "timeout"
    assert result.live is False


def test_invalid_status_rejected_when_constructed_directly():
    with pytest.raises(ValueError):
        ToolResult(status="succeeded")


def test_result_is_serializable_and_keeps_time_range():
    payload = {"logs": [{"m": 1}], "source": "tencent_cloud",
               "time_range": {"start_time": 1, "end_time": 2}}
    payload["subqueries"] = []
    result = classify_payload(payload)
    data = result.to_dict()
    assert data["status"] == "ok"
    assert data["live"] is True
