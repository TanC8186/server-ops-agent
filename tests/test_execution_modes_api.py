"""双模式 HTTP 契约；使用假服务，不连接 MCP、模型或向量库。"""

import json
import re

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
    # 响应形状整体锁定：新增字段必须显式改这个断言，不能悄悄多吐东西。
    # G8 加了 requires_credentials / failure_behavior / failure_text / resource_cost
    # 四项"选模式时必须知道"的取舍；它们只含**变量名**与固定文案，不含任何值。
    for item in modes.values():
        assert set(item) == {
            "id", "label", "description", "configured", "model",
            "requires_credentials", "failure_behavior", "failure_text",
            "resource_cost",
        }
        assert item["label"]
        assert item["description"]
        # 只允许出现配置项**变量名**，不能带出任何凭据值。
        assert item["requires_credentials"], "必须说明该模式依赖哪个配置项"
        assert all(name.isupper() for name in item["requires_credentials"])
        assert item["failure_behavior"] in {"stop", "report_error"}
        assert item["failure_text"]
        assert item["resource_cost"]

    # 模式语义必须与运行器一致：本地明确停止，不静默切云端。
    assert modes["local"]["failure_behavior"] == "stop"
    assert "不会自动切换" in modes["local"]["failure_text"]
    assert "LOCAL_LLM_BASE_URL" in modes["local"]["requires_credentials"]
    assert "LLM_API_KEY" in modes["cloud_multi"]["requires_credentials"]
    assert secret not in response.text
    # 不能断言"响应里没有 api_key 字样"了：G8 会回传**配置项变量名**
    # LLM_API_KEY（这是让用户知道该配什么所必需的，且 .env.example 里本来就公开）。
    # 要守的是"不能带出值"：任何像密钥的长串都不许出现。
    lowered = response.text.lower()
    assert "api_key=" not in lowered
    assert "bearer " not in lowered
    # 变量名之外不许出现任何 >20 字符的疑似密钥串（测试密钥本身已单独断言）。
    assert not re.search(r"[A-Za-z0-9_\-]{24,}", response.text.replace(secret, ""))
    assert service.calls == []
