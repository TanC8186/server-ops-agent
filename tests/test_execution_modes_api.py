"""双模式 HTTP 契约；使用假服务，不连接 MCP、模型或向量库。"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import aiops
from app.config import config


class FakeAIOpsService:
    def __init__(self):
        self.calls = []

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
        yield {
            "type": "complete",
            "execution_mode": execution_mode,
            "run_id": "fake-run",
        }


@pytest.fixture()
def mode_client(monkeypatch):
    fake_service = FakeAIOpsService()
    monkeypatch.setattr(aiops, "aiops_service", fake_service)
    # Isolate only this router: the main application lifespan connects Milvus.
    test_app = FastAPI()
    test_app.include_router(aiops.router, prefix="/api")
    with TestClient(test_app) as client:
        yield client, fake_service


def _sse_data(response):
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.parametrize("mode", ["local", "cloud_multi"])
def test_mode_and_diagnostic_input_reach_service(mode_client, mode):
    client, service = mode_client
    payload = {
        "session_id": "session-mode-test",
        "execution_mode": mode,
        "symptom": "CPU 使用率 97%，请求延迟升高。",
        "service_name": "api-gateway-service",
    }

    response = client.post("/api/aiops", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert service.calls == [payload]
    assert _sse_data(response) == [
        {"type": "complete", "execution_mode": mode, "run_id": "fake-run"}
    ]


def test_http_requests_default_to_local_mode(mode_client):
    client, service = mode_client

    response = client.post("/api/aiops", json={"session_id": "default-mode"})

    assert response.status_code == 200
    assert service.calls == [
        {
            "session_id": "default-mode",
            "execution_mode": "local",
            "symptom": None,
            "service_name": None,
        }
    ]


@pytest.mark.parametrize("mode", ["automatic", "cloud", "", None, 1])
def test_unknown_or_null_execution_mode_is_rejected(mode_client, mode):
    client, service = mode_client

    response = client.post("/api/aiops", json={"execution_mode": mode})

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize(
    ("field", "length"), [("symptom", 4001), ("service_name", 121)]
)
def test_oversized_input_is_rejected_before_execution(mode_client, field, length):
    client, service = mode_client

    response = client.post("/api/aiops", json={field: "x" * length})

    assert response.status_code == 422
    assert service.calls == []


def test_documented_input_length_limits_are_accepted(mode_client):
    client, service = mode_client
    payload = {"symptom": "症" * 4000, "service_name": "s" * 120}

    response = client.post("/api/aiops", json=payload)

    assert response.status_code == 200
    assert service.calls[0]["symptom"] == payload["symptom"]
    assert service.calls[0]["service_name"] == payload["service_name"]


@pytest.mark.parametrize("local_ready", [False, True])
@pytest.mark.parametrize("cloud_ready", [False, True])
def test_mode_configuration_status_does_not_expose_credentials(
    mode_client, monkeypatch, local_ready, cloud_ready
):
    client, service = mode_client
    secret = "unit-test-cloud-key-must-not-appear"
    monkeypatch.setattr(
        config, "local_llm_base_url", "http://127.0.0.1:8080/v1" if local_ready else ""
    )
    monkeypatch.setattr(config, "local_llm_model", "qwen-ops-test")
    monkeypatch.setattr(config, "llm_base_url", "https://model.example.invalid/v1")
    monkeypatch.setattr(config, "llm_api_key", secret if cloud_ready else "")
    monkeypatch.setattr(config, "llm_model", "cloud-model-test")

    response = client.get("/api/execution-modes")

    assert response.status_code == 200
    modes = {item["id"]: item for item in response.json()["modes"]}
    assert set(modes) == {"local", "cloud_multi"}
    assert modes["local"]["configured"] is local_ready
    assert modes["cloud_multi"]["configured"] is cloud_ready
    assert modes["local"]["model"] == "qwen-ops-test"
    assert modes["cloud_multi"]["model"] == "cloud-model-test"
    for item in modes.values():
        assert set(item) == {"id", "label", "description", "configured", "model"}
        assert item["label"]
        assert item["description"]
    assert secret not in response.text
    assert "api_key" not in response.text.lower()
    assert service.calls == []
