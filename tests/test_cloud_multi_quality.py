"""BE-05：在线多 Agent 报告必须把生命周期与诊断质量分开。

回归的四个反例：

1. 报告生成成功不等于证据充分；全部工具失败仍要出报告，但
   ``quality=insufficient``、``status=degraded``。
2. 模型写出的数值必须回到引用证据核对：对的留在“当前观测”，错的进
   “待验证假设”并在“证据缺口”留下可执行的缺口说明。
3. demo 证据永远不能出现在“当前观测”。
4. 知识片段只能出现在“知识依据”，并标注手册免责声明。

同时检查 ``complete`` 事件携带 ``status``/``lifecycle``/``quality``，
其中 ``status`` 由服务端按质量计算，不采用模型文本。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

multi = importlib.import_module("app.agent.aiops.cloud_multi")
quality = importlib.import_module("app.agent.aiops.quality")

REQUIRED_SECTIONS = [
    "## 当前观测",
    "## 待验证假设",
    "## 知识依据",
    "## 证据缺口",
    "## 后续检查建议",
    "## 原始证据索引",
]


def _turn(steps=None, findings=None, done=False):
    return {"steps": steps or [], "findings": findings or [], "done": done}


def _finding(claim, evidence_ids, status="supported"):
    return {"claim": claim, "status": status, "evidence_ids": evidence_ids}


def _sections(report: str) -> dict[str, str]:
    """按 H2 切分报告，便于断言“某内容只出现在某节”。"""
    parts = report.split("\n## ")
    result = {}
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        result[title.strip()] = body
    return result


class FakeRuntime:
    """真实运行器的最小替身：保留证据结构、并发屏障与调用记录。"""

    def __init__(self, emit, *, fulfill=True, source="tencent_cloud", knowledge=None):
        self.emit = emit
        self.by_name = {name: object() for name in (
            "list_all_services", "query_cpu_metrics", "query_memory_metrics",
            "search_service_logs", "read_log_file",
        )}
        self.evidence = list(knowledge or [])
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.pair_started = asyncio.Event()
        self.fulfill = fulfill
        self.source = source
        self.target = SimpleNamespace(service="checkout-service")

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
            if len(self.calls) <= 2:
                await asyncio.wait_for(self.pair_started.wait(), 2)
            await asyncio.sleep(0)
            evidence_id = f"E{len(self.evidence) + 1}"
            if self.fulfill:
                evidence = {
                    "id": evidence_id, "tool": name, "agent": agent, "args": args,
                    "ok": True, "status": "ok", "live": True,
                    "demo": "demo" in self.source, "source": self.source,
                    "data": {"value": 96}, "error": "",
                    "target": "checkout-service",
                }
            else:
                evidence = {
                    "id": evidence_id, "tool": name, "agent": agent, "args": args,
                    "ok": False, "status": "error", "live": False, "demo": False,
                    "source": "unavailable", "data": None,
                    "error": "AccessDenied: 无权限读取监控", "target": "checkout-service",
                }
            self.evidence.append(evidence)
            self.emit({"type": "tool_step", "agent": agent,
                       "evidence_id": evidence["id"]})
            return evidence
        finally:
            self.active -= 1


class Harness:
    def __init__(self, monkeypatch):
        self.runtime = None
        self.messages = []
        self.traces = []
        self.role_rounds = {}
        self.fail_tools = False
        self.demo = False
        self.knowledge = None
        self.bad_reviewer = False
        #: 覆盖 Reviewer 的 observations 文本；None 表示使用默认话术。
        self.observation_claims = None

        async def create_runtime(run_id, mode, emit):
            assert mode == "cloud_multi"
            self.runtime = FakeRuntime(
                emit,
                fulfill=not self.fail_tools,
                source="demo,tencent_cloud" if self.demo else "tencent_cloud",
                knowledge=self.knowledge,
            )
            return self.runtime

        def factory(**kwargs):
            assert kwargs["streaming"] is False
            return self.FakeModel(self)

        monkeypatch.setattr(multi, "create_run_tools", create_runtime)
        monkeypatch.setattr(multi, "create_chat_model", factory)
        monkeypatch.setattr(multi, "trace_event", lambda run_id, event, **fields:
                            self.traces.append((event, fields)))

    class FakeModel:
        def __init__(self, harness):
            self.h = harness

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
            h = self.h
            h.messages.append((role, list(messages)))
            h.role_rounds[role] = h.role_rounds.get(role, 0) + 1
            if role == "supervisor":
                payload = {"assignments": [
                    {"agent": "metrics", "focus": "检查 CPU 并按结果检查内存",
                     "tools": ["query_cpu_metrics", "query_memory_metrics"]},
                    {"agent": "logs", "focus": "查询错误日志",
                     "tools": ["search_service_logs"]},
                ]}
            elif role == "reviewer":
                if h.bad_reviewer:
                    raise RuntimeError("reviewer upstream unavailable")
                cpu = next((item for item in h.runtime.evidence
                            if item["tool"] == "query_cpu_metrics"),
                           h.runtime.evidence[0])
                if h.observation_claims is None:
                    observations = [_finding("CPU 使用率较高", [cpu["id"]])]
                else:
                    observations = [_finding(claim, [cpu["id"]], status)
                                    for claim, status in h.observation_claims]
                payload = {"observations": observations,
                           "hypotheses": [_finding("可能存在资源竞争", [cpu["id"]])],
                           "recommendations": ["补充高负载时段只读进程查询"],
                           "evidence_gaps": []}
            elif h.role_rounds[role] == 1:
                tool = "query_cpu_metrics" if role == "metrics" else "search_service_logs"
                payload = _turn([{"tool": tool, "args": {"service_name": "checkout-service"}}])
            else:
                context = json.loads(messages[-1][1])
                if role == "metrics" and h.role_rounds[role] == 2:
                    payload = _turn([{"tool": "query_memory_metrics",
                                      "args": {"service_name": "checkout-service"}}],
                                    done=True)
                else:
                    payload = _turn(
                        findings=[_finding("日志查询已完成",
                                           [context["tool_results"][0]["id"]])],
                        done=True,
                    )
            return SimpleNamespace(content=json.dumps(payload),
                                   usage_metadata={"input_tokens": 12, "output_tokens": 8})


@pytest.fixture
def harness(monkeypatch):
    return Harness(monkeypatch)


async def _events():
    return [event async for event in multi.run_cloud_multi(
        "checkout-service CPU 高且请求超时", "session", "run",
        service_name="checkout-service")]


def _report(events: list[dict]) -> str:
    assert events[-1]["type"] == "complete"
    return events[-1]["response"]


def _assert_sections_in_order(report: str) -> None:
    assert [line for line in report.splitlines() if line.startswith("## ")] == REQUIRED_SECTIONS


@pytest.mark.asyncio
async def test_all_tools_failing_still_renders_insufficient_degraded_report(harness):
    harness.fail_tools = True
    events = await _events()
    report = _report(events)

    _assert_sections_in_order(report)
    assert "生命周期: completed｜诊断质量: insufficient" in report
    assert events[-1]["status"] == "degraded"
    assert events[-1]["quality"] == "insufficient"
    assert "AccessDenied" in _sections(report)["证据缺口"]


@pytest.mark.asyncio
async def test_supported_number_stays_observed_and_wrong_number_becomes_hypothesis(harness):
    harness.observation_claims = [
        ("CPU 使用率 96%", "supported"),
        ("内存使用率 99%", "supported"),
    ]
    events = await _events()
    report = _report(events)
    sections = _sections(report)

    _assert_sections_in_order(report)
    assert "CPU 使用率 96%" in sections["当前观测"]
    assert "内存使用率 99%" not in sections["当前观测"]
    assert "内存使用率 99%" in sections["待验证假设"]
    gap = sections["证据缺口"]
    assert "99" in gap
    assert "无法在引用证据中找到" in gap


@pytest.mark.asyncio
async def test_demo_evidence_never_appears_as_an_observation(harness):
    harness.demo = True
    events = await _events()
    report = _report(events)
    sections = _sections(report)

    _assert_sections_in_order(report)
    assert "CPU 使用率较高" not in sections["当前观测"]
    assert "[证据观察] CPU" not in report
    assert "演示" in sections["证据缺口"]


@pytest.mark.asyncio
async def test_knowledge_chunk_only_appears_under_knowledge_section(harness):
    harness.knowledge = [{
        "id": "K1", "tool": "retrieve_knowledge", "agent": "logs",
        "ok": True, "status": "ok", "kind": "knowledge", "live": False,
        "demo": False, "source": "manual", "target": "", "error": "",
        "data": {"title": "Redis 超时排查手册",
                 "text": "连接池耗尽时先检查 maxclients"},
    }]
    events = await _events()
    report = _report(events)
    sections = _sections(report)

    _assert_sections_in_order(report)
    assert "Redis 超时排查手册" in sections["知识依据"]
    assert "（手册，不代表当前主机状态）" in sections["知识依据"]
    assert "Redis 超时排查手册" not in sections["当前观测"]
    assert "Redis 超时排查手册" not in sections["原始证据索引"]
    assert "maxclients" not in sections["当前观测"]


@pytest.mark.asyncio
async def test_reviewer_failure_still_renders_sections_and_degrades(harness):
    harness.bad_reviewer = True
    events = await _events()
    report = _report(events)
    sections = _sections(report)

    _assert_sections_in_order(report)
    assert events[-1]["status"] == "degraded"
    assert "reviewer upstream unavailable" in sections["证据缺口"]
    assert "query_cpu_metrics" in sections["原始证据索引"]


@pytest.mark.asyncio
async def test_complete_event_carries_lifecycle_and_quality(harness):
    events = await _events()
    terminal = events[-1]

    assert terminal["type"] == "complete"
    assert {"status", "lifecycle", "quality"} <= set(terminal)
    assert terminal["lifecycle"] == "completed"
    assert terminal["quality"] in {"sufficient", "partial", "insufficient"}
    expected = "degraded" if terminal["quality"] == "insufficient" else terminal["status"]
    assert expected == terminal["status"]
