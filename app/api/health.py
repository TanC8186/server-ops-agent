"""健康检查接口

``/health`` 是一个**能力摘要**，不是“所有依赖都正常”的断言：

- 顶层 ``status`` 只表达进程本身能否工作。Milvus / RAG / 模型端点不可用属于
  能力降级，写在 ``data.capabilities`` 里，不会把进程判成不健康（诊断主链路
  不依赖向量库，容器编排也不该因为知识库暂不可用就反复重启进程）。
- ``data.auth`` 如实报告当前生效的访问控制状态，便于部署侧核对“这台实例
  到底要不要鉴权”。只报告模式与布尔值，绝不包含密钥本身、掩码或长度。
- ``data.capabilities`` 的每一项都只有 ``status`` + ``reason``（``tracing``
  额外带计数；``trace_retention`` 额外带 ``last_run_at / deleted / failed /
  scanned / dry_run / enabled``），状态取值固定为 ``unconfigured | ready |
  degraded | unavailable``；``rag`` 额外允许 ``invalid_index``（索引与当前
  embedding 配置不一致，需要显式重建，不能并入“依赖不可用”）。
- 所有能力判断只读取配置与进程内计数，**不主动探测**远端模型服务，也不向
  响应写入任何密钥、令牌或凭证片段。

响应外层保持 ``{code, message, data}`` 不变，既有前端与探针继续可解析。
"""

from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger

from app import observability
from app.config import config
from app.core.auth import auth_status
from app.core.milvus_client import milvus_manager

router = APIRouter()

#: 能力状态取值；``rag`` 额外允许 ``invalid_index``。
_CAPABILITY_STATUSES = frozenset({"unconfigured", "ready", "degraded", "unavailable"})
_RAG_STATUSES = _CAPABILITY_STATUSES | {"invalid_index"}


def _is_http_url(value: str) -> bool:
    """判断配置值是否是带主机名的 http(s) 地址（不发起网络请求）。"""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _capability(status: str, reason: str | None = None) -> dict[str, Any]:
    """构造一个能力项；``ready`` 不需要原因，其余状态必须有原因。"""
    return {"status": status, "reason": reason}


def _diagnosis_local_capability() -> dict[str, Any]:
    """本地诊断能力：只看 ``LOCAL_LLM_BASE_URL``，沿用 aiops 的 configured 语义。

    这里不探测端点。配置了地址但地址不合法、或模型名为空时，标记为
    ``degraded``：进程仍可工作，但本地模式实际不可用。
    """
    base_url = config.local_llm_base_url.strip()
    if not base_url:
        return _capability("unconfigured", "未配置 LOCAL_LLM_BASE_URL")
    if not _is_http_url(base_url):
        return _capability("degraded", "LOCAL_LLM_BASE_URL 不是有效的 http(s) 地址")
    if not config.local_llm_model.strip():
        return _capability("degraded", "LOCAL_LLM_MODEL 为空")
    return _capability("ready")


def _diagnosis_cloud_capability() -> dict[str, Any]:
    """在线诊断能力：判定规则与 ``/api/execution-modes`` 保持一致。

    ``your-`` 前缀是仓库模板里的占位符，必须视为未配置；原因文案只说明
    “未配置/占位符”，不读取、不回显密钥内容。
    """
    api_key = config.llm_api_key.strip()
    if not api_key:
        return _capability("unconfigured", "未配置 LLM_API_KEY")
    if api_key.startswith("your-"):
        return _capability("unconfigured", "LLM_API_KEY 仍是 your- 占位符")
    if not _is_http_url(config.llm_base_url.strip()):
        return _capability("degraded", "LLM_BASE_URL 不是有效的 http(s) 地址")
    if not config.llm_model.strip():
        return _capability("degraded", "LLM_MODEL 为空")
    return _capability("ready")


def _vector_store_state() -> tuple[bool, BaseException | None]:
    """读取向量存储的**已记录**状态，不触发连接或初始化。

    懒加载设计下，进程启动后第一次检索才会真正连接 Milvus 并校验描述符。
    健康检查不能为了探测而触发一次真实检索（会拖慢探针、也可能产生副作用），
    因此这里只读取管理器上已有的结果：

    - ``_initialized``：是否已完成连接 + 描述符校验；
    - ``_init_error``：上一次初始化失败保留的异常（没有失败则为 None）。

    尚未初始化不算失败：Milvus 已连接时能力仍视为 ``ready``，等首次检索时
    再按真实结果收敛。
    """
    try:
        from app.services.vector_store_manager import vector_store_manager
    except Exception as exc:  # noqa: BLE001 - 可选依赖导入失败只能降级
        return False, exc
    return (
        bool(getattr(vector_store_manager, "_initialized", False)),
        getattr(vector_store_manager, "_init_error", None),
    )


def _rag_capability(milvus_status: str, milvus_message: str) -> dict[str, Any]:
    """RAG 能力：Milvus 故障只降级这一项，不影响顶层 status。"""
    if milvus_status != "connected":
        return _capability("unavailable", milvus_message)

    initialized, init_error = _vector_store_state()
    if init_error is None:
        return _capability("ready")

    error_name = type(init_error).__name__
    reason = str(init_error)[:200] or error_name
    if error_name == "VectorStoreInvalidIndex":
        # 描述符/维度/度量不一致：独立状态，必须显式重建索引。
        return _capability("invalid_index", reason)
    if error_name in {"VectorStoreUnavailable", "DescriptorMismatch"}:
        return _capability("unavailable", reason)
    # 未预期的异常（例如可选依赖导入失败）只要求人工查看，不假装可用。
    return _capability("degraded", f"向量存储状态异常（{error_name}）：{reason}")


def _mcp_capability() -> dict[str, Any]:
    """MCP 工具能力：只报告**进程内已缓存**的真实结果，不主动连接。

    「配置里写了三个 MCP 服务」不等于「工具已经连上」。这里区分三件事：
    - ``unconfigured``：没有任何 MCP 服务配置；
    - ``degraded``：已配置但本进程还没成功加载过工具（首次诊断才会连接）；
    - ``ready``：本进程确实拿到过工具列表（``mcp_client._tools_cache`` 非空）。

    ``ready`` 只代表“加载过一次工具列表”，不代表那一刻之后服务仍健康——这一点
    写进 ``reason``，避免把缓存当成实时探活。不触发任何网络/子进程调用。
    """
    servers = getattr(config, "mcp_servers", None) or {}
    if not servers:
        return _capability("unconfigured", "未配置任何 MCP 服务")
    try:
        from app.agent import mcp_client
    except Exception as exc:  # noqa: BLE001 - 可选依赖导入失败只能降级
        return _capability("degraded", f"MCP 客户端不可用（{type(exc).__name__}）")
    tools = getattr(mcp_client, "_tools_cache", None)
    count = len(tools) if isinstance(tools, list) else 0
    if count:
        return _capability(
            "ready",
            f"本进程已加载 {count} 个 MCP 工具（缓存结果；不代表之后仍健康）",
        )
    return _capability(
        "degraded",
        f"已配置 {len(servers)} 个 MCP 服务，但本进程尚未成功加载工具（首次诊断时才连接）",
    )


def _trace_retention_capability() -> dict[str, Any]:
    """trace 保留策略能力：上一次清理的事实 + 当前开关，不触发任何清理。

    状态语义（沿用 ``unconfigured|ready|degraded|unavailable``）：

    - ``unconfigured``：``TRACE_RETENTION_DAYS=0``，自动清理关闭（这是配置选择，
      不是故障，因此不判 degraded）；
    - ``ready``：已开启且最近一次清理没有失败（含"调度器已起、首轮还没到"）；
    - ``degraded``：已开启但有失败文件，**或**已开启却既没跑过清理、调度器也
      没在运行（后台任务没起来——这正是需要被看见的状态）；
    - ``unavailable``：读取清理状态本身抛异常。

    dry-run 只影响 ``reason`` 文案，不改变状态：dry-run 下"没删掉东西"是预期
    行为，不能读成失败。绝不暴露文件路径或 trace 内容。
    """
    try:
        from app.api.runs import purge_stats

        stats = purge_stats()
    except Exception as exc:  # noqa: BLE001 - 保留策略模块故障不能拖垮探针
        logger.warning(f"trace retention 状态读取失败: {type(exc).__name__}")
        return {
            "status": "unavailable",
            "reason": f"保留策略状态读取失败（{type(exc).__name__}）",
            "last_run_at": None,
            "deleted": 0,
            "failed": 0,
            "scanned": 0,
            "dry_run": False,
            "enabled": False,
        }

    enabled = bool(stats.get("enabled"))
    dry_run = bool(stats.get("dry_run"))
    last_run_at = stats.get("last_run_at")
    deleted = int(stats.get("deleted", 0))
    failed = int(stats.get("failed", 0))
    scanned = int(stats.get("scanned", 0))

    scheduler_running = bool(stats.get("scheduler_running"))

    if not enabled:
        status = "unconfigured"
        reason = "TRACE_RETENTION_DAYS=0，自动清理已关闭"
    elif failed:
        status = "degraded"
        reason = f"最近一次清理有 {failed} 个文件删除失败"
    elif not last_run_at and not scheduler_running:
        # 开着保留策略却既没跑过、后台任务也不在：这才是需要人工介入的降级。
        # （在后台任务正常运行时，启动后的首轮宽限期内 last_run_at 仍为 None，
        #   那属于正常状态，不能报 degraded。）
        status = "degraded"
        reason = "保留策略已开启，但本进程尚未执行过清理且调度器未运行"
    else:
        status = "ready"
        if dry_run:
            reason = "dry-run：只统计不删除"
        elif not last_run_at:
            reason = "调度器已启动，等待首轮清理"
        else:
            reason = None

    return {
        "status": status,
        "reason": reason,
        "last_run_at": last_run_at,
        "deleted": deleted,
        "failed": failed,
        "scanned": scanned,
        "dry_run": dry_run,
        "enabled": enabled,
    }


def _tracing_capability() -> dict[str, Any]:
    """trace 写入健康：只暴露计数，不暴露 last_error / 事件载荷。

    ``failures > 0`` 表示最近至少有一次写盘失败；trace 失败不会影响诊断
    主流程，因此只把这一项标记为 ``degraded``。
    """
    try:
        health = observability.get_trace_health()
        writes = int(health.get("writes", 0))
        failures = int(health.get("failures", 0))
    except Exception as exc:  # noqa: BLE001 - 观测模块自身故障不能拖垮探针
        logger.warning(f"trace 健康快照读取失败: {type(exc).__name__}")
        return {"status": "unavailable", "writes": 0, "failures": 0}

    return {
        "status": "degraded" if failures else "ready",
        "writes": writes,
        "failures": failures,
    }


@router.get("/health")
async def health_check():
    """健康检查接口

    返回进程状态、访问控制状态与各能力（本地/在线诊断、RAG、trace 写入）的
    可用性。能力探测失败只影响对应字段，响应始终是 HTTP 200：

    - ``diagnosis_local`` / ``diagnosis_cloud``：配置是否完整，不探测端点；
    - ``mcp``：本进程是否真的加载过 MCP 工具（缓存事实，不是实时探活）；
    - ``rag``：Milvus 连通性与已记录的索引校验结果；
    - ``tracing``：进程内写入/失败计数；
    - ``trace_retention``：retention 开关与最近一次清理的事实（时间/删除/失败/
      扫描/dry-run），只读快照，不触发清理。

    ``AUTH_MODE=deployment`` 且未配置密钥时，进程会在 lifespan 的
    ``startup_check()`` 阶段就拒绝启动，因此线上不会出现“deployment 但未
    鉴权”的实例；万一真的被访问到，``authentication_required`` 仍为 true。
    ``dev`` 模式未配置密钥是仓库默认的本地开发状态，只报告，不让接口失败。

    Returns:
        JSONResponse: 健康检查结果
    """
    # 检查服务基本状态
    health_data: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
        "service": config.app_name,
        "version": config.app_version,
        "status": "healthy",
        "auth": auth_status(),
    }

    # 检查 Milvus 连接状态（能力探测，失败只降级 RAG，不影响进程健康）
    try:
        milvus_healthy = milvus_manager.health_check()
        milvus_status: str = "connected" if milvus_healthy else "disconnected"
        milvus_message: str = "Milvus 连接正常" if milvus_healthy else "Milvus 连接异常"
    except Exception as e:
        # 只记录/返回异常类型，避免把连接串等细节扩散到公开探针。
        logger.warning(f"Milvus 健康检查失败: {type(e).__name__}")
        milvus_status = "error"
        milvus_message = f"Milvus 检查失败（{type(e).__name__}）"

    health_data["milvus"] = {
        "status": milvus_status,
        "message": milvus_message,
    }

    capabilities = {
        "diagnosis_local": _diagnosis_local_capability(),
        "diagnosis_cloud": _diagnosis_cloud_capability(),
        "mcp": _mcp_capability(),
        "rag": _rag_capability(milvus_status, milvus_message),
        "tracing": _tracing_capability(),
        "trace_retention": _trace_retention_capability(),
    }
    for name, item in capabilities.items():
        allowed = _RAG_STATUSES if name == "rag" else _CAPABILITY_STATUSES
        if item["status"] not in allowed:
            # 兜底：实现有 bug 时也不能把非法状态发给探针。
            logger.error(f"能力 {name} 返回了非法状态: {item['status']}")
            item["status"] = "degraded"
            item["reason"] = item.get("reason") or "能力状态计算异常"
    health_data["capabilities"] = capabilities

    return JSONResponse(
        status_code=200,
        content={
            "code": 200,
            "message": "服务运行正常",
            "data": health_data,
        },
    )
