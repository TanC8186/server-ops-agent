"""BE-03 跨层回归：目标歧义是“需要选择”，不是执行失败。

真实链路：``aiops_service.diagnose`` -> ``run_diagnostic`` -> 真实云图/真实本地
运行器。只替换两个外部边界：

1. MCP 工具列表（用真实 ``StructuredTool``，保留 schema 校验）；
2. 聊天模型（云端）或本地 planner（本地模式）。

断言重点：目标不唯一时零目标数据工具调用、唯一终态是 ``complete`` 且
``status="target_required"``、候选与原因进入事件与 trace、本地 planner 根本
不会被调用；唯一候选仍然自动绑定。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agent.aiops import cloud_multi, local_runtime, runtime_tools
from app.api import aiops as aiops_api
from app.config import config
from app.services.aiops_service import aiops_service

TWO_SERVICES = [
    {"service_name": "svc-a", "instance_id": "ins-a", "region": "ap-guangzhou"},
    {"service_name": "svc-b", "instance_id": "ins-b", "region": "ap-shanghai"},
]
ONE_SERVICE = TWO_SERVICES[:1]
AMBIGUOUS_SYMPTOM = "系统整体变慢，用户反馈请求延迟升高，请排查"


class _NoArgs(BaseModel):
    pass


class _ServiceArgs(BaseModel):
    service_name: str | None = Field(default=None)
    region_code: str | None = Field(default=None)


def _fake_mcp_tools(calls: list, services: list) -> list:
    """真实 StructuredTool；每次调用都记录 (工具名, 实际参数)。"""

    def list_all_services() -> dict:
        calls.append(("list_all_services", {}))
        return {"services": services, "source": "tencent_cloud"}

    def query_cpu_metrics(service_name: str | None = None,
                          region_code: str | None = None) -> dict:
        calls.append(("query_cpu_metrics",
                      {"service_name": service_name, "region_code": region_code}))
        return {"source": "tencent_cloud",
                "data_points": [{"timestamp": 1700000000, "value": 12.0}]}

    def search_service_logs(service_name: str | None = None,
                            region_code: str | None = None) -> dict:
        calls.append(("search_service_logs",
                      {"service_name": service_name, "region_code": region_code}))
        return {"source": "tencent_cloud", "total": 1,
                "logs": [{"level": "ERROR", "message": "upstream timeout"}]}

    return [
        StructuredTool.from_function(func=list_all_services, name="list_all_services",
                                     description="列出服务", args_schema=_NoArgs),
        StructuredTool.from_function(func=query_cpu_metrics, name="query_cpu_metrics",
                                     description="查询 CPU", args_schema=_ServiceArgs),
        StructuredTool.from_function(func=search_service_logs, name="search_service_logs",
                                     description="查询日志", args_schema=_ServiceArgs),
    ]


class _ScriptedModel:
    """按 system prompt 判别角色，只产出确定性结构。"""

    async def ainvoke(self, messages):
        system = messages[0][1]
        if "Supervisor" in system:
            payload = {"assignments": [
                {"agent": "metrics", "focus": "检查 CPU",
                 "tools": ["query_cpu_metrics"]},
                {"agent": "logs", "focus": "检查错误日志",
                 "tools": ["search_service_logs"]},
            ]}
        elif "Reviewer" in system:
            payload = {"observations": [], "hypotheses": [],
                       "recommendations": ["复核上游超时时段"], "evidence_gaps": []}
        elif "独立的 metrics" in system:
            payload = {"steps": [{"tool": "query_cpu_metrics",
                                  "args": {"service_name": "svc-a"}}],
                       "findings": [], "done": True}
        else:
            payload = {"steps": [{"tool": "search_service_logs",
                                  "args": {"service_name": "svc-a"}}],
                       "findings": [], "done": True}
        return SimpleNamespace(content=json.dumps(payload),
                               usage_metadata={"input_tokens": 1, "output_tokens": 1})


@pytest.fixture()
def bridge(monkeypatch):
    """只替换 MCP 工具列表；工具调用记录和目录可由用例调整。"""
    state: dict = {"calls": [], "services": list(TWO_SERVICES)}

    async def fake_list():
        return _fake_mcp_tools(state["calls"], state["services"])

    monkeypatch.setattr(runtime_tools, "get_mcp_tools_cached", fake_list)
    monkeypatch.setattr(config, "llm_api_key", "sk-unit-test")
    monkeypatch.setattr(config, "local_llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(config, "diagnostic_timeout_seconds", 15.0)
    return state


def _terminal(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("type") in {"complete", "error"}]


def _trace_records(trace_dir, run_id: str) -> list[dict]:
    path = trace_dir / f"{run_id}.jsonl"
    assert path.exists(), f"缺少 trace 文件: {path}"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_same_terminal_contract(events: list[dict], calls: list) -> dict:
    """两种模式共享的终态契约：唯一 complete、候选齐全、无数据工具调用。"""
    terminal = _terminal(events)
    assert len(terminal) == 1, f"必须恰好一个终态: {[e['type'] for e in events]}"
    assert terminal[0]["type"] == "complete"
    assert terminal[0]["status"] == "target_required"
    assert terminal[0]["lifecycle"] == "completed"
    assert terminal[0]["quality"] == "insufficient"
    assert terminal[0]["response"].strip(), "目标歧义也要给出可读说明"

    required = next(event for event in events if event["type"] == "target_required")
    assert required["reason"]
    assert {item["service"] for item in required["candidates"]} == {"svc-a", "svc-b"}
    assert all({"service", "region"} <= set(item) for item in required["candidates"])
    assert {item["service"] for item in terminal[0]["candidates"]} == {"svc-a", "svc-b"}

    # 只允许服务发现；任何目标数据工具都意味着“悄悄选了第一个”。
    assert [name for name, _ in calls] == ["list_all_services"]
    return terminal[0]


@pytest.mark.asyncio
async def test_cloud_ambiguity_completes_with_target_required(bridge, monkeypatch,
                                                              isolated_trace_dir):
    def forbidden_model(**kwargs):
        raise AssertionError("目标歧义时不得调用聊天模型")

    monkeypatch.setattr(cloud_multi, "create_chat_model", forbidden_model)

    events = [event async for event in aiops_service.diagnose(
        "session-ambiguous-cloud", execution_mode="cloud_multi",
        symptom=AMBIGUOUS_SYMPTOM)]

    terminal = _assert_same_terminal_contract(events, bridge["calls"])

    records = _trace_records(isolated_trace_dir, terminal["run_id"])
    target_records = [record for record in records if record["event"] == "target_required"]
    assert len(target_records) == 1, [record["event"] for record in records]
    assert {item["service"] for item in target_records[0]["candidates"]} == {"svc-a", "svc-b"}
    assert target_records[0]["reason"]
    lifecycle = [record["event"] for record in records
                 if record["event"] in {"run_complete", "run_error"}]
    assert lifecycle == ["run_complete"]


@pytest.mark.asyncio
async def test_local_ambiguity_skips_planner_and_completes(bridge, monkeypatch,
                                                           isolated_trace_dir):
    async def forbidden_plan(*args, **kwargs):
        raise AssertionError("目标歧义时不得调用本地 planner")

    monkeypatch.setattr(local_runtime, "try_local_plan", forbidden_plan)
    monkeypatch.setattr("app.core.llm_factory.create_chat_model",
                        lambda **kwargs: pytest.fail("本地模式不得构造云模型"))

    events = [event async for event in aiops_service.diagnose(
        "session-ambiguous-local", execution_mode="local", symptom=AMBIGUOUS_SYMPTOM)]

    terminal = _assert_same_terminal_contract(events, bridge["calls"])
    assert all(event["execution_mode"] == "local" for event in events)

    records = _trace_records(isolated_trace_dir, terminal["run_id"])
    target_records = [record for record in records if record["event"] == "target_required"]
    assert len(target_records) == 1, [record["event"] for record in records]
    assert {item["service"] for item in target_records[0]["candidates"]} == {"svc-a", "svc-b"}


@pytest.mark.asyncio
async def test_single_candidate_still_auto_binds_in_local_mode(bridge, monkeypatch):
    bridge["services"] = list(ONE_SERVICE)

    async def plan(symptom, service, tools, run_id="", *, allow_cloud_fallback=True):
        assert service == "svc-a", "唯一候选必须被自动绑定"
        assert allow_cloud_fallback is False
        return {"severity": "P2", "category": "cpu", "plan": [
            {"kind": "tool", "tool": "list_all_services", "args": {}, "description": "discover"},
            {"kind": "tool", "tool": "query_cpu_metrics",
             "args": {"service_name": "svc-a"}, "description": "cpu"},
        ]}

    monkeypatch.setattr(local_runtime, "try_local_plan", plan)

    events = [event async for event in aiops_service.diagnose(
        "session-single-local", execution_mode="local", symptom=AMBIGUOUS_SYMPTOM)]

    assert not any(event["type"] == "target_required" for event in events)
    assert _terminal(events)[-1]["type"] == "complete"
    assert _terminal(events)[-1].get("status") != "target_required"
    data_calls = [(name, args) for name, args in bridge["calls"] if name != "list_all_services"]
    assert [name for name, _ in data_calls] == ["query_cpu_metrics"]
    assert data_calls[0][1]["service_name"] == "svc-a"
    assert data_calls[0][1]["region_code"] == "ap-guangzhou"


@pytest.mark.asyncio
async def test_single_candidate_still_auto_binds_in_cloud_mode(bridge, monkeypatch):
    bridge["services"] = list(ONE_SERVICE)
    monkeypatch.setattr(cloud_multi, "create_chat_model", lambda **kwargs: _ScriptedModel())

    events = [event async for event in aiops_service.diagnose(
        "session-single-cloud", execution_mode="cloud_multi", symptom=AMBIGUOUS_SYMPTOM)]

    assert not any(event["type"] == "target_required" for event in events)
    terminal = _terminal(events)
    assert len(terminal) == 1
    assert terminal[0]["type"] == "complete"
    assert terminal[0]["status"] == "completed"
    names = [name for name, _ in bridge["calls"]]
    assert names[0] == "list_all_services"
    assert set(names[1:]) == {"query_cpu_metrics", "search_service_logs"}
    assert all(args["service_name"] == "svc-a" for name, args in bridge["calls"]
               if name != "list_all_services")


def _sse_events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_http_target_choice_binds_selected_service_only(bridge, monkeypatch):
    """POST /api/aiops 携带 target_choice 时绑定该服务，且不查询其他服务。

    只挂载被测 router：主应用 lifespan 会连接 Milvus。服务层使用真实单例，
    这样 ``_service_kwargs`` 的签名投影（target_choice -> 运行时）也被覆盖。
    """
    bridge["services"] = list(TWO_SERVICES)

    async def plan(symptom, service, tools, run_id="", *, allow_cloud_fallback=True):
        assert service == "svc-b", "用户显式选择必须成为绑定目标"
        assert allow_cloud_fallback is False
        return {"severity": "P2", "category": "cpu", "plan": [
            {"kind": "tool", "tool": "query_cpu_metrics",
             "args": {"service_name": "svc-b"}, "description": "cpu"},
        ]}

    monkeypatch.setattr(local_runtime, "try_local_plan", plan)

    test_app = FastAPI()
    test_app.include_router(aiops_api.router, prefix="/api")
    with TestClient(test_app) as client:
        response = client.post(
            "/api/aiops",
            json={"session_id": "s-choice-api", "execution_mode": "local",
                  "target_choice": "svc-b"},
        )

    assert response.status_code == 200
    events = _sse_events(response)
    terminal = _terminal(events)
    assert len(terminal) == 1
    assert terminal[0]["type"] == "complete"
    assert terminal[0].get("status") != "target_required"

    names = [name for name, _ in bridge["calls"]]
    assert names[0] == "list_all_services"
    data_calls = [(name, args) for name, args in bridge["calls"]
                  if name != "list_all_services"]
    assert [name for name, _ in data_calls] == ["query_cpu_metrics"]
    assert data_calls[0][1]["service_name"] == "svc-b"
    assert data_calls[0][1]["region_code"] == "ap-shanghai"
