"""MCP Server 配置读取：只读 env 文件，永不污染进程环境。

历史实现里 ``servers/*.py`` 在 import 时调用 ``load_dotenv()``，把 ``.env``
中的真实密钥直接写进 ``os.environ``。任何同进程消费者（测试、``Settings``、
其他 server）都会看到这些值，导致“测试进程凭空多出 LLM_API_KEY”这类隔离
缺陷，也把真实凭证扩散到不需要它的代码路径。

这里改为按需读取：

- 进程环境优先（部署时以容器/宿主注入的值为准）；
- 其次读取 env 文件（``AI_CONFIG_FILE`` 优先，否则项目根 ``.env``）；
- 只返回字符串，**不写** ``os.environ``，也不记录任何值。

env 文件用 ``dotenv_values`` 解析并按 mtime+size 缓存，文件被重写后会重新
解析，便于测试与热更新。日志只记录来源（进程/文件/默认），不记录内容。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from dotenv import dotenv_values
from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_FILE = _PROJECT_ROOT / ".env"

_TRUE_VALUES = ("1", "true", "yes")

# 解析缓存：str(path) -> ((mtime_ns, size), {键: 值})，避免每次读取都解析文件。
_ENV_CACHE: Dict[str, Tuple[Tuple[int, int], Dict[str, str]]] = {}

_UNSET = object()


def _resolve_env_path() -> Optional[Path]:
    """确定本次读取使用的 env 文件；不存在时返回 None。

    ``AI_CONFIG_FILE`` 是测试/部署 seam，必须来自进程环境，避免“配置文件
    自己指定配置文件”的递归；它指向的文件不存在时回退到项目根 ``.env``。
    """
    override = os.environ.get("AI_CONFIG_FILE", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate
    if _DEFAULT_ENV_FILE.is_file():
        return _DEFAULT_ENV_FILE
    return None


def _file_values(path: Path) -> Dict[str, str]:
    """读取并缓存 env 文件的键值；文件不可读时返回空映射。"""
    cache_key = str(path)
    try:
        stat = path.stat()
    except OSError:
        _ENV_CACHE.pop(cache_key, None)
        return {}

    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _ENV_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    try:
        raw = dotenv_values(str(path), encoding="utf-8")
    except (OSError, ValueError) as exc:
        # 只记录异常类型与路径，绝不回显文件内容（可能含密钥）。
        logger.warning(f"env 文件解析失败: {path} ({type(exc).__name__})")
        raw = {}

    # dotenv_values 对无值键返回 None，这里统一成空串。
    values = {key: (value if value is not None else "") for key, value in raw.items()}
    _ENV_CACHE[cache_key] = (signature, values)
    return values


def _raw_value(name: str):
    """返回配置原始值；进程环境优先，缺失时返回 ``_UNSET``。"""
    if name in os.environ:
        return os.environ[name]
    path = _resolve_env_path()
    if path is not None:
        values = _file_values(path)
        if name in values:
            return values[name]
    return _UNSET


def read_env(name: str, default: str = "") -> str:
    """读取字符串配置：进程环境 > env 文件 > 默认值。"""
    raw = _raw_value(name)
    if raw is _UNSET:
        return default
    return str(raw)


def read_env_bool(name: str, default: bool = False) -> bool:
    """读取布尔配置，语义与旧实现一致：1/true/yes（忽略大小写）为真。"""
    raw = _raw_value(name)
    if raw is _UNSET:
        return default
    return str(raw).lower() in _TRUE_VALUES


def read_env_int(name: str, default: int) -> int:
    """读取整数配置；缺失或非法值回退默认值（不抛异常）。"""
    raw = _raw_value(name)
    if raw is _UNSET:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(f"环境配置 {name} 不是合法整数，使用默认值")
        return default


def env_source(name: str) -> str:
    """诊断用：返回 ``process`` / ``file:<路径>`` / ``default``，不含值。"""
    if name in os.environ:
        return "process"
    path = _resolve_env_path()
    if path is not None and name in _file_values(path):
        return f"file:{path}"
    return "default"
