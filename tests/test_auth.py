"""可选 API Key 鉴权测试（密钥统一来自 Settings，而非模块级 os.getenv 快照）。"""

import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, config
from app.core import auth

DUMMY_SECRET = "dummy-secret-must-never-leak"


@pytest.fixture()
def auth_config(monkeypatch):
    def apply(key: str, mode: str = "dev"):
        monkeypatch.setattr(config, "api_key", key)
        monkeypatch.setattr(config, "auth_mode", mode)

    return apply


@pytest.mark.asyncio
async def test_auth_disabled_when_no_key(auth_config):
    auth_config("")
    await auth.require_api_key("")  # 不应抛出


@pytest.mark.asyncio
async def test_auth_rejects_wrong_key(auth_config):
    auth_config("secret")
    with pytest.raises(HTTPException) as exc:
        await auth.require_api_key("wrong")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_auth_accepts_correct_key(auth_config):
    auth_config("secret")
    await auth.require_api_key("secret")  # 不应抛出


@pytest.mark.asyncio
async def test_deployment_mode_without_key_is_fail_closed(auth_config):
    auth_config("", "deployment")
    with pytest.raises(HTTPException) as exc:
        await auth.require_api_key("")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", ["   ", "your-api-key-here", "change-me"])
async def test_placeholder_key_is_not_a_real_key(auth_config, placeholder):
    auth_config(placeholder, "deployment")
    with pytest.raises(HTTPException) as exc:
        await auth.require_api_key(placeholder.strip())
    assert exc.value.status_code == 401


def test_startup_check_rejects_deployment_without_key(auth_config):
    auth_config("", "deployment")
    with pytest.raises(RuntimeError, match="API_KEY"):
        auth.startup_check()


def test_startup_check_allows_dev_without_key(auth_config):
    auth_config("", "dev")
    auth.startup_check()


def test_error_message_never_echoes_key(auth_config):
    auth_config("super-secret-value")

    async def call():
        with pytest.raises(HTTPException) as exc:
            await auth.require_api_key("wrong")
        return str(exc.value.detail)

    detail = asyncio.run(call())
    assert "super-secret-value" not in detail


# ====== P0-02 收尾：配置序列化、健康信号、模式校验 ======


def test_config_dump_never_contains_api_key():
    """config.model_dump()/model_dump_json() 必须对密钥打码。"""
    settings = Settings(
        _env_file=None,
        auth_mode="deployment",
        api_key=DUMMY_SECRET,
        llm_api_key=DUMMY_SECRET + "-llm",
        embedding_api_key=DUMMY_SECRET + "-embedding",
        tencentcloud_secret_id=DUMMY_SECRET + "-tc-id",
        tencentcloud_secret_key=DUMMY_SECRET + "-tc-key",
    )

    dumped = settings.model_dump()
    dumped_json = settings.model_dump_json()
    dumped_json_mode = settings.model_dump(mode="json")

    for payload in (dumped, dumped_json, dumped_json_mode):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        assert DUMMY_SECRET not in text, "配置导出不能包含明文密钥"

    for field in (
        "api_key",
        "llm_api_key",
        "embedding_api_key",
        "tencentcloud_secret_id",
        "tencentcloud_secret_key",
    ):
        assert dumped[field] == "***", f"{field} 必须打码"

    # repr 同样不能回显（模型、异常、调试输出都可能带 repr）
    assert DUMMY_SECRET not in repr(settings)


def test_config_dump_keeps_empty_secret_empty():
    """未配置的密钥保持空串，避免被当成“已配置”。"""
    settings = Settings(_env_file=None)

    assert settings.model_dump()["api_key"] == ""
    assert settings.model_dump()["llm_api_key"] == ""


def test_health_reports_auth_mode_but_not_key(monkeypatch):
    """/health 暴露有效鉴权模式与密钥是否配置，但绝不回显密钥。"""
    from app.api import health

    monkeypatch.setattr(config, "auth_mode", "deployment")
    monkeypatch.setattr(config, "api_key", DUMMY_SECRET)
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)

    test_app = FastAPI()
    test_app.include_router(health.router)
    with TestClient(test_app) as client:
        response = client.get("/health")

    body = response.json()
    assert response.status_code == 200
    auth_data = body["data"]["auth"]
    assert auth_data["mode"] == "deployment"
    assert auth_data["api_key_configured"] is True
    assert auth_data["authentication_required"] is True
    explain = json.dumps(body["data"], ensure_ascii=False)
    assert DUMMY_SECRET not in response.text
    # 字段集合固定：只报告模式与两个布尔值，不携带密钥值/掩码/长度
    assert set(auth_data) == {"mode", "api_key_configured", "authentication_required"}
    assert all(DUMMY_SECRET not in str(value) for value in auth_data.values())
    assert "api-key" not in explain


def test_health_dev_mode_without_key_is_still_healthy(monkeypatch):
    """dev 无密钥只是未鉴权，不应让健康检查失败。"""
    from app.api import health

    monkeypatch.setattr(config, "auth_mode", "dev")
    monkeypatch.setattr(config, "api_key", "")
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)

    test_app = FastAPI()
    test_app.include_router(health.router)
    with TestClient(test_app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    auth_data = response.json()["data"]["auth"]
    assert auth_data["mode"] == "dev"
    assert auth_data["api_key_configured"] is False
    assert auth_data["authentication_required"] is False


def _health_client(monkeypatch, milvus_check):
    """只挂 health router：主应用的 lifespan 会连 Milvus，与本接口无关。"""
    from app.api import health

    monkeypatch.setattr(health.milvus_manager, "health_check", milvus_check)
    test_app = FastAPI()
    test_app.include_router(health.router)
    return TestClient(test_app)


def _raise_connection_error():
    raise ConnectionError("milvus down")


@pytest.mark.parametrize(
    "milvus_check",
    [lambda: False, _raise_connection_error],
    ids=["disconnected", "exception"],
)
def test_health_degrades_rag_capability_but_stays_healthy(monkeypatch, milvus_check):
    """Milvus 不可用是能力降级，不是进程不健康：仍返回 200。"""
    with _health_client(monkeypatch, milvus_check) as client:
        response = client.get("/health")

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 200
    assert body["message"]
    assert body["data"]["status"] == "healthy"
    assert body["data"]["milvus"]["status"] != "connected"

    rag = body["data"]["capabilities"]["rag"]
    assert rag["status"] == "unavailable"
    assert rag["reason"], "降级必须给出原因，不能只给状态"


def test_health_envelope_and_capabilities_intact(monkeypatch):
    """保持 {code,message,data} 外层，避免前端 api.health() 与探针解析失败。"""
    with _health_client(monkeypatch, lambda: True) as client:
        response = client.get("/health")

    body = response.json()
    assert set(body) == {"code", "message", "data"}
    # 能力状态词汇统一为 M2_API_CONTRACT 第 5 节的
    # unconfigured|ready|degraded|unavailable（rag 另有 invalid_index）。
    assert body["data"]["capabilities"]["rag"] == {"status": "ready", "reason": None}
    assert body["data"]["auth"]["api_key_configured"] is False
    assert body["data"]["service"]


@pytest.mark.parametrize("bad_mode", ["staging", "prod", "", "  "])
def test_settings_rejects_unknown_auth_mode(monkeypatch, bad_mode):
    """AUTH_MODE 只接受 dev / deployment，未知值在启动前就报错。"""
    # 走真实来源：进程环境（部署时 docker/宿主就是这么传的）
    monkeypatch.setenv("AUTH_MODE", bad_mode)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_health_auth_block_matches_auth_module_status(monkeypatch):
    """健康检查的鉴权摘要必须与 auth_status() 同源，避免两处各写一套判断。"""
    from app.api import health

    monkeypatch.setattr(config, "auth_mode", "deployment")
    monkeypatch.setattr(config, "api_key", DUMMY_SECRET)
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)

    test_app = FastAPI()
    test_app.include_router(health.router)
    with TestClient(test_app) as client:
        response = client.get("/health")

    assert response.json()["data"]["auth"] == auth.auth_status()


def test_cors_origins_accepts_comma_separated_env(monkeypatch):
    """CORS_ORIGINS 作为 Settings 列表字段，兼容逗号分隔的旧写法。"""
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:9900, http://127.0.0.1:9900 ,")
    settings = Settings(_env_file=None)

    assert settings.cors_origins == ["http://localhost:9900", "http://127.0.0.1:9900"]


def test_cors_origins_from_env_file_only(monkeypatch, tmp_path):
    """只在 env_file 里配置 CORS_ORIGINS 也必须生效（与 P0-02 同类缺陷）。"""
    env_file = tmp_path / "cors.env"
    env_file.write_text(
        "CORS_ORIGINS=https://ops.example.com,https://admin.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    settings = Settings(_env_file=str(env_file))

    assert settings.cors_origins == [
        "https://ops.example.com",
        "https://admin.example.com",
    ]


def test_cors_origins_default_is_localhost_only():
    settings = Settings(_env_file=None)

    assert settings.cors_origins == ["http://localhost:9900", "http://127.0.0.1:9900"]
    assert "*" not in settings.cors_origins


def test_web_server_binds_configured_host_and_port(monkeypatch):
    """web_server 是容器 entrypoint 实际 exec 的进程，必须遵守 HOST/PORT。"""
    from servers import web_server

    monkeypatch.setattr(config, "host", "127.0.0.1")
    monkeypatch.setattr(config, "port", 9900)
    options = web_server.uvicorn_options()
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 9900

    monkeypatch.setattr(config, "host", "0.0.0.0")
    monkeypatch.setattr(config, "port", 8080)
    options = web_server.uvicorn_options()
    assert options["host"] == "0.0.0.0"
    assert options["port"] == 8080


def test_cors_config_is_not_read_via_os_getenv_at_import():
    """main.py 必须是“导入时读 Settings”，不能再用 os.getenv 快照。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    assert "os.getenv" not in source
    assert "config.cors_origins" in source


def test_auth_failure_logs_never_echo_key(auth_config):
    """日志是另一个泄密面：鉴权失败日志同样不能包含密钥。"""
    from loguru import logger

    auth_config(DUMMY_SECRET, "deployment")
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    try:

        async def call():
            with pytest.raises(HTTPException):
                await auth.require_api_key("wrong-key")

        asyncio.run(call())
    finally:
        logger.remove(sink_id)

    logs = "".join(captured)
    assert DUMMY_SECRET not in logs
    assert "wrong-key" not in logs
