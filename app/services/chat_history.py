"""知识问答会话历史（RAG-09）。

旧实现的两个真实问题：

1. ``RagAgentService.get_session_history`` 把 ``checkpointer.get()`` 的返回值当
   ``tuple[0]`` 读，当前安装版本返回 dict，于是抛 TypeError 后被 ``except``
   吞掉并返回空列表——历史永远是空的，而且错误不可见。
2. 历史没有任何边界：没有 TTL、没有条数/token 上限、没有角色与工具调用的配对，
   长会话会把整个上下文塞回模型。

这里的实现只做可离线验证的部分：

- 每轮问答记录 ``role`` / ``content`` / ``citations`` / ``retrieval_status``；
- 只保留最近 ``max_turns`` 条且不超过 ``max_chars``，按整轮裁剪（不把工具调用与
  其回复拆开）；
- 会话有 TTL，过期后读取为空并清空磁盘记录；
- 落盘到 JSON（原子替换），进程重启后可恢复；``session_id`` 做文件名清洗，
  不能借路径逃逸。

它不承诺“多用户鉴权”：归属由 HTTP 层的 ``X-API-Key`` 与调用方会话决定。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

#: 默认会话 TTL（秒）：12 小时。
DEFAULT_TTL_SECONDS = 12 * 3600
#: 单会话最多保留的轮数（一问一答算一轮）。
DEFAULT_MAX_TURNS = 40
#: 单会话历史的最大字符数（用于裁剪上下文预算）。
DEFAULT_MAX_CHARS = 12000

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

_LOCK = threading.Lock()


def _history_dir() -> Path:
    """历史目录；``CHAT_HISTORY_DIR`` 可覆盖（测试/部署）。"""
    override = os.environ.get("CHAT_HISTORY_DIR", "").strip()
    base = Path(override) if override else Path(__file__).resolve().parents[2] / "data" / "chat"
    return base


def _safe_session(session_id: str) -> str:
    cleaned = _SAFE_ID.sub("_", str(session_id or "")).strip("._")
    return (cleaned or "default")[:80]


def _path(session_id: str) -> Path:
    return _history_dir() / f"{_safe_session(session_id)}.json"


class ChatHistoryStore:
    """按会话保存问答记录（有界、按时效、可持久化）。"""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_turns: int = DEFAULT_MAX_TURNS,
                 max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_turns = int(max_turns)
        self.max_chars = int(max_chars)

    # ── 读写 ──────────────────────────────────────────────────

    def load(self, session_id: str) -> list[dict[str, Any]]:
        """读取未过期的历史；过期或损坏返回空列表（并清理磁盘）。"""
        path = _path(session_id)
        with _LOCK:
            payload = self._read_file(path)
        if payload is None:
            return []
        updated = float(payload.get("updated_at") or 0)
        if self.ttl_seconds > 0 and time.time() - updated > self.ttl_seconds:
            logger.info(f"会话历史已过期，已清空: {_safe_session(session_id)}")
            self.clear(session_id)
            return []
        messages = payload.get("messages")
        return [item for item in messages if isinstance(item, dict)] if isinstance(messages, list) else []

    def append_turn(self, session_id: str, *, question: str, answer: str,
                    citations: list[dict[str, Any]] | None = None,
                    retrieval_status: str = "",
                    outcome: str = "") -> list[dict[str, Any]]:
        """追加一轮问答，返回裁剪后的完整历史。"""
        messages = self.load(session_id)
        messages.append({"role": "user", "content": str(question or "")})
        messages.append({
            "role": "assistant",
            "content": str(answer or ""),
            "citations": list(citations or []),
            "retrieval_status": retrieval_status,
            "outcome": outcome,
        })
        messages = self._trim(messages)
        self._write(session_id, messages)
        return messages

    def clear(self, session_id: str) -> bool:
        path = _path(session_id)
        with _LOCK:
            try:
                path.unlink(missing_ok=True)
                return True
            except OSError as exc:  # 清理失败要可见，不能静默返回 True
                logger.error(f"清空会话历史失败: {path}: {exc}")
                return False

    def count(self, session_id: str) -> int:
        return len(self.load(session_id))

    # ── 内部 ──────────────────────────────────────────────────

    def _trim(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """整轮裁剪：先按条数，再按字符预算，始终保持“问→答”配对。"""
        # 每两条算一轮；奇数时从最早的孤立条目开始丢。
        if len(messages) % 2:
            messages = messages[1:]
        keep = max(2, self.max_turns * 2)
        if len(messages) > keep:
            messages = messages[-keep:]
        total = sum(len(str(item.get("content") or "")) for item in messages)
        while total > self.max_chars and len(messages) > 2:
            removed = messages[:2]
            messages = messages[2:]
            total -= sum(len(str(item.get("content") or "")) for item in removed)
        return messages

    def _read_file(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"会话历史损坏，已忽略: {path.name}: {type(exc).__name__}")
            return None
        return payload if isinstance(payload, dict) else None

    def _write(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        path = _path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": _safe_session(session_id),
            "updated_at": time.time(),
            "messages": messages,
        }
        temp = path.with_suffix(".json.tmp")
        with _LOCK:
            try:
                temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                encoding="utf-8")
                os.replace(temp, path)
            except OSError as exc:
                # 历史写入失败不能让问答失败，但必须可观测。
                logger.error(f"写入会话历史失败: {path}: {exc}")


#: 全局单例。
chat_history = ChatHistoryStore()
