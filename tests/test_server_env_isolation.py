"""server 环境隔离回归测试。

历史缺陷：``servers/*.py`` 在 import 时调用 ``load_dotenv()``，把 ``.env``
里的真实密钥写进 ``os.environ``。于是同一个进程里的其他消费者（``Settings``、
测试）会莫名其妙看到“已配置”的密钥，``test_auth`` 在完整套件里因此失败。

这里锁定三条性质：

1. 导入三个 server 模块后，进程环境不会多出 env 文件里的任何键；
2. 进程环境优先于 env 文件；
3. 缺失键返回默认值，``env_source`` 只报告来源、不暴露值。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 子进程排序回归的防递归开关：子 pytest 里跳过“再起一个 pytest”的用例。
_ORDERING_CHILD_FLAG = "ONCALL_SERVER_ENV_ORDERING_CHILD"

_DUMMY_ENV = {
    "LLM_API_KEY": "dummy-llm-key-not-a-secret",
    "API_KEY": "dummy-api-key-not-a-secret",
    "TENCENTCLOUD_SECRET_ID": "dummy-secret-id",
    "TENCENTCLOUD_SECRET_KEY": "dummy-secret-key",
    "TENCENTCLOUD_REGION": "dummy-region-1",
    "DEMO_MODE": "1",
    "SSH_HOST": "dummy-ssh-host.invalid",
    "SSH_PORT": "2222",
    "SSH_USER": "dummy-user",
    "SSH_KEY_FILE": "/tmp/dummy-key.pem",
    "SSH_SERVER_TYPE": "linux",
}


def _write_env_file(path: Path, values: dict[str, str]) -> Path:
    """写一份只含合成值的 env 文件；绝不复制真实 .env。"""
    body = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    path.write_text(body, encoding="utf-8")
    return path


def _child_env(env_file: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：剔除所有会被 env 文件覆盖的键，避免真实值混入。"""
    env = {key: value for key, value in os.environ.items() if key not in _DUMMY_ENV}
    env.pop("AI_CONFIG_FILE", None)
    env["AI_CONFIG_FILE"] = str(env_file)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env.update(extra or {})
    return env


def _server_import_script() -> str:
    return textwrap.dedent(
        f"""
        import json
        import os
        import sys

        sys.path.insert(0, {str(PROJECT_ROOT)!r})

        import servers.cls_server as cls_server
        import servers.monitor_server as monitor_server
        import servers.ssh_server as ssh_server

        dummy_keys = {list(_DUMMY_ENV)!r}
        leaked = sorted(key for key in dummy_keys if key in os.environ)

        print(json.dumps({{
            "leaked_keys": leaked,
            "ssh_host": ssh_server.SSH_HOST,
            "ssh_port": ssh_server.SSH_PORT,
            "ssh_user": ssh_server.SSH_USER,
            "ssh_server_type": ssh_server.SERVER_TYPE,
            "cls_secret_id": cls_server.SECRET_ID,
            "cls_region": cls_server.DEFAULT_REGION,
            "cls_use_real_api": cls_server.USE_REAL_API,
            "cls_demo_mode": cls_server.DEMO_MODE,
            "monitor_region": monitor_server.DEFAULT_REGION,
            "monitor_demo_mode": monitor_server.DEMO_MODE,
            "monitor_use_real_api": monitor_server.USE_REAL_API,
        }}))
        """
    )


def test_importing_servers_does_not_mutate_process_env(tmp_path):
    """三个 server 模块导入后不得向 os.environ 写入 env 文件里的任何键。"""
    env_file = _write_env_file(tmp_path / "synthetic.env", _DUMMY_ENV)
    script = tmp_path / "import_servers.py"
    script.write_text(_server_import_script(), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=str(PROJECT_ROOT),
        env=_child_env(env_file),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result["leaked_keys"] == [], "server import 不得把 env 文件写进进程环境"

    # 每个模块都必须真的读到了文件里的合成值（证明不是“没读到所以没泄漏”）。
    assert result["ssh_host"] == _DUMMY_ENV["SSH_HOST"]
    assert result["ssh_port"] == int(_DUMMY_ENV["SSH_PORT"])
    assert result["ssh_user"] == _DUMMY_ENV["SSH_USER"]
    assert result["ssh_server_type"] == _DUMMY_ENV["SSH_SERVER_TYPE"]
    assert result["cls_secret_id"] == _DUMMY_ENV["TENCENTCLOUD_SECRET_ID"]
    assert result["cls_region"] == _DUMMY_ENV["TENCENTCLOUD_REGION"]
    assert result["cls_use_real_api"] is True
    assert result["cls_demo_mode"] is True
    assert result["monitor_region"] == _DUMMY_ENV["TENCENTCLOUD_REGION"]
    assert result["monitor_demo_mode"] is True
    assert result["monitor_use_real_api"] is True


def test_process_env_wins_over_env_file(tmp_path, monkeypatch):
    """部署时由容器/宿主注入的值必须优先于 env 文件。"""
    from servers import _config

    env_file = _write_env_file(tmp_path / "synthetic.env", {"SSH_HOST": "file-host.invalid"})
    monkeypatch.setenv("AI_CONFIG_FILE", str(env_file))
    monkeypatch.setenv("SSH_HOST", "process-host.invalid")

    assert _config.read_env("SSH_HOST") == "process-host.invalid"
    assert _config.env_source("SSH_HOST") == "process"


def test_missing_key_returns_default_and_reports_default(tmp_path, monkeypatch):
    """缺失键返回默认值，来源诊断不得泄露任何值。"""
    from servers import _config

    env_file = _write_env_file(tmp_path / "synthetic.env", {"SSH_HOST": "file-host.invalid"})
    monkeypatch.setenv("AI_CONFIG_FILE", str(env_file))
    monkeypatch.delenv("SSH_USER", raising=False)

    assert _config.read_env("SSH_USER", "fallback-user") == "fallback-user"
    assert _config.read_env_int("SSH_PORT", 22) == 22
    assert _config.read_env_bool("DEMO_MODE", False) is False
    assert _config.env_source("SSH_USER") == "default"
    assert _config.env_source("SSH_HOST") == f"file:{env_file}"

    # 布尔语义与旧实现一致：1/true/yes（忽略大小写）。
    monkeypatch.setenv("DEMO_MODE", "YES")
    assert _config.read_env_bool("DEMO_MODE") is True
    monkeypatch.setenv("DEMO_MODE", "0")
    assert _config.read_env_bool("DEMO_MODE") is False


def test_read_env_int_falls_back_on_invalid_value(monkeypatch):
    """非数字端口不能让 server 在 import 时崩掉。"""
    from servers import _config

    monkeypatch.setenv("SSH_PORT", "not-a-number")
    assert _config.read_env_int("SSH_PORT", 22) == 22


def test_env_file_rewrite_is_picked_up(tmp_path, monkeypatch):
    """env 文件被重写后缓存必须失效（mtime/size 检查）。"""
    from servers import _config

    env_file = _write_env_file(tmp_path / "synthetic.env", {"SSH_HOST": "host-one.invalid"})
    monkeypatch.setenv("AI_CONFIG_FILE", str(env_file))
    monkeypatch.delenv("SSH_HOST", raising=False)
    assert _config.read_env("SSH_HOST") == "host-one.invalid"

    _write_env_file(env_file, {"SSH_HOST": "host-two-longer.invalid"})
    assert _config.read_env("SSH_HOST") == "host-two-longer.invalid"


def _load_dotenv_calls(tree: ast.AST) -> list[int]:
    """返回 AST 中真实调用 load_dotenv 的行号。

    只认调用节点，注释和文档字符串里的 ``load_dotenv(`` 不算执行证据；
    同时跟踪 ``from dotenv import load_dotenv as ld`` 这类别名。
    """
    aliases = {"load_dotenv"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "dotenv":
            for imported in node.names:
                if imported.name == "load_dotenv":
                    aliases.add(imported.asname or imported.name)

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in aliases:
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "load_dotenv":
            lines.append(node.lineno)
    return lines


def _scan_for_load_dotenv() -> list[str]:
    """扫描 app/ 与 servers/ 下的 Python 源码，找出真实的 load_dotenv 调用点。

    使用 AST 而非字符串匹配：``_config.py`` 的模块说明里记录了历史实现，
    字符串扫描会把它误判成仍在执行；注释/文档字符串不产生调用节点。
    """
    hits: list[str] = []
    syntax_errors: list[str] = []
    for base in ("app", "servers"):
        for path in sorted((PROJECT_ROOT / base).rglob("*.py")):
            rel = str(path.relative_to(PROJECT_ROOT))
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(text, filename=rel)
            except SyntaxError as exc:  # 解析失败本身就是问题，不能静默跳过
                syntax_errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
                continue
            lines = _load_dotenv_calls(tree)
            if lines:
                hits.append(f"{rel}:{lines[0]}")
    assert syntax_errors == [], f"无法解析的模块: {syntax_errors}"
    return hits


def test_no_module_calls_load_dotenv_anymore():
    """load_dotenv 会把密钥写进进程环境，app/ 与 servers/ 中禁止真实调用。"""
    # 允许列表：只用于确有必要的独立脚本；当前为空，新增必须写清原因。
    allowlist: set[str] = set()
    hits = [hit for hit in _scan_for_load_dotenv() if hit not in allowlist]
    assert hits == [], f"以下模块仍在调用 load_dotenv: {hits}"


def test_ast_scanner_ignores_comments_and_docstrings(tmp_path):
    """注释/文档字符串里的 load_dotenv( 不得被当作执行证据，真实调用必须被抓到。"""
    fake = tmp_path / "fake_module.py"
    fake.write_text(
        '"""历史实现里调用 load_dotenv(env_path)。"""\n'
        "# load_dotenv(path) 只是注释\n"
        "value = 1\n",
        encoding="utf-8",
    )
    assert _load_dotenv_calls(ast.parse(fake.read_text(encoding="utf-8"))) == []

    fake.write_text(
        "from dotenv import load_dotenv as ld\n"
        "ld('.env')\n",
        encoding="utf-8",
    )
    assert _load_dotenv_calls(ast.parse(fake.read_text(encoding="utf-8"))) == [2]

    fake.write_text(
        "import dotenv\n"
        "dotenv.load_dotenv('.env')\n",
        encoding="utf-8",
    )
    assert _load_dotenv_calls(ast.parse(fake.read_text(encoding="utf-8"))) == [2]


@pytest.mark.skipif(
    os.environ.get(_ORDERING_CHILD_FLAG) == "1",
    reason="避免子 pytest 递归再起 pytest",
)
def test_full_suite_ordering_regression_passes_in_one_process():
    """完整套件里“auth 先于 upload/本文件”的顺序必须全绿（真实缺陷的复现顺序）。"""
    env = dict(os.environ)
    env[_ORDERING_CHILD_FLAG] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_auth.py",
            "tests/test_p0_upload_index.py",
            "tests/test_server_env_isolation.py",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout[-6000:] + completed.stderr[-4000:]
