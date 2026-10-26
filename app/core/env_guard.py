"""阻断第三方库的隐式 ``load_dotenv()``，保护进程环境隔离。

背景（真实缺陷，不是假想）：``pymilvus/settings.py`` 在模块顶部调用无参
``load_dotenv()``，``find_dotenv()`` 会从 **当前工作目录** 向上找 ``.env``。
于是只要 ``app.core.milvus_client`` 被导入，项目根 ``.env`` 里的
``LLM_API_KEY`` / ``SSH_HOST`` / ``TENCENTCLOUD_SECRET_ID`` 等真实值就被写进
``os.environ``：

- ``Settings(_env_file=None)`` 仍会读 ``os.environ``，于是“未配置的密钥”变成
  非空，鉴权语义被悄悄改变；
- 服务器进程凭空多出 SSH/云凭证，任何 ``os.getenv`` 消费者都能看到；
- 测试进程之间互相污染，失败顺序依赖导入顺序。

本项目只通过两种只读方式取配置：``app.config.Settings``（pydantic env_file）
与 ``servers._config``（``dotenv_values``）。两者都不需要进程环境被写入，所以
第三方库的隐式写入必须关掉。

实现是幂等的：重复调用、多个入口（``app/__init__``、pytest conftest、独立脚本）
都安全。这是行为补丁而不是安全边界的唯一防线——``servers._config`` 仍单独
保证不写环境。
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_installed = False


def _noop_load_dotenv(*args, **kwargs) -> bool:
    """替代 ``dotenv.load_dotenv``：不读取、不写入进程环境。"""
    return False


def install() -> bool:
    """安装补丁；返回本次是否真的做了修改（已安装则返回 False）。"""
    global _installed
    with _LOCK:
        if _installed:
            return False
        try:
            import dotenv
            import dotenv.main as dotenv_main
        except ImportError:  # 没有安装 python-dotenv 时无事可做
            _installed = True
            return False

        # 两处都要打：`from dotenv import load_dotenv` 取的是包命名空间里的名字，
        # `dotenv.main.load_dotenv` 是直接引用实现的名字。
        dotenv.load_dotenv = _noop_load_dotenv  # type: ignore[assignment]
        dotenv_main.load_dotenv = _noop_load_dotenv  # type: ignore[assignment]
        _installed = True
        return True


def is_installed() -> bool:
    return _installed
