"""API Key 鉴权。

密钥来源只有一个：``Settings``（会读取进程环境与 env_file）。不再直接调用
``os.getenv``，否则只在 ``.env`` 里配置的密钥永远进不了鉴权。

两种模式：

- ``AUTH_MODE=dev``（默认）：未设置密钥时不鉴权，方便本机演示；一旦设置了
  密钥就强制校验。
- ``AUTH_MODE=deployment``：未设置密钥时**所有**受保护请求都 401（fail
  closed），避免“以为开了鉴权其实没有”。

比较使用 ``hmac.compare_digest``；密钥不会出现在日志、异常或响应里。
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Header, HTTPException
from loguru import logger

from app.config import config

_PLACEHOLDER_PREFIXES = ("your-", "sk-your-", "replace-with", "change-me", "changeme", "todo")


def configured_key() -> str:
    """返回生效的密钥；空白或占位符视为未配置。"""
    key = (config.api_key or "").strip()
    if not key:
        return ""
    lowered = key.lower()
    if lowered.startswith(_PLACEHOLDER_PREFIXES):
        return ""
    return key


def auth_required() -> bool:
    """当前配置是否必须校验密钥。"""
    return bool(configured_key()) or config.auth_mode == "deployment"


def auth_status() -> dict[str, object]:
    """当前生效的访问控制状态，供 /health 等只读接口如实报告。

    只包含模式名与布尔值；**不包含密钥本身**，也不包含长度/前后缀等可推断信息。
    """
    return {
        "mode": config.auth_mode,
        "api_key_configured": bool(configured_key()),
        "authentication_required": auth_required(),
    }


def key_fingerprint(api_key: str) -> str:
    """密钥的不可逆短指纹，用作 principal_id 后缀。

    只用 SHA-256 前 12 位十六进制：足以区分密钥，又不能从中还原密钥本身。
    空密钥返回空串（调用方据此判断“没有身份”）。
    """
    candidate = (api_key or "").strip()
    if not candidate:
        return ""
    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]


def verify_key(candidate: str | None) -> bool:
    """候选密钥是否等于当前生效密钥（常数时间比较）。

    未配置密钥时返回 ``False``——"没有密钥"不等于"任何密钥都对"，
    fail-closed 的判定留给调用方（见 ``app.core.principal``）。
    """
    key = configured_key()
    if not key:
        return False
    return hmac.compare_digest((candidate or "").encode("utf-8"), key.encode("utf-8"))


def _reject(message: str) -> HTTPException:
    return HTTPException(status_code=401, detail=message)


async def require_api_key(x_api_key: str = Header(default="", alias="X-API-Key")) -> None:
    key = configured_key()
    if not key:
        if config.auth_mode == "deployment":
            logger.warning("AUTH_MODE=deployment 但未配置 API_KEY：拒绝访问")
            raise _reject("api key not configured; access denied")
        return
    if not verify_key(x_api_key):
        raise _reject("invalid or missing X-API-Key")


def startup_check() -> None:
    """启动时校验部署配置，缺失密钥直接报错而不是静默放行。"""
    if config.auth_mode == "deployment" and not configured_key():
        raise RuntimeError(
            "AUTH_MODE=deployment 需要设置 API_KEY；拒绝在没有访问控制的部署模式启动"
        )
