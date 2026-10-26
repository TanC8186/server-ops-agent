"""P0-02 回归：仅通过 env_file 提供 API_KEY 时鉴权必须生效。

子进程只通过临时 env_file 注入配置（不放在进程环境里），因此能复现
“pydantic-settings 读了 .env，但 os.getenv 仍是空”的原始缺陷。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _child_script() -> str:
    """子进程：真实 auth 依赖 + 最小路由，全部请求在进程内闭环。"""
    return textwrap.dedent(
        """
        import json, sys
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        from app.core.auth import require_api_key
        from app.config import config

        app = FastAPI()

        @app.get("/api/protected", dependencies=[Depends(require_api_key)])
        def protected():
            return {"ok": True}

        @app.get("/health")
        def health():
            return {"ok": True}

        client = TestClient(app)
        results = {}
        results["no_key"] = client.get("/api/protected").status_code
        results["wrong_key"] = client.get(
            "/api/protected", headers={"X-API-Key": "wrong-key"}
        ).status_code
        results["right_key"] = client.get(
            "/api/protected", headers={"X-API-Key": "correct-key"}
        ).status_code
        results["health_open"] = client.get("/health").status_code
        results["configured_key_present"] = bool(getattr(config, "api_key", ""))
        results["child_environ_has_key"] = "API_KEY" in __import__("os").environ
        print(json.dumps(results))
        """
    )


def _run_child(tmp_path: Path, env_file_body: str, *, extra_env: dict | None = None):
    env_file = tmp_path / "child.env"
    env_file.write_text(env_file_body, encoding="utf-8")
    script = tmp_path / "child_app.py"
    script.write_text(_child_script(), encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "API_KEY"}
    env["AI_CONFIG_FILE"] = str(env_file)
    env.update(extra_env or {})
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    completed = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_env_file_only_key_authenticates(tmp_path):
    result = _run_child(
        tmp_path,
        "API_KEY=correct-key\nAUTH_MODE=deployment\n",
    )

    assert result["child_environ_has_key"] is False, "反例前提：密钥只在 env_file 中"
    assert result["no_key"] == 401
    assert result["wrong_key"] == 401
    assert result["right_key"] == 200
    assert result["health_open"] == 200
    assert result["configured_key_present"] is True


def test_deployment_mode_without_key_fails_closed(tmp_path):
    result = _run_child(tmp_path, "AUTH_MODE=deployment\n")

    assert result["no_key"] == 401
    assert result["wrong_key"] == 401
    assert result["right_key"] == 401
    assert result["health_open"] == 200


def test_explicit_dev_mode_allows_local_without_key(tmp_path):
    result = _run_child(tmp_path, "AUTH_MODE=dev\n")

    assert result["no_key"] == 200
    assert result["child_environ_has_key"] is False


def test_dev_mode_still_enforces_key_when_provided(tmp_path):
    result = _run_child(tmp_path, "AUTH_MODE=dev\nAPI_KEY=correct-key\n")

    assert result["no_key"] == 401
    assert result["wrong_key"] == 401
    assert result["right_key"] == 200


@pytest.mark.parametrize("key", ["", "   ", "your-api-key-here"])
def test_placeholder_key_never_counts_as_configured(tmp_path, key):
    result = _run_child(tmp_path, f"AUTH_MODE=deployment\nAPI_KEY={key}\n")

    assert result["right_key"] == 401


# ====== P0-02 收尾：进程环境空值优先于 env_file（compose 契约） ======


def test_empty_process_env_overrides_key_from_env_file(tmp_path):
    """空进程环境值会压过 env_file —— 这正是 compose 不能导出空 API_KEY 的原因。

    pydantic-settings 的优先级是「进程环境 > env_file」；``API_KEY=`` 作为
    进程环境变量存在时，其空字符串会覆盖挂载 .env 里的真实密钥。本测试把
    这个易踩的语义钉住：compose 必须靠“不导出空值”来保证密钥生效，而不是
    指望 env_file 覆盖进程环境。
    """
    result = _run_child(
        tmp_path,
        "AUTH_MODE=deployment\nAPI_KEY=correct-key\n",
        extra_env={"API_KEY": ""},
    )

    assert result["child_environ_has_key"] is True, "反例前提：进程环境里存在空的 API_KEY"
    # env_file 里的密钥被空的进程环境值覆盖，进程实际没有可用密钥
    assert result["configured_key_present"] is False
    assert result["no_key"] == 401
    assert result["right_key"] == 401, "空进程值使 env_file 中的密钥失效"


def _load_compose() -> dict:
    yaml = pytest.importorskip("yaml")
    compose_path = PROJECT_ROOT / "docker-compose.yml"
    return yaml.safe_load(compose_path.read_text(encoding="utf-8"))


def test_compose_never_exports_empty_api_key():
    """compose 只引用 API_KEY 会导出空值；必须只透传变量名，不写空默认值。"""
    app_service = _load_compose()["services"]["app"]

    env_entries = app_service.get("environment") or []
    if isinstance(env_entries, dict):
        entries = [f"{k}={v}" for k, v in env_entries.items()]
    else:
        entries = list(env_entries)

    api_entries = [entry for entry in entries if entry.split("=", 1)[0] == "API_KEY"]
    # 允许“透传式”（只有变量名）：未设置时不注入空值，设置时原样传入
    assert api_entries in ([], ["API_KEY"]), (
        f"compose environment 不能给 API_KEY 写值（包括空默认值）；实际: {api_entries}"
    )

    env_file = app_service.get("env_file")
    assert env_file, "必须通过 env_file 注入宿主机 .env"

    # 鉴权模式显式传播，且默认 fail closed；容器用独立变量，避免 compose 的
    # .env 自动插值把开发机的 AUTH_MODE=dev 带进容器。
    auth_entry = next(e for e in entries if e.startswith("AUTH_MODE="))
    assert auth_entry == "AUTH_MODE=${CONTAINER_AUTH_MODE:-deployment}"

    # 容器内必须显式监听 0.0.0.0（宿主机默认仍是 127.0.0.1，不在此处暴露）
    host_entry = next(e for e in entries if e.startswith("HOST="))
    assert host_entry == "HOST=0.0.0.0"


def test_compose_does_not_export_other_empty_secrets():
    """同一类缺陷：空 EMBEDDING_API_KEY / 云凭证默认值也不应写进 environment。"""
    app_service = _load_compose()["services"]["app"]
    env_entries = app_service.get("environment") or []
    entries = (
        env_entries
        if isinstance(env_entries, list)
        else [f"{k}={v}" for k, v in env_entries.items()]
    )

    for entry in entries:
        key, sep, value = entry.partition("=")
        if key == "EMBEDDING_API_KEY":
            pytest.fail("不能导出空 EMBEDDING_API_KEY，否则会覆盖 env_file 中的真实值")
        if key in {"TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"}:
            # 透传式（只有变量名、没有 "="）最安全：未设置时不会注入空值
            assert not sep or value.strip(), f"{key} 不应以空默认值导出"
