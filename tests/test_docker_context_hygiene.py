"""G8 P0：构建上下文的凭据排除必须覆盖**嵌套路径**。

原始缺陷（实测确认，非推断）：
``.dockerignore`` 里裸写 ``*.pem`` / ``*.key`` / ``.env*`` / ``__pycache__/``。
Docker 的 .dockerignore 用 ``filepath.Match`` 规则，``*`` **不跨 ``/``**，
于是这些模式只匹配构建上下文根目录：

- ``servers/AgentTest.pem``（一份**未加密的真实 RSA 私钥**）被 ``COPY . .``
  拷进镜像，落在 ``/app/servers/AgentTest.pem``（755 / 1674 B，
  sha256 与宿主一致）。实测镜像 ``on_call_agent-app:latest`` 里确实存在。
- 293 个项目 ``.pyc`` 与嵌套 ``__pycache__/`` 一并进入镜像。

这些测试**不做 docker build**（慢且需要网络）。它们直接按 Docker 的匹配语义
检查 ``.dockerignore`` 的每一条模式：任何"敏感模式"必须写成能匹配嵌套路径的
形式。这样规则退化成裸 glob 时会在秒级 CI 里失败，而不是等到下一次出镜像。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCKERIGNORE = REPO / ".dockerignore"

#: 构建上下文里绝对不能出现的路径（相对仓库根）。
MUST_EXCLUDE = [
    "servers/AgentTest.pem",          # 真实 RSA 私钥
    "agent.key",
    ".env",
    ".env.docker",
    ".env.production",
    "config/.env.local",
    "deep/nested/.env",
    "servers/id_rsa",
    "keys/deploy.ppk",
    "a/b/c.pem",
    "a/b/c.key",
    "app/__pycache__/x.pyc",
    "a/b/deep.pyc",
    "logs/cuda_cfg.log",
    "tmp/probe.py",
    ".g8_apikey",
    "data/principal_acl.json",
    "training/out/model.gguf",
]

#: 必须留在上下文里（镜像构建需要）。
MUST_KEEP = [
    ".env.example",
    "app/main.py",
    "servers/monitor_server.py",
    "README.md",
]


def _patterns() -> list[str]:
    lines: list[str] = []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        lines.append(text)
    return lines


def _excluded(path: str, patterns: list[str]) -> bool:
    """按 Docker 的语义判断路径是否被排除。

    Docker 从**后往前**匹配：最后一条命中的规则决定结果，``!`` 取反。
    匹配语义近似 ``filepath.Match``——``*`` 不跨 ``/``、``**`` 可以跨——
    但对"是否被排除"这个问题，用一段段比对足以覆盖我们关心的模式形态。
    """
    parts = path.split("/")
    decision = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        body = pattern[1:] if negate else pattern
        if _matches(path, parts, body):
            decision = not negate
    return decision


def _matches(path: str, parts: list[str], pattern: str) -> bool:
    """pattern → 该相对路径是否命中。"""
    # 目录模式（以 / 结尾）匹配该目录及其下所有内容。
    if pattern.endswith("/"):
        stem = pattern.rstrip("/")
        if _matches(path, parts, stem):
            return True
        return _matches_dir_prefix(parts, stem)
    # 带 **/ 前缀：可以在任意深度开始匹配。
    if pattern.startswith("**/"):
        rest = pattern[3:]
        if _matches(path, parts, rest):
            return True
        # 逐层剥掉前缀目录再试，模拟 **/ 的"任意深度"语义。
        for index in range(1, len(parts)):
            if _matches("/".join(parts[index:]), parts[index:], rest):
                return True
        return False
    if fnmatch.fnmatchcase(path, pattern):
        return True
    # 裸模式只匹配根层：单段模式不跨 "/"。
    if "/" not in pattern and len(parts) == 1:
        return fnmatch.fnmatchcase(parts[0], pattern)
    # 带斜杠但不以 / 结尾的模式（如 `training/out`、`training/data/*.jsonl`）
    # 同样可以命中该目录下的内容——Docker 里排除一个目录即排除其子树。
    if "/" in pattern:
        for index in range(len(parts) - 1, 0, -1):
            if fnmatch.fnmatchcase("/".join(parts[:index]), pattern):
                return True
    return False


def _matches_dir_prefix(parts: list[str], stem: str) -> bool:
    """``logs/`` / ``__pycache__/`` 这类目录模式：任一层目录名命中即排除其内容。"""
    if "/" in stem:
        return False
    return any(fnmatch.fnmatchcase(part, stem) for part in parts[:-1])


@pytest.mark.parametrize("path", MUST_EXCLUDE)
def test_sensitive_paths_are_excluded_from_build_context(path):
    """凭据、临时产物、运行期数据都不能进构建上下文。"""
    patterns = _patterns()
    assert _excluded(path, patterns), (
        f"{path} 会进入 docker build 上下文。"
        f"当前 .dockerignore 模式：{patterns}"
    )


@pytest.mark.parametrize("path", MUST_KEEP)
def test_required_files_stay_in_build_context(path):
    """排除规则不能宽到把构建必需的文件也剔掉。"""
    patterns = _patterns()
    assert not _excluded(path, patterns), f"{path} 被误排除，镜像会缺文件"


def test_no_bare_sensitive_glob_patterns():
    """敏感模式不允许退化回裸 glob。

    这条是给未来的人看的：``*.pem`` 看起来像"排除所有 pem"，实际只排除根层。
    想排除任意深度必须写 ``**/*.pem``。
    """
    bare = {
        line for line in _patterns()
        if not line.startswith("**/")
        and not line.startswith("!")
        and "/" not in line.rstrip("/")
    }
    sensitive_suffixes = (".pem", ".key", ".ppk", ".p12", ".pfx", ".log", ".pid")
    offenders = sorted(
        line for line in bare
        if line.lstrip("!").endswith(sensitive_suffixes)
        # 目录型通配（__pycache__/、tmp/）由 test_sensitive_paths 单独验证。
        or line.rstrip("/").endswith(("__pycache__", "htmlcov", "pytest_cache"))
    )
    assert not offenders, (
        "这些模式只匹配构建上下文根目录，嵌套路径会漏进镜像："
        f"{offenders}；请改成 **/ 前缀形式"
    )


def test_env_example_is_the_only_env_file_kept():
    """``.env*`` 之外必须放行模板；否则镜像里没有任何配置说明。"""
    patterns = _patterns()
    assert _excluded(".env", patterns)
    assert _excluded(".env.docker", patterns)
    assert not _excluded(".env.example", patterns), "必须保留 .env.example 作为模板"
