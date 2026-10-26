"""两种诊断模式共用的只读工具、参数校验和原始证据边界。

三个边界在这里统一：

1. **工具白名单**：新注册的 MCP 工具不会自动获得执行权限。
2. **目标绑定**：服务/实例/地域/日志主题由可信目录决定，工具参数不能覆盖
   （见 ``app.core.targets``）。多候选时抛 ``TargetRequiredError``，由 UI 选择。
3. **返回契约**：每个结果都有 ``status``（ok/empty/partial/error/timeout/
   unavailable/unsupported）、``source_kind`` 与 ``live``（见
   ``app.core.tool_contract``），失败不会被伪装成“零命中”。
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator

from app.agent.mcp_client import get_mcp_tools_cached
from app.config import config
from app.core.budgets import BoundedLogParams, truncate_for_prompt
from app.core.targets import TOPIC_KEYS, Target, TargetResolution, bind_tool_args, resolve_target
from app.core.tool_contract import classify_payload
from app.observability import trace_event
from app.tools.time_tool import get_current_time

#: 知识库检索工具名：由运行器内置，不来自 MCP。
KNOWLEDGE_TOOL = "retrieve_knowledge"

# 新注册的 MCP 工具不会自动得到执行权限。
READ_ONLY_TOOLS = frozenset({
    "list_all_services", "get_service_info", "query_cpu_metrics",
    "query_memory_metrics", "query_disk_metrics", "query_network_metrics",
    "query_process_list", "search_historical_tickets", "search_service_logs",
    "analyze_log_pattern", "search_topic_by_service_name", "get_topic_info_by_name",
    "get_region_code_by_name", "get_current_timestamp", "get_current_time",
    "get_system_info", "get_process_list", "search_system_logs",
    "check_disk_usage", "read_log_file", "search_log",
    KNOWLEDGE_TOOL,
})
SSH_TOOLS = frozenset({
    "get_system_info", "get_process_list", "search_system_logs",
    "check_disk_usage", "read_log_file",
})
#: 走子进程/MCP 通道的工具（与 in-process 工具共享同一并发预算）。
SUBPROCESS_TOOLS = READ_ONLY_TOOLS - {"get_current_time"}

#: 按日志主题范围读取的工具：必须校验主题归属。
TOPIC_SCOPED_TOOLS = frozenset({"get_topic_info_by_name", "search_log"})

#: 知识检索：不绑定目标，但只能读到知识库内容，永远不能当观测证据。
KNOWLEDGE_SCOPED_TOOLS = frozenset({KNOWLEDGE_TOOL})

#: 单次知识检索的 query 长度上限（防止把整段日志塞进检索）。
MAX_KNOWLEDGE_QUERY_CHARS = 400

#: 不需要目标绑定即可执行的元数据/发现类工具。
#:
#: ``get_topic_info_by_name`` 不在其中：它按名字取具体日志主题，属于“目标数据”，
#: 必须先绑定目标、并由 ``bind_tool_args`` 校验主题属于该服务。旧实现把它当发现
#: 工具放行，等于让模型自由枚举任意主题（BE-04）。
DISCOVERY_TOOLS = frozenset({
    "list_all_services", "get_current_time", "get_current_timestamp",
    "get_region_code_by_name", "search_topic_by_service_name",
})

#: 带 ``limit``/``query`` 语义的日志类工具：参数必须按配置收敛（A07）。
#:
#: 旧实现只提供了 ``BoundedLogParams`` 这个 helper，却没有任何调用点，于是
#: 模型给出的 ``limit=999999`` 会原样发给 MCP，并保留约 200k 字符的返回。
LOG_PARAM_TOOLS = frozenset({
    "search_service_logs", "search_log", "search_system_logs", "read_log_file",
    "analyze_log_pattern",
})


class TargetRequiredError(ValueError):
    """多个候选需要用户选择；在绑定前不得调用目标数据工具。"""

    def __init__(self, resolution: TargetResolution):
        super().__init__(resolution.reason or "需要选择诊断目标")
        self.resolution = resolution
        self.candidates = list(resolution.candidates)


def _json_size(value: Any) -> int:
    """序列化长度（与预算/blob 计量口径一致，不抛异常）。"""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def tool_schema(tool: Any) -> dict:
    schema = getattr(tool, "args_schema", None)
    if hasattr(schema, "model_json_schema"):
        schema = schema.model_json_schema()
    if not isinstance(schema, dict):
        schema = tool.get_input_schema().model_json_schema()
    result = copy.deepcopy(schema)
    result.setdefault("type", "object")
    result.setdefault("properties", {})
    result["additionalProperties"] = False
    return result


def schema_property_names(tool: Any) -> set[str]:
    return set(tool_schema(tool).get("properties", {}))


def validate_args(tool: Any, args: dict) -> None:
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是对象")
    errors = list(Draft202012Validator(tool_schema(tool)).iter_errors(args))
    if errors:
        raise ValueError("工具参数不符合注册 schema: " + errors[0].message[:200])


def unwrap_payload(value: Any) -> Any:
    """保留 structuredContent / TextContent 内部的错误与来源标记。"""
    if hasattr(value, "structuredContent") and value.structuredContent is not None:
        value = value.structuredContent
    elif hasattr(value, "content"):
        if getattr(value, "isError", False):
            return {"ok": False, "source": "unavailable", "error": str(value.content)}
        value = value.content
    if isinstance(value, str):
        try:
            return unwrap_payload(json.loads(value))
        except (ValueError, TypeError):
            return value
    if isinstance(value, list):
        blocks = [
            unwrap_payload(item.get("text", ""))
            if isinstance(item, dict) and item.get("type") == "text"
            else unwrap_payload(item)
            for item in value
        ]
        return blocks[0] if len(blocks) == 1 else blocks
    return value


def payload_error(value: Any) -> str:
    """兼容旧调用：返回错误描述，空串表示没有错误。"""
    result = classify_payload(value)
    if result.status in {"error", "timeout", "unavailable", "unsupported"}:
        error = result.error or {}
        return str(error.get("message") or error.get("code") or result.status)[:300]
    if result.status == "partial":
        error = result.error or {}
        return str(error.get("message") or "partial")[:300]
    return ""


def payload_sources(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = {str(value["source"])} if value.get("source") else set()
        for item in value.values():
            found |= payload_sources(item)
        return found
    if isinstance(value, list):
        return set().union(*(payload_sources(item) for item in value)) if value else set()
    return set()


class RunTools:
    def __init__(self, tools: list, run_id: str, execution_mode: str,
                 emit: Callable[[dict], None], *, budget=None):
        self.by_name = {tool.name: tool for tool in tools if tool.name in READ_ONLY_TOOLS}
        self.run_id = run_id
        self.execution_mode = execution_mode
        self.emit = emit
        self.evidence: list[dict] = []
        self.target: Target | None = None
        self.target_service = ""
        self.target_instance = ""
        self.calls = 0
        self.deadline: float | None = None
        self.cancelled = False
        #: 运行级硬预算（app.core.budgets.RequestBudget）；None 表示未接入。
        self.budget = budget
        #: 参数收敛/证据截断的说明汇总；写进报告“证据缺口”（A07）。
        self.budget_notes: list[str] = []
        #: 每次调用的稳定 ID 前缀；同一 run 内唯一，重放时不变。
        self._call_prefix = run_id[:8] if run_id else "run"

    # ── 目录与目标 ───────────────────────────────────────────

    def catalog(self, names: list[str] | None = None) -> str:
        selected = set(names) if names is not None else set(self.by_name)
        return json.dumps({
            name: {"description": getattr(tool, "description", "")[:250],
                   "parameters": tool_schema(tool)}
            for name, tool in sorted(self.by_name.items()) if name in selected
        }, ensure_ascii=False)

    async def discover_service(self, requested: str | None, user_input: str,
                               *, candidate: str | None = None) -> str:
        """绑定目标；多候选时必须由调用方让用户选择。

        ``candidate`` 是 UI 候选卡片上的稳定候选 ID（A04）：同名多实例只能靠它
        绑定到具体实例/地域；服务名仅作为没有候选 ID 时的回退。
        """
        resolution = await self.resolve_service(requested, user_input, candidate=candidate)
        if resolution.status == "target_required":
            raise TargetRequiredError(resolution)
        if resolution.status != "bound" or resolution.target is None:
            raise ValueError(resolution.reason or "服务发现失败，无法绑定诊断目标")
        self.bind(resolution.target)
        self.emit({
            "type": "status", "stage": "target_bound",
            "message": f"本次诊断目标：{resolution.target.service}",
            "service_name": resolution.target.service,
            "target": resolution.target.to_dict(),
        })
        return resolution.target.service

    async def resolve_service(self, requested: str | None,
                              user_input: str, *, candidate: str | None = None,
                              ) -> TargetResolution:
        evidence = await self.call("list_all_services", {}, agent="discovery")
        payload = evidence["data"]
        if not evidence["ok"] or not isinstance(payload, dict):
            raise ValueError("服务发现失败，无法绑定诊断目标")
        services = payload.get("services", [])
        if not isinstance(services, list):
            raise ValueError("服务列表格式不合法，无法绑定诊断目标")
        # 候选 ID 必先在**当前**目录里重新校验；目录变化时明确失败，而不是沿用旧绑定。
        return resolve_target(services, requested, user_input, candidate=candidate)

    def bind(self, target: Target) -> None:
        self.target = target
        self.target_service = target.service
        self.target_instance = target.instance_id

    # ── 执行 ────────────────────────────────────────────────

    async def call(self, name: str, args: dict, *, agent: str = "local",
                   step_id: str = "") -> dict:
        self.calls += 1  # increment before awaiting: one shared budget across parallel agents
        call_id = f"{self._call_prefix}-c{self.calls}"
        started = time.monotonic()
        data: Any = None
        result = None
        error = ""
        status = "error"
        try:
            if self.cancelled:
                raise ValueError("运行已取消，不再发起新的工具调用")
            if self.budget is not None:
                # 预算是跨 agent 共享的硬上限；超支抛 BudgetExhausted，
                # 由下方 except 记成一次失败的证据（不中断整个运行）。
                self.budget.reserve("tool_calls", 1)
                if name in SUBPROCESS_TOOLS:
                    self.budget.reserve("subprocess_calls", 1)
            elif self.calls > config.diagnostic_max_tool_calls:
                raise ValueError("本次诊断工具调用预算已用尽")
            tool = self.by_name.get(name)
            if tool is None:
                raise ValueError(f"工具不在本次只读白名单中: {name}")
            validate_args(tool, args)
            prepared = self._prepare_args(name, tool, args)
            # A05：补全目标字段后再按 schema 完整校验一次，避免注入工具不接受的键。
            validate_args(tool, prepared)
            timeout = self._effective_timeout()
            raw = await asyncio.wait_for(tool.ainvoke(prepared), timeout)
            data = unwrap_payload(raw)
            result = classify_payload(data)
            status = result.status
            error = payload_error(data)
            # A07：并入证据前预占/消费 evidence_chars，超额走有界摘要。
            data, budget_note = self._apply_evidence_budget(data)
            if budget_note:
                error = f"{error}；{budget_note}" if error else budget_note
            args = prepared
        except (TimeoutError, asyncio.TimeoutError):
            status, error = "timeout", "工具调用超时"
        except Exception as exc:  # noqa: BLE001 - 工具错误必须进入证据而不是中断运行
            error = str(exc)[:400]
            status = "unsupported" if "白名单" in error else "error"
        sources = payload_sources(data)
        live = bool(result.live) if result is not None else False
        demo = bool(result.demo) if result is not None else False
        evidence = {
            "id": f"E{len(self.evidence) + 1}", "tool": name, "args": args,
            "agent": agent, "step_id": step_id, "tool_call_id": call_id,
            "ok": status in {"ok", "empty", "partial"},
            "status": status,
            "source": ",".join(sorted(sources)) or ("unavailable" if error else "unspecified"),
            "source_kind": ",".join(sorted(sources)) or (
                "unavailable" if error else "unspecified"),
            "live": live,
            "demo": demo,
            "data": data, "error": error,
            "target": "configured_ssh_endpoint" if name in SSH_TOOLS else self.target_service,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        self.evidence.append(evidence)
        preview = json.dumps(evidence, ensure_ascii=False, default=str)
        fields = {"tools": [{"name": name, "args": args}], "step": name,
                  "agent": agent, "evidence_id": evidence["id"], "ok": evidence["ok"],
                  "status": status, "source": evidence["source"],
                  "step_id": step_id, "tool_call_id": call_id,
                  "result_preview": preview[:2000],
                  "result_chars": len(preview), "execution_mode": self.execution_mode}
        # Raw structured evidence is persisted separately from UI previews.
        trace_event(self.run_id, "evidence", evidence=evidence)
        trace_event(self.run_id, "tool_step", **fields)
        self.emit({"type": "tool_step", **fields})
        self.emit({"type": "step_complete", "current_step": name,
                   "evidence_id": evidence["id"], "ok": evidence["ok"],
                   "status": status, "step_id": step_id, "tool_call_id": call_id,
                   "tool": name, "agent": agent})
        return evidence

    def _prepare_args(self, name: str, tool: Any, args: dict) -> dict:
        """把工具参数收敛到已绑定目标；元数据类工具不做目标约束。"""
        if name in DISCOVERY_TOOLS:
            return dict(args or {})
        if name in KNOWLEDGE_SCOPED_TOOLS:
            # 知识检索只接受一个受限长度的 query：没有目标概念，也不能读任意 URL。
            query = str((args or {}).get("query") or "").strip()
            if not query:
                raise ValueError("知识检索的 query 不能为空")
            if len(query) > MAX_KNOWLEDGE_QUERY_CHARS:
                raise ValueError(
                    f"知识检索 query 超过 {MAX_KNOWLEDGE_QUERY_CHARS} 字符，请先归纳问题"
                )
            return {"query": query}
        # A05：只把目标字段补进工具 schema 真正接受的键。此前这里把 TOPIC_KEYS
        # 并入 ``accepts``，于是 schema 只有 ``topic_name`` 的工具也会被塞进
        # ``topic_id``/``topic``，StructuredTool 直接报 unexpected keyword argument。
        accepts = schema_property_names(tool)
        if self.target is not None:
            bound = bind_tool_args(args, self.target, accepts=accepts)
        elif self.target_service and "service_name" in (args or {}):
            # 兼容旧调用：只手工设置了 service 字符串（没有完整 Target）。
            if args["service_name"] != self.target_service:
                raise ValueError("工具目标与本次选择的服务不一致")
            bound = dict(args)
        else:
            raise ValueError("尚未绑定诊断目标，不能执行目标数据工具")
        if name in LOG_PARAM_TOOLS:
            # A07：日志类参数必须收敛到有界范围，并把“丢了什么”记成证据缺口。
            # ``clamp`` 总会写入 ``limit``；这里再按 schema 收窄一次，确保只保留
            # 工具真正接受的键（否则会注入工具不认识的参数）。
            bound, notes = BoundedLogParams.from_config(config).clamp(bound)
            bound = {key: value for key, value in bound.items() if key in accepts}
            self.budget_notes.extend(notes)
        return bound

    def _apply_evidence_budget(self, data: Any) -> tuple[Any, str]:
        """把工具结果并入证据前预占/消费 ``evidence_chars``（A07）。

        返回 ``(记录用的数据, 说明)``。超预算时按 ``truncate_for_prompt`` 做有界
        摘要（仍是合法 JSON 结构），并说明被截断多少；预算耗尽时只保留一条
        显式占位，绝不把 200k 字符原样写进证据/prompt。
        """
        if self.budget is None or data is None:
            return data, ""
        try:
            remaining = int(self.budget.remaining("evidence_chars"))
        except Exception:  # noqa: BLE001 - 预算种类异常时不阻断调用
            return data, ""
        size = _json_size(data)
        if remaining <= 0:
            # 预算已耗尽：数据字段只留**最小占位**（空对象，2 字符），说明文字
            # 只进证据缺口，不再往证据载荷里塞 50+ 字符的 note。
            # 旧实现每次仍记一条带 note 的占位且不做预占，导致“记录的数据总量”
            # 可以超过 evidence_chars（虽然单次有界）。
            note = "证据预算已用尽，本次结果仅保留占位"
            self.budget_notes.append(note)
            return {}, note
        if size <= remaining:
            try:
                self.budget.reserve("evidence_chars", size)
            except Exception:  # noqa: BLE001 - 并发竞争下退化为占位
                return {"_truncated": True, "note": "证据预算竞争失败"}, ""
            return data, ""
        bounded = truncate_for_prompt(data, remaining)
        kept: Any = data
        if isinstance(data, (dict, list)):
            try:
                kept = json.loads(bounded.text)
            except (ValueError, TypeError):
                kept = bounded.text
        else:
            kept = bounded.text
        kept_size = _json_size(kept)
        if kept_size > remaining:
            # 预算极小（例如 evidence_chars=10）时，连“省略说明”都放不下：
            # 数据本身退化为空对象，说明文字只进证据缺口/prompt 摘要，不进数据。
            kept = {}
            kept_size = _json_size(kept)
        try:
            self.budget.reserve("evidence_chars", min(kept_size, remaining))
        except Exception:  # noqa: BLE001
            pass
        if bounded.note:
            self.budget_notes.append(bounded.note)
        return kept, bounded.note

    def _effective_timeout(self) -> float:
        budget = float(config.diagnostic_tool_timeout_seconds)
        if self.deadline is None:
            return budget
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("运行 deadline 已到")
        return min(budget, remaining)

    def cancel(self) -> None:
        """标记取消：之后不再发起新调用；已发出的调用只能等待其返回。"""
        self.cancelled = True


def _describe_tool_load_error(exc: BaseException) -> str:
    """把连接类异常（含 ExceptionGroup）变成可执行的提示，不泄漏内部载荷。"""
    leaves: list[str] = []

    def walk(error: BaseException, depth: int = 0) -> None:
        if depth > 4:
            return
        sub = getattr(error, "exceptions", None)
        if sub:
            for item in sub:
                walk(item, depth + 1)
            return
        text = str(error).strip() or type(error).__name__
        leaves.append(f"{type(error).__name__}: {text}"[:200])

    walk(exc)
    detail = "; ".join(dict.fromkeys(leaves)) or type(exc).__name__
    return (
        "MCP 工具服务不可用，无法开始诊断："
        f"{detail}。请确认 CLS/Monitor/SSH 三个 MCP 服务已启动"
        "（8003/8004/8005），或先不依赖工具地阅读知识库。"
    )


def _accepts_keyword(func: Any, name: str) -> bool:
    """目标函数是否接受该关键字参数（含 ``**kwargs``）。"""
    import inspect

    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return name in parameters


async def discover_service_compat(runtime: Any, requested: str | None, user_input: str,
                                  *, candidate: str | None = None):
    """调用 ``runtime.discover_service``；旧版/替身没有 ``candidate`` 时退回原签名。"""
    func = getattr(runtime, "discover_service")
    kwargs: dict[str, Any] = {}
    if candidate and _accepts_keyword(func, "candidate"):
        kwargs["candidate"] = candidate
    return await func(requested, user_input, **kwargs)


async def create_tools_compat(create: Callable, run_id: str, execution_mode: str,
                              emit: Callable[[dict], None], *,
                              deadline: float | None = None, budget=None):
    """调用 ``create_run_tools``，按目标签名裁剪可选参数。

    运行器可能被测试替身替换（只接受 3 个位置参数）。deadline/budget 是新增的
    可选能力，不能因为替身不认识它们就让整次运行 TypeError。
    """
    kwargs: dict[str, Any] = {}
    if _accepts_keyword(create, "deadline"):
        kwargs["deadline"] = deadline
    if _accepts_keyword(create, "budget"):
        kwargs["budget"] = budget
    return await create(run_id, execution_mode, emit, **kwargs)


async def create_run_tools(run_id: str, execution_mode: str,
                           emit: Callable[[dict], None],
                           *, deadline: float | None = None,
                           budget=None) -> RunTools:
    timeout = config.diagnostic_tool_timeout_seconds
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("运行 deadline 已到")
        timeout = min(timeout, remaining)
    if budget is not None:
        budget.reserve("subprocess_calls", 1)
    try:
        tools = await asyncio.wait_for(get_mcp_tools_cached(), timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"MCP 工具服务在 {timeout:g}s 内没有响应：请确认 8003/8004/8005 端口已启动"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - ExceptionGroup 不该直接暴露给用户
        # langchain-mcp-adapters 连接失败会抛 ExceptionGroup/TaskGroup 异常，
        # 直接透传时用户只看到 "unhandled errors in a TaskGroup"，无法定位。
        raise RuntimeError(_describe_tool_load_error(exc)) from exc
    # 知识检索是进程内工具（不在 MCP 里）；失败只影响它自己的证据条目。
    from app.agent.aiops.knowledge_wiring import knowledge_tools

    runtime = RunTools(
        [*tools, get_current_time, *knowledge_tools(execution_mode=execution_mode)],
        run_id, execution_mode, emit, budget=budget,
    )
    runtime.deadline = deadline
    return runtime
