"""服务端 principal 与资源归属校验（M-ACL / G3-3）。

今天的问题是：run / trace / 会话的“归属”实际上只是共享的 ``session_id``——
任何人知道（或猜到）一个 session_id 就能读别人的运行历史、回放整份 trace、
删除知识文档。这里补一条最小但真实的授权边界。

设计定位（刻意保持最小）：

- **单租户 + 归属检查，不是 RBAC**：没有用户表、没有角色继承、没有策略引擎。
  一个 principal 就是“这把 API Key（或 dev 模式下的单一主体）”，能力集合由
  :data:`ACTION_SCOPES` 静态给出。
- **归属是服务端记录的事实**：资源创建时把 ``principal_id`` 写进 trace 的
  ``run_start``（run）或 ACL 侧表（会话、知识文档）；读取/删除/回放时只在
  “记录过 owner 且 owner 不符”时拒绝。**没有记录 owner 的旧资源保持兼容**——
  这是有意的取舍，边界只在能证明归属之后才生效，绝不臆造归属。
- **dev 模式不是“没有检查”**：所有请求都映射到 :data:`DEV_PRINCIPAL_ID`，
  但 :func:`authorize` 依然逐条比较 owner，拒绝路径可以在测试里直接断言。
- **伪造的 principal 文本一律不采信**：:func:`current_principal` 的
  ``request_text`` 只用于日志关联，**绝不参与身份判定**；身份只能来自经过
  常数时间比较的 API Key。启用鉴权时，缺失/错误的 key 抛
  ``PermissionDenied(status_code=401)``，不是 500，也不是“放行”。
- 依赖只有标准库 + 现有 ``app.core.auth`` / ``app.config``；不引入用户库。

资源种类（``kind``）目前只有三种：``run`` / ``session`` / ``knowledge_document``。
新增种类必须在 :data:`RESOURCE_KINDS` 里显式登记，避免拼错种类名导致“查不到
owner 于是放行”的静默漏洞。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.config import config
from app.core import auth

#: dev 模式下唯一的 principal（所有人映射到这里，但检查依然执行）。
DEV_PRINCIPAL_ID = "principal:dev"
#: 由 API Key 派生出的 principal 前缀。
API_KEY_PRINCIPAL_PREFIX = "principal:key-"

#: 动作 -> 需要的 scope。scope 名与动作同名，保持可读、可审计。
ACTION_SCOPES: dict[str, str] = {
    "run:read": "run:read",
    "run:replay": "run:replay",
    "run:delete": "run:delete",
    "session:read": "session:read",
    "session:write": "session:write",
    "session:delete": "session:delete",
    "knowledge:read": "knowledge:read",
    "knowledge:write": "knowledge:write",
    "knowledge:delete": "knowledge:delete",
    "knowledge:activate": "knowledge:activate",
}

#: 当前实现里所有 principal 拥有同一组 scope（单租户）；scope 检查先落地，
#: 将来细分时只改这一处，不用改调用方。
_ALL_SCOPES = frozenset(ACTION_SCOPES.values())

#: 允许记录归属的资源种类白名单。
RESOURCE_KINDS = frozenset({"run", "session", "knowledge_document"})

#: ACL 侧表路径覆盖（测试/部署）。
ACL_FILE_ENV = "PRINCIPAL_ACL_FILE"

_MAX_RESOURCE_ID_CHARS = 128
_NOT_AN_OWNER = frozenset({"", "anonymous", "none", "null", "unknown"})


class PermissionDenied(PermissionError):
    """调用方不是资源所有者，或没有通过认证。

    ``status_code`` 让 HTTP 层不必猜：认证失败是 401，归属/scope 不符是 403。
    ``str(exc)`` 只包含动作与资源种类，**不包含** owner 的具体 principal_id，
    避免把“谁拥有它”作为探测信号返回给客户端。
    """

    def __init__(self, message: str, *, action: str = "", principal_id: str = "",
                 owner: str = "", status_code: int = 403) -> None:
        super().__init__(message)
        self.action = action
        self.principal_id = principal_id
        self.owner = owner
        self.status_code = int(status_code)

    def as_detail(self) -> str:
        """给 HTTP 响应体用的说明（不含 owner 身份）。"""
        return str(self)


@dataclass(frozen=True)
class Principal:
    """一个已认证的调用方。

    ``scopes`` 是不可变集合：任何“临时加权限”的写法都必须构造新的 Principal，
    不能就地修改，避免授权状态被跨请求串改。
    """

    principal_id: str
    scopes: frozenset[str]
    #: ``dev`` 或 ``api_key``；只用于日志与审计，不参与判定。
    source: str = "dev"

    def can(self, action: str) -> bool:
        needed = ACTION_SCOPES.get(action)
        return needed is None or needed in self.scopes


def principal_id_for_key(api_key: str) -> str:
    """由 API Key 派生 principal_id（不可逆指纹，不泄漏密钥本身）。"""
    return f"{API_KEY_PRINCIPAL_PREFIX}{auth.key_fingerprint(api_key)}"


def current_principal(request_text: str | None = None,
                      api_key: str | None = None) -> Principal:
    """解析当前请求的 principal。

    - 未配置密钥且不是 deployment：返回 ``principal:dev``（dev 模式）；
    - 配置了密钥：只认与生效密钥**常数时间相等**的候选，否则 401；
    - deployment 模式没配密钥：fail closed（401），绝不退化成 dev。

    ``request_text`` 是故意保留但不采信的参数：它可以包含 X-Principal /
    X-User 之类的客户端自述，只能进日志，**不能**成为身份。
    """
    if request_text:
        logger.debug(f"请求自述身份（不采信）: {str(request_text)[:80]}")

    if not auth.configured_key():
        if config.auth_mode == "deployment":
            logger.warning("AUTH_MODE=deployment 但未配置 API_KEY：所有权检查 fail closed")
            raise PermissionDenied(
                "api key not configured; access denied", status_code=401
            )
        return Principal(DEV_PRINCIPAL_ID, _ALL_SCOPES, "dev")

    if not auth.verify_key(api_key):
        raise PermissionDenied("invalid or missing X-API-Key", status_code=401)
    return Principal(principal_id_for_key(auth.configured_key()), _ALL_SCOPES, "api_key")


def authorize(principal: Principal | None, resource_owner: str | None, *,
              action: str) -> None:
    """校验 ``principal`` 是否有权对某资源执行 ``action``。

    - ``principal is None``：未认证，抛 401；
    - scope 不足：抛 403；
    - 资源**没有记录 owner**（旧数据）：放行，并在 debug 日志里标记——不臆造归属；
    - 记录了 owner 但不等于调用方：抛 403。

    这个函数是纯检查，不读写磁盘，方便在测试里逐条断言拒绝路径。
    """
    if principal is None:
        raise PermissionDenied(
            f"缺少 principal，无权执行 {action}", action=action, status_code=401
        )
    if not principal.can(action):
        raise PermissionDenied(
            f"principal 没有执行 {action} 的权限", action=action,
            principal_id=principal.principal_id,
        )
    owner = str(resource_owner or "").strip()
    if owner.lower() in _NOT_AN_OWNER:
        logger.debug(f"资源未记录 owner，按兼容策略放行: action={action}")
        return
    if owner != principal.principal_id:
        logger.warning(
            f"资源归属校验失败: action={action}, principal={principal.principal_id}"
        )
        raise PermissionDenied(
            f"无权对不属于本 principal 的资源执行 {action}",
            action=action, principal_id=principal.principal_id, owner=owner,
        )


# ── 归属侧表（会话 / 知识文档） ──────────────────────────────

_LOCK = threading.Lock()


def _acl_path() -> Path:
    """ACL 侧表路径；``PRINCIPAL_ACL_FILE`` 可覆盖（测试/部署）。"""
    override = os.environ.get(ACL_FILE_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / "principal_acl.json"


def _record_key(kind: str, resource_id: str) -> str:
    if kind not in RESOURCE_KINDS:
        raise ValueError(f"未知的归属资源种类: {kind}")
    rid = str(resource_id or "").strip()
    if not rid:
        raise ValueError("resource_id 不能为空")
    if len(rid) > _MAX_RESOURCE_ID_CHARS:
        raise ValueError("resource_id 过长")
    return f"{kind}:{rid}"


def _read_acl() -> dict[str, str]:
    path = _acl_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    owners = payload.get("owners")
    if not isinstance(owners, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in owners.items()
        if str(key) and str(value)
    }


def record_owner(kind: str, resource_id: str, principal_id: str, *,
                 overwrite: bool = True) -> bool:
    """记录资源归属；空 ``principal_id`` 表示删除该记录。

    ``overwrite=False`` 表示**只占用空位**：已经有别的 owner 时返回 ``False``
    且不修改磁盘。这是“创建时声明归属”该用的模式——否则第二个上传同名文档的
    调用方就能直接把别人的文档认领走。

    失败只返回 ``False`` 并打 ERROR 日志，不抛异常——但调用方（上传/建会话）
    必须把 ``False`` 当成“这次归属没记下来”处理，不能静默当成成功。
    """
    try:
        key = _record_key(kind, resource_id)
    except ValueError as exc:
        logger.error(f"记录资源归属失败（参数非法）: {exc}")
        return False
    owner = str(principal_id or "").strip()
    path = _acl_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            owners = _read_acl()
            existing = owners.get(key)
            if owner and not overwrite and existing and existing != owner:
                logger.warning(
                    f"资源已归属其它 principal，拒绝覆盖: kind={kind}"
                )
                return False
            if owner:
                owners[key] = owner
            else:
                owners.pop(key, None)
            temp = path.with_name(f"{path.name}.tmp")
            temp.write_text(
                json.dumps({"version": 1, "owners": owners}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(temp, path)
        return True
    except OSError as exc:
        logger.error(f"写入资源归属失败: {path}: {exc}")
        return False


def claim_owner(kind: str, resource_id: str, principal_id: str) -> bool:
    """只占空位的归属声明（等价于 ``record_owner(..., overwrite=False)``）。"""
    return record_owner(kind, resource_id, principal_id, overwrite=False)


def resource_owner(kind: str, resource_id: str) -> str | None:
    """读取资源归属；没有记录或种类非法时返回 ``None``（= 无 owner 信息）。"""
    try:
        key = _record_key(kind, resource_id)
    except ValueError:
        return None
    with _LOCK:
        owners = _read_acl()
    return owners.get(key) or None


# ── run 归属：以 trace 的 run_start 为唯一事实 ─────────────────


def run_owner(run_id: str) -> str | None:
    """读取 run 的 owner（来自 trace ``run_start``，其次 ACL 侧表）。"""
    from app.core.run_context import run_owner as _trace_run_owner

    owner = _trace_run_owner(run_id)
    if owner:
        return owner
    return resource_owner("run", run_id)


def enforce_run_access(run_id: str, api_key: str | None = None, *,
                       action: str = "run:read") -> Principal:
    """校验当前调用方是否有权读取/回放/删除某个 run。

    第三个参数是 ``api_key``（通常是请求头 ``X-API-Key`` 的原值）；缺失在
    dev 模式下等价于 dev principal，在启用鉴权时抛 401。
    """
    principal = current_principal(None, api_key)
    authorize(principal, run_owner(run_id), action=action)
    return principal


def enforce_run_access_http(run_id: str, api_key: str | None = None, *,
                            action: str = "run:read") -> None:
    """:func:`enforce_run_access` 的 HTTP 版本：把拒绝映射成 401/403。

    单独放在这里，是为了让 `app/api/runs.py` 只需要一行调用，不必复制
    ``try/except PermissionDenied -> HTTPException`` 的样板代码。
    """
    from fastapi import HTTPException

    try:
        enforce_run_access(run_id, api_key, action=action)
    except PermissionDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


def run_visible(run_id: str, principal: Principal | None) -> bool:
    """只读谓词：``principal`` 能否看到这个 run（列表过滤用）。

    与 :func:`authorize` 判定一致，但返回布尔而不是抛异常：列表接口要把
    不可见条目**直接省略**，而不是整页 403。未认证（``None``）一律不可见。
    """
    try:
        authorize(principal, run_owner(run_id), action="run:read")
    except PermissionDenied:
        return False
    return True


def owner_kwargs(principal: Principal) -> dict[str, str]:
    """便捷：把 owner 写进 trace/记录时统一的字段形状。

    运行方在 ``trace_event(run_id, "run_start", ..., **owner_kwargs(principal))``
    里展开即可，不必在调用点拼字段名。
    """
    return {"owner": principal.principal_id}
