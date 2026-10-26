"""2026-10-27 验收反例：目标候选绑定（A04）、参数补全（A05）、预算（A07）、分页（A09）。

全部使用真实业务代码：真实 ``resolve_target`` / ``RunTools`` / ``get_run``，
只把 MCP 工具与外部目录换成进程内假实现（含真实 ``StructuredTool`` schema 校验）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.tools import StructuredTool

from app.agent.aiops import runtime_tools
from app.api import runs
from app.core.budgets import RequestBudget
from app.core.targets import (
    Target,
    candidate_id,
    resolve_target,
    target_candidate,
)

SAME_SERVICE_CATALOG = [
    {"service_name": "orders", "instance_id": "i-a", "region": "r-a"},
    {"service_name": "orders", "instance_id": "i-b", "region": "r-b"},
]


# ── A04：同名多实例必须能被区分并绑定 ──────────────────────────


def test_same_service_instances_get_distinct_candidate_ids():
    candidates = [target_candidate(target) for target in _targets(SAME_SERVICE_CATALOG)]

    assert len(candidates) == 2, "同名不同实例不能被去重成一个候选"
    assert {item["instance_id"] for item in candidates} == {"i-a", "i-b"}
    ids = {item["target_id"] for item in candidates}
    assert len(ids) == 2, f"每个候选必须有稳定且互不相同的 ID: {ids}"
    assert all(item.startswith("tgt-") for item in ids)


def test_candidate_id_is_stable_across_requests():
    """候选 ID 只由目录内容决定：同一候选多次解析得到同一 ID。"""
    first = resolve_target(SAME_SERVICE_CATALOG, None, "订单服务变慢")
    second = resolve_target(SAME_SERVICE_CATALOG, None, "订单服务变慢")

    assert first.status == "target_required"
    assert second.status == "target_required"
    assert [candidate_id(t) for t in first.candidates] == [
        candidate_id(t) for t in second.candidates
    ]


def test_candidate_id_binds_exact_instance_and_region():
    """用候选 ID 重新请求时绑定到具体实例，而不是退回服务名歧义。"""
    resolution = resolve_target(SAME_SERVICE_CATALOG, None, "订单服务变慢")
    chosen = resolution.candidates[1]
    chosen_id = candidate_id(chosen)

    bound = resolve_target(SAME_SERVICE_CATALOG, None, "订单服务变慢", candidate=chosen_id)

    assert bound.status == "bound"
    assert bound.target is not None
    assert bound.target.instance_id == "i-b"
    assert bound.target.region == "r-b"


def test_stale_candidate_id_is_rejected_instead_of_silently_rebinding():
    """候选已下线时必须明确失败，不能悄悄绑定到别的实例。"""
    resolution = resolve_target(SAME_SERVICE_CATALOG, None, "订单服务变慢")
    stale = candidate_id(resolution.candidates[0])
    changed_catalog = [{"service_name": "orders", "instance_id": "i-b", "region": "r-b"}]

    result = resolve_target(changed_catalog, None, "订单服务变慢", candidate=stale)

    assert result.status == "not_found"
    assert result.target is None
    assert "资源目录" in result.reason


def test_service_name_alone_still_reports_ambiguity():
    """只传服务名（旧客户端）仍必须要求选择，不能自动取第一项。"""
    result = resolve_target(SAME_SERVICE_CATALOG, "orders", "")

    assert result.status == "target_required"
    assert len(result.candidates) == 2


def _targets(catalog):
    from app.core.targets import catalog_targets

    return catalog_targets(catalog)


# ── A05：目标参数补全只使用 schema 接受的字段 ──────────────────


def _topic_tool(calls: list) -> StructuredTool:
    async def get_topic_info_by_name(topic_name, region_code=None):
        calls.append({"topic_name": topic_name, "region_code": region_code})
        return {"source": "tencent_cloud", "topic_name": topic_name}

    return StructuredTool(
        name="get_topic_info_by_name",
        description="主题元数据（schema 只接受 topic_name/region_code）",
        args_schema={
            "type": "object",
            "properties": {
                "topic_name": {"type": "string"},
                "region_code": {"type": "string"},
            },
            "required": ["topic_name"],
        },
        coroutine=get_topic_info_by_name,
    )


def _legit_topic(topic_name: str = "topic-a") -> tuple[str, StructuredTool]:
    """带 topic_a 声明的目标 + 真实 StructuredTool（schema 校验是真的）。"""
    return topic_name, StructuredTool(
        name="search_log",
        description="按主题查日志",
        args_schema={
            "type": "object",
            "properties": {
                "topic_name": {"type": "string"},
                "region_code": {"type": "string"},
            },
            "required": ["topic_name"],
        },
        coroutine=lambda topic_name, region_code=None: {
            "source": "tencent_cloud", "logs": []
        },
    )


def test_valid_topic_query_succeeds_without_injecting_unknown_keys():
    """合法 topic 查询必须成功，且不得注入 schema 不接受的 topic_id/topic。"""
    calls: list[dict] = []
    tool = _topic_tool(calls)

    async def run() -> dict:
        runtime = runtime_tools.RunTools([tool], "run-1", "cloud_multi", lambda _: None)
        runtime.bind(Target(service="svc-a", region="region-a", topics=("topic-a",)))
        return await runtime.call("get_topic_info_by_name", {"topic_name": "topic-a"})

    evidence = asyncio.run(run())

    assert evidence["ok"] is True, evidence["error"]
    assert calls == [{"topic_name": "topic-a", "region_code": "region-a"}], (
        f"只允许补 schema 接受的字段，实际: {calls}"
    )
    assert set(calls[0]) <= {"topic_name", "region_code"}


def test_topic_outside_binding_is_still_rejected():
    """补全逻辑放宽后，越界 topic 仍必须拒绝。"""
    calls: list[dict] = []
    tool = _topic_tool(calls)

    async def run() -> dict:
        runtime = runtime_tools.RunTools([tool], "run-1", "cloud_multi", lambda _: None)
        runtime.bind(Target(service="svc-a", topics=("topic-a",)))
        return await runtime.call("get_topic_info_by_name", {"topic_name": "topic-z"})

    evidence = asyncio.run(run())

    assert evidence["ok"] is False
    assert "日志主题" in evidence["error"]
    assert calls == [], "越界 topic 不得真正执行工具"


def test_no_topic_is_injected_when_target_declares_none():
    """目标没有声明 topic 时，补全不得凭空造一个主题出来。"""
    calls: list[dict] = []
    tool = _topic_tool(calls)

    async def run() -> dict:
        runtime = runtime_tools.RunTools([tool], "run-1", "cloud_multi", lambda _: None)
        runtime.bind(Target(service="svc-a"))
        return await runtime.call("get_topic_info_by_name", {"topic_name": "topic-a"})

    evidence = asyncio.run(run())

    assert evidence["ok"] is False
    assert "未声明日志主题" in evidence["error"]


# ── A07：日志参数收敛与证据字节预算 ────────────────────────────


def _log_tool(observed: list) -> StructuredTool:
    async def search_service_logs(service_name, limit=100):
        observed.append(limit)
        return {"source": "tencent_cloud", "logs": [{"message": "x" * 200_000}]}

    return StructuredTool(
        name="search_service_logs",
        description="查询服务日志",
        args_schema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["service_name"],
        },
        coroutine=search_service_logs,
    )


def test_log_limit_is_clamped_and_evidence_budget_is_consumed():
    """limit=999999 必须被真实收敛；evidence_chars 必须被真实消费与限制。"""
    observed: list = []
    tool = _log_tool(observed)
    budget = RequestBudget(
        tool_calls=10, llm_calls=12, subprocess_calls=10,
        prompt_chars=20_000, evidence_chars=10,
    )

    async def run() -> dict:
        runtime = runtime_tools.RunTools([tool], "run-1", "local", lambda _: None,
                                         budget=budget)
        runtime.bind(Target(service="svc-a"))
        return await runtime.call("search_service_logs",
                                  {"service_name": "svc-a", "limit": 999_999})

    evidence = asyncio.run(run())

    assert observed and observed[-1] != 999_999, f"limit 未被收敛: {observed}"
    assert observed[-1] <= 100
    retained = len(json.dumps(evidence["data"]))
    assert retained <= 64, f"保留的证据数据未被限制，实际 {retained} 字符"
    snapshot = budget.snapshot()["evidence_chars"]
    assert snapshot["used"] > 0, "证据预算必须被真实消费"
    assert snapshot["remaining"] < snapshot["limit"]


def test_budget_notes_describe_what_was_dropped():
    """参数被收敛/证据被截断时，必须给出可读说明（用于证据缺口）。"""
    observed: list = []
    tool = _log_tool(observed)
    budget = RequestBudget(
        tool_calls=10, llm_calls=12, subprocess_calls=10,
        prompt_chars=20_000, evidence_chars=200,
    )

    async def run():
        runtime = runtime_tools.RunTools([tool], "run-1", "local", lambda _: None,
                                         budget=budget)
        runtime.bind(Target(service="svc-a"))
        await runtime.call("search_service_logs", {"service_name": "svc-a", "limit": 50_000})
        return runtime

    runtime = asyncio.run(run())
    joined = " ".join(runtime.budget_notes)

    assert "50000" in joined or "limit" in joined
    assert runtime.budget_notes, "收敛/截断必须有说明"


# ── A09：混合 seq / 无 seq 的分页必须能读到终态 ────────────────

MIXED_TRACE = [
    {"event": "run_start"},
    {"event": "status", "seq": 1},
    {"event": "evidence", "data": 1},
    {"event": "tool_step"},
    {"event": "status", "seq": 2},
    {"event": "run_complete"},
]


def _write_trace(directory: Path, run_id: str, events: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{run_id}.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )


RUN_ID = "a" * 32


def test_mixed_seq_page_always_reaches_terminal_event(tmp_path, monkeypatch):
    """after_seq=1、limit=2 时绝不能返回 truncated=true 且没有可用游标。"""
    monkeypatch.setattr(runs, "TRACE_DIR", tmp_path)
    _write_trace(tmp_path, RUN_ID, MIXED_TRACE)

    page = asyncio.run(runs.get_run(RUN_ID, after_seq=1, limit=2))

    names = [event["event"] for event in page["events"]]
    assert "run_complete" in names, f"必须能读到终态，实际: {names}"
    assert not (page["truncated"] and page["next_after_seq"] is None), (
        f"分页不可继续: truncated={page['truncated']} cursor={page['next_after_seq']}"
    )


def test_paging_walks_all_events_without_gaps_or_duplicates(tmp_path, monkeypatch):
    """按游标续读必须无遗漏、无重复，最终读到终态。"""
    monkeypatch.setattr(runs, "TRACE_DIR", tmp_path)
    events = [{"event": "run_start", "seq": 1}]
    events += [{"event": "tool_step", "seq": index} for index in range(2, 9)]
    events += [{"event": "run_complete", "seq": 9}]
    _write_trace(tmp_path, RUN_ID, events)

    collected: list[dict] = []
    after_seq = 0
    for _ in range(20):
        page = asyncio.run(runs.get_run(RUN_ID, after_seq=after_seq, limit=3))
        collected.extend(page["events"])
        if not page["truncated"]:
            break
        assert page["next_after_seq"] is not None
        after_seq = page["next_after_seq"]

    assert [item["seq"] for item in collected] == [item["seq"] for item in events]
    assert collected[-1]["event"] == "run_complete"


def test_log_limit_is_clamped_even_without_a_budget_object():
    """没有接入 RequestBudget 时，limit 仍然必须收敛（A07 不依赖预算接线）。"""
    observed: list = []
    tool = _log_tool(observed)

    async def run() -> tuple[dict, list[str]]:
        runtime = runtime_tools.RunTools([tool], "run-1", "local", lambda _: None)
        runtime.bind(Target(service="svc-a"))
        evidence = await runtime.call("search_service_logs",
                                     {"service_name": "svc-a", "limit": 999_999})
        return evidence, list(runtime.budget_notes)

    evidence, notes = asyncio.run(run())

    assert evidence["ok"] is True
    assert observed[-1] == 100, f"limit 必须收敛到上限，实际 {observed[-1]}"
    assert any("limit" in note for note in notes), f"必须说明收敛了什么: {notes}"


def test_budget_notes_reach_local_quality_gaps():
    """参数收敛/证据截断说明必须出现在本地报告的证据缺口里。"""
    from app.agent.aiops import local_runtime

    observed: list = []
    tool = _log_tool(observed)
    budget = RequestBudget(
        tool_calls=10, llm_calls=12, subprocess_calls=10,
        prompt_chars=20_000, evidence_chars=50,
    )

    async def collect() -> runtime_tools.RunTools:
        runtime = runtime_tools.RunTools([tool], "run-1", "local", lambda _: None,
                                         budget=budget)
        runtime.bind(Target(service="svc-a"))
        await runtime.call("search_service_logs", {"service_name": "svc-a", "limit": 999_999})
        return runtime

    runtime = asyncio.run(collect())
    quality = local_runtime.assess_local_quality(
        runtime.evidence, target_bound=True, extra_gaps=list(runtime.budget_notes)
    )

    joined = " ".join(quality["gaps"])
    assert "limit" in joined
    assert any("截断" in gap or "超过" in gap for gap in quality["gaps"])


def test_normal_completion_is_not_marked_cancelled():
    """A11 反向保证：正常走完的运行不能被误标成取消。"""
    from types import SimpleNamespace

    from app.agent.aiops import cloud_multi

    class _Runtime:
        def __init__(self) -> None:
            self.cancelled = False
            self.calls = 0
            self.evidence: list = []
            self.by_name: dict = {}

        def cancel(self) -> None:
            self.cancelled = True

        async def discover_service(self, requested, user_input, candidate=None):
            return "svc-a"

        def catalog(self, names=None):
            return "{}"

    class _Model:
        async def ainvoke(self, messages):
            system = messages[0][1]
            if "Supervisor" in system:
                payload = {"assignments": [
                    {"agent": "metrics", "focus": "c", "tools": []},
                    {"agent": "logs", "focus": "l", "tools": []},
                ]}
            else:
                payload = {"observations": [], "hypotheses": [],
                           "recommendations": [], "evidence_gaps": []}
            return SimpleNamespace(content=json.dumps(payload))

    holder: dict = {}

    async def create_runtime(run_id, mode, emit, **kwargs):
        holder["runtime"] = _Runtime()
        return holder["runtime"]

    async def collect():
        return [event async for event in cloud_multi.run_cloud_multi(
            "svc-a high", "s", "a" * 32)]

    import unittest.mock as mock

    with mock.patch.object(cloud_multi, "create_run_tools", create_runtime), \
         mock.patch.object(cloud_multi, "create_chat_model", lambda **k: _Model()), \
         mock.patch.object(cloud_multi, "trace_event", lambda *a, **k: None):
        events = asyncio.run(collect())

    assert events[-1]["type"] == "complete"
    assert holder["runtime"].cancelled is False, "正常完成不得标记为已取消"
