"""P0-01/P0-02 回归：FastAPI → 真实 service → 真实 diagnostic runtime → 真实 cloud graph。

只有聊天模型和 MCP 工具列表被替换；运行器、事件协议、trace 写入均为真实实现。
覆盖三件事：

1. 保留字段由服务端独占，云端伪造的 run_id/session_id/execution_mode 不能穿透；
2. 每次 run 恰好一条终态生命周期记录，且事件源 ``aclose()`` 失败不会污染终态；
3. app/ 下不允许再出现把原始事件字典直接展开进 trace 的调用点。
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app import observability
from app.agent.aiops import cloud_multi
from app.api import aiops, runs
from app.config import config
from app.services import diagnostic_runtime

ROOT = Path(__file__).resolve().parents[1]


class _ServiceArgs(BaseModel):
    service_name: str | None = Field(default=None, description="服务名")


class _NoArgs(BaseModel):
    pass


def _fake_mcp_tools() -> list:
    """真实 StructuredTool：保留 schema 校验、与 RunTools 的真实交互。"""

    def list_all_services() -> dict:
        return {"services": [
            {"service_name": "svc-a", "instance_id": "ins-a", "region": "ap-guangzhou"},
        ], "source": "tencent_cloud"}

    def query_cpu_metrics(service_name: str) -> dict:
        return {"source": "tencent_cloud", "data_points": [
            {"timestamp": 1700000000, "value": 12.0},
        ], "metric": "cpu_usage", "unit": "percent"}

    def search_service_logs(service_name: str) -> dict:
        return {"source": "tencent_cloud", "total": 1,
                "logs": [{"level": "ERROR", "message": "upstream timeout"}]}

    return [
        StructuredTool.from_function(
            func=list_all_services, name="list_all_services",
            description="列出服务", args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            func=query_cpu_metrics, name="query_cpu_metrics",
            description="查询 CPU", args_schema=_ServiceArgs,
        ),
        StructuredTool.from_function(
            func=search_service_logs, name="search_service_logs",
            description="查询日志", args_schema=_ServiceArgs,
        ),
    ]


class _ScriptedModel:
    """按 system prompt 判别角色，只产出确定性结构。"""

    def __init__(self, evidence_seen: dict, control: dict):
        self.evidence_seen = evidence_seen
        self.control = control

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
            if self.control.get("fail_reviewer"):
                raise RuntimeError("reviewer boom")
            ids = list(self.evidence_seen.get("ids", []))
            payload = {
                "observations": [
                    {"claim": "CPU 使用率 12%", "status": "supported",
                     "evidence_ids": ids[:1]},
                ],
                "hypotheses": [
                    {"claim": "可能存在上游超时", "status": "hypothesis",
                     "evidence_ids": ids[1:2]},
                ],
                "recommendations": ["复核上游超时时段"],
                "evidence_gaps": [],
            }
        elif "独立的 metrics" in system:
            payload = {"steps": [{"tool": "query_cpu_metrics",
                                  "args": {"service_name": "svc-a"}}],
                       "findings": [], "done": True}
        else:
            payload = {"steps": [{"tool": "search_service_logs",
                                  "args": {"service_name": "svc-a"}}],
                       "findings": [], "done": True}
        return SimpleNamespace(content=json.dumps(payload),
                               usage_metadata={"input_tokens": 5, "output_tokens": 3})


@pytest.fixture()
def pipeline(monkeypatch, tmp_path):
    # trace 写入真实实现，只换目录，便于断言回放。
    trace_dir = tmp_path / "traces"
    monkeypatch.setattr(observability, "TRACE_DIR", trace_dir)
    monkeypatch.setattr(diagnostic_runtime, "trace_event", observability.trace_event)
    monkeypatch.setattr(config, "llm_api_key", "sk-unit-test")
    monkeypatch.setattr(config, "diagnostic_timeout_seconds", 15.0)

    # 只替换外部边界：MCP 工具列表与模型 SDK。
    from app.agent.aiops import runtime_tools

    async def fake_tools():
        return _fake_mcp_tools()

    monkeypatch.setattr(runtime_tools, "get_mcp_tools_cached", fake_tools)

    seen: dict[str, list[str]] = {"ids": []}
    control: dict[str, bool] = {"fail_reviewer": False}

    def fake_factory(**kwargs):
        assert kwargs["streaming"] is False
        return _ScriptedModel(seen, control)

    monkeypatch.setattr(cloud_multi, "create_chat_model", fake_factory)

    # RunTools 记录的 evidence 需要暴露给 Reviewer：包装真实 RunTools.call。
    original_call = runtime_tools.RunTools.call

    async def recording_call(self, name, args, *, agent="local", step_id=""):
        evidence = await original_call(self, name, args, agent=agent, step_id=step_id)
        seen["ids"].append(evidence["id"])
        return evidence

    monkeypatch.setattr(runtime_tools.RunTools, "call", recording_call)

    test_app = FastAPI()
    test_app.include_router(aiops.router, prefix="/api")
    return TestClient(test_app), trace_dir, control


def _sse_events(response) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _terminal(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") in {"complete", "error"}]


def _submit(client, *, session_id: str = "p0-01"):
    payload = {
        "session_id": session_id,
        "execution_mode": "cloud_multi",
        "symptom": "svc-a CPU 使用率异常，请求变慢",
        "service_name": "svc-a",
    }
    response = client.post("/api/aiops", json=payload)
    assert response.status_code == 200
    return _sse_events(response)


def _read_trace(trace_dir: Path, run_id: str) -> list[dict]:
    path = trace_dir / f"{run_id}.jsonl"
    assert path.exists(), f"缺少 trace 文件: {path}"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_cloud_multi_full_pipeline_completes_without_run_id_collision(pipeline):
    client, trace_dir, _ = pipeline
    events = _submit(client)
    kinds = [event["type"] for event in events]

    # 曾经中断的位置：trace_event(run_id, "agent_status", run_id=...)。
    assert not any("multiple values for argument 'run_id'" in json.dumps(event, ensure_ascii=False)
                   for event in events), kinds

    # (a) 每个 SSE 事件都必须满足统一信封不变式。
    run_ids = {event["run_id"] for event in events}
    session_ids = {event["session_id"] for event in events}
    assert len(run_ids) == 1, f"一次 run 只能有一个 run_id: {run_ids}"
    assert session_ids == {"p0-01"}, f"会话必须来自请求: {session_ids}"
    run_id = run_ids.pop()
    for expected_seq, event in enumerate(events, start=1):
        assert event["schema_version"] == "1"
        assert event["execution_mode"] == "cloud_multi"
        assert event["seq"] == expected_seq, f"seq 必须从 1 连续递增: {kinds}"
        assert event["event_id"] == f"{run_id}:{expected_seq}"

    # (b) 首事件是 run_start，终态之前包含完整的可解释链路。
    assert events[0]["type"] == "status"
    assert events[0]["stage"] == "run_start"

    terminal = _terminal(events)
    assert len(terminal) == 1, f"必须有唯一终态，实际: {kinds}"
    assert terminal[0]["type"] == "complete"
    assert "在线 API 多 Agent 运维诊断报告" in terminal[0]["response"]
    assert "E1" in terminal[0]["response"]

    before_terminal = kinds[:kinds.index("complete")]
    assert before_terminal[0] == "status"
    for kind in ("plan", "tool_step", "step_complete", "report"):
        assert kind in before_terminal, f"终态前缺少 {kind}: {before_terminal}"
    assert before_terminal.count("agent_status") >= 4

    # trace 同样只有一条终态生命周期记录。
    records = _read_trace(trace_dir, run_id)
    names = [record["event"] for record in records]
    assert names[0] == "run_start"
    assert "agent_status" in names
    assert [name for name in names if name in {"run_complete", "run_error"}] == ["run_complete"]
    assert names[-1] == "run_complete"
    assert all(record["run_id"] == run_id for record in records)


def test_trace_replay_through_http_api(pipeline, monkeypatch):
    """(c) trace 回放：列表摘要 + 明细事件都能通过 HTTP API 读到。"""
    client, trace_dir, _ = pipeline
    events = _submit(client)
    run_id = events[0]["run_id"]

    monkeypatch.setattr(runs, "TRACE_DIR", trace_dir)
    monkeypatch.setattr(runs, "RESULT_DIR", trace_dir.parent / "eval-results")
    replay_app = FastAPI()
    replay_app.include_router(runs.router, prefix="/api")
    with TestClient(replay_app) as replay:
        listing = replay.get("/api/runs")
        detail = replay.get(f"/api/runs/{run_id}")

    assert listing.status_code == 200
    run_rows = listing.json()["runs"]
    assert [row["run_id"] for row in run_rows] == [run_id], "临时 trace 目录里只能有这一次 run"
    assert run_rows[0]["status"] == "ok"
    assert run_rows[0]["steps"] > 0
    assert run_rows[0]["session_id"] == "p0-01"
    assert run_rows[0]["execution_mode"] == "cloud_multi"

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["run_id"] == run_id
    assert len(detail_body["events"]) > 0
    assert detail_body["events"][0]["event"] == "run_start"


def test_reviewer_failure_yields_single_degraded_terminal(pipeline):
    """(d) Reviewer 抛错也要降级完成：唯一 complete、status=degraded、无协议异常。"""
    client, trace_dir, control = pipeline
    control["fail_reviewer"] = True

    events = _submit(client)
    kinds = [event["type"] for event in events]
    terminal = _terminal(events)

    assert len(terminal) == 1, f"reviewer 失败不能产生第二个终态: {kinds}"
    assert terminal[0]["type"] == "complete"
    assert terminal[0]["status"] == "degraded"
    assert "reviewer boom" in terminal[0]["response"]
    assert "query_cpu_metrics" in terminal[0]["response"]
    assert "TypeError" not in json.dumps(events, ensure_ascii=False)
    assert not any(event["type"] == "error" for event in events), kinds

    records = _read_trace(trace_dir, terminal[0]["run_id"])
    names = [record["event"] for record in records]
    assert [name for name in names if name in {"run_complete", "run_error"}] == ["run_complete"]
    assert names[-1] == "run_complete"


def test_forged_reserved_fields_cannot_override_server_values(pipeline, monkeypatch):
    """(e) 云端擅自写入保留字段：交付事件与 trace 都保持服务端值。"""
    client, trace_dir, _ = pipeline

    async def forged_workflow(user_input, session_id, run_id, service_name, emit):
        emit({"type": "agent_status", "agent": "supervisor", "status": "running",
              "run_id": "forged-run", "session_id": "forged-session",
              "execution_mode": "local", "message": "forged"})
        emit({"type": "complete", "response": "# 服务端字段不可伪造",
              "run_id": "forged-run", "session_id": "forged-session",
              "execution_mode": "local"})

    monkeypatch.setattr(cloud_multi, "_workflow", forged_workflow)

    events = _submit(client)
    server_run_id = events[0]["run_id"]
    assert server_run_id != "forged-run"
    for event in events:
        assert event["run_id"] == server_run_id
        assert event["session_id"] == "p0-01"
        assert event["execution_mode"] == "cloud_multi"
    assert "forged-run" not in json.dumps(events, ensure_ascii=False)

    records = _read_trace(trace_dir, server_run_id)
    assert records, "trace 必须写出真实记录"
    assert all(record["run_id"] == server_run_id for record in records)
    assert all(record.get("execution_mode") != "local" for record in records)
    assert {
        record.get("execution_mode") for record in records if "execution_mode" in record
    } <= {"cloud_multi"}
    assert not (trace_dir / "forged-run.jsonl").exists()
    status_record = next(record for record in records if record["event"] == "agent_status")
    assert status_record["run_id"] == server_run_id


class _RaisingCloseSource:
    """模拟 aclose() 抛错的云端事件源（自定义迭代器，不是 async generator）。"""

    def __init__(self, events: list[dict]):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        raise RuntimeError("close boom")


@pytest.mark.asyncio
async def test_runtime_survives_raising_source_close(monkeypatch, isolated_trace_dir):
    """(1) aclose() 失败只记录，不覆盖、不复制已发出的终态事件。"""
    monkeypatch.setattr(config, "llm_api_key", "sk-unit-test")
    source = _RaisingCloseSource([
        {"type": "agent_status", "agent": "supervisor", "status": "running"},
        {"type": "complete", "response": "# 报告"},
    ])
    monkeypatch.setattr(cloud_multi, "run_cloud_multi", lambda *args, **kwargs: source)

    events = [event async for event in diagnostic_runtime.run_diagnostic(
        "CPU high", "session", "cloud_multi")]

    terminal = _terminal(events)
    assert len(terminal) == 1, f"aclose 失败不能改变终态数量: {events}"
    assert terminal[0]["type"] == "complete"
    assert terminal[0]["response"] == "# 报告"

    records = _read_trace(isolated_trace_dir, terminal[0]["run_id"])
    names = [record["event"] for record in records]
    assert names.count("run_complete") == 1
    assert names.count("run_error") == 0
    assert names.count("source_close_error") == 1
    close_error = next(record for record in records if record["event"] == "source_close_error")
    assert "close boom" in str(close_error.get("message", ""))
    assert close_error.get("error") == "RuntimeError"


@pytest.mark.asyncio
async def test_runtime_yields_single_terminal_when_cloud_fails(monkeypatch, isolated_trace_dir):
    """(2) 云端致命错误：唯一终态；trace 只有一条 run_error，且保留原始信息。"""
    monkeypatch.setattr(config, "llm_api_key", "sk-unit-test")

    async def failing_cloud(*args, **kwargs):
        yield {"type": "agent_status", "agent": "supervisor", "status": "error",
               "run_id": "from-cloud", "execution_mode": "cloud_multi",
               "message": "boom"}
        yield {"type": "error", "message": "boom", "fatal": True,
               "run_id": "from-cloud", "execution_mode": "cloud_multi"}

    monkeypatch.setattr(cloud_multi, "run_cloud_multi", failing_cloud)
    events = [e async for e in diagnostic_runtime.run_diagnostic(
        "CPU high", "session", "cloud_multi")]

    terminal = _terminal(events)
    assert len(terminal) == 1
    assert terminal[0]["type"] == "error"
    assert "boom" in terminal[0]["message"]
    run_ids = {e.get("run_id") for e in events}
    assert len(run_ids) == 1, f"服务端必须统一 run_id，云端自报值不能覆盖: {run_ids}"

    records = _read_trace(isolated_trace_dir, terminal[0]["run_id"])
    names = [record["event"] for record in records]
    terminal_records = [name for name in names if name in {"run_complete", "run_error"}]
    assert terminal_records == ["run_error"], f"每次 run 只能有一条终态记录: {names}"
    assert names[-1] == "run_error", f"终态记录必须最后写入: {names}"
    assert "error" not in names, "入站 error 事件必须改名，避免与终态 run_error 重复计数"
    assert "runtime_error_event" in names
    run_error = next(record for record in records if record["event"] == "run_error")
    assert "boom" in str(run_error.get("message", ""))
    assert run_error.get("execution_mode") == "cloud_multi"


def test_cloud_runner_emits_server_owned_run_id_and_mode(monkeypatch):
    """保留字段由服务端生成：云端 emit 不能用其他 run_id 覆盖。"""
    captured = []

    async def fake_workflow(user_input, session_id, run_id, service_name, emit):
        emit({"type": "agent_status", "status": "running", "run_id": "forged",
              "session_id": "forged-session", "execution_mode": "local"})

    monkeypatch.setattr(cloud_multi, "_workflow", fake_workflow)

    async def collect():
        return [e async for e in cloud_multi.run_cloud_multi(
            "q", "s", uuid.uuid4().hex)]

    events = asyncio.run(collect())
    captured.extend(events)
    event = captured[0]
    assert event["run_id"] != "forged"
    assert event["session_id"] == "s"
    assert event["execution_mode"] == "cloud_multi"


# --- 守卫：app/ 下禁止把原始事件字典直接展开进 trace -------------------------

#: 允许出现 ``trace_event(..., **name)`` 的文件及理由。新增裸展开会立刻失败。
ALLOWED_TRACE_EXPANSIONS: dict[str, str] = {
    "app/services/diagnostic_runtime.py":
        "唯一展开入口：fields 来自 trace_fields()，保留字段已剔除",
    "app/agent/aiops/runtime_tools.py":
        "fields 是同函数内字面量构造的 UI 预览字段，不是外来事件字典",
}

_TRACE_EVENT_TOKEN = re.compile(r"\btrace_event\s*\(")


def _trace_expansion_calls(path: Path) -> list[tuple[int, str]]:
    """返回真实 ``trace_event(...)`` 调用里的 ``**name``：行号与展开的变量名。

    借助 AST 区分真实调用与函数定义/文档字符串；仅用正则做快速短路。
    """
    source = path.read_text(encoding="utf-8")
    if not _TRACE_EVENT_TOKEN.search(source):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:  # pragma: no cover - app/ 下不应出现
        raise AssertionError(f"无法解析 {path}") from None
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "trace_event":
            continue
        for keyword in node.keywords:
            if keyword.arg is not None:
                continue
            expanded = keyword.value.id if isinstance(keyword.value, ast.Name) else "<expr>"
            found.append((node.lineno, expanded))
    return found


def _app_expansion_sites(root: Path = ROOT) -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for path in sorted((root / "app").rglob("*.py")):
        for line, expanded in _trace_expansion_calls(path):
            sites.append((path.relative_to(root).as_posix(), line, expanded))
    return sites


def _assert_only_allowed_expansions(sites: list[tuple[str, int, str]]) -> None:
    files = {path for path, _, _ in sites}
    # 允许清单里暂未命中的条目不算错误（文件可能仍在演进），但任何未登记的
    # 新增展开点都必须立刻失败。
    unexpected = files - set(ALLOWED_TRACE_EXPANSIONS)
    assert not unexpected, (
        "发现未登记的 trace_event(**...) 展开点，请改用 trace_fields() 或在 "
        f"ALLOWED_TRACE_EXPANSIONS 中写明理由。实际: {sorted(unexpected)}"
    )
    assert all(reason.strip() for reason in ALLOWED_TRACE_EXPANSIONS.values())

    for path, line, expanded in sites:
        assert expanded == "fields", (
            f"{path}:{line} 直接展开 {expanded}；保留字段可能覆盖 run_id/session_id"
        )


def test_guard_detects_raw_expansion_in_sample(tmp_path):
    """守卫自身的自检：函数定义和文档字符串不误报，裸展开必须被识别。"""
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""示例：trace_event(run_id, "x", **event) 只是文档。"""\n'
        "def trace_event(run_id, event, **fields):\n"
        "    pass\n"
        "\n"
        "def emit(event):\n"
        '    trace_event(run_id, "kind", **event)\n',
        encoding="utf-8",
    )

    assert _trace_expansion_calls(sample) == [(6, "event")]


def test_guard_rejects_new_raw_expansion(tmp_path):
    """守卫自身的负向自检：新增的裸展开必须被拒绝，而不是靠人眼 review。"""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "newmod.py").write_text(
        "def emit(run_id, event):\n"
        '    trace_event(run_id, "kind", **event)\n',
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="未登记"):
        _assert_only_allowed_expansions(_app_expansion_sites(tmp_path))


def test_no_unsanitized_trace_expansion_in_app():
    """root cause 守卫：app/ 只允许经 trace_fields() 清洗后的字典展开。"""
    sites = _app_expansion_sites()
    _assert_only_allowed_expansions(sites)

    runtime_sites = [site for site in sites if site[0] == "app/services/diagnostic_runtime.py"]
    assert runtime_sites, "diagnostic_runtime 必须保留唯一的清洗后展开入口"
    runtime_path = ROOT / "app/services/diagnostic_runtime.py"
    runtime_source = runtime_path.read_text(encoding="utf-8")
    assert runtime_source.count("trace_fields(") >= len(runtime_sites), (
        "每个展开点都必须先经过 trace_fields() 清洗"
    )
    # 事件名必须由本模块确定：字面量，或经过唯一改名函数。入站 type 直接当作
    # trace 记录名会让伪造的 type=run_error 与真终态一起重复计数。
    _TRACE_NAME_HELPERS = {"_incoming_trace_name"}
    tree = ast.parse(runtime_source, filename=str(runtime_path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "trace_event" or len(node.args) < 2:
            continue
        target = node.args[1]
        helper = (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Name)
            and target.func.id in _TRACE_NAME_HELPERS
        )
        assert isinstance(target, ast.Constant) or helper, (
            f"{runtime_path.name}:{node.lineno} trace 事件名必须是字面量或"
            f"来自 {sorted(_TRACE_NAME_HELPERS)}"
        )
