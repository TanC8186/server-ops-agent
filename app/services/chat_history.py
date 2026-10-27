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

它不承诺“多用户鉴权”：归属由 HTTP 层的 ``X-API-Key`` 与调用方会话决定；
principal 归属由 ``app.core.principal`` 的 ACL 侧表记录并在读写前校验。

**历史恢复 ≠ 多轮上下文**（G2.3 / G3 任务 3）：``load()`` 返回的是给界面看的
完整历史（最多 200 条），直接塞回模型会让长会话挤掉证据。回答时只使用
:meth:`ChatHistoryStore.recent_turns` 给出的有界窗口：条数与字符数双上限，
超预算时**从最旧的整轮开始丢**，绝不截成半轮。
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
#: 回灌模型的有界历史窗口：默认 6 轮（问+答）/ 2000 字符。
DEFAULT_CONTEXT_TURNS = 6
DEFAULT_CONTEXT_CHARS = 2000

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

#: 可重入：``claim``/``append_turn`` 会在持锁期间调用同样加锁的 ``_write``。
_LOCK = threading.RLock()


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


#: 单条内容被预算截断时追加的标记（与 observability 的约定一致）。
TRUNCATION_MARKER = "…[truncated]"


def _shrink_block(block: list[tuple[str, str]], budget: int) -> list[tuple[str, str]]:
    """把一整轮（问+答）压进 ``budget``；问答角色都保留。

    只有“最新一轮都放不下”时才会走到这里。策略：答案占 2/3 预算、问题占 1/3，
    各自用截断标记收尾；宁可短也不能丢角色（丢了角色模型会误判谁在说话）。
    """
    if budget <= 0:
        return []
    q_text = block[0][1] if block else ""
    answer = block[1][1] if len(block) > 1 else ""
    if not q_text and not answer:
        return []
    if len(q_text) + len(answer) <= budget:
        return block
    answer_budget = max(1, (budget * 2) // 3)
    question_budget = max(0, budget - answer_budget)
    trimmed: list[tuple[str, str]] = []
    if q_text and question_budget > len(TRUNCATION_MARKER):
        trimmed.append(
            ("user", q_text[: question_budget - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER)
        )
    if answer and answer_budget > len(TRUNCATION_MARKER):
        trimmed.append(
            ("assistant", answer[: answer_budget - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER)
        )
    return trimmed


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
        # 归属必须随每次追加一起保留：``_write`` 是整文件替换，漏带 owner 会
        # 让下一次读取判定“没有 owner”，等于悄悄注销归属。
        self._write(session_id, messages, owner=self.owner(session_id))
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

    def owner(self, session_id: str) -> str | None:
        """会话所属 principal（建会话时写入）；没有记录返回 ``None``。"""
        with _LOCK:
            payload = self._read_file(_path(session_id))
        if not payload:
            return None
        owner = payload.get("owner")
        return owner if isinstance(owner, str) and owner.strip() else None

    def claim(self, session_id: str, principal_id: str) -> bool:
        """记录会话归属（首次写入时调用；已有会话不覆盖）。

        不覆盖是刻意的：先到先得的归属不能被后续请求“抢走”。属于同一
        principal 的重复调用是幂等的。
        """
        principal_id = str(principal_id or "").strip()
        if not principal_id:
            return False
        with _LOCK:
            path = _path(session_id)
            payload = self._read_file(path) or {}
            existing = payload.get("owner")
            if isinstance(existing, str) and existing.strip():
                return existing == principal_id
            messages = payload.get("messages")
            self._write(session_id, messages if isinstance(messages, list) else [],
                        owner=principal_id)
            return True

    def recent_turns(self, session_id: str, *, max_turns: int,
                     max_chars: int) -> list[tuple[str, str]]:
        """有界历史窗口：返回最近的 ``(role, content)`` 列表，最旧优先丢弃。

        边界规则（任何一条都不能放宽）：

        - 只取最后 ``max_turns`` 轮（一问一答算一轮）；
        - 命中字符预算 ``max_chars`` 时**从最旧的整轮**开始丢，不截半轮；
        - 单条内容本身超预算时截断该条内容（这是唯一允许的“半个”操作），
          并在截断处加 ``…[truncated]``，保证拼出来的 prompt 有明确上限；
        - 空会话、过期会话、损坏文件一律返回 ``[]``，不返回占位内容。
        """
        turns = max(0, int(max_turns))
        budget = max(0, int(max_chars))
        if turns <= 0 or budget <= 0:
            return []
        messages = self.load(session_id)
        if not messages:
            return []
        # 只保留成对的“问→答”；孤立的用户提问（上次回答失败/中断）不能当成
        # 上下文，否则模型会把没被回答的问题当既定事实。
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for index in range(0, len(messages) - 1, 2):
            question, answer = messages[index], messages[index + 1]
            if str(question.get("role")) != "user" or str(answer.get("role")) != "assistant":
                continue
            pairs.append((question, answer))
        pairs = pairs[-turns:] if turns else []

        selected: list[tuple[str, str]] = []
        total = 0
        for question, answer in reversed(pairs):
            block = [
                ("user", str(question.get("content") or "").strip()),
                ("assistant", str(answer.get("content") or "").strip()),
            ]
            size = sum(len(text) for _role, text in block)
            if total + size > budget:
                # 整轮都放不进去：如果窗口还是空的，就把这一轮压到预算内；
                # 否则从最旧的整轮开始丢，直接停止。
                if selected:
                    break
                block = _shrink_block(block, budget)
                size = sum(len(text) for _role, text in block)
                if not size:
                    break
            selected = block + selected
            total += size
        return [(role, text) for role, text in selected if text]

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

    def _write(self, session_id: str, messages: list[dict[str, Any]],
               owner: str | None = None) -> None:
        path = _path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": _safe_session(session_id),
            "updated_at": time.time(),
            "messages": messages,
        }
        if owner:
            payload["owner"] = str(owner)
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
