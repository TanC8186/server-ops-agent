"""终态/运行历史/追问链路的行为回归（BE-05 / BE-08 / M4）。

本文件锁定四个曾经真实存在的缺陷：

1. ``target_required`` 被 HTTP 层当成终态直接 break，运行器来不及写
   ``run_complete`` ——运行历史里这次运行永远是 ``status=running``；
2. ``status`` 只看 lifecycle，``quality=insufficient`` 也显示 ``ok``；
3. ``GET /runs/{id}`` 的 ``next_after_seq`` 取“最后一条元素的 seq”，
   终态记录没有 seq 时返回 null，续读不可用；
4. 云端 Reviewer 的 ``agent_result`` trace 只写 evidence_ids，
   追问（``run_context``）永远读不到观测/假设/缺口。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import aiops as aiops_api
from app.api import runs as runs_api
from app.core import run_context
from app.services import diagnostic_runtime


def _write_trace(directory: Path, run_id: str, events: list[dict]) -> Path:
    path = directory / f"{run_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 1. target_required 不是终态：后面的 complete 必须被交付
# ---------------------------------------------------------------------------


def test_target_required_is_not_terminal():
    """HTTP 层只把 complete/error 当终态，target_required 之后还继续读流。"""
    assert aiops_api._is_terminal({"type": "target_required"}) is False
    assert aiops_api._is_terminal({"type": "complete", "status": "target_required"}) is True
    assert aiops_api._is_terminal({"type": "error"}) is True


def test_stream_delivers_complete_after_target_required(monkeypatch):
    """两条事件都到前端，且流在 complete 处结束（不是 target_required）。"""
    async def fake_diagnose(**_kwargs):
        yield {"type": "target_required", "candidates": [{"service": "svc-a"}],
               "reason": "多个候选"}
        yield {"type": "complete", "status": "target_required",
               "lifecycle": "completed", "quality": "insufficient",
               "response": "# 需要选择诊断目标"}

    monkeypatch.setattr(aiops_api.aiops_service, "diagnose", fake_diagnose)

    app = FastAPI()
    app.include_router(aiops_api.router, prefix="/api")
    with TestClient(app) as client:
        response = client.post("/api/aiops", json={
            "session_id": "s", "execution_mode": "local", "symptom": "CPU 高",
        })

    events = [json.loads(line[len("data: "):])
              for line in response.text.splitlines() if line.startswith("data: ")]
    kinds = [event["type"] for event in events]
    assert kinds == ["target_required", "complete"]
    assert events[-1]["status"] == "target_required"


# ---------------------------------------------------------------------------
# 2/3. 运行历史：degraded 与续读游标
# ---------------------------------------------------------------------------


@pytest.fixture()
def runs_client(tmp_path, monkeypatch):
    trace_dir = tmp_path / "traces"
    # conftest 的 autouse fixture 已把 observability.TRACE_DIR 指到这里。
    trace_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(runs_api, "TRACE_DIR", trace_dir)
    app = FastAPI()
    app.include_router(runs_api.router, prefix="/api")
    with TestClient(app) as client:
        yield client, trace_dir


def test_completed_but_insufficient_is_flagged_degraded(runs_client):
    client, trace_dir = runs_client
    run_id = "b" * 32
    _write_trace(trace_dir, run_id, [
        {"ts": "2026-10-26T12:00:00", "run_id": run_id, "event": "run_start",
         "session_id": "s1", "execution_mode": "local"},
        {"ts": "2026-10-26T12:00:05", "run_id": run_id, "event": "run_complete",
         "duration_ms": 5000, "lifecycle": "completed", "quality": "insufficient"},
    ])

    summary = client.get("/api/runs").json()["runs"][0]

    assert summary["lifecycle"] == "completed"
    assert summary["quality"] == "insufficient"
    # status 只表达“跑没跑完”，不能被低质量改写成 error。
    assert summary["status"] == "ok"
    assert summary["degraded"] is True


def test_sufficient_run_is_not_degraded(runs_client):
    client, trace_dir = runs_client
    run_id = "c" * 32
    _write_trace(trace_dir, run_id, [
        {"ts": "2026-10-26T12:00:00", "run_id": run_id, "event": "run_start",
         "session_id": "s1", "execution_mode": "cloud_multi"},
        {"ts": "2026-10-26T12:00:05", "run_id": run_id, "event": "run_complete",
         "lifecycle": "completed", "quality": "sufficient"},
    ])

    summary = client.get("/api/runs").json()["runs"][0]
    assert summary["degraded"] is False


def test_next_after_seq_uses_max_seq_even_when_last_record_has_none(runs_client):
    client, trace_dir = runs_client
    run_id = "d" * 32
    events = [
        {"ts": "t", "run_id": run_id, "event": "run_start", "seq": 1, "session_id": "s"},
        {"ts": "t", "run_id": run_id, "event": "tool_step", "seq": 2, "step": "a"},
        # 终态记录通常不带 seq；旧实现取 selected[-1]["seq"] -> None，无法续读。
        {"ts": "t", "run_id": run_id, "event": "run_complete", "lifecycle": "completed"},
    ]
    _write_trace(trace_dir, run_id, events)

    page = client.get(f"/api/runs/{run_id}", params={"limit": 2}).json()
    assert page["truncated"] is True
    assert page["next_after_seq"] == 2

    rest = client.get(f"/api/runs/{run_id}", params={"after_seq": page["next_after_seq"]}).json()
    assert [event["event"] for event in rest["events"]] == ["run_complete"]
    assert rest["truncated"] is False


# ---------------------------------------------------------------------------
# 4. 追问：cloud Reviewer 的 agent_result 必须能提供摘要
# ---------------------------------------------------------------------------


def test_run_context_reads_reviewer_grounded_result(tmp_path, monkeypatch):
    trace_dir = tmp_path / "traces"
    # conftest 的 autouse fixture 已把 observability.TRACE_DIR 指到这里。
    trace_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(run_context, "TRACE_DIR", trace_dir)
    run_id = "e" * 32
    _write_trace(trace_dir, run_id, [
        {"ts": "t", "run_id": run_id, "event": "run_start", "session_id": "s-1",
         "execution_mode": "cloud_multi"},
        {"ts": "t", "run_id": run_id, "event": "agent_result", "agent": "reviewer",
         "lifecycle": "completed", "quality": "partial",
         "observations": ["CPU 使用率 12%"],
         "hypotheses": ["可能存在上游超时"],
         "gaps": ["缺少磁盘证据"]},
        {"ts": "t", "run_id": run_id, "event": "run_complete",
         "lifecycle": "completed", "quality": "partial"},
    ])

    context = run_context.load_run_context(run_id, "s-1")

    assert context is not None
    assert context["observations"] == ["CPU 使用率 12%"]
    assert context["hypotheses"] == ["可能存在上游超时"]
    assert "缺少磁盘证据" in context["gaps"]
    assert context["quality"] == "partial"


def test_run_context_never_leaks_across_sessions(tmp_path, monkeypatch):
    trace_dir = tmp_path / "traces"
    # conftest 的 autouse fixture 已把 observability.TRACE_DIR 指到这里。
    trace_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(run_context, "TRACE_DIR", trace_dir)
    run_id = "f" * 32
    _write_trace(trace_dir, run_id, [
        {"ts": "t", "run_id": run_id, "event": "run_start", "session_id": "owner"},
        {"ts": "t", "run_id": run_id, "event": "run_complete", "lifecycle": "completed"},
    ])

    assert run_context.load_run_context(run_id, "intruder") is None


# ---------------------------------------------------------------------------
# 5. 运行时：知识检索与 step_id/tool_call_id
# ---------------------------------------------------------------------------


class _FakeTool:
    name = "query_cpu_metrics"
    description = "查询 CPU"

    class args_schema:  # noqa: N801 - 与 langchain 工具的字段形状一致
        @staticmethod
        def model_json_schema():
            return {"type": "object",
                    "properties": {"service_name": {"type": "string"}},
                    "required": ["service_name"]}

    async def ainvoke(self, args):
        return {"source": "tencent_cloud", "metric": "cpu_usage",
                "statistics": {"avg": 12.0}}


class _FakeDiscovery(_FakeTool):
    name = "list_all_services"
    description = "列出服务"

    class args_schema:  # noqa: N801
        @staticmethod
        def model_json_schema():
            return {"type": "object", "properties": {}}

    async def ainvoke(self, args):
        return {"services": [{"service_name": "svc-a", "instance_id": "ins-1",
                              "region": "ap-guangzhou"}], "source": "tencent_cloud"}


def _runtime(monkeypatch, deadline=None):
    from app.agent.aiops import runtime_tools

    events: list[dict] = []

    async def fake_tools():
        return [_FakeDiscovery(), _FakeTool()]

    monkeypatch.setattr(runtime_tools, "get_mcp_tools_cached", fake_tools)
    return asyncio.run(runtime_tools.create_run_tools(
        "0123456789abcdef0123456789abcdef", "cloud_multi", events.append,
        deadline=deadline,
    )), events


def test_knowledge_tool_is_in_whitelist(monkeypatch):
    """知识检索必须真的进运行器（不是只存在于独立模块）。"""
    runtime, _events = _runtime(monkeypatch)
    assert "retrieve_knowledge" in runtime.by_name


def test_tool_events_carry_step_and_call_ids(monkeypatch):
    runtime, events = _runtime(monkeypatch)

    async def run():
        await runtime.discover_service("svc-a", "")
        await runtime.call("query_cpu_metrics", {"service_name": "svc-a"},
                           agent="metrics", step_id="step-metrics")

    asyncio.run(run())

    steps = [event for event in events if event.get("type") == "tool_step"]
    focus = next(event for event in steps if event.get("step") == "query_cpu_metrics")
    assert focus["step_id"] == "step-metrics"
    assert focus["tool_call_id"].startswith("01234567-c")
    # 同一 step 的多次调用必须有不同的 call id（否则前端会去重掉）。
    assert focus["tool_call_id"] != steps[0].get("tool_call_id")


def test_budget_is_shared_with_runtime(monkeypatch):
    """预算真的接到运行器：额度用尽后调用记成失败证据而不是无限放行。"""
    from app.core.budgets import RequestBudget

    runtime, _events = _runtime(monkeypatch)
    runtime.budget = RequestBudget(tool_calls=1, llm_calls=1, subprocess_calls=1,
                                   prompt_chars=1000, evidence_chars=1000)

    async def run():
        await runtime.discover_service("svc-a", "")  # 用掉唯一额度
        return await runtime.call("query_cpu_metrics", {"service_name": "svc-a"})

    evidence = asyncio.run(run())
    assert evidence["ok"] is False
    assert evidence["status"] in {"error", "unsupported"}
    assert "预算" in evidence["error"]


# ---------------------------------------------------------------------------
# 6. MCP 不可用时的错误可读性
# ---------------------------------------------------------------------------


def test_mcp_taskgroup_error_becomes_actionable_message():
    """langchain-mcp-adapters 的 ExceptionGroup 不该原样透给用户。"""
    from app.agent.aiops.runtime_tools import _describe_tool_load_error

    class Group(Exception):
        def __init__(self, message, errors):
            super().__init__(message)
            self.exceptions = errors

    group = Group("unhandled errors in a TaskGroup",
                  [ConnectionError("Connection refused to 127.0.0.1:8004")])
    message = _describe_tool_load_error(group)

    assert "Connection refused" in message
    assert "8003/8004/8005" in message
    assert "TaskGroup" not in message


def test_runtime_receives_shared_budget_and_deadline(monkeypatch):
    """deadline/budget 必须从运行器传到运行器实例（不是只在测试里构造）。"""
    import time as _time

    from app.agent.aiops import runtime_tools
    from app.core.budgets import RequestBudget

    captured: dict = {}

    async def fake_tools():
        return [_FakeDiscovery(), _FakeTool()]

    monkeypatch.setattr(runtime_tools, "get_mcp_tools_cached", fake_tools)

    budget = RequestBudget(tool_calls=3, llm_calls=1, subprocess_calls=3,
                           prompt_chars=100, evidence_chars=100)
    deadline = _time.monotonic() + 30
    runtime = asyncio.run(runtime_tools.create_run_tools(
        "0123456789abcdef0123456789abcdef", "local", captured.setdefault,
        deadline=deadline, budget=budget,
    ))

    assert runtime.deadline == deadline
    assert runtime.budget is budget
    assert runtime._effective_timeout() <= 30


def test_create_tools_compat_tolerates_old_signature():
    """测试替身只接受 3 个位置参数时，不应因新增可选参数 TypeError。"""
    from app.agent.aiops.runtime_tools import create_tools_compat

    calls: list[tuple] = []

    async def legacy(run_id, execution_mode, emit):
        calls.append((run_id, execution_mode))
        return "ok"

    result = asyncio.run(create_tools_compat(
        legacy, "run-1", "local", lambda event: None,
        deadline=1.0, budget=object(),
    ))

    assert result == "ok"
    assert calls == [("run-1", "local")]
