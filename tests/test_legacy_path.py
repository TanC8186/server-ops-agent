"""P0-01 回归：HTTP 层只剩 ``run_diagnostic`` 一条诊断路径。

旧实现保留了一条 ``execution_mode is None`` 的 LangGraph Plan-Execute-Replan
分支，但 ``AIOpsRequest.execution_mode`` 始终带默认值，该分支在 HTTP 上永远
不可达；它的文档却仍在描述早已不存在的 ``plan`` / ``diagnosis`` 契约。

本文件只使用真实路由与真实 service，``run_diagnostic`` 被替换为记录参数的
假实现，从而断言“请求没有携带模式时转发的是什么”。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import aiops
from app.services import aiops_service as service_module

ROOT = Path(__file__).resolve().parents[1]


def _sse_data(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.fixture()
def captured_runs(monkeypatch):
    """记录 service 转发给 run_diagnostic 的参数。"""
    calls: list[dict] = []

    async def fake_run_diagnostic(user_input, session_id, execution_mode, *, service_name=None):
        calls.append(
            {
                "user_input": user_input,
                "session_id": session_id,
                "execution_mode": execution_mode,
                "service_name": service_name,
            }
        )
        yield {"type": "complete", "response": "# 诊断报告", "run_id": "fake-run"}

    monkeypatch.setattr(service_module, "run_diagnostic", fake_run_diagnostic)
    return calls


@pytest.fixture()
def client():
    # 只挂载该路由：主应用 lifespan 会连接 Milvus。
    test_app = FastAPI()
    test_app.include_router(aiops.router, prefix="/api")
    with TestClient(test_app) as test_client:
        yield test_client


def test_post_without_execution_mode_forwards_to_run_diagnostic(client, captured_runs):
    response = client.post("/api/aiops", json={"session_id": "legacy-check"})

    assert response.status_code == 200
    assert len(captured_runs) == 1
    call = captured_runs[0]
    assert call["session_id"] == "legacy-check"
    assert call["execution_mode"] == "local"
    assert call["service_name"] is None
    assert call["user_input"], "没有 symptom 时应使用默认诊断任务描述"
    assert "Plan-Execute" not in call["user_input"]

    events = _sse_data(response)
    assert events and events[-1]["type"] == "complete"
    # 旧 translator 会把 complete 改写成 diagnosis 包装；新契约原样透传。
    assert "diagnosis" not in events[-1]
    assert events[-1]["response"] == "# 诊断报告"


def test_post_forwards_explicit_symptom_and_service(client, captured_runs):
    response = client.post(
        "/api/aiops",
        json={"session_id": "s-2", "symptom": "CPU 97%", "service_name": "svc-a"},
    )

    assert response.status_code == 200
    assert captured_runs[0]["user_input"] == "CPU 97%"
    assert captured_runs[0]["execution_mode"] == "local"
    assert captured_runs[0]["service_name"] == "svc-a"


@pytest.mark.asyncio
async def test_public_service_methods_default_to_local(captured_runs):
    service = service_module.AIOpsService()

    execute_events = [event async for event in service.execute("诊断", "session-x")]
    diagnose_events = [event async for event in service.diagnose("session-x")]

    assert [call["execution_mode"] for call in captured_runs] == ["local", "local"]
    assert execute_events[-1]["type"] == "complete"
    assert diagnose_events[-1]["type"] == "complete"


def test_aiops_service_no_longer_owns_a_graph():
    service = service_module.AIOpsService()

    assert not hasattr(service, "graph")
    assert not hasattr(service, "checkpointer")

    source = (ROOT / "app" / "services" / "aiops_service.py").read_text(encoding="utf-8")
    for token in (
        "StateGraph",
        "MemorySaver",
        "PlanExecuteState",
        "_format_planner_event",
        "_format_executor_event",
        "_format_replanner_event",
    ):
        assert token not in source, f"遗留实现仍残留在 aiops_service.py: {token}"


def test_http_layers_never_construct_the_legacy_graph():
    """grep 式断言：HTTP/service 层不存在构建旧图的代码路径。"""
    graph_call = re.compile(r"StateGraph\s*\(\s*PlanExecuteState")
    legacy_import = re.compile(
        r"from\s+app\.agent\.aiops\s+import[\s\S]{0,200}?"
        r"\b(PlanExecuteState|planner|executor|replanner|verifier)\b"
    )

    offenders: list[str] = []
    for layer in ("app/api", "app/services"):
        for path in (ROOT / layer).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if graph_call.search(source) or legacy_import.search(source):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == [], f"旧 Plan-Execute 图仍可从 HTTP 层到达: {offenders}"


def test_endpoint_docstring_describes_the_real_contract():
    doc = aiops.diagnose_stream.__doc__ or ""

    # 旧契约：Graph 模式 + diagnosis 包装 + 自动拉取告警。
    assert "使用 Plan-Execute-Replan 模式" not in doc
    assert "diagnosis_complete" not in doc
    assert "fetching_alerts" not in doc
    # 新契约：明确写清 run_diagnostic 与真实事件类型。
    assert "run_diagnostic" in doc
    assert "tool_step" in doc
    assert "没有 Plan-Execute-Replan 图" in doc
