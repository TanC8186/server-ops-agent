"""并发诊断的运行级隔离：两个 run 不得共享证据、目标或事件状态。

旧实现用同一个 LangGraph ``thread_id`` 承载多次运行，``past_steps`` 等
channel 可能跨 run 累积；该图已被移除。现在的隔离性质由
``run_diagnostic`` 的请求级运行时保证：每次调用生成独立 ``run_id``、独立
``RunTools``（证据列表、已绑定目标、工具预算都是实例属性）。

本文件使用**真实的** ``run_diagnostic`` / ``run_local`` / ``RunTools``，
只把 MCP 工具列表与本地规划模型替换为确定性假实现。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.agent.aiops import local_runtime
from app.config import config
from app.services import diagnostic_runtime

SERVICES = [
    {"service_name": "svc-a", "instance_id": "ins-a", "region": "ap-guangzhou"},
    {"service_name": "svc-b", "instance_id": "ins-b", "region": "ap-shanghai"},
]


class _NoArgs(BaseModel):
    pass


class _ServiceArgs(BaseModel):
    service_name: str | None = None


def _fake_mcp_tools(record: list):
    """每个 run 一套独立工具；record 只记录本 run 的调用。"""

    def list_all_services() -> dict:
        record.append(("list_all_services", {}))
        return {"services": list(SERVICES), "source": "tencent_cloud"}

    def query_memory_metrics(service_name: str) -> dict:
        record.append(("query_memory_metrics", {"service_name": service_name}))
        return {
            "source": "tencent_cloud",
            "service_name": service_name,
            "data_points": [{"metric": "memory_usage", "value": 42.0}],
        }

    return [
        StructuredTool.from_function(
            func=list_all_services,
            name="list_all_services",
            description="列出服务",
            args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            func=query_memory_metrics,
            name="query_memory_metrics",
            description="查询内存",
            args_schema=_ServiceArgs,
        ),
    ]


@pytest.fixture()
def concurrent_runtime(monkeypatch):
    """真实 RunTools；每个 run 有自己的调用记录与目标。"""
    created: list = []

    async def fake_create_run_tools(run_id, execution_mode, emit, *, deadline=None):
        from app.agent.aiops.runtime_tools import RunTools

        record: list = []
        tools = RunTools(_fake_mcp_tools(record), run_id, execution_mode, emit)
        tools.record = record
        tools.deadline = deadline
        created.append(tools)
        return tools

    async def fake_plan(user_input, target, tools, run_id, *, allow_cloud_fallback=False):
        assert allow_cloud_fallback is False
        # 两个 run 在规划阶段交错，确保并发而不是串行。
        await asyncio.sleep(0.01)
        return {
            "severity": "P2",
            "category": "memory",
            "plan": [
                {
                    "kind": "tool",
                    "tool": "list_all_services",
                    "args": {},
                    "description": f"discover:{target}",
                    "run": run_id,
                },
                {
                    "kind": "tool",
                    "tool": "query_memory_metrics",
                    "args": {"service_name": target},
                    "description": f"memory:{target}",
                    "run": run_id,
                },
            ],
        }

    monkeypatch.setattr(local_runtime, "create_run_tools", fake_create_run_tools)
    monkeypatch.setattr(local_runtime, "try_local_plan", fake_plan)
    monkeypatch.setattr(local_runtime, "_LOCAL_SLOT", asyncio.Semaphore(2))
    monkeypatch.setattr(config, "local_llm_base_url", "http://127.0.0.1:9999/v1")
    return created


async def _collect(service_name: str) -> list[dict]:
    return [
        event
        async for event in diagnostic_runtime.run_diagnostic(
            f"排查 {service_name} 的内存",
            f"session-{service_name}",
            "local",
            service_name=service_name,
        )
    ]


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_share_evidence(concurrent_runtime):
    events_a, events_b = await asyncio.gather(_collect("svc-a"), _collect("svc-b"))

    tools_a, tools_b = concurrent_runtime
    assert tools_a is not tools_b
    assert tools_a.evidence is not tools_b.evidence

    # 每个 run 的证据只来自自己的目标（服务发现证据在绑定前，target 为空）。
    data_targets_a = {
        item["target"] for item in tools_a.evidence if item["tool"] != "list_all_services"
    }
    data_targets_b = {
        item["target"] for item in tools_b.evidence if item["tool"] != "list_all_services"
    }
    assert data_targets_a == {"svc-a"}
    assert data_targets_b == {"svc-b"}
    assert [
        call[1].get("service_name") for call in tools_a.record if call[0] == "query_memory_metrics"
    ] == ["svc-a"]
    assert [
        call[1].get("service_name") for call in tools_b.record if call[0] == "query_memory_metrics"
    ] == ["svc-b"]

    # 证据 ID 在每个 run 内独立编号，不出现跨 run 递增。
    for events in (events_a, events_b):
        ids = [e["evidence_id"] for e in events if e["type"] == "tool_step"]
        assert ids == ["E1", "E2"], ids


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_share_event_state(concurrent_runtime):
    events_a, events_b = await asyncio.gather(_collect("svc-a"), _collect("svc-b"))

    run_ids_a = {event["run_id"] for event in events_a}
    run_ids_b = {event["run_id"] for event in events_b}
    assert len(run_ids_a) == 1 and len(run_ids_b) == 1
    assert run_ids_a != run_ids_b

    # 序号在每个 run 内从 1 连续递增，不会因为另一个 run 而跳号。
    for events in (events_a, events_b):
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
        assert {event["session_id"] for event in events} == {events[0]["session_id"]}

    # 目标数据工具互不渗透：A 的报告只引用 svc-a 的证据，B 只引用 svc-b。
    # （服务列表本身是全局目录，两个 run 都会看到 svc-a 与 svc-b。）
    def data_steps(events):
        return [
            event["step"]
            for event in events
            if event["type"] == "tool_step" and event["step"] != "list_all_services"
        ]

    assert data_steps(events_a) == ["query_memory_metrics"]
    assert "目标服务：`svc-a`" in events_a[-1]["response"]
    assert "目标服务：`svc-b`" in events_b[-1]["response"]

    # 每个 run 的目标数据工具结果只带自己的服务名：另一方的数据不会混进
    # 本次的证据预览（服务发现结果是全局目录，属于正常内容）。
    def data_preview(events):
        [preview] = [
            event["result_preview"]
            for event in events
            if event["type"] == "tool_step" and event["step"] == "query_memory_metrics"
        ]
        return json.loads(preview)

    preview_a = data_preview(events_a)
    preview_b = data_preview(events_b)
    assert preview_a["args"]["service_name"] == "svc-a"
    assert preview_a["target"] == "svc-a"
    assert preview_b["args"]["service_name"] == "svc-b"
    assert preview_b["target"] == "svc-b"

    assert events_a[-1]["type"] == "complete"
    assert events_b[-1]["type"] == "complete"


@pytest.mark.asyncio
async def test_run_tools_budget_and_target_are_per_instance(concurrent_runtime):
    await asyncio.gather(_collect("svc-a"), _collect("svc-b"))

    tools_a, tools_b = concurrent_runtime
    assert tools_a.target is not None and tools_b.target is not None
    assert tools_a.target.service == "svc-a"
    assert tools_b.target.service == "svc-b"
    assert tools_a.target.instance_id == "ins-a"
    assert tools_b.target.instance_id == "ins-b"

    tools_a.cancel()
    assert tools_a.cancelled is True
    assert tools_b.cancelled is False


@pytest.mark.asyncio
async def test_service_layer_is_stateless_between_runs(concurrent_runtime):
    from app.services import aiops_service as service_module

    service = service_module.AIOpsService()
    first, second = await asyncio.gather(
        _drain(service.execute("诊断 svc-a", "session-a")),
        _drain(service.execute("诊断 svc-b", "session-b")),
    )

    assert first[-1]["type"] == "complete"
    assert second[-1]["type"] == "complete"
    assert first[0]["run_id"] != second[0]["run_id"]


async def _drain(generator) -> list[dict]:
    return [event async for event in generator]
