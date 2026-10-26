"""Offline contracts for API-backed, isolated multi-agent diagnosis."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

multi = importlib.import_module("app.agent.aiops.cloud_multi")


def _turn(steps=None, findings=None, done=False):
    return {"steps": steps or [], "findings": findings or [], "done": done}


def _finding(claim, evidence_ids, status="supported"):
    return {"claim": claim, "status": status, "evidence_ids": evidence_ids}


class FakeRuntime:
    def __init__(self, emit):
        self.emit = emit
        self.by_name = {name: object() for name in (
            "list_all_services", "query_cpu_metrics", "query_memory_metrics",
            "search_service_logs", "read_log_file",
        )}
        self.evidence = []
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.pair_started = asyncio.Event()
        self.block = False
        self.cancelled = 0
        self.source = "tencent_cloud"

    def catalog(self, names=None):
        return json.dumps({name: {} for name in names or self.by_name})

    async def discover_service(self, requested, user_input):
        assert requested in (None, "checkout-service")
        return "checkout-service"

    async def call(self, name, args, *, agent, step_id=""):
        self.calls.append((agent, name, args))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == 2:
            self.pair_started.set()
        try:
            if self.block:
                await asyncio.Future()
            elif len(self.calls) <= 2:
                await asyncio.wait_for(self.pair_started.wait(), 2)
            await asyncio.sleep(0)
            evidence = {"id": f"E{len(self.evidence) + 1}", "tool": name,
                        "agent": agent, "args": args, "ok": True,
                        "source": self.source, "data": {"value": 96},
                        "error": "", "target": "checkout-service"}
            self.evidence.append(evidence)
            self.emit({"type": "tool_step", "agent": agent, "evidence_id": evidence["id"]})
            return evidence
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


class Harness:
    def __init__(self, monkeypatch):
        self.runtime = None
        self.messages = []
        self.traces = []
        self.factory_calls = 0
        self.role_rounds = {}
        self.bad_tool = False
        self.bad_citation = False
        self.bad_supervisor = False
        self.bad_reviewer = False
        self.metrics_error = False
        self.block = False
        self.demo = False
        self.instances = []

        async def create_runtime(run_id, mode, emit):
            assert mode == "cloud_multi"
            self.runtime = FakeRuntime(emit)
            self.runtime.block = self.block
            if self.demo:
                self.runtime.source = "demo,tencent_cloud"
            return self.runtime

        def factory(**kwargs):
            assert kwargs["streaming"] is False
            assert kwargs["temperature"] == 0
            self.factory_calls += 1
            model = self.FakeModel(self)
            self.instances.append(model)
            return model

        monkeypatch.setattr(multi, "create_run_tools", create_runtime)
        monkeypatch.setattr(multi, "create_chat_model", factory)
        monkeypatch.setattr(multi, "trace_event", lambda run_id, event, **fields:
                            self.traces.append((event, fields)))

    class FakeModel:
        def __init__(self, harness):
            self.h = harness
            self.roles = set()

        async def ainvoke(self, messages):
            system = messages[0][1]
            if "你是运维诊断 Supervisor" in system:
                role = "supervisor"
            elif "你是独立 Reviewer" in system:
                role = "reviewer"
            elif "独立的 metrics" in system:
                role = "metrics"
            else:
                role = "logs"
            self.roles.add(role)
            h = self.h
            h.messages.append((role, list(messages)))
            h.role_rounds[role] = h.role_rounds.get(role, 0) + 1
            if role == "supervisor":
                if h.bad_supervisor:
                    return SimpleNamespace(content="not JSON")
                payload = {"assignments": [
                    {"agent": "metrics", "focus": "检查 CPU 并按结果检查内存",
                     "tools": ["query_cpu_metrics", "query_memory_metrics"]},
                    {"agent": "logs", "focus": "查询错误日志",
                     "tools": ["search_service_logs"]},
                ]}
            elif role == "reviewer":
                if h.bad_reviewer:
                    raise RuntimeError("cloud API unavailable")
                expected = {"logs"} if h.metrics_error else {"metrics", "logs"}
                assert {entry[0] for entry in h.runtime.calls} == expected
                assert h.runtime.active == 0, "Reviewer must join both specialists"
                cpu = next((item for item in h.runtime.evidence if item["tool"] == "query_cpu_metrics"), h.runtime.evidence[0])
                refs = ["invented-E999"] if h.bad_citation else [cpu["id"]]
                payload = {"observations": [_finding("CPU 使用率较高", refs)],
                           "hypotheses": [_finding("可能存在资源竞争", [cpu["id"]])],
                           "recommendations": ["补充高负载时段只读进程查询"], "evidence_gaps": []}
            elif h.role_rounds[role] == 1:
                if role == "metrics" and h.metrics_error:
                    h.runtime.pair_started.set()
                    raise RuntimeError("rate limit")
                tool = "query_cpu_metrics" if role == "metrics" else "search_service_logs"
                if h.bad_tool and role == "logs":
                    # Bypass only the test's concurrency rendezvous: no foreign tool executes.
                    h.runtime.pair_started.set()
                    tool = "query_memory_metrics"
                payload = _turn([{"tool": tool, "args": {"service_name": "checkout-service"}}])
            else:
                context = json.loads(messages[-1][1])
                assert context["tool_results"], "Next decision must receive actual tool results"
                assert all(item["agent"] == role for item in context["tool_results"])
                if role == "metrics" and h.role_rounds[role] == 2:
                    payload = _turn([{"tool": "query_memory_metrics", "args": {"service_name": "checkout-service"}}], done=True)
                else:
                    payload = _turn(findings=[_finding("日志查询已完成", [context["tool_results"][0]["id"]])], done=True)
            return SimpleNamespace(content=json.dumps(payload),
                                   usage_metadata={"input_tokens": 12, "output_tokens": 8})


@pytest.fixture
def harness(monkeypatch):
    return Harness(monkeypatch)


async def _events():
    return [event async for event in multi.run_cloud_multi(
        "checkout-service CPU 高且请求超时", "session", "run",
        service_name="checkout-service")]


@pytest.mark.asyncio
async def test_parallel_specialists_have_isolated_tools_feedback_and_join(harness):
    events = await _events()
    assert harness.runtime.max_active == 2
    assert harness.role_rounds == {"supervisor": 1, "metrics": 2, "logs": 2, "reviewer": 1}
    assert harness.factory_calls == 4
    assert all(len(model.roles) == 1 for model in harness.instances)
    assert [(a, t) for a, t, _ in harness.runtime.calls].count(("metrics", "query_memory_metrics")) == 1
    metrics_system = next(messages[0][1] for role, messages in harness.messages if role == "metrics")
    logs_system = next(messages[0][1] for role, messages in harness.messages if role == "logs")
    assert "search_service_logs" not in metrics_system
    assert "query_cpu_metrics" not in logs_system
    assert events[-1]["type"] == "complete"
    assert events[-1]["status"] == "completed"
    assert "待验证" in events[-1]["response"]
    assert len([event for event in events if event["type"] == "tool_step"]) == 3
    traces = [fields for event, fields in harness.traces if event == "agent_llm"]
    assert {trace["agent"] for trace in traces} == {"supervisor", "metrics", "logs", "reviewer"}
    assert all(trace["input_tokens"] == 12 and trace["output_tokens"] == 8 for trace in traces)


@pytest.mark.asyncio
async def test_foreign_specialist_tool_is_rejected_and_degraded(harness):
    harness.bad_tool = True
    events = await _events()
    assert not any(agent == "logs" for agent, _, _ in harness.runtime.calls)
    assert any(event["type"] == "agent_status" and event.get("status") == "error"
               and "未授权" in event["message"] for event in events)
    assert events[-1]["status"] == "degraded"
    assert events[-1]["response"]


@pytest.mark.asyncio
async def test_invented_and_demo_citations_cannot_support_report(harness):
    harness.bad_citation = True
    events = await _events()
    report = events[-1]["response"]
    assert "[待验证假设] CPU" in report
    assert "（证据: invented-E999）" not in report
    assert any(event["type"] == "agent_status" and event.get("status") == "error"
               and "不存在的证据" in event["message"] for event in events)
    assert events[-1]["status"] == "degraded"


@pytest.mark.asyncio
async def test_mixed_demo_source_is_demoted(harness):
    harness.demo = True
    events = await _events()
    assert "[证据观察] CPU" not in events[-1]["response"]
    assert "[待验证假设] CPU" in events[-1]["response"]
    assert events[-1]["status"] == "degraded"


@pytest.mark.asyncio
async def test_reviewer_api_error_yields_evidence_only_degraded_report(harness):
    harness.bad_reviewer = True
    events = await _events()
    assert any(event["type"] == "agent_status" and event.get("status") == "error"
               and event["agent"] == "reviewer" for event in events)
    assert events[-1]["type"] == "complete"
    assert events[-1]["status"] == "degraded"
    assert "query_cpu_metrics" in events[-1]["response"]
    assert "cloud API unavailable" in events[-1]["response"]


@pytest.mark.asyncio
async def test_one_specialist_api_error_still_joins_other_evidence(harness):
    harness.metrics_error = True
    events = await _events()
    assert any(event["type"] == "agent_status" and event.get("status") == "error" and event["agent"] == "metrics"
               and "rate limit" in event["message"] for event in events)
    assert any(item["tool"] == "search_service_logs" for item in harness.runtime.evidence)
    assert harness.role_rounds["reviewer"] == 1
    assert events[-1]["type"] == "complete"
    assert events[-1]["status"] == "degraded"


@pytest.mark.asyncio
async def test_invalid_supervisor_json_is_explicit_fatal_error(harness):
    harness.bad_supervisor = True
    events = await _events()
    assert events[-1]["type"] == "error"
    assert events[-1]["fatal"] is True
    assert harness.runtime.calls == []
    assert not any(event["type"] == "complete" for event in events)


@pytest.mark.asyncio
async def test_stream_close_cancels_both_specialists(harness):
    harness.block = True
    stream = multi.run_cloud_multi("CPU high", "session", "run")
    while harness.runtime is None or not harness.runtime.pair_started.is_set():
        await asyncio.wait_for(anext(stream), 2)
    await stream.aclose()
    assert harness.runtime.active == 0
    assert harness.runtime.cancelled == 2
    assert "reviewer" not in harness.role_rounds


@pytest.mark.asyncio
async def test_cloud_llm_timeout_is_observable(monkeypatch, harness):
    class HangingModel:
        async def ainvoke(self, messages):
            await asyncio.Future()
    monkeypatch.setattr(multi, "create_chat_model", lambda **kwargs: HangingModel())
    monkeypatch.setattr(multi, "LLM_TIMEOUT_SECONDS", 0.01)
    events = await _events()
    assert events[-1]["type"] == "error"
    assert events[-1]["fatal"] is True
    assert "TimeoutError" in events[-1]["message"]


def test_langgraph_contains_four_roles_and_fanout_join_edges():
    graph = multi._build_cloud_graph()
    topology = graph.get_graph()
    assert {"supervisor", "metrics", "logs", "reviewer"} <= set(topology.nodes)
    edges = {(edge.source, edge.target) for edge in topology.edges}
    assert {
        ("__start__", "supervisor"),
        ("supervisor", "metrics"), ("supervisor", "logs"),
        ("metrics", "reviewer"), ("logs", "reviewer"),
        ("reviewer", "__end__"),
    } <= edges
    assert graph.checkpointer is None


@pytest.mark.asyncio
async def test_actual_langgraph_fanout_runs_both_nodes_before_reviewer_join(harness):
    graph = multi._build_cloud_graph()
    emitted = []
    updates = [update async for update in graph.astream(
        {"user_input": "CPU high", "session_id": "session", "run_id": "graph-run",
         "service_name": "checkout-service", "emit": emitted.append},
        config={"max_concurrency": 2}, stream_mode="updates",
    )]
    nodes = [node for update in updates for node in update]
    assert nodes[0] == "supervisor"
    assert set(nodes[1:3]) == {"metrics", "logs"}
    assert nodes[3:] == ["reviewer"]
    assert harness.runtime.max_active == 2
    assert harness.role_rounds["reviewer"] == 1
    assert updates[-1]["reviewer"]["response"] == emitted[-1]["response"]
