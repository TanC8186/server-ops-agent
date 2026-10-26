"""Independent, offline counterexamples from the 2026-10-27 acceptance review.

Run from the repository with its virtualenv Python. Only temporary files, fake
models and in-process fake tools are used. No model, MCP, SSH, cloud or database
request is made. The JSON output records counterexamples, not claimed fixes.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.tools import StructuredTool

from app.agent.aiops import quality
from app.core.budgets import RequestBudget
from app.core.targets import Target, resolve_target

runtime_tools = importlib.import_module("app.agent.aiops.runtime_tools")
cloud_multi = importlib.import_module("app.agent.aiops.cloud_multi")
runs = importlib.import_module("app.api.runs")


def facts_probe() -> dict:
    evidence = {
        "id": "E1", "tool": "query_network_metrics", "ok": True,
        "status": "ok", "live": True, "source": "tencent_cloud", "target": "svc-a",
        "data": {"metric": "latency", "statistics": {"avg": 12}, "unit": "ms",
                 "time_range": {"start_time": 100, "end_time": 200}},
    }
    cases = []
    for claim in ("网络延迟 999ms", "网络延迟 12%", "网络延迟 12ms，svc-b在昨天的结果"):
        assessment = quality.assess(
            [evidence], [],
            {"observations": [{"claim": claim, "status": "supported", "evidence_ids": ["E1"]}],
             "hypotheses": [], "evidence_gaps": []},
            target_bound=True,
        )
        cases.append({"claim": claim, "accepted_as_supported": bool(assessment["observations"])})
    return {"actual_latency_ms": 12, "actual_target": "svc-a", "cases": cases}


def target_choice_probe() -> dict:
    catalog = [{"service_name": "svc", "instance_id": "i-a"},
               {"service_name": "svc", "instance_id": "i-b"}]
    # The public target_choice field is a service-name string. Re-submitting
    # either displayed candidate's service cannot disambiguate the instances.
    return {
        "choose_service_name": resolve_target(catalog, "svc", "").to_dict(),
        "choose_instance_id": resolve_target(catalog, "i-a", "").to_dict(),
    }


async def topic_and_budget_probe() -> dict:
    observed_limits = []

    async def get_topic_info_by_name(topic_name, region_code=None):
        return {"source": "tencent_cloud", "topic_name": topic_name}

    async def search_service_logs(service_name, limit=100):
        observed_limits.append(limit)
        return {"source": "tencent_cloud", "logs": [{"message": "x" * 200_000}]}

    topic_tool = StructuredTool(
        name="get_topic_info_by_name", description="Fake topic metadata",
        args_schema={"type": "object", "properties": {
            "topic_name": {"type": "string"}, "region_code": {"type": "string"}},
            "required": ["topic_name"]},
        coroutine=get_topic_info_by_name,
    )
    log_tool = StructuredTool(
        name="search_service_logs", description="Fake logs",
        args_schema={"type": "object", "properties": {
            "service_name": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["service_name"]},
        coroutine=search_service_logs,
    )
    with patch.object(runtime_tools, "trace_event", lambda *args, **kwargs: None):
        tools = runtime_tools.RunTools([topic_tool], "offline-probe", "cloud_multi", lambda _: None)
        tools.bind(Target(service="svc-a", region="region-a", topics=("topic-a",)))
        topic = await tools.call("get_topic_info_by_name", {"topic_name": "topic-a"})

        budget = RequestBudget(tool_calls=10, llm_calls=12, subprocess_calls=10,
                               prompt_chars=20_000, evidence_chars=10)
        tools = runtime_tools.RunTools([log_tool], "offline-probe", "local", lambda _: None,
                                      budget=budget)
        tools.bind(Target(service="svc-a"))
        logs = await tools.call("search_service_logs", {"service_name": "svc-a", "limit": 999_999})
    return {
        "valid_topic_query": {"ok": topic["ok"], "error": topic["error"]},
        "log_query": {"ok": logs["ok"], "executed_limit": observed_limits[-1],
                      "retained_data_chars": len(json.dumps(logs["data"])),
                      "evidence_budget": budget.snapshot()["evidence_chars"]},
    }


async def cloud_cancel_probe() -> dict:
    holder = {}
    started = set()
    both = asyncio.Event()

    async def discover():
        return {"source": "tencent_cloud", "services": [{"service_name": "svc-a"}]}

    async def pending(service_name):
        started.add(asyncio.current_task().get_name())
        holder["runtime"].emit({"type": "status", "stage": "pending_fake_tool"})
        if len(started) == 2:
            both.set()
        await asyncio.Future()

    async def fake_create(run_id, mode, emit, **kwargs):
        tools = [StructuredTool(
            name="list_all_services", description="Fake discovery",
            args_schema={"type": "object", "properties": {}}, coroutine=discover)]
        for name in ("query_cpu_metrics", "search_service_logs"):
            tools.append(StructuredTool(
                name=name, description="Fake pending query",
                args_schema={"type": "object", "properties": {"service_name": {"type": "string"}},
                             "required": ["service_name"]}, coroutine=pending))
        holder["runtime"] = runtime_tools.RunTools(tools, run_id, mode, emit,
                                                  budget=kwargs.get("budget"))
        return holder["runtime"]

    class FakeModel:
        async def ainvoke(self, messages):
            system = messages[0][1]
            if "Supervisor" in system:
                data = {"assignments": [
                    {"agent": "metrics", "focus": "cpu", "tools": ["query_cpu_metrics"]},
                    {"agent": "logs", "focus": "logs", "tools": ["search_service_logs"]}]}
            else:
                tool = "query_cpu_metrics" if "独立的 metrics" in system else "search_service_logs"
                data = {"steps": [{"tool": tool, "args": {"service_name": "svc-a"}}],
                        "findings": [], "done": True}
            return SimpleNamespace(content=json.dumps(data))

    with patch.object(cloud_multi, "create_run_tools", fake_create), \
         patch.object(cloud_multi, "create_chat_model", lambda **kwargs: FakeModel()), \
         patch.object(cloud_multi, "trace_event", lambda *args, **kwargs: None), \
         patch.object(runtime_tools, "trace_event", lambda *args, **kwargs: None):
        stream = cloud_multi.run_cloud_multi("svc-a high", "offline-session", "a" * 32)
        try:
            while not both.is_set():
                await asyncio.wait_for(anext(stream), 2)
        finally:
            await stream.aclose()
    return {"tools_started": len(started), "runtime_cancelled_flag": holder["runtime"].cancelled}


async def trace_page_probe() -> dict:
    with tempfile.TemporaryDirectory(prefix="oncall-offline-review-") as tmp:
        key = "a" * 32
        events = [{"event": "run_start"}, {"event": "status", "seq": 1},
                  {"event": "evidence", "data": 1}, {"event": "tool_step"},
                  {"event": "status", "seq": 2}, {"event": "run_complete"}]
        (Path(tmp) / (key + ".jsonl")).write_text(
            "\n".join(json.dumps(event) for event in events), encoding="utf-8")
        with patch.object(runs, "TRACE_DIR", Path(tmp)):
            return await runs.get_run(key, after_seq=1, limit=2)


async def main() -> None:
    output = {"facts": facts_probe(), "target_choice": target_choice_probe(),
              "topic_and_budget": await topic_and_budget_probe(),
              "cloud_cancel": await cloud_cancel_probe(),
              "trace_page": await trace_page_probe()}
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
