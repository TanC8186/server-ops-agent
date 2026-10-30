"""双模式本地路径的隔离、证据及预算反例；不依赖模型或远程服务。"""

import asyncio
import importlib
import json

import pytest
from langchain_core.tools import tool

from app.agent.aiops.runtime_tools import RunTools, payload_error, unwrap_payload
from app.config import config

local = importlib.import_module("app.agent.aiops.local_runtime")
runtime = importlib.import_module("app.services.diagnostic_runtime")
planner = importlib.import_module("app.agent.aiops.local_planner")


@tool
def list_all_services() -> dict:
    """Discover services."""
    return {"services": [{"service_name": "svc-a", "instance_id": "i-a"}]}


@tool
def query_cpu_metrics(service_name: str) -> list:
    """Query CPU."""
    return [{"type": "text", "text": json.dumps({"ok": False, "source": "unavailable"})}]


async def fake_tools(run_id, mode, emit):
    return RunTools([list_all_services, query_cpu_metrics], run_id, mode, emit)


@pytest.fixture
def local_setup(monkeypatch):
    monkeypatch.setattr(local, "create_run_tools", fake_tools)
    monkeypatch.setattr(local, "_LOCAL_SLOT", asyncio.Semaphore(1))
    monkeypatch.setattr(config, "local_llm_base_url", "http://127.0.0.1:9999/v1")

    def forbidden(*args, **kwargs):
        raise AssertionError("local must not construct a cloud model")

    monkeypatch.setattr("app.core.llm_factory.create_chat_model", forbidden)


@pytest.mark.asyncio
async def test_local_failure_never_escalates_to_cloud(local_setup, monkeypatch):
    async def invalid(*args, **kwargs):
        assert kwargs["allow_cloud_fallback"] is False
        return None

    monkeypatch.setattr(local, "try_local_plan", invalid)
    events = [e async for e in runtime.run_diagnostic("CPU high", "same", "local")]
    assert events[-1]["type"] == "error"
    assert all(e["execution_mode"] == "local" for e in events)
    assert not any(e["type"] == "complete" for e in events)


@pytest.mark.asyncio
async def test_local_report_preserves_mcp_unavailable(local_setup, monkeypatch):
    async def plan(*args, **kwargs):
        return {"severity": "P2", "category": "cpu", "plan": [
            {"kind": "tool", "tool": "list_all_services", "args": {}, "description": "discover"},
            {"kind": "tool", "tool": "query_cpu_metrics", "args": {"service_name": "svc-a"}, "description": "cpu"},
        ]}

    monkeypatch.setattr(local, "try_local_plan", plan)
    events = [e async for e in runtime.run_diagnostic("CPU high", "same", "local")]
    assert events[-1]["type"] == "complete"
    assert "证据不足" in events[-1]["response"]
    assert "unavailable" in events[-1]["response"]
    assert any(e["type"] == "tool_step" and not e["ok"] for e in events)


def test_wrapped_tool_failure_is_not_usable():
    wrapped = [{"type": "text", "text": '{"ok":false,"source":"unavailable"}'}]
    assert payload_error(unwrap_payload(wrapped))


@pytest.mark.asyncio
async def test_invalid_args_and_wrong_target_never_invoke_tool():
    tools = RunTools([query_cpu_metrics], "run", "local", lambda _: None)
    tools.target_service = "svc-a"
    for args in [{}, {"service_name": 123}, {"service_name": "svc-b"}]:
        evidence = await tools.call("query_cpu_metrics", args)
        assert evidence["ok"] is False
        assert evidence["data"] is None


def test_marked_invalid_plan_is_rejected():
    assert not planner._plan_is_valid([
        {"kind": "tool", "tool": "list_all_services", "args": {}},
        {"kind": "tool", "tool": "query_cpu_metrics", "args": {}, "_invalid": True},
    ], [list_all_services, query_cpu_metrics])


@pytest.mark.asyncio
async def test_unknown_service_is_rejected_before_planning(local_setup, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("unknown target must stop before model")

    monkeypatch.setattr(local, "try_local_plan", forbidden)
    events = [e async for e in runtime.run_diagnostic("check", "session", "local", service_name="svc-b")]
    assert events[-1]["type"] == "error"
    assert "发现列表" in events[-1]["message"]


@pytest.mark.asyncio
async def test_parallel_tool_budget_is_shared(monkeypatch):
    monkeypatch.setattr(config, "diagnostic_max_tool_calls", 1)
    tools = RunTools([list_all_services], "run", "cloud_multi", lambda _: None)
    results = await asyncio.gather(tools.call("list_all_services", {}), tools.call("list_all_services", {}))
    assert sum(e["ok"] for e in results) == 1
    assert len({e["id"] for e in results}) == 2


@pytest.mark.asyncio
async def test_runtime_timeout_cancels_local_worker(local_setup, monkeypatch):
    cancelled = asyncio.Event()

    async def pending(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(local, "try_local_plan", pending)
    monkeypatch.setattr(config, "diagnostic_timeout_seconds", 0.05)
    events = [e async for e in runtime.run_diagnostic("check", "session", "local")]
    assert events[-1]["type"] == "error"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_empty_completion_is_failure(local_setup, monkeypatch):
    async def empty(*args, **kwargs):
        yield {"type": "complete", "response": ""}

    monkeypatch.setattr(local, "run_local", empty)
    events = [e async for e in runtime.run_diagnostic("check", "session", "local")]
    assert events[-1]["type"] == "error"


@pytest.mark.parametrize("payload", ["/; echo injected", "$(id)", "C:/'"])
def test_disk_tool_rejects_injection_at_entry(monkeypatch, payload):
    """注入型路径必须在执行前被拒绝（空串不在其中，见下一条）。

    空串从本用例移出：它不再是"非法路径"，而是"未指定路径"的语义
    （按平台取默认值）。原来的默认值是硬编码 "C:/"，在 Linux 上必然失败，
    所以空串必须能落到 "/" 而不是报错。空串的注入风险为零。
    """
    from servers import ssh_server

    monkeypatch.setattr(ssh_server, "SERVER_TYPE", "linux")
    monkeypatch.setattr(ssh_server, "_exec_readonly", lambda _: pytest.fail("must not execute"))
    result = ssh_server.check_disk_usage(payload)
    assert result.get("error")


@pytest.mark.parametrize("server_type,expected", [("linux", "/"), ("windows", "C:/")])
def test_disk_tool_empty_path_uses_platform_default(monkeypatch, server_type, expected):
    """未给路径时按平台取默认值，而不是用硬编码的 C:/ 在 Linux 上报错。"""
    from servers import ssh_server

    monkeypatch.setattr(ssh_server, "SERVER_TYPE", server_type)
    monkeypatch.setattr(
        ssh_server, "_exec_readonly",
        lambda cmds: {cmds[0]: "Filesystem Size Used Avail Use% Mounted on /dev/x 1G 1G 0 100% /"},
    )
    result = ssh_server.check_disk_usage("")
    assert result.get("path") == expected, f"{server_type} 默认路径应为 {expected}，实际 {result.get('path')}"
