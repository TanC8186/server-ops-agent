"""跨请求资源边界（BE-06）。

本模块只提供“原语 + 配置”，不直接改调用方；调用方的接线由各自分区的 agent 完成。
设计目标：任何单个请求（诊断/问答）都不能用光进程级资源，排队也必须是有界等待。

四个原语
--------
``AdmissionController``
    进程级并发闸门。``async with controller:`` 之外的部分不占名额；超过
    ``max_concurrent`` 的请求最多等待 ``queue_timeout`` 秒，超时抛
    ``AdmissionRejected(retry_after)``。纯 asyncio 实现（``asyncio.Semaphore`` +
    ``asyncio.wait_for``），不依赖线程，等待者被取消时会被正确移出等待集合。

``RequestBudget``
    单次运行的硬预算（tool/llm/subprocess(mcp)/prompt 字符/evidence 字符）。
    ``reserve(kind, amount)`` 在会超额时抛 ``BudgetExhausted(kind)`` 且不留下半截计数。
    同一个实例可被多个 agent 共享，因为 ``reserve`` 内部没有 await 点，
    在事件循环里是原子的，两个协程无法通过竞争超支。

``BoundedLogParams``
    日志查询参数收敛：``limit`` 不超过 ``diagnostic_log_limit``，逗号分隔关键词
    不超过 ``diagnostic_log_max_keywords`` 个、每个不超过 64 字符；返回
    ``(params, notes)``，notes 明确说明被丢掉了什么，便于写进证据缺口。

``truncate_for_prompt``
    有界抽取。字符串保留头尾并插入显式标记；dict/list 先解析再截断
    （优先丢最大的列表元素，其次缩短最长字符串），返回的 ``text`` 始终是合法 JSON，
    绝不会把 JSON 从中间切断。

配置来源（app/config.py）
------------------------
``max_concurrent_diagnostics`` / ``admission_queue_timeout_seconds`` 给准入闸门；
``diagnostic_max_tool_calls`` / ``diagnostic_max_llm_calls`` /
``diagnostic_max_prompt_chars`` / ``diagnostic_max_evidence_chars`` 给运行预算；
``diagnostic_log_limit`` / ``diagnostic_log_max_keywords`` 给日志参数收敛。
均提供 ``from_config(config)``，避免调用方自己抄一遍默认值。

预期接线（由 runtime_tools.py / cloud_multi.py 的分区 owner 完成，本模块不碰）
-----------------------------------------------------------------------------
- 诊断入口（``run_diagnostic`` 或 ``RunTools`` 的创建处）::

      controller = AdmissionController(
          config.max_concurrent_diagnostics,
          config.admission_queue_timeout_seconds,
      )
      async with controller:
          budget = RequestBudget.from_config(config)
          ...  # 整个 run 用同一个 budget

- ``RunTools`` 每次工具调用前::

      budget.reserve("tool_calls", 1)
      if tool_name in SUBPROCESS_TOOLS:
          budget.reserve("mcp_calls", 1)  # 别名，落到 subprocess_calls

- 每次 LLM 调用前 ``budget.reserve("llm_calls", 1)``，拼 prompt 前
  ``budget.reserve("prompt_chars", len(prompt))``，把工具结果并入证据前
  ``budget.reserve("evidence_chars", len(chunk))``。
- 日志专员调用 ``search_log`` 前::

      params, notes = BoundedLogParams.from_config(config).clamp(params)
      if notes:
          evidence_gaps.extend(notes)
- 工具结果进入 prompt 前::

      bounded = truncate_for_prompt(result, config.diagnostic_max_evidence_chars)
      if bounded.truncated:
          evidence_gaps.append(bounded.note)

``AdmissionRejected`` 的 ``retry_after`` 应原样返回给前端/调用方（HTTP 429 的
Retry-After 或 SSE 错误事件的 payload），不要伪装成普通的诊断失败。
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

#: 单个日志关键词的最大长度（CLS 查询串里的一个 token）。
MAX_KEYWORD_CHARS = 64

#: 规范的预算种类。``mcp_calls`` 是 ``subprocess_calls`` 的别名。
BUDGET_KINDS = (
    "tool_calls",
    "llm_calls",
    "subprocess_calls",
    "prompt_chars",
    "evidence_chars",
)
_KIND_ALIASES = {"mcp_calls": "subprocess_calls"}

#: 截断标记（保持英文，便于模型识别；注释/日志用中文）。
_STRING_MARKER = "\n…[{dropped} chars truncated]…\n"
_JSON_MARKER_KEY = "_truncated"


class AdmissionRejected(Exception):
    """并发超限且排队超时。``retry_after`` 是建议的重试等待秒数。"""

    def __init__(self, retry_after: float, message: str | None = None) -> None:
        self.retry_after = float(retry_after)
        super().__init__(message or f"诊断并发已满，排队超时；请在 {self.retry_after:.1f} 秒后重试")


class BudgetExhausted(Exception):
    """单次运行的预算耗尽。带上种类与额度，调用方可据此降级而不是崩溃。"""

    def __init__(self, kind: str, *, limit: int, used: int, amount: int) -> None:
        self.kind = kind
        self.limit = limit
        self.used = used
        self.amount = amount
        super().__init__(f"运行预算耗尽: {kind} 已用 {used}/{limit}，本次请求 {amount}")


@dataclass(frozen=True)
class AdmissionStats:
    """准入闸门的即时快照（只读）。"""

    active: int
    waiting: int
    max_concurrent: int
    queue_timeout: float
    rejected_total: int
    timeouts_total: int

    def to_dict(self) -> dict[str, int | float]:
        return {
            "active": self.active,
            "waiting": self.waiting,
            "max_concurrent": self.max_concurrent,
            "queue_timeout": self.queue_timeout,
            "rejected_total": self.rejected_total,
            "timeouts_total": self.timeouts_total,
        }


class AdmissionController:
    """进程级并发准入控制（asyncio 专用，无线程）。

    用法::

        controller = AdmissionController(2, 30)
        async with controller:
            await run_diagnostic(...)

    超限请求在 ``queue_timeout`` 内等待空位；超时抛 ``AdmissionRejected``。
    等待集合由 asyncio.Semaphore 自己维护，成功/超时/取消都会清理，
    不会随请求数无限增长。
    """

    def __init__(self, max_concurrent: int, queue_timeout: float) -> None:
        if int(max_concurrent) < 1:
            raise ValueError("max_concurrent 必须 >= 1")
        if float(queue_timeout) < 0:
            raise ValueError("queue_timeout 不能为负数")
        self._max_concurrent = int(max_concurrent)
        self._queue_timeout = float(queue_timeout)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._active = 0
        self._waiting = 0
        self._rejected_total = 0
        self._timeouts_total = 0

    # -- 只读属性 ---------------------------------------------------------
    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def queue_timeout(self) -> float:
        return self._queue_timeout

    @property
    def stats(self) -> AdmissionStats:
        return AdmissionStats(
            active=self._active,
            waiting=self._waiting,
            max_concurrent=self._max_concurrent,
            queue_timeout=self._queue_timeout,
            rejected_total=self._rejected_total,
            timeouts_total=self._timeouts_total,
        )

    @property
    def retry_after(self) -> float:
        """拒绝时建议的重试间隔：排队上限本身；0 表示“立即重试”不可行，给 1 秒。"""
        return self._queue_timeout if self._queue_timeout > 0 else 1.0

    # -- 上下文管理 -------------------------------------------------------
    async def __aenter__(self) -> AdmissionController:
        self._waiting += 1
        try:
            if self._queue_timeout <= 0:
                # 不排队：只在确实有空位时放行，避免 wait_for(timeout=0) 的伪超时
                if self._semaphore.locked():
                    self._count_rejection()
                    raise AdmissionRejected(self.retry_after) from None
                await self._semaphore.acquire()
            else:
                try:
                    await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
                except TimeoutError:
                    self._count_rejection()
                    raise AdmissionRejected(self.retry_after) from None
        finally:
            self._waiting -= 1
        self._active += 1
        return self

    def _count_rejection(self) -> None:
        """记一次超时拒绝；调用方随后抛 AdmissionRejected。"""
        self._timeouts_total += 1
        self._rejected_total += 1
        logger.warning(
            "诊断准入拒绝: active={} waiting={} timeout={}s",
            self._active,
            self._waiting,
            self._queue_timeout,
        )

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._active -= 1
        self._semaphore.release()


class RequestBudget:
    """单次运行的硬预算；同一实例可在多个 agent 协程间共享。

    构造函数直接接收各额度；``from_config`` 从 Settings 取（推荐）。
    ``reserve`` 没有 await 点，因此在事件循环内是原子的：多个协程共享同一个
    预算也不会超支。
    """

    def __init__(
        self,
        *,
        tool_calls: int,
        llm_calls: int,
        subprocess_calls: int,
        prompt_chars: int,
        evidence_chars: int,
    ) -> None:
        self._limits: dict[str, int] = {
            "tool_calls": int(tool_calls),
            "llm_calls": int(llm_calls),
            "subprocess_calls": int(subprocess_calls),
            "prompt_chars": int(prompt_chars),
            "evidence_chars": int(evidence_chars),
        }
        for kind, limit in self._limits.items():
            if limit < 0:
                raise ValueError(f"预算额度不能为负: {kind}={limit}")
        self._used: dict[str, int] = dict.fromkeys(self._limits, 0)

    @classmethod
    def from_config(cls, config: Any) -> RequestBudget:
        """从 Settings 构建；subprocess/mcp 默认与工具调用共用额度。"""
        tool_limit = int(getattr(config, "diagnostic_max_tool_calls", 10))
        return cls(
            tool_calls=tool_limit,
            llm_calls=int(getattr(config, "diagnostic_max_llm_calls", 12)),
            subprocess_calls=int(getattr(config, "diagnostic_max_subprocess_calls", tool_limit)),
            prompt_chars=int(getattr(config, "diagnostic_max_prompt_chars", 20000)),
            evidence_chars=int(getattr(config, "diagnostic_max_evidence_chars", 60000)),
        )

    @staticmethod
    def _canonical_kind(kind: str) -> str:
        canonical = _KIND_ALIASES.get(kind, kind)
        if canonical not in BUDGET_KINDS:
            raise ValueError(f"未知的预算种类: {kind}")
        return canonical

    def reserve(self, kind: str, amount: int = 1) -> int:
        """预占额度，返回预占后的已用量；超额抛 ``BudgetExhausted``。"""
        canonical = self._canonical_kind(kind)
        amount = int(amount)
        if amount < 0:
            raise ValueError("amount 不能为负数")
        limit = self._limits[canonical]
        used = self._used[canonical]
        if used + amount > limit:
            raise BudgetExhausted(canonical, limit=limit, used=used, amount=amount)
        self._used[canonical] = used + amount
        return self._used[canonical]

    def remaining(self, kind: str) -> int:
        canonical = self._canonical_kind(kind)
        return self._limits[canonical] - self._used[canonical]

    def snapshot(self) -> dict[str, dict[str, int]]:
        """各类别的 used/limit/remaining，可直接写进 trace。"""
        return {
            kind: {
                "used": self._used[kind],
                "limit": self._limits[kind],
                "remaining": self._limits[kind] - self._used[kind],
            }
            for kind in BUDGET_KINDS
        }


class BoundedLogParams:
    """把不可信的日志查询参数收敛到有界范围。

    ``clamp`` 不修改入参，返回 ``(params, notes)``；notes 逐条说明被丢弃/截断的内容。
    """

    def __init__(self, max_limit: int, max_keywords: int) -> None:
        if int(max_limit) < 1:
            raise ValueError("max_limit 必须 >= 1")
        if int(max_keywords) < 0:
            raise ValueError("max_keywords 不能为负数")
        self.max_limit = int(max_limit)
        self.max_keywords = int(max_keywords)

    @classmethod
    def from_config(cls, config: Any) -> BoundedLogParams:
        return cls(
            max_limit=int(getattr(config, "diagnostic_log_limit", 100)),
            max_keywords=int(getattr(config, "diagnostic_log_max_keywords", 5)),
        )

    def clamp(self, params: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        bounded: dict[str, Any] = dict(params)
        notes: list[str] = []

        # --- limit ---
        raw_limit = bounded.get("limit", self.max_limit)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            notes.append(f"日志 limit 非整数 {raw_limit!r}，已回退到 {self.max_limit}")
            limit = self.max_limit
        if limit < 1:
            notes.append(f"日志 limit {limit} 非法，已收敛为 1")
            limit = 1
        if limit > self.max_limit:
            notes.append(f"日志 limit {limit} 超过上限，已收敛为 {self.max_limit}")
            limit = self.max_limit
        bounded["limit"] = limit

        # --- query: 逗号分隔关键词 ---
        query = bounded.get("query")
        if isinstance(query, str) and query.strip():
            keywords = [item.strip() for item in query.split(",") if item.strip()]
            if self.max_keywords == 0 and keywords:
                notes.append(f"日志查询不支持关键词，已丢弃全部 {len(keywords)} 个关键词")
                keywords = []
            elif len(keywords) > self.max_keywords:
                dropped = keywords[self.max_keywords :]
                notes.append(f"日志关键词超过 {self.max_keywords} 个，已丢弃: {', '.join(dropped)}")
                keywords = keywords[: self.max_keywords]
            clamped_keywords: list[str] = []
            for keyword in keywords:
                if len(keyword) > MAX_KEYWORD_CHARS:
                    notes.append(
                        f"日志关键词超过 {MAX_KEYWORD_CHARS} 字符，已截断: "
                        f"{keyword[:MAX_KEYWORD_CHARS]}…"
                    )
                    keyword = keyword[:MAX_KEYWORD_CHARS]
                clamped_keywords.append(keyword)
            bounded["query"] = ",".join(clamped_keywords)
        return bounded, notes


@dataclass(frozen=True)
class TruncatedValue:
    """有界抽取结果：``text`` 可直接进 prompt，``note`` 写进证据缺口。"""

    text: str
    truncated: bool
    note: str = ""


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))


def _largest_list_container(root: Any) -> tuple[list, int] | None:
    """返回 (列表对象, 最大元素下标)；只考虑非空列表。"""
    best: tuple[list, int] | None = None
    best_size = -1
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            for index, item in enumerate(node):
                size = _json_size(item)
                if size > best_size:
                    best, best_size = (node, index), size
                stack.append(item)
    return best


def _largest_string_container(root: Any) -> tuple[MutableMapping, str] | None:
    """返回 (字典, 最长字符串键)；用于没有列表可丢时的兜底。"""
    best: tuple[MutableMapping, str] | None = None
    best_len = 16  # 太短的字符串不值得截断，也避免无意义循环
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and len(value) > best_len:
                    best, best_len = (node, key), len(value)
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return best


def _truncate_json(payload: Any, max_chars: int) -> tuple[str, bool, str]:
    """先解析再截断：优先丢最大列表里的最大元素，其次缩短最长字符串。"""
    data = copy.deepcopy(payload)
    changed = False
    # 给末尾的 ``_truncated`` 标记预留空间，避免“裁剪到刚好超线”再整体省略
    target = max(0, max_chars - 40)
    for _ in range(1000):
        if _json_size(data) <= target:
            break
        target_list = _largest_list_container(data)
        if target_list is not None:
            container, index = target_list
            if container:
                del container[index]
                changed = True
                continue
        target_str = _largest_string_container(data)
        if target_str is None:
            break
        container, key = target_str
        current = container[key]
        excess = _json_size(data) - target
        new_len = len(current) - excess - 32
        if new_len >= len(current):
            new_len = len(current) // 2
        new_len = max(0, new_len)
        container[key] = current[:new_len] + "…[truncated]"
        changed = True

    if isinstance(data, dict) and changed:
        data[_JSON_MARKER_KEY] = True

    text = json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)
    if len(text) > max_chars:
        # 结构太大且无可丢元素（或标记本身就超预算）：整体省略，但仍是合法 JSON
        text = json.dumps(
            {_JSON_MARKER_KEY: True, "note": "内容超出 prompt 预算，已整体省略"},
            ensure_ascii=False,
        )
        changed = True
    notes = f"结果超过 {max_chars} 字符，已解析后截断为合法 JSON"
    return text, changed, notes if changed else ""


def truncate_for_prompt(value: Any, max_chars: int) -> TruncatedValue:
    """把任意工具结果收敛到 ``max_chars`` 字符以内。

    - ``str``：保留头尾，中间插入 ``…[N chars truncated]…`` 标记；
    - ``dict`` / ``list``：解析后逐步裁剪，输出仍是合法 JSON；
    - 其他类型：先转成字符串再按字符串处理。
    """
    if int(max_chars) <= 0:
        raise ValueError("max_chars 必须为正数")
    max_chars = int(max_chars)

    if isinstance(value, str):
        text = value
        if len(text) <= max_chars:
            return TruncatedValue(text=text, truncated=False)
        marker = _STRING_MARKER.format(dropped=len(text) - max_chars)
        remaining = max_chars - len(marker)
        if remaining < 8:
            # 预算太小，放不下标记 + 头尾，退化为纯前缀截断
            text_out = text[:max_chars]
        else:
            head = remaining * 2 // 3
            tail = remaining - head
            text_out = text[:head] + marker + text[-tail:]
        note = f"内容超过 {max_chars} 字符，已保留头尾并截断 {len(text) - max_chars} 字符"
        return TruncatedValue(text=text_out, truncated=True, note=note)

    if isinstance(value, (dict, list)):
        text, truncated, note = _truncate_json(value, max_chars)
        return TruncatedValue(text=text, truncated=truncated, note=note)

    return truncate_for_prompt(str(value), max_chars)
