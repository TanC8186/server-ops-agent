"""轻量级 per-run trace。

每次诊断写一份 `logs/traces/<run_id>.jsonl`，记录节点、工具调用、决策和
token 用量。目的不是替代 Langfuse/OTel，而是在没有额外服务的情况下，
让一次失败诊断可以被回放和归因。

设计约束：
- trace 失败绝不能影响主流程：任何异常都在本模块内被吞掉，但会累计到
  ``get_trace_health()`` 并打 WARNING 日志（只带异常类型，不带载荷）。
- 敏感字段落盘前脱敏；单条记录与单 run 文件都有大小上限。
- 同一文件的追加在进程内加锁，避免 Windows 上多线程追加产生半行；同一台机器
  上的多个进程再叠加一层**跨进程文件锁**（``<run_id>.jsonl.lock``），避免多
  worker 同时追加时事件丢失或交织。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

if os.name == "nt":  # pragma: no cover - 平台分支实际只走一条
    import msvcrt
else:  # pragma: no cover
    import fcntl

TRACE_DIR = Path(__file__).resolve().parents[1] / "logs" / "traces"

#: retention 清理的"最小静默时间"：mtime 比这个更新的文件一律不删。
#:
#: 为什么需要它：per-path 锁只能防住"正在写"的那一瞬间。进程刚写完一条事件、
#: 下一轮诊断 200ms 后还要继续追加的 run，如果按保留策略被删掉，前端回放会看到
#: 一段被腰斩的 trace。默认 5 分钟，``PURGE_MIN_AGE_SECONDS`` 可覆盖。
DEFAULT_PURGE_MIN_AGE_SECONDS = 300
PURGE_MIN_AGE_ENV = "PURGE_MIN_AGE_SECONDS"

#: 单条序列化记录的上限（字节，含 JSON 结构，不含行尾换行）。
MAX_RECORD_BYTES = 64 * 1024
#: 单个 run 文件的上限（字节）。
MAX_RUN_BYTES = 32 * 1024 * 1024
#: 脱敏后的占位符。
REDACTED = "[redacted]"
#: 超长字符串被截断后追加的标记。
TRUNCATION_MARKER = "...[truncated]"

#: 命中即脱敏的键（大小写不敏感，``-`` 视同 ``_``）。
_SECRET_KEY_PARTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
)
#: ``sk-`` 开头的模型/接口密钥形态。
_SECRET_VALUE_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}")
#: token 用量类指标键（input_tokens/max_tokens……）不是秘密，不能误脱敏。
_USAGE_KEY_RE = re.compile(
    r"^(input|output|prompt|completion|total|max|cached|reasoning)_?tokens?$"
)
#: 记录自身的固定字段，调用方不得通过 **fields 覆盖。
_FIXED_RECORD_FIELDS: frozenset[str] = frozenset({"ts", "run_id", "event"})
#: run_id 会进入文件名，禁止路径分隔符与 .. 之类逃逸。
_RUN_ID_FORBIDDEN = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")

_STATS_LOCK = threading.Lock()
_STATS: dict[str, Any] = {
    "writes": 0,
    "failures": 0,
    "last_error": None,
    "last_error_at": None,
    "dropped": 0,
}

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}
#: 已达文件上限、不再接受写入的 run。
_TRUNCATED_RUNS: dict[str, bool] = {}
_MAX_TRUNCATED_RUNS = 4096


def get_trace_health() -> dict[str, Any]:
    """返回进程级 trace 健康快照。

    ``failures`` 是写盘/序列化失败次数，``dropped`` 是因单 run 配额或空
    run_id 被丢弃的事件数，``last_error`` 只保留异常类型和截断后的消息，
    不包含事件载荷。
    """
    with _STATS_LOCK:
        return dict(_STATS)


def _record_failure(exc: BaseException) -> None:
    """记录一次写入失败：计数 + WARNING（只打异常类型与短消息）。"""
    message = f"{type(exc).__name__}: {str(exc)[:200]}"
    now = datetime.now().isoformat(timespec="milliseconds")
    with _STATS_LOCK:
        _STATS["failures"] += 1
        _STATS["last_error"] = message
        _STATS["last_error_at"] = now
    logger.warning("trace 写入失败，已忽略以保护诊断主流程，异常类型: {}",
                   type(exc).__name__)


def _record_drop(count: int = 1) -> None:
    with _STATS_LOCK:
        _STATS["dropped"] += count


def _record_write() -> None:
    with _STATS_LOCK:
        _STATS["writes"] += 1


def _safe_run_id(run_id: str) -> str:
    """把 run_id 收敛成安全文件名；非法字符替换为 ``_``，避免路径逃逸。"""
    cleaned = _RUN_ID_FORBIDDEN.sub("_", str(run_id)).strip().replace("..", "_")
    return cleaned or "unknown"


def _lock_key(path: Path | str) -> str:
    """把路径收敛成锁键。

    写入方用 ``TRACE_DIR / f"{run_id}.jsonl"``，清理方拿到的是 ``resolve()``
    之后的路径；两者字符串不一定相同（大小写、符号链接、``.``/``..``）。锁必须
    按**同一个键**取，否则清理会拿到另一把无人持有的锁，"协同删除"就成了摆设。
    """
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(str(path)))


def _path_lock(path: Path) -> threading.Lock:
    """取（并按需创建）某个文件对应的锁；不同 run 之间互不阻塞。"""
    key = _lock_key(path)
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def is_file_locked(path: Path | str) -> bool:
    """该文件的 per-path 锁当前是否被持有（任何线程，含当前线程）。

    只用于测试与排查：``threading.Lock.locked()`` 不区分持有者，返回 True 不能
    推断"别人正在写"，只能推断"此刻拿不到锁"。
    """
    return _path_lock(Path(path)).locked()


# ── 跨进程文件锁 ─────────────────────────────────────────────
#
# 进程内锁只能保护同一进程的线程。多个 uvicorn worker / CLI 脚本同时给同一个
# run 追加事件时，Windows 上 ``open(..., "a")`` 之间仍可能互相覆盖尾部，表现
# 为整行丢失或两条记录粘成一行。这里在同一目录用 ``<run_id>.jsonl.lock`` 做
# 一把操作系统级的独占锁（Windows ``msvcrt.locking`` / POSIX ``flock``）：
#
# - 不加新依赖，也不依赖锁文件里写 pid（进程崩溃后 OS 自动释放锁，不会留下
#   "谁也不敢碰"的死锁文件）；
# - 锁文件是 0 字节的旁路文件，retention 只扫 ``*.jsonl``，因此不会被误删。
#   刻意**不在删除 trace 时删锁文件**：POSIX 上删除一个仍被他人持有的锁文件
#   会让后来的进程拿到新 inode 的锁，互斥直接失效（经典 ABA）；即使"确认没人
#   持有"再删，写入方"open 之后、lock 之前"的空窗也会踩到同一问题。代价是
#   ``logs/traces`` 里每个 run 留一个 0 字节文件，比互斥失效便宜得多。
# - 不可跨进程重入：同一进程的第二次加锁会失败并等待到超时。进程内已经有
#   ``_path_lock`` 串行化，正常调用路径不会嵌套 ``_write_record``。

#: 跨进程锁的默认等待上限（秒）。``TRACE_LOCK_TIMEOUT_SECONDS`` 可覆盖。
DEFAULT_TRACE_LOCK_TIMEOUT_SECONDS = 10.0
TRACE_LOCK_TIMEOUT_ENV = "TRACE_LOCK_TIMEOUT_SECONDS"


class TraceLockTimeout(RuntimeError):
    """在等待上限内没能拿到跨进程 trace 锁。

    单独成类型，是为了让 ``_record_failure`` 的 ``last_error`` 能直接读成
    "锁超时"，而不是伪装成磁盘错误；该条事件计入 ``failures`` 并打 WARNING，
    绝不静默丢弃。
    """


def lock_timeout_seconds() -> float:
    """跨进程锁等待上限（秒）；非法值回退默认，``0`` 表示只尝试一次。"""
    raw = os.environ.get(TRACE_LOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return float(DEFAULT_TRACE_LOCK_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TRACE_LOCK_TIMEOUT_SECONDS)
    return max(0.0, value)


def trace_lock_path(path: Path | str) -> Path:
    """trace 文件对应的锁文件：``<name>.lock``，与 trace 同目录。"""
    target = Path(path)
    return target.with_name(f"{target.name}.lock")


def _try_lock_fd(fd: int) -> bool:
    """非阻塞地尝试获取 fd 上的独占锁；被占用返回 False。

    被占用时底层抛 ``OSError``（Windows 是 errno 13，POSIX 是 EWOULDBLOCK）。
    这里把所有 ``OSError`` 都当成"本次拿不到"，由调用方在最外层给出有界等待
    和明确超时——锁是尽力而为的互斥，不值得为不同平台错误码再分叉一套语义。
    """
    if os.name == "nt":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_fd(fd: int) -> None:
    """释放 fd 上的锁；失败只可能是句柄已失效，关闭时 OS 同样会释放。"""
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def _cross_process_trace_lock(
    path: Path | str, *, timeout: float | None = None
) -> Iterator[None]:
    """持有 ``<trace>.lock`` 的跨进程独占锁，退出时释放并关闭句柄。

    有界等待：线性尝试 + 1ms→50ms 的退避，超过上限抛 :class:`TraceLockTimeout`，
    绝不无限等待。``timeout=0`` 等价于"只试一次"，供 retention 的非阻塞删除使用。

    为什么每次重新 open 而不是缓存 fd：open/close 一次约 0.45ms（Windows 上要
    建句柄），相比每条事件自身的序列化 + 追加开销仍可接受；换来的是任何异常
    路径都由 ``finally`` 关闭句柄，进程被强杀时 OS 也会回收。缓存 fd 反而要处理
    "锁文件被别人删掉后新 inode 上互斥失效"的经典 ABA，不值得。
    """
    target = Path(path)
    lock_path = trace_lock_path(target)
    limit = lock_timeout_seconds() if timeout is None else max(0.0, float(timeout))
    deadline = time.monotonic() + limit
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except FileNotFoundError:
        # 目录尚未建好（第一次写这个 run）：只在这种冷路径上付一次 mkdir。
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        delay = 0.001
        while not _try_lock_fd(fd):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TraceLockTimeout(
                    f"等待 trace 锁超时（{limit:.1f}s）: {lock_path.name}"
                )
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.05)
        try:
            yield
        finally:
            _unlock_fd(fd)
    finally:
        os.close(fd)


def purge_min_age_seconds() -> float:
    """清理的最小静默时间（秒）；非法值回退默认，``0`` 表示不做静默保护。"""
    raw = os.environ.get(PURGE_MIN_AGE_ENV, "").strip()
    if not raw:
        return float(DEFAULT_PURGE_MIN_AGE_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_PURGE_MIN_AGE_SECONDS)
    return max(0.0, value)


#: 协同删除的结果码；``purge_expired_traces`` 用它做精确计数。
REMOVE_DELETED = "deleted"
REMOVE_RECENT = "recent"
REMOVE_LOCKED = "locked"
REMOVE_MISSING = "missing"
REMOVE_FAILED = "failed"


def _remove_trace_file(
    path: Path | str,
    *,
    min_age_seconds: float | None = None,
    now: float | None = None,
    delete: bool = True,
) -> str:
    """在 per-path 锁内删除一个 trace 文件，返回结果码。

    与 ``_write_record`` 共用同一把锁，且用**非阻塞**获取：
    - 拿到锁：写入方此刻不在写；删除完成前它会在锁上等待，不会把半行写进
      一个已经被 unlink 的 inode。
    - 拿不到锁（有 append 正在进行）：直接放弃本次删除，下一轮再试。

    拿到进程内锁后再试一次**跨进程**锁（同样非阻塞）：另一台 worker 正在追加
    时，只靠本进程的锁是看不出来的。两级都拿到才允许删除，否则按 ``locked``
    跳过——宁可晚一轮删，也不制造腰斩的 trace。

    只在锁内才检查 mtime：写入方刚追加完会刷新 mtime，锁外读取会让"看起来已经
    静默 5 分钟"的文件在真正删除前又被写一次。

    ``delete=False`` 是 dry-run：走完全相同的锁与静默判定，只是不 unlink，
    因此"按这个策略真跑会删几个"的预测与实际执行严格一致。
    """
    target = Path(path)
    lock = _path_lock(target)
    if not lock.acquire(blocking=False):
        return REMOVE_LOCKED
    try:
        # trace 已经不在了：没有必要为它新建一把锁文件，直接如实返回。
        if not target.exists():
            return REMOVE_MISSING
        try:
            # 跨进程锁必须**覆盖 stat 与 unlink**：只在锁内 stat、锁外 unlink 的话，
            # 别的进程可以在两者之间开始追加，删除它就又变成腰斩 trace 了。
            with _cross_process_trace_lock(target, timeout=0):
                return _unlink_if_expired(
                    target,
                    min_age_seconds=min_age_seconds,
                    now=now,
                    delete=delete,
                )
        except TraceLockTimeout:
            return REMOVE_LOCKED
        except OSError:
            return REMOVE_FAILED
    finally:
        lock.release()


def _unlink_if_expired(
    target: Path,
    *,
    min_age_seconds: float | None,
    now: float | None,
    delete: bool,
) -> str:
    """在当前持有的两级锁内做静默判定并（可选）删除，返回结果码。"""
    try:
        stat = target.stat()
    except FileNotFoundError:
        return REMOVE_MISSING
    except OSError:
        return REMOVE_FAILED
    age_limit = purge_min_age_seconds() if min_age_seconds is None else min_age_seconds
    current = time.time() if now is None else now
    if age_limit > 0 and current - stat.st_mtime < age_limit:
        return REMOVE_RECENT
    if not delete:
        return REMOVE_DELETED
    try:
        target.unlink()
    except FileNotFoundError:
        return REMOVE_MISSING
    except OSError:
        return REMOVE_FAILED
    return REMOVE_DELETED


def remove_trace_file(
    path: Path | str,
    *,
    min_age_seconds: float | None = None,
    now: float | None = None,
) -> bool:
    """retention 清理用的协同删除：只有真的删掉了才返回 True。

    跳过（正在被追加 / 仍在静默窗口内 / 文件已不存在）与失败都返回 False，
    细节原因见 ``_remove_trace_file`` 的结果码。
    """
    return remove_trace_file_detail(
        path, min_age_seconds=min_age_seconds, now=now
    ) == REMOVE_DELETED


def remove_trace_file_detail(
    path: Path | str,
    *,
    min_age_seconds: float | None = None,
    now: float | None = None,
    delete: bool = True,
) -> str:
    """``remove_trace_file`` 的结果码版本，供清理统计区分"跳过"与"失败"。

    返回 ``deleted | recent | locked | missing | failed`` 之一。调用方据此把
    "本轮不该删"（recent/locked/missing）与真正的删除失败分开计数——混在一起
    会让 /health 把正常工作读成 degraded。

    ``delete=False`` 走同一套判定但不真的删，用于 dry-run 的精确预测。
    """
    reason = _remove_trace_file(
        path, min_age_seconds=min_age_seconds, now=now, delete=delete
    )
    if reason == REMOVE_FAILED:
        logger.warning("trace 文件删除失败: {}", type(OSError).__name__)
    return reason


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if _USAGE_KEY_RE.match(normalized):
        return False
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def _redact_value(key: str | None, value: Any) -> Any:
    """递归脱敏。

    - 键命中 ``_SECRET_KEY_PARTS`` 的值整体替换为 ``[redacted]``；数值型
      token 用量（``input_tokens=12`` 之类）保留，否则可观测性会被自己的
      脱敏毁掉。
    - 任意字符串里出现 ``sk-...`` 形态的密钥也替换。
    """
    if key is not None and _is_secret_key(key) and value is not None:
        if isinstance(value, (int, float, bool)):
            return value
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): _redact_value(str(item_key), item)
                for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        return REDACTED
    return value


def _collect_strings(value: Any, out: list[tuple[Any, Any, str]] | None = None
                     ) -> list[tuple[Any, Any, str]]:
    """递归收集可变的字符串槽位 ``(父容器, 键/下标, 字符串)``。"""
    if out is None:
        out = []
    if isinstance(value, dict):
        for item_key, item in value.items():
            if isinstance(item, str):
                out.append((value, item_key, item))
            else:
                _collect_strings(item, out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                out.append((value, index, item))
            else:
                _collect_strings(item, out)
    return out


def _dump(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, default=str).encode("utf-8")


def _shrink_to_limit(record: dict[str, Any]) -> bytes:
    """把序列化后的记录压到 ``MAX_RECORD_BYTES`` 以内。

    做法：标记 ``truncated``，反复把最长的字符串字段截短并追加
    ``TRUNCATION_MARKER``，最后兜底为最小记录。返回不带换行的字节串。
    """
    record["truncated"] = True
    for _ in range(32):
        data = _dump(record)
        excess = len(data) - MAX_RECORD_BYTES
        if excess <= 0:
            return data
        candidates = [
            item for item in _collect_strings(record)
            if item[2] and not item[2].endswith(TRUNCATION_MARKER)
        ]
        if not candidates:
            break
        parent, key, value = max(candidates, key=lambda item: len(item[2]))
        keep = max(0, len(value) - excess - len(TRUNCATION_MARKER))
        parent[key] = value[:keep] + TRUNCATION_MARKER
    # 兜底：结构本身过大（键极多）时只保留最小可归因记录。
    minimal = {
        "ts": record.get("ts"),
        "run_id": record.get("run_id"),
        "event": record.get("event"),
        "truncated": True,
        "error": "record_too_large",
    }
    data = _dump(minimal)
    if len(data) <= MAX_RECORD_BYTES:
        return data
    return _dump({"run_id": record.get("run_id"), "event": "trace_error",
                  "truncated": True})[:MAX_RECORD_BYTES]


def _prepare_line(record: dict[str, Any]) -> bytes:
    """脱敏 + 序列化 + 单条限长，返回可直接写入的一行（含换行）。"""
    payload = _redact_value(None, record)
    data = _dump(payload)
    if len(data) > MAX_RECORD_BYTES:
        data = _shrink_to_limit(payload)
    return data + b"\n"


def _truncation_marker(run_id: str, limit: int) -> bytes:
    record = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "run_id": run_id,
        "event": "trace_truncated",
        "truncated": True,
        "limit_bytes": limit,
    }
    return _dump(record) + b"\n"


def _remember_truncated(run_id: str) -> None:
    _TRUNCATED_RUNS[run_id] = True
    if len(_TRUNCATED_RUNS) > _MAX_TRUNCATED_RUNS:
        _TRUNCATED_RUNS.pop(next(iter(_TRUNCATED_RUNS)))


def _already_truncated(path: Path) -> bool:
    """文件尾部是否已经有一条 ``trace_truncated`` 标记。

    跨进程场景下"谁先触顶"只有最先拿到锁的进程知道；``_TRUNCATED_RUNS`` 是
    进程内缓存，别的 worker 看不到。触顶时读一次尾部（<=4KB），已经标记过就
    不再追加第二条，否则每个进程都会在文件末尾重复写一个标记。
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4096), os.SEEK_SET)
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return False
    try:
        return json.loads(lines[-1]).get("event") == "trace_truncated"
    except json.JSONDecodeError:
        return False


def _write_record(run_id: str, record: dict[str, Any]) -> None:
    """在锁内完成“脱敏/限长 → 配额检查 → 追加写入”。

    两级锁：进程内 ``_path_lock`` 串行化线程，跨进程 ``<run>.jsonl.lock``
    串行化 worker/脚本。两级都拿到才读写文件大小并追加——配额判断必须发生在
    互斥区内，否则两个进程会各自读到旧大小、同时越过 ``MAX_RUN_BYTES``。

    :class:`TraceLockTimeout` 会向上抛（由 ``trace_event`` 统一计入
    ``failures`` 并告警）：锁等不到时必须让"这条事件没写进去"可见，不能悄悄
    丢；其它异常仍在内部吞掉，保证观测故障不中断诊断。
    """
    path = TRACE_DIR / f"{_safe_run_id(run_id)}.jsonl"
    try:
        line = _prepare_line(record)
    except Exception as exc:  # noqa: BLE001 - 序列化失败同属观测失败
        _record_failure(exc)
        return

    with _path_lock(path):
        if run_id in _TRUNCATED_RUNS:
            _record_drop()
            return
        try:
            # 目录由加锁本身保证存在（锁文件与 trace 同目录，冷路径里会建目录）。
            with _cross_process_trace_lock(path):
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    size = 0
                if size + len(line) > MAX_RUN_BYTES:
                    _remember_truncated(run_id)
                    # 触发超限的这条事件本身被丢弃，计入 dropped；标记每条 run
                    # 只写一次（跨进程已写过的就不再重复）。
                    if not _already_truncated(path):
                        marker = _truncation_marker(run_id, MAX_RUN_BYTES)
                        with open(path, "a", encoding="utf-8", newline="\n") as handle:
                            handle.write(marker.decode("utf-8"))
                        _record_write()
                    _record_drop()
                    return
                with open(path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line.decode("utf-8"))
                _record_write()
        except TraceLockTimeout:
            raise
        except Exception as exc:  # noqa: BLE001 - 观测不能中断诊断
            _record_failure(exc)


def trace_event(run_id: str, event: str, **fields: Any) -> None:
    """追加一条 trace 事件；失败只计数/告警，绝不向调用方抛异常。

    函数签名保持不变：库内大量调用方依赖 ``trace_event(run_id, event, **fields)``。
    """
    if not run_id:
        _record_drop()
        return
    # 固定字段由服务端持有：调用方误传同名键时丢弃，避免覆盖 run_id/event。
    safe_fields = {key: value for key, value in fields.items()
                   if key not in _FIXED_RECORD_FIELDS}
    record = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "run_id": run_id,
        "event": event,
        **safe_fields,
    }
    try:
        _write_record(run_id, record)
    except Exception as exc:  # noqa: BLE001 - 最后一道防线
        _record_failure(exc)


def trace_envelope(run_id: str, envelope: dict[str, Any]) -> None:
    """写入 ``normalize_event`` 产出的统一信封。

    记录中保留 ``seq`` / ``event_id`` / ``session_id`` / ``execution_mode`` /
    ``schema_version``，业务字段放在 ``payload`` 下（同时保留扁平兼容字段），
    便于 trace 与 SSE/回放按同一份序号对齐。

    不带 ``**fields`` 展开，避免与「trace_event 不得裸展开外来字典」的
    root-cause 守卫冲突；字段在这里逐个显式挑选。
    """
    if not run_id:
        _record_drop()
        return
    if not isinstance(envelope, dict):
        _record_failure(TypeError("envelope 必须是 dict"))
        return
    try:
        payload = envelope.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        record: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "run_id": run_id,
            "event": str(envelope.get("type") or "event"),
        }
        for key in ("schema_version", "event_id", "seq", "session_id", "execution_mode"):
            if envelope.get(key) is not None:
                record[key] = envelope[key]
        if envelope.get("ts"):
            record["envelope_ts"] = envelope["ts"]
        record["payload"] = payload
        _write_record(run_id, record)
    except Exception as exc:  # noqa: BLE001 - 观测不能中断诊断
        _record_failure(exc)
