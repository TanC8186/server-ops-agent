"""``/health`` 能力摘要回归（M5 / OPS-01 / OPS-02）。

覆盖点：

- 能力字段集合固定为 ``diagnosis_local / diagnosis_cloud / rag / tracing``；
- 状态词汇严格是 ``unconfigured|ready|degraded|unavailable``，``rag`` 额外
  允许 ``invalid_index``；
- Milvus 故障只降级 ``rag``：顶层 ``status`` 仍是 healthy，HTTP 仍是 200；
- 配置判定沿用 ``/api/execution-modes`` 的规则（``your-`` 前缀视为未配置）；
- 响应里绝不出现密钥、令牌等敏感值与 ``last_error`` 载荷。

这些用例只挂 health router，不触发主应用 lifespan（会真实连 Milvus）。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import observability
from app.config import config
from app.services.vector_store_manager import (
    VectorStoreInvalidIndex,
    VectorStoreUnavailable,
    vector_store_manager,
)

_ALLOWED = {"unconfigured", "ready", "degraded", "unavailable"}
_RAG_ALLOWED = _ALLOWED | {"invalid_index"}
_SECRET = "sk-live-health-must-never-leak-42"


def _client(monkeypatch, milvus_check=lambda: True, *, init_error=None, initialized=False):
    """构造只挂 /health 的临时应用，并把外部依赖替换成可预期的状态。"""
    from app.api import health

    monkeypatch.setattr(health.milvus_manager, "health_check", milvus_check)
    monkeypatch.setattr(vector_store_manager, "_init_error", init_error, raising=False)
    monkeypatch.setattr(vector_store_manager, "_initialized", initialized, raising=False)
    app = FastAPI()
    app.include_router(health.router)
    return TestClient(app)


def _get(monkeypatch, milvus_check=lambda: True, **kwargs) -> dict:
    with _client(monkeypatch, milvus_check, **kwargs) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"code", "message", "data"}
    return body["data"]


def test_capabilities_have_exact_keys_and_valid_status_vocabulary(monkeypatch):
    data = _get(monkeypatch)
    caps = data["capabilities"]

    assert set(caps) == {"diagnosis_local", "diagnosis_cloud", "rag", "tracing"}
    for name in ("diagnosis_local", "diagnosis_cloud", "rag"):
        assert set(caps[name]) == {"status", "reason"}, name
        assert caps[name]["status"] in _RAG_ALLOWED, name
    # tracing 额外带写入/失败计数，且不暴露 last_error 等载荷
    assert set(caps["tracing"]) == {"status", "writes", "failures"}
    assert caps["tracing"]["status"] in _ALLOWED
    assert isinstance(caps["tracing"]["writes"], int)
    assert isinstance(caps["tracing"]["failures"], int)
    assert data["status"] == "healthy"
    assert data["milvus"]["status"] == "connected"


def test_diagnosis_local_reports_unconfigured_ready_and_degraded(monkeypatch):
    monkeypatch.setattr(config, "local_llm_base_url", "")
    assert _get(monkeypatch)["capabilities"]["diagnosis_local"] == {
        "status": "unconfigured",
        "reason": "未配置 LOCAL_LLM_BASE_URL",
    }

    monkeypatch.setattr(config, "local_llm_base_url", "http://127.0.0.1:8000/v1")
    monkeypatch.setattr(config, "local_llm_model", "qwen-ops")
    assert _get(monkeypatch)["capabilities"]["diagnosis_local"] == {
        "status": "ready",
        "reason": None,
    }

    # 配了地址但不是 http(s) 端点：进程还能跑，本地诊断实际不可用。
    monkeypatch.setattr(config, "local_llm_base_url", "127.0.0.1:8000")
    degraded = _get(monkeypatch)["capabilities"]["diagnosis_local"]
    assert degraded["status"] == "degraded"
    assert degraded["reason"]


def test_diagnosis_cloud_treats_placeholder_as_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "llm_api_key", "")
    assert _get(monkeypatch)["capabilities"]["diagnosis_cloud"] == {
        "status": "unconfigured",
        "reason": "未配置 LLM_API_KEY",
    }

    # 仓库模板里的占位符必须视为未配置（与 /api/execution-modes 一致）。
    monkeypatch.setattr(config, "llm_api_key", "your-api-key-here")
    placeholder = _get(monkeypatch)["capabilities"]["diagnosis_cloud"]
    assert placeholder["status"] == "unconfigured"
    assert "your-" in placeholder["reason"]
    assert "your-api-key-here" not in str(placeholder), "reason 不得回显配置值"

    monkeypatch.setattr(config, "llm_api_key", _SECRET)
    monkeypatch.setattr(config, "llm_base_url", "https://api.example.com")
    monkeypatch.setattr(config, "llm_model", "claude-sonnet-4-6")
    assert _get(monkeypatch)["capabilities"]["diagnosis_cloud"] == {
        "status": "ready",
        "reason": None,
    }


def test_milvus_failure_only_degrades_rag_and_keeps_http_200(monkeypatch):
    data = _get(monkeypatch, lambda: False)

    assert data["status"] == "healthy"
    assert data["milvus"]["status"] == "disconnected"
    rag = data["capabilities"]["rag"]
    assert rag["status"] == "unavailable"
    assert rag["reason"]
    # 其它能力不受 Milvus 影响
    assert data["capabilities"]["tracing"]["status"] in _ALLOWED


def test_milvus_exception_does_not_leak_exception_detail(monkeypatch):
    def broken():
        raise ConnectionError(f"connect failed key={_SECRET}")

    data = _get(monkeypatch, broken)
    assert data["status"] == "healthy"
    rag = data["capabilities"]["rag"]
    assert rag["status"] == "unavailable"
    # 只保留异常类型，不把连接串/异常消息扩散到公开探针
    assert "ConnectionError" in rag["reason"]
    assert _SECRET not in str(data)


def test_rag_reports_invalid_index_separately_from_unavailable(monkeypatch):
    error = VectorStoreInvalidIndex(
        "collection biz 的距离度量是 L2，而当前 embedding 描述符声明 cosine"
    )
    data = _get(monkeypatch, init_error=error, initialized=False)

    rag = data["capabilities"]["rag"]
    assert rag["status"] == "invalid_index"
    assert rag["reason"]
    assert data["status"] == "healthy"


def test_rag_reports_unavailable_for_connection_failure(monkeypatch):
    data = _get(
        monkeypatch,
        init_error=VectorStoreUnavailable("向量库不可用: connection refused"),
    )
    assert data["capabilities"]["rag"]["status"] == "unavailable"


def test_tracing_reports_counters_and_degrades_on_failures(monkeypatch):
    monkeypatch.setattr(
        observability,
        "get_trace_health",
        lambda: {"writes": 7, "failures": 0, "last_error": "", "last_error_at": "", "dropped": 0},
    )
    tracing = _get(monkeypatch)["capabilities"]["tracing"]
    assert tracing == {"status": "ready", "writes": 7, "failures": 0}

    monkeypatch.setattr(
        observability,
        "get_trace_health",
        lambda: {
            "writes": 9,
            "failures": 2,
            "last_error": "PermissionError: access denied",
            "last_error_at": "2026-10-26T00:00:00",
            "dropped": 0,
        },
    )
    tracing = _get(monkeypatch)["capabilities"]["tracing"]
    assert tracing == {"status": "degraded", "writes": 9, "failures": 2}
    # 错误详情不进 /health，避免把文件路径/载荷带到公开探针
    assert "PermissionError" not in str(tracing)


def test_tracing_module_failure_does_not_break_health(monkeypatch):
    def broken():
        raise RuntimeError("tracing stats unavailable")

    monkeypatch.setattr(observability, "get_trace_health", broken)
    data = _get(monkeypatch)
    assert data["status"] == "healthy"
    tracing = data["capabilities"]["tracing"]
    assert tracing["status"] == "unavailable"
    assert tracing["writes"] == 0 and tracing["failures"] == 0


@pytest.mark.parametrize(
    "status_check",
    [
        lambda data: "llm_api_key" in str(data),
        lambda data: _SECRET in str(data),
        lambda data: "your-api-key-here" in str(data),
    ],
    ids=["no-key-field", "no-secret-value", "no-placeholder-value"],
)
def test_health_never_contains_credentials(monkeypatch, status_check):
    monkeypatch.setattr(config, "llm_api_key", _SECRET)
    monkeypatch.setattr(config, "local_llm_base_url", "http://127.0.0.1:8000/v1")
    data = _get(monkeypatch)
    assert not status_check(data)
