"""M1 目标绑定契约（BE-03 / BE-04）。

一次诊断只能绑定一个可信目标；多候选必须进入 target_required 由 UI 选择，
不能取列表第一项。绑定的范围包含 service/instance/region/topic/SSH，
工具参数不能覆盖这些字段。
"""

from __future__ import annotations

import pytest

from app.core.targets import (
    Target,
    TargetResolution,
    bind_tool_args,
    resolve_target,
)

CATALOG = [
    {"service_name": "svc-a", "instance_id": "ins-a", "region": "ap-guangzhou",
     "topics": ["topic-a-1"], "ssh_host": "10.0.0.1"},
    {"service_name": "svc-b", "instance_id": "ins-b", "region": "ap-shanghai",
     "topics": ["topic-b-1", "topic-b-2"]},
]


def test_explicit_request_is_bound():
    result = resolve_target(CATALOG, "svc-a", "随便看看")
    assert result.status == "bound"
    assert result.target == Target(
        service="svc-a", instance_id="ins-a", region="ap-guangzhou",
        topics=("topic-a-1",), ssh_host="10.0.0.1",
    )


def test_unknown_explicit_request_is_not_found():
    result = resolve_target(CATALOG, "svc-z", "CPU 高")
    assert result.status == "not_found"
    assert result.target is None
    assert list(result.candidates) == []


def test_unique_mention_in_symptom_is_bound():
    result = resolve_target(CATALOG, None, "svc-b 的请求超时")
    assert result.status == "bound"
    assert result.target is not None
    assert result.target.service == "svc-b"
    assert result.target.region == "ap-shanghai"


def test_multiple_mentions_require_explicit_choice():
    result = resolve_target(CATALOG, None, "svc-a 和 svc-b 同时异常")
    assert result.status == "target_required"
    assert result.target is None
    assert {candidate.service for candidate in result.candidates} == {"svc-a", "svc-b"}


def test_no_mention_with_multiple_candidates_requires_choice():
    """原始缺陷：没有提及时默认取列表第一项。"""
    result = resolve_target(CATALOG, None, "系统变慢了")
    assert result.status == "target_required"
    assert result.target is None
    assert {candidate.service for candidate in result.candidates} == {"svc-a", "svc-b"}


def test_no_mention_with_single_candidate_binds_it():
    result = resolve_target(CATALOG[:1], None, "系统变慢了")
    assert result.status == "bound"
    assert result.target is not None
    assert result.target.service == "svc-a"


def test_empty_catalog_is_not_found():
    result = resolve_target([], None, "任何症状")
    assert result.status == "not_found"


def test_malformed_catalog_rows_are_ignored():
    catalog = [
        {"instance_id": "ins-x"},                       # 缺 service_name
        {"service_name": "", "instance_id": "ins-y"},   # 空名
        "not-a-dict",
        {"service_name": "svc-ok", "instance_id": "ins-ok"},
    ]
    result = resolve_target(catalog, None, "svc-ok CPU 高")
    assert result.status == "bound"
    assert result.target.service == "svc-ok"


def test_partial_service_name_does_not_match():
    """`svc` 不能匹配 `svc-a`：避免模糊输入变成越权目标。"""
    result = resolve_target(CATALOG, None, "svc 出现异常")
    assert result.status == "target_required"


def test_resolution_is_serializable():
    result: TargetResolution = resolve_target(CATALOG, "svc-b", "")
    payload = result.to_dict()
    assert payload["status"] == "bound"
    assert payload["target"]["service"] == "svc-b"
    assert payload["target"]["topics"] == ["topic-b-1", "topic-b-2"]


@pytest.mark.parametrize(
    ("args", "expected_fragment"),
    [
        ({"service_name": "svc-b"}, "服务"),
        ({"instance_id": "ins-b"}, "实例"),
        ({"region_code": "ap-shanghai"}, "地域"),
        ({"region": "ap-shanghai"}, "地域"),
        ({"topic_id": "topic-b-2"}, "日志主题"),
    ],
)
def test_bound_tool_args_reject_overrides(args, expected_fragment):
    target = Target(service="svc-a", instance_id="ins-a", region="ap-guangzhou",
                    topics=("topic-a-1",))
    with pytest.raises(ValueError, match=expected_fragment):
        bind_tool_args(args, target)


def test_bound_tool_args_fill_and_allow_matching_values():
    target = Target(service="svc-a", instance_id="ins-a", region="ap-guangzhou",
                    topics=("topic-a-1",))
    filled = bind_tool_args(
        {"service_name": "svc-a"}, target,
        accepts=["service_name", "instance_id", "region_code"],
    )
    assert filled["service_name"] == "svc-a"
    assert filled["instance_id"] == "ins-a"
    assert filled["region_code"] == "ap-guangzhou"
    # schema 不接受的字段不会被注入
    assert "topic_id" not in filled


def test_bound_tool_args_only_fill_what_schema_accepts():
    target = Target(service="svc-a", instance_id="ins-a", region="ap-guangzhou")
    filled = bind_tool_args({"service_name": "svc-a"}, target,
                            accepts=["service_name", "keyword"])
    assert filled == {"service_name": "svc-a"}


def test_bound_tool_args_keep_allowed_topic():
    target = Target(service="svc-b", topics=("topic-b-1", "topic-b-2"))
    filled = bind_tool_args({"topic_id": "topic-b-2"}, target)
    assert filled["topic_id"] == "topic-b-2"


def test_ssh_scope_is_not_inferred_from_target():
    """SSH 只有显式映射时才属于目标；否则证据范围是独立主机（BE-04）。"""
    target = Target(service="svc-b", instance_id="ins-b")
    assert target.ssh_host == ""
    assert target.ssh_in_scope is False


def test_same_service_multiple_instances_are_distinct_targets():
    """同一服务的两个实例/地域不能被“按服务名去重”吞掉（BE-03）。"""
    catalog = [
        {"service_name": "svc-a", "instance_id": "ins-a1", "region": "ap-guangzhou"},
        {"service_name": "svc-a", "instance_id": "ins-a2", "region": "ap-shanghai"},
    ]

    result = resolve_target(catalog, "svc-a", "CPU 高")

    assert result.status == "target_required"
    assert {c.instance_id for c in result.candidates} == {"ins-a1", "ins-a2"}


def test_explicit_name_with_single_instance_still_binds():
    catalog = [
        {"service_name": "svc-a", "instance_id": "ins-a1", "region": "ap-guangzhou"},
    ]
    result = resolve_target(catalog, "svc-a", "CPU 高")
    assert result.status == "bound"
    assert result.target is not None
    assert result.target.instance_id == "ins-a1"


def test_duplicate_catalog_rows_are_collapsed():
    row = {"service_name": "svc-a", "instance_id": "ins-a1", "region": "ap-guangzhou"}
    result = resolve_target([row, dict(row), dict(row)], "svc-a", "CPU 高")
    assert result.status == "bound"
