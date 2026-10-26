"""BE-02 回归：CLS 包装层不得吞掉底层 error / demo / 部分成功状态。

历史缺陷：``search_service_logs`` / ``analyze_log_pattern`` 把底层
``search_log`` / ``search_topic_by_service_name`` 的失败状态丢掉，只返回
``{total: 0, logs: [], message: "成功查询 0 条"}``，上层
``classify_payload`` 因此把 AccessDenied 判成 empty（“没有错误日志”）。

所有用例都用 monkeypatch 替换本模块内的 ``search_log`` /
``search_topic_by_service_name``，不连真实腾讯云。
"""

from __future__ import annotations

import pytest

import servers.cls_server as cls_server
from app.core.tool_contract import classify_payload

_ACCESS_DENIED = {
    "ok": False,
    "source": "tencent_cloud",
    "total": 0,
    "logs": [],
    "error": {
        "code": "AuthFailure.UnauthorizedOperation",
        "message": "API 错误: [TencentCloudSDKException] 无权限访问该日志主题",
    },
    "message": "查询失败: 无权限访问该日志主题",
}


def _topic_ok(topic_id: str = "topic-001", source: str = "tencent_cloud") -> dict:
    return {
        "source": source,
        "total": 1,
        "topics": [
            {
                "topic_id": topic_id,
                "topic_name": f"{topic_id}-日志",
                "service_name": "data-sync-service",
                "region_code": "ap-beijing",
                "source": source,
            }
        ],
        "message": "找到 1 个匹配的日志主题",
    }


def _logs_ok(count: int = 2, source: str = "tencent_cloud") -> dict:
    logs = [{"message": f"log-{i}", "level": "ERROR"} for i in range(count)]
    return {
        "ok": True,
        "source": source,
        "total": count,
        "logs": logs,
        "message": f"成功查询 {count} 条日志",
    }


def _logs_empty(source: str = "tencent_cloud") -> dict:
    return {
        "ok": True,
        "source": source,
        "total": 0,
        "logs": [],
        "message": "成功查询 0 条日志",
    }


def test_topic_access_denied_is_error_not_missing_topic(monkeypatch):
    """主题查询 AccessDenied 必须报 error，不能返回“未找到主题”的成功形状。"""
    monkeypatch.setattr(
        cls_server, "search_topic_by_service_name",
        lambda *a, **kw: {
            "ok": False,
            "source": "tencent_cloud",
            "total": 0,
            "topics": [],
            "error": {
                "code": "AuthFailure.UnauthorizedOperation",
                "message": "API 错误: 无权限访问 DescribeTopics",
            },
            "message": "查询失败",
        },
    )
    # 主题查询失败时不应再调用 search_log
    monkeypatch.setattr(
        cls_server, "search_log",
        lambda *a, **kw: pytest.fail("主题查询失败时不应执行日志检索"),
    )

    result = cls_server.search_service_logs("data-sync-service")

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["source"] == "tencent_cloud"
    assert "AuthFailure" in result["error"]["code"] + result["error"]["message"]
    assert "未找到" not in result["message"]

    classified = classify_payload(result)
    assert classified.status == "error"
    assert not classified.live


def test_topic_unavailable_is_error_not_empty(monkeypatch):
    """source=unavailable 的主题查询不能被当成零命中。"""
    monkeypatch.setattr(
        cls_server, "search_topic_by_service_name",
        lambda *a, **kw: {
            "ok": False,
            "source": "unavailable",
            "total": 0,
            "topics": [],
            "message": "未配置腾讯云凭证且未开启 DEMO_MODE",
        },
    )
    monkeypatch.setattr(cls_server, "search_log", lambda *a, **kw: pytest.fail("不应检索"))

    result = cls_server.search_service_logs("data-sync-service")

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["source"] == "unavailable"
    assert result["error"] is not None
    assert classify_payload(result).status == "error"


def test_partial_search_failure_is_reported(monkeypatch):
    """一个 topic 成功、一个失败：ok=True 但必须带 partial 与失败明细。"""
    monkeypatch.setattr(
        cls_server, "search_topic_by_service_name",
        lambda *a, **kw: {
            "source": "tencent_cloud",
            "total": 2,
            "topics": [
                {"topic_id": "t-ok", "topic_name": "ok", "region_code": "ap-beijing"},
                {"topic_id": "t-denied", "topic_name": "denied", "region_code": "ap-beijing"},
            ],
        },
    )

    def fake_search_log(topic_id, *a, **kw):
        if topic_id == "t-denied":
            return dict(_ACCESS_DENIED)
        return _logs_ok(2)

    monkeypatch.setattr(cls_server, "search_log", fake_search_log)

    result = cls_server.search_service_logs("data-sync-service")

    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["total"] == 2
    assert result["partial_errors"]
    assert result["partial_errors"][0]["topic_id"] == "t-denied"
    statuses = {item["topic_id"]: item["status"] for item in result["subqueries"]}
    assert statuses == {"t-ok": "ok", "t-denied": "error"}

    classified = classify_payload(result)
    assert classified.status == "partial"
    assert classified.error is not None


def test_all_topics_failed_is_error(monkeypatch):
    """全部 topic 失败不得算成 total=0 的成功。"""
    monkeypatch.setattr(
        cls_server, "search_topic_by_service_name",
        lambda *a, **kw: {
            "source": "tencent_cloud",
            "total": 2,
            "topics": [
                {"topic_id": "t1", "topic_name": "a", "region_code": "ap-beijing"},
                {"topic_id": "t2", "topic_name": "b", "region_code": "ap-beijing"},
            ],
        },
    )
    monkeypatch.setattr(cls_server, "search_log", lambda *a, **kw: dict(_ACCESS_DENIED))

    result = cls_server.search_service_logs("data-sync-service")

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["total"] == 0
    assert result["error"]["code"] == "AuthFailure.UnauthorizedOperation"
    assert len(result["subqueries"]) == 2
    assert all(item["status"] == "error" for item in result["subqueries"])
    assert classify_payload(result).status == "error"


def test_genuine_zero_hits_is_empty(monkeypatch):
    """查询全部成功但零命中：status=empty，不是 error。"""
    monkeypatch.setattr(
        cls_server, "search_topic_by_service_name", lambda *a, **kw: _topic_ok()
    )
    monkeypatch.setattr(cls_server, "search_log", lambda *a, **kw: _logs_empty())

    result = cls_server.search_service_logs("data-sync-service")

    assert result["ok"] is True
    assert result["status"] == "empty"
    assert result["total"] == 0
    assert "error" not in result

    classified = classify_payload(result)
    assert classified.status == "empty"
    assert classified.error is None


def test_demo_source_survives_and_is_not_live(monkeypatch):
    """demo 来源必须透出 source=demo 与 demo=True。"""
    monkeypatch.setattr(
        cls_server, "search_topic_by_service_name",
        lambda *a, **kw: _topic_ok(source="demo"),
    )
    monkeypatch.setattr(
        cls_server, "search_log",
        lambda *a, **kw: {**_logs_ok(3, source="demo"), "demo_mode": True},
    )

    result = cls_server.search_service_logs("data-sync-service")

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["source"] == "demo"
    assert result["demo"] is True

    classified = classify_payload(result)
    assert classified.demo is True
    assert classified.live is False


def test_analyze_all_levels_failed_is_error(monkeypatch):
    """三个级别全部失败必须报 error，并保留 level_summary 字段。"""
    monkeypatch.setattr(
        cls_server, "search_service_logs", lambda *a, **kw: dict(_ACCESS_DENIED)
    )

    result = cls_server.analyze_log_pattern("data-sync-service")

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"] is not None
    assert set(result["level_summary"]) == {"ERROR", "WARN", "INFO"}
    assert result["level_summary"] == {"ERROR": 0, "WARN": 0, "INFO": 0}
    assert result["total_count"] == 0
    assert classify_payload(result).status == "error"


def test_analyze_partial_failure_keeps_summary(monkeypatch):
    """部分级别失败：status=partial，level_summary/level_percent 仍保留。"""
    def fake_search(service_name, log_level=None, **kw):
        if log_level == "WARN":
            return dict(_ACCESS_DENIED)
        if log_level == "ERROR":
            return _logs_ok(2)
        return _logs_ok(3)

    monkeypatch.setattr(cls_server, "search_service_logs", fake_search)

    result = cls_server.analyze_log_pattern("data-sync-service")

    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["level_summary"] == {"ERROR": 2, "WARN": 0, "INFO": 3}
    assert result["total_count"] == 5
    assert result["level_percent"]["ERROR"] == 40.0
    assert result["partial_errors"][0]["level"] == "WARN"
    assert classify_payload(result).status == "partial"


def test_analyze_success_and_empty(monkeypatch):
    """全部成功有数据 -> ok；全部成功零命中 -> empty（不是 error）。"""
    monkeypatch.setattr(
        cls_server, "search_service_logs", lambda *a, **kw: _logs_ok(1)
    )
    ok_result = cls_server.analyze_log_pattern("data-sync-service")
    assert ok_result["ok"] is True
    assert ok_result["status"] == "ok"
    assert ok_result["level_percent"]["ERROR"] == 33.3

    monkeypatch.setattr(
        cls_server, "search_service_logs", lambda *a, **kw: _logs_empty()
    )
    empty_result = cls_server.analyze_log_pattern("data-sync-service")
    assert empty_result["ok"] is True
    assert empty_result["status"] == "empty"
    assert empty_result["total_count"] == 0
    assert "error" not in empty_result
    assert classify_payload(empty_result).status == "empty"
