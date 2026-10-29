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


def test_plain_text_success_is_not_an_error():
    """纯文本成功返回不得被判成 error。

    实测（G8，目标切到 SSH 主机后）：本地计划里的 `get_current_time` 成功返回
    "2026-10-29 18:56:59"，旧实现把所有非空字符串都判成 error，于是证据表里
    出现一条 `get_current_time · 不可用 · unavailable`——时间戳被当成工具故障。
    """
    result = classify_payload("2026-10-29 18:56:59")
    assert result.status == "ok", "时间戳是正常返回，不能记成工具失败"
    assert result.data == "2026-10-29 18:56:59"


def test_text_error_shapes_are_still_errors():
    """文本型的真失败必须仍然判成 error（不能为了修上面那条把失败也放过去）。"""
    for text in (
        "获取时间失败: 时区不合法",
        "工具执行失败: connection refused",
        "Error: upstream 502",
    ):
        result = classify_payload(text)
        assert result.status == "error", f"{text!r} 应判为 error"
        assert result.error["code"] == "text_error"


def test_long_text_mentioning_failure_is_not_an_error():
    """长正文里偶尔出现"失败"字样不算失败——判据要求同时简短且带标记。"""
    long_text = "查询完成。" + "本次巡检覆盖全部 12 个检查项。" * 20 + "其中 0 条失败记录。"
    assert classify_payload(long_text).status == "ok"
