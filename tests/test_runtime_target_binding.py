"""M1 回归：目标绑定贯通运行器（BE-03 / BE-04）。

原始缺陷：用户没指定服务时，运行器直接取服务列表第一项；绑定 svc-a 之后，
请求其他地域/主题的参数仍能通过，证据却被标成 svc-a。

本文件只使用真实 RunTools + 真实 langchain StructuredTool（schema 校验真实），
MCP 调用本身是本地函数。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agent.aiops.runtime_tools import RunTools, TargetRequiredError


class _ListArgs(BaseModel):
    region_code: str | None = Field(default=None)


class _ServiceArgs(BaseModel):
    service_name: str | None = Field(default=None)
    region_code: str | None = Field(default=None)


class _InstanceArgs(BaseModel):
    service_name: str | None = Field(default=None)
    instance_id: str | None = Field(default=None)


class _TopicArgs(BaseModel):
    topic_id: str = Field(...)
    start_time: int = 0
    end_time: int = 0


CATALOG = {
    "services": [
        {"service_name": "svc-a", "instance_id": "ins-a", "region": "ap-guangzhou",
         "topics": ["topic-a-1"]},
        {"service_name": "svc-b", "instance_id": "ins-b", "region": "ap-shanghai",
         "topics": ["topic-b-1"]},
    ]
}


def _tools(record: list) -> list:
    def list_all_services(region_code=None):
        record.append(("list_all_services", {"region_code": region_code}))
        return dict(CATALOG, source="tencent_cloud")

    def query_cpu_metrics(service_name=None, region_code=None):
        record.append(("query_cpu_metrics",
                       {"service_name": service_name, "region_code": region_code}))
        return {"source": "tencent_cloud", "data_points": [{"value": 12.0}]}

    def get_service_info(service_name=None, instance_id=None):
        record.append(("get_service_info",
                       {"service_name": service_name, "instance_id": instance_id}))
        return {"source": "tencent_cloud", "status": "healthy"}

    def search_log(topic_id, start_time=0, end_time=0):
        record.append(("search_log", {"topic_id": topic_id}))
        return {"source": "tencent_cloud", "logs": []}

    return [
        StructuredTool.from_function(func=list_all_services, name="list_all_services",
                                     description="列出服务", args_schema=_ListArgs),
        StructuredTool.from_function(func=query_cpu_metrics, name="query_cpu_metrics",
                                     description="CPU", args_schema=_ServiceArgs),
        StructuredTool.from_function(func=get_service_info, name="get_service_info",
                                     description="服务信息", args_schema=_InstanceArgs),
        StructuredTool.from_function(func=search_log, name="search_log",
                                     description="日志", args_schema=_TopicArgs),
    ]


@pytest.fixture()
def calls():
    return []


def _tools_for(calls) -> RunTools:
    return RunTools(_tools(calls), "run-1", "cloud_multi", lambda event: None)


@pytest.mark.asyncio
async def test_ambiguous_target_requires_choice_and_calls_no_data_tool(calls):
    tools = _tools_for(calls)

    with pytest.raises(TargetRequiredError) as exc:
        await tools.discover_service(None, "系统变慢了")

    error = exc.value
    assert {candidate.service for candidate in error.candidates} == {"svc-a", "svc-b"}
    assert [name for name, _ in calls] == ["list_all_services"], "未绑定前不得调用目标数据工具"
    assert tools.target is None


@pytest.mark.asyncio
async def test_explicit_unique_target_binds_full_scope(calls):
    tools = _tools_for(calls)

    service = await tools.discover_service("svc-b", "svc-b 变慢")

    assert service == "svc-b"
    assert tools.target is not None
    assert tools.target.region == "ap-shanghai"
    assert tools.target.instance_id == "ins-b"
    assert tools.target.topics == ("topic-b-1",)


@pytest.mark.asyncio
async def test_bound_region_is_injected_into_tool_args(calls):
    tools = _tools_for(calls)
    await tools.discover_service("svc-b", "")

    evidence = await tools.call("query_cpu_metrics", {"service_name": "svc-b"})

    assert evidence["ok"] is True
    name, args = calls[-1]
    assert name == "query_cpu_metrics"
    assert args["service_name"] == "svc-b"
    assert args["region_code"] == "ap-shanghai"


@pytest.mark.asyncio
async def test_cross_region_service_is_rejected(calls):
    tools = _tools_for(calls)
    await tools.discover_service("svc-a", "")

    evidence = await tools.call("query_cpu_metrics",
                                {"service_name": "svc-b", "region_code": "ap-shanghai"})

    assert evidence["ok"] is False
    assert "服务" in evidence["error"]
    assert [name for name, _ in calls if name != "list_all_services"] == []


@pytest.mark.asyncio
async def test_region_override_is_rejected(calls):
    tools = _tools_for(calls)
    await tools.discover_service("svc-a", "")

    evidence = await tools.call("query_cpu_metrics",
                                {"service_name": "svc-a", "region_code": "ap-shanghai"})

    assert evidence["ok"] is False
    assert "地域" in evidence["error"]


@pytest.mark.asyncio
async def test_unknown_instance_override_is_rejected(calls):
    tools = _tools_for(calls)
    await tools.discover_service("svc-a", "")

    evidence = await tools.call("get_service_info",
                                {"service_name": "svc-a", "instance_id": "ins-b"})

    assert evidence["ok"] is False
    assert "实例" in evidence["error"]


@pytest.mark.asyncio
async def test_topic_outside_bound_service_is_rejected(calls):
    tools = _tools_for(calls)
    await tools.discover_service("svc-a", "")

    evidence = await tools.call("search_log", {"topic_id": "topic-b-1"})

    assert evidence["ok"] is False
    assert "日志主题" in evidence["error"]


@pytest.mark.asyncio
async def test_topic_inside_bound_service_is_allowed(calls):
    tools = _tools_for(calls)
    await tools.discover_service("svc-a", "")

    evidence = await tools.call("search_log", {"topic_id": "topic-a-1"})

    assert evidence["ok"] is True
    assert calls[-1][1]["topic_id"] == "topic-a-1"


@pytest.mark.asyncio
async def test_unknown_service_stops_before_any_data_tool(calls):
    tools = _tools_for(calls)

    with pytest.raises(ValueError) as exc:
        await tools.discover_service("svc-z", "")

    assert "发现列表" in str(exc.value)
    assert [name for name, _ in calls] == ["list_all_services"]


@pytest.mark.asyncio
async def test_data_tool_without_binding_is_refused(calls):
    tools = _tools_for(calls)

    evidence = await tools.call("query_cpu_metrics", {"service_name": "svc-a"})

    assert evidence["ok"] is False
    assert "目标" in evidence["error"]


@pytest.mark.asyncio
async def test_ssh_evidence_is_marked_as_outside_target_scope(calls):
    from servers import ssh_server  # noqa: F401  (tool name mapping only)

    tools = _tools_for(calls)
    await tools.discover_service("svc-a", "")
    tools.by_name["get_system_info"] = _ssh_tool(calls)

    evidence = await tools.call("get_system_info", {})

    assert evidence["target"] == "configured_ssh_endpoint"
    assert "target_service" not in evidence or evidence["target_service"] == "svc-a"


def _ssh_tool(calls):
    class _Empty(BaseModel):
        pass

    def get_system_info():
        calls.append(("get_system_info", {}))
        return {"os": "Linux"}

    return StructuredTool.from_function(func=get_system_info, name="get_system_info",
                                       description="系统信息", args_schema=_Empty)


@pytest.mark.asyncio
async def test_evidence_records_structured_status(calls):
    tools = _tools_for(calls)
    await tools.discover_service("svc-a", "")

    evidence = await tools.call("search_log", {"topic_id": "topic-a-1"})

    assert evidence["status"] == "empty", "真实零命中必须与错误分开"
    assert evidence["source_kind"] == "tencent_cloud"
    assert evidence["target"] == "svc-a"


@pytest.mark.asyncio
async def test_tool_timeout_is_a_distinct_status(calls, monkeypatch):
    import asyncio

    from app.config import config

    class _Slow(BaseModel):
        pass

    async def slow():
        await asyncio.sleep(5)
        return {}

    tools = _tools_for(calls)
    tools.by_name["slow_tool"] = StructuredTool.from_function(
        coroutine=slow, name="slow_tool", description="慢", args_schema=_Slow,
    )
    monkeypatch.setattr(config, "diagnostic_tool_timeout_seconds", 0.05)
    await tools.discover_service("svc-a", "")

    evidence = await tools.call("slow_tool", {})

    assert evidence["status"] == "timeout"
    assert evidence["ok"] is False


@pytest.mark.asyncio
async def test_demo_source_is_flagged_and_not_live(calls):
    def demo_metric(service_name=None, region_code=None):
        return {"source": "demo", "demo_mode": True, "data_points": [{"value": 96.0}]}

    tools = _tools_for(calls)
    tools.by_name["query_cpu_metrics"] = StructuredTool.from_function(
        func=demo_metric, name="query_cpu_metrics", description="CPU",
        args_schema=_ServiceArgs,
    )
    await tools.discover_service("svc-a", "")

    evidence = await tools.call("query_cpu_metrics", {"service_name": "svc-a"})

    assert evidence["demo"] is True
    assert evidence["live"] is False
    assert evidence["status"] == "ok"
