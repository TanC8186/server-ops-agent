"""BE-03 / BE-08 HTTP 契约：目标选择与运行元数据。

只挂载被测 router，服务用假实现；不连接 MCP、模型或向量库，也不写真实 trace。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import aiops, runs

VALID_RUN = "abcdef0123456789abcdef0123456789"


class FakeAIOpsService:
    """与真实服务相同的公开签名；只回放预置事件。"""

    def __init__(self, events: list[dict]):
        self.events = events
        self.calls: list[dict] = []

    async def diagnose(
        self,
        session_id,
        execution_mode=None,
        symptom=None,
        service_name=None,
    ):
        self.calls.append(
            {
                "session_id": session_id,
                "execution_mode": execution_mode,
                "symptom": symptom,
                "service_name": service_name,
            }
        )
        for event in self.events:
            yield event


COMPLETE = {
    "type": "complete",
    "status": "completed",
    "lifecycle": "completed",
    "quality": "partial",
    "response": "# 报告",
}

TARGET_REQUIRED = {
    "type": "complete",
    "status": "target_required",
    "candidates": [
        {"service": "svc-a", "region": "ap-guangzhou"},
        {"service": "svc-b", "region": "ap-shanghai"},
    ],
    "reason": "未指定目标服务，且有多个候选",
}


@pytest.fixture()
def aiops_client(monkeypatch):
    def build(events: list[dict]):
        service = FakeAIOpsService(events)
        monkeypatch.setattr(aiops, "aiops_service", service)
        # 只挂载被测 router：主应用的 lifespan 会连接 Milvus。
        test_app = FastAPI()
        test_app.include_router(aiops.router, prefix="/api")
        return TestClient(test_app), service

    return build


def _sse_data(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _terminals(events: list[dict]) -> list[dict]:
    return [
        event for event in events
        if event.get("type") in {"complete", "error", "target_required"}
    ]


def test_target_required_completion_is_delivered_and_ends_stream(aiops_client):
    client, _service = aiops_client([{"type": "status", "stage": "run_start"}, TARGET_REQUIRED])

    with client:
        response = client.post("/api/aiops", json={"session_id": "s-target"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    data = _sse_data(response)
    terminals = _terminals(data)
    assert len(terminals) == 1
    assert terminals[0]["status"] == "target_required"
    assert [item["service"] for item in terminals[0]["candidates"]] == ["svc-a", "svc-b"]


def test_target_choice_is_forwarded_to_service(aiops_client):
    """当前服务没有 target_choice 形参：选择以 service_name 下发（显式目标）。"""
    client, service = aiops_client([COMPLETE])

    with client:
        response = client.post(
            "/api/aiops",
            json={"session_id": "s-choice", "target_choice": "svc-b", "service_name": "svc-a"},
        )

    assert response.status_code == 200
    assert service.calls[0]["service_name"] == "svc-b"


def test_target_choice_accepts_max_length(aiops_client):
    client, service = aiops_client([COMPLETE])

    with client:
        response = client.post(
            "/api/aiops", json={"session_id": "s-max", "target_choice": "s" * 120}
        )

    assert response.status_code == 200
    assert service.calls[0]["service_name"] == "s" * 120


def test_oversized_target_choice_is_rejected(aiops_client):
    client, service = aiops_client([COMPLETE])

    with client:
        response = client.post(
            "/api/aiops", json={"session_id": "s-long", "target_choice": "s" * 121}
        )

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize(
    "time_window",
    [
        {"start": "2026-10-20T00:00:00+08:00", "end": "2026-10-19T00:00:00+08:00"},
        {"start": 100, "end": 100},
        {"start": 0, "end": 7 * 24 * 3600 + 1},
    ],
)
def test_invalid_time_window_is_rejected(aiops_client, time_window):
    client, service = aiops_client([COMPLETE])

    with client:
        response = client.post(
            "/api/aiops", json={"session_id": "s-window", "time_window": time_window}
        )

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize(
    "time_window",
    [
        {"start": "2026-10-20T00:00:00+08:00", "end": "2026-10-21T00:00:00+08:00"},
        {"start": 1_700_000_000, "end": 1_700_000_000 + 7 * 24 * 3600},
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-20T06:00:00Z"},
    ],
)
def test_valid_time_window_is_accepted(aiops_client, time_window):
    client, service = aiops_client([COMPLETE])

    with client:
        response = client.post(
            "/api/aiops", json={"session_id": "s-window", "time_window": time_window}
        )

    assert response.status_code == 200
    assert len(service.calls) == 1


def test_malformed_time_window_is_rejected(aiops_client):
    client, service = aiops_client([COMPLETE])

    with client:
        response = client.post(
            "/api/aiops",
            json={"session_id": "s-window", "time_window": {"start": "not-a-time", "end": 2}},
        )

    assert response.status_code == 422
    assert service.calls == []


def test_service_yielding_two_terminals_still_yields_one(aiops_client):
    client, _service = aiops_client(
        [
            COMPLETE,
            {"type": "error", "message": "迟到的错误"},
        ]
    )

    with client:
        response = client.post("/api/aiops", json={"session_id": "s-double"})

    assert response.status_code == 200
    data = _sse_data(response)
    assert _terminals(data) == [COMPLETE]


@pytest.fixture()
def runs_client(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(runs, "RESULT_DIR", tmp_path / "results")
    (tmp_path / "traces").mkdir(exist_ok=True)
    (tmp_path / "results").mkdir(exist_ok=True)
    test_app = FastAPI()
    test_app.include_router(runs.router, prefix="/api")
    with TestClient(test_app) as client:
        yield client, tmp_path


def _write_trace(root: Path, run_id: str, events: list[dict]) -> None:
    path = root / "traces" / f"{run_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def test_list_runs_surfaces_lifecycle_quality_and_target(runs_client):
    client, root = runs_client
    _write_trace(
        root,
        VALID_RUN,
        [
            {"event": "run_start", "ts": "2026-10-26T10:00:00", "session_id": "s1",
             "execution_mode": "local"},
            {"event": "target_bound", "target": {"service": "svc-a", "region": "ap-guangzhou"}},
            {"event": "tool_step", "step": "query_cpu_metrics", "input_tokens": 4},
            {"event": "report", "result_preview": "# 报告"},
            {"event": "run_complete", "duration_ms": 1200, "quality": "partial",
             "lifecycle": "completed", "response_chars": 10},
        ],
    )

    response = client.get("/api/runs")

    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["execution_mode"] == "local"
    assert run["lifecycle"] == "completed"
    assert run["quality"] == "partial"
    assert run["target"] == {"service": "svc-a", "region": "ap-guangzhou"}
    assert run["has_report"] is True
    assert run["status"] == "ok"


def test_list_runs_terminal_error_maps_to_failed(runs_client):
    client, root = runs_client
    _write_trace(
        root,
        VALID_RUN,
        [
            {"event": "run_start", "ts": "2026-10-26T10:00:00", "execution_mode": "cloud_multi"},
            {"event": "run_error", "message": "工具失败", "duration_ms": 30},
        ],
    )

    run = client.get("/api/runs").json()["runs"][0]

    assert run["lifecycle"] == "failed"
    assert run["quality"] is None
    assert run["target"] is None
    assert run["has_report"] is False
    assert run["status"] == "error"
    assert run["execution_mode"] == "cloud_multi"


def test_list_runs_without_terminal_has_null_lifecycle(runs_client):
    client, root = runs_client
    _write_trace(
        root,
        VALID_RUN,
        [{"event": "run_start", "ts": "2026-10-26T10:00:00", "execution_mode": "local"}],
    )

    run = client.get("/api/runs").json()["runs"][0]

    assert run["lifecycle"] is None
    assert run["quality"] is None
    assert run["status"] == "running"


def test_unknown_run_still_404(runs_client):
    client, _root = runs_client

    assert client.get(f"/api/runs/{VALID_RUN}").status_code == 404
