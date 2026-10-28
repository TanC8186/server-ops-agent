"""聊天流事件持久化与按 seq 续读。

为什么需要这一层：``/api/chat_stream`` 的每个 SSE 事件都带 ``event_id`` /
``seq`` / ``run_id``（见 ``app.api.chat._event_frame``），但聊天事件不落
trace。浏览器断流（EOF 却没有 ``done``）或标签页被关闭时，用户只剩半截
回答，无法续读；``GET /api/runs/{run_id}`` 只服务诊断 trace，拿不到聊天事件。

这里只做一件事：把一次流按 ``run_id`` 追加到
``logs/chat_streams/<run_id>.jsonl``，并提供 ``read_after(run_id, after_seq,
limit)`` 纯服务函数，让前端按 ``lastSeq`` 续读。它不解析业务语义、不重发
模型调用，也不补造缺失事件——读不到终态就如实告诉调用方。

边界（都是防止“看起来能用、实际出错”的设计）：

- ``run_id`` 只接受 32 位小写十六进制（与 ``app.api.runs`` 同一格式）。任何
  其他值（``..``、盘符、UNC、大写、点段）在拼路径前就被拒绝，杜绝路径穿越。
- 每条记录必须有**正整数 seq**。缺 seq 的记录按游标无法定位，写入即拒绝：
  否则它会成为永远读不到的尾部，续读看起来成功却少内容。
- 单文件有字节上限（默认 4 MiB）。超限后停止追加并返回 ``False``，由调用方
  决定是否提示“本次事件未全部保留”；绝不静默丢弃。
- 文件有 TTL（默认 24 小时），由应用 ``lifespan`` 启动时调用一次
  ``purge_expired()`` 清理，另有 ``purge()`` 可供运维任务显式调用；不挂后台
  线程——这个数据只在一次断流续读窗口内有意义。
- 追加是单次 ``os.write`` 整行（``O_APPEND``），进程内再加锁；同进程并发写
  不会交错。跨进程追加依赖操作系统对 ``O_APPEND`` 小写入的原子性，这一点
  在模块文档里如实说明，不额外承诺分布式一致性。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger

#: 服务端唯一生成格式：``uuid4().hex``。与 ``app.api.runs.RUN_ID_PATTERN`` 一致。
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: 存储目录覆盖（测试/部署）。
CHAT_STREAM_DIR_ENV = "CHAT_STREAM_DIR"
#: 单文件字节上限覆盖。
CHAT_STREAM_MAX_BYTES_ENV = "CHAT_STREAM_MAX_BYTES"
#: TTL（小时）覆盖。
CHAT_STREAM_TTL_HOURS_ENV = "CHAT_STREAM_TTL_HOURS"

#: 默认单文件上限 4 MiB：一次问答的事件（含 400 字符片段引用）远小于此值。
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
#: 默认保留 24 小时：覆盖“关掉标签页第二天再打开”的常见场景。
DEFAULT_TTL_SECONDS = 24 * 3600

#: 单次续读默认/最大条数，避免异常文件把整份内容一次返回。
DEFAULT_READ_LIMIT = 2000
MAX_READ_LIMIT = 20000

_LOCK = threading.Lock()


class ChatStreamNotFound(LookupError):
    """该 run 没有落盘的聊天事件（可能从未持久化、已过期或 run_id 不合法）。

    调用方（路由）应把它映射成 404，而不是空列表——空列表会让“记录已清理”
    与“这次流没有任何事件”无法区分，前端会把后者当成“已经读完”。
    """


def storage_dir() -> Path:
    """事件目录；``CHAT_STREAM_DIR`` 可覆盖（测试用临时目录）。"""
    override = os.environ.get(CHAT_STREAM_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "logs" / "chat_streams"


def max_bytes() -> int:
    """单文件上限；非法或非正值回退默认（不放行无限增长）。"""
    raw = os.environ.get(CHAT_STREAM_MAX_BYTES_ENV, "").strip()
    try:
        value = int(float(raw)) if raw else DEFAULT_MAX_BYTES
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def ttl_seconds() -> float:
    """文件保留秒数；非法值回退默认。``0`` 表示不自动过期。"""
    raw = os.environ.get(CHAT_STREAM_TTL_HOURS_ENV, "").strip()
    try:
        hours = float(raw) if raw else DEFAULT_TTL_SECONDS / 3600
    except (TypeError, ValueError):
        return float(DEFAULT_TTL_SECONDS)
    return max(0.0, hours * 3600)


def _path(run_id: str) -> Path:
    """run_id -> 文件路径；格式不符直接拒绝，绝不拼接可疑字符串。"""
    value = str(run_id or "")
    if not RUN_ID_PATTERN.fullmatch(value):
        raise ValueError("run_id 必须是 32 位小写十六进制")
    return storage_dir() / f"{value}.jsonl"


def _usable_seq(event: Mapping[str, Any]) -> int | None:
    """续读游标只用正整数 seq；bool 不是合法 seq。"""
    seq = event.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq if seq > 0 else None


class ChatEventStore:
    """按 run_id 追加事件，并按 seq 续读。"""

    def __init__(self, *, max_bytes_limit: int | None = None,
                 ttl: float | None = None) -> None:
        # 显式参数优先于环境变量，便于测试固定边界而不改全局环境。
        # 字节上限必须为正：显式传 0/负数按“未设置”处理（否则每条事件都被拒，
        # 看起来像持久化坏了）。TTL 例外——`ttl=0` 是“显式关闭过期清理”的语义。
        self._max_bytes = (
            int(max_bytes_limit) if max_bytes_limit is not None and max_bytes_limit > 0 else None
        )
        self._ttl = float(ttl) if ttl is not None else None

    # ── 写 ────────────────────────────────────────────────────

    def append(self, run_id: str, event: Mapping[str, Any], *,
               session_id: str = "") -> bool:
        """追加一条事件；返回是否写入。

        返回 ``False`` 的原因只有两种，都是可以观测、可以提示用户的：
        ``seq`` 不是正整数（无法按游标定位），或文件已达字节上限。
        写入异常记日志后同样返回 ``False``——持久化是续读能力，不是回答的
        前提，不能让它把一次成功的问答变成 500。

        ``session_id`` 是服务端当前请求的会话：显式传入时覆盖事件里可能带上
        的值。续读要按它拒绝跨会话读取，所以不能采信事件字典里的旧字段。
        """
        try:
            path = _path(run_id)
        except ValueError:
            logger.warning(f"聊天事件 run_id 不合法，已拒绝写入: {run_id!r}")
            return False
        seq = _usable_seq(event)
        if seq is None:
            logger.warning(f"聊天事件缺少正整数 seq，已拒绝写入: run={run_id}")
            return False
        record = {str(key): value for key, value in event.items()}
        record["seq"] = seq
        record["run_id"] = str(run_id)
        if session_id:
            record["session_id"] = str(session_id)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        payload = line.encode("utf-8")
        limit = self._max_bytes if self._max_bytes is not None else max_bytes()
        with _LOCK:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size + len(payload) > limit:
                    logger.warning(
                        f"聊天事件文件达到上限，停止追加: {path.name} "
                        f"({path.stat().st_size}+{len(payload)}>{limit})"
                    )
                    return False
                # 单次 write 整行 + O_APPEND：同进程并发写不会交错。
                fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                return True
            except OSError as exc:
                logger.error(f"写入聊天事件失败: {path}: {type(exc).__name__}: {exc}")
                return False

    # ── 读 ────────────────────────────────────────────────────

    def read_after(self, run_id: str, after_seq: int = 0,
                   limit: int = DEFAULT_READ_LIMIT, *,
                   session_id: str = ""
                   ) -> tuple[list[dict[str, Any]], bool, int | None]:
        """读取 ``seq > after_seq`` 的事件。

        返回 ``(events, truncated, next_after_seq)``：

        - ``events`` 按落盘顺序（即服务端 seq 顺序），已去重 ``event_id``
          ——重连重放可能让同一条事件被写两次，续读侧不能把它当两条内容；
        - ``truncated=True`` 表示还有剩余，调用方必须带 ``next_after_seq`` 再读；
        - ``next_after_seq`` 是本页最大 seq（无剩余时为 ``None``）。因为写入
          强制要求正整数 seq，这里不存在“边界无游标”的死页。

        给了 ``session_id`` 时，任何一条将要返回的记录都必须带同一个会话：
        否则抛 :class:`ChatStreamNotFound`（路由映射 404）。会话归属只证明
        “这个会话属于你”，不证明这些事件由它产生；缺少 ``session_id`` 字段的
        旧记录按不匹配处理，绝不放行。
        """
        path = _path(run_id)
        if not path.is_file():
            raise ChatStreamNotFound(run_id)
        after = int(after_seq) if isinstance(after_seq, int) and after_seq > 0 else 0
        page_limit = MAX_READ_LIMIT if limit > MAX_READ_LIMIT else max(1, int(limit))
        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        with _LOCK:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                logger.error(f"读取聊天事件失败: {path}: {type(exc).__name__}: {exc}")
                raise ChatStreamNotFound(run_id) from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # 半行/损坏行：跳过并计数，绝不让整个续读失败。
                logger.warning(f"聊天事件行损坏，已跳过: {path.name}")
                continue
            if not isinstance(record, dict):
                continue
            seq = _usable_seq(record)
            if seq is None:
                continue
            # 会话校验先于游标与去重：它校验的是“这个文件是否属于该会话”，不能
            # 因为 after_seq 已经越过别人的记录就跳过——那样同一份文件会按游标
            # 位置给出不同结论（空页 vs 404），等于把“存在”与“不存在”变成可探测。
            if session_id and str(record.get("session_id") or "") != str(session_id):
                logger.warning(
                    f"聊天事件会话不符，拒绝续读: run={run_id} "
                    f"record_session={record.get('session_id')!r}"
                )
                raise ChatStreamNotFound(run_id)
            if seq <= after:
                continue
            event_id = record.get("event_id")
            if isinstance(event_id, str) and event_id:
                if event_id in seen:
                    continue
                seen.add(event_id)
            events.append(record)
            if len(events) >= page_limit + 1:
                # 多读一条只为判断 truncated，不返回给调用方。
                break
        truncated = len(events) > page_limit
        page = events[:page_limit]
        next_after = max((_usable_seq(item) or 0) for item in page) if page else None
        return page, truncated, (next_after if truncated else None)

    def exists(self, run_id: str) -> bool:
        try:
            return _path(run_id).is_file()
        except ValueError:
            return False

    # ── 清理 ──────────────────────────────────────────────────

    def purge(self, *, now: float | None = None) -> int:
        """删除超过 TTL 的文件，返回删除数量。

        TTL 为 0 时什么都不删（显式关闭自动过期）。清理失败只记日志：
        磁盘问题不该影响问答主流程。
        """
        ttl = self._ttl if self._ttl is not None else ttl_seconds()
        if ttl <= 0:
            return 0
        directory = storage_dir()
        if not directory.is_dir():
            return 0
        deadline = (now if now is not None else time.time()) - ttl
        removed = 0
        with _LOCK:
            for file in directory.glob("*.jsonl"):
                try:
                    if file.stat().st_mtime >= deadline:
                        continue
                    file.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    logger.warning(f"清理聊天事件失败: {file.name}: {type(exc).__name__}")
        if removed:
            logger.info(f"已清理过期聊天事件: {removed} 个文件")
        return removed


#: 进程内单例；路由与测试都通过它读写（测试用 ``CHAT_STREAM_DIR`` 隔离）。
chat_events = ChatEventStore()


def record(run_id: str, event: Mapping[str, Any], *, session_id: str = "") -> bool:
    """便于路由接线的一行入口：追加一条事件（见报告中的接线补丁）。"""
    return chat_events.append(run_id, event, session_id=session_id)


def read_after(run_id: str, after_seq: int = 0,
               limit: int = DEFAULT_READ_LIMIT, *,
               session_id: str = ""
               ) -> tuple[list[dict[str, Any]], bool, int | None]:
    """按 seq 续读（路由 ``GET /api/chat/events/{run_id}`` 直接调用）。

    没有记录或会话不符时抛 :class:`ChatStreamNotFound`；路由应映射成 404，
    而不是返回空列表——后者会让“记录已清理”和“这次流没有事件”无法区分。
    """
    return chat_events.read_after(run_id, after_seq, limit, session_id=session_id)


def purge_expired(*, now: float | None = None) -> int:
    """删除超过 TTL 的文件（供启动/定时任务显式调用）。"""
    return chat_events.purge(now=now)
