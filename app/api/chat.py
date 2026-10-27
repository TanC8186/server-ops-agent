"""对话接口：知识问答固定流水线（RAG-01 / RAG-02）+ 追问上下文（M4 / RAG-10）。

与旧实现的差别：

- 旧实现把 ``create_agent`` 交给模型自己决定是否检索，并且可以调用全部 MCP
  运维工具；知识问答因此既不可核验、也没有权限边界。
- 现在固定 ``retrieve → 门控 → answer``（``app.services.knowledge_service``），
  知识模式没有运维工具权限，事件带服务端分配的引用 ID。
- ``run_id`` 是可选追问：服务端校验该 run 属于**当前会话**后才返回受控摘要，
  否则 ``run_context=null`` 并说明原因，绝不串到别的运行。
- **principal 归属（G3-3）**：会话历史与追问的 run 都带 principal 归属。
  ``load_run_context`` 现在同时校验 session 与 owner；会话历史在首次写入时
  ``claim`` 到当前 principal，读/清前先 ``authorize``。dev 模式所有请求映射到
  同一 principal，但检查照旧执行（拒绝路径可测）。
- **有界多轮上下文（G3 任务 3 / G2.3）**：历史不再只是展示用的。回答时会取
  ``recent_turns(max_turns, max_chars)`` 的有界窗口交给
  ``answer_question(history=...)``；条数与字符数双上限，最旧的整轮先丢。

事件形状（SSE，``data`` 为 JSON）：

    {"type": "retrieval", "status": "hit|miss|unavailable|timeout|invalid_index",
     "reason": "...", "retrieval": {...}}
    {"type": "content", "data": "增量 Markdown"}
    {"type": "done", "outcome": "answered|insufficient_evidence|failed|cancelled",
     "retrieval_status": "...", "content": "...", "citations": [...],
     "removed_citations": [...], "run_context": {...}|null, "tool_calls": []}
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Header, HTTPException
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.config import config
from app.core import principal as acl
from app.core.llm_factory import create_chat_model
from app.core.run_context import (
    load_run_context_async,
    render_run_context,
)
from app.models.request import ChatRequest, ClearRequest
from app.models.response import ApiResponse, SessionInfoResponse
from app.services import knowledge_service
from app.services.chat_history import chat_history

router = APIRouter()

#: 知识问答的会话历史条数上限（只用于展示，不参与回答生成）。
MAX_HISTORY_MESSAGES = 200

#: 回灌模型的有界历史窗口默认值（可通过 ``Settings`` 覆盖）。
DEFAULT_CONTEXT_TURNS = 6
DEFAULT_CONTEXT_CHARS = 2000


def _context_window() -> tuple[int, int]:
    """读取有界历史窗口配置；非法值回退到默认，绝不放大到“全量历史”。

    配置来源优先级：进程环境（``CHAT_CONTEXT_MAX_TURNS`` /
    ``CHAT_CONTEXT_MAX_CHARS``）→ ``Settings`` 同名属性（若已定义）→ 默认值。
    用 ``getattr`` 读 Settings，是为了不要求配置类先行升级。
    """
    import os

    def read(env_name: str, attr: str, default: int) -> int:
        for raw in (os.environ.get(env_name, ""), getattr(config, attr, "")):
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                value = int(float(text))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return default

    return (
        read("CHAT_CONTEXT_MAX_TURNS", "chat_context_max_turns", DEFAULT_CONTEXT_TURNS),
        read("CHAT_CONTEXT_MAX_CHARS", "chat_context_max_chars", DEFAULT_CONTEXT_CHARS),
    )


def _api_key(x_api_key: str | None) -> str:
    return x_api_key or ""


def _principal_for(x_api_key: str | None) -> acl.Principal:
    """解析调用方；认证失败映射成 401/403，不是 500。"""
    try:
        return acl.current_principal(None, _api_key(x_api_key))
    except acl.PermissionDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


async def _load_context_async(run_id: str, session_id: str,
                              principal: acl.Principal) -> dict[str, Any] | None:
    """读追问上下文：会话 + principal 双重归属校验（见 ``run_context``）。"""
    return await load_run_context_async(run_id, session_id, principal.principal_id)


class _ChatModel:
    """把 ``create_chat_model`` 适配成 knowledge_service 需要的 ``ainvoke``。

    知识问答只调用一次模型（检索上下文已确定），因此这里不做流式；SSE 的
    ``content`` 事件由服务端按段落切片产生。
    """

    def __init__(self) -> None:
        self._model = None

    def _get(self):
        if self._model is None:
            self._model = create_chat_model(
                temperature=0.2, streaming=False, max_tokens=config.llm_max_tokens
            )
        return self._model

    async def ainvoke(self, messages: list) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage

        prepared = []
        for role, text in messages:
            cls = SystemMessage if role == "system" else HumanMessage
            prepared.append(cls(content=text))
        return await self._get().ainvoke(prepared)


_chat_model = _ChatModel()


def _retriever():
    """生产检索器：VectorStoreManager（真实 Milvus，失败时给出明确状态）。"""
    return knowledge_service.VectorStoreRetriever()


def _event_frame(event: dict[str, Any], run_id: str, session_id: str,
                 seq: int) -> dict[str, str]:
    """统一 SSE 帧：保留字段由服务端补齐，业务字段同时平铺。"""
    payload = {
        "schema_version": "1",
        "event_id": f"{run_id}:{seq}",
        "seq": seq,
        "run_id": run_id,
        "session_id": session_id,
        "type": event.get("type"),
        "ts": event.get("ts"),
    }
    for key, value in event.items():
        if key in {"type", "session_id", "run_id"}:
            continue
        payload[key] = value
    return {"event": "message", "data": json.dumps(payload, ensure_ascii=False, default=str)}


def _question_with_run_context(question: str, context: dict[str, Any] | None) -> str:
    rendered = render_run_context(context)
    return f"{rendered}\n\n用户追问：{question}" if rendered else question


def _bound_history(session_id: str) -> list[tuple[str, str]]:
    """取有界历史窗口；失败时返回空窗口（历史不是回答的前提）。"""
    turns, chars = _context_window()
    try:
        return chat_history.recent_turns(session_id, max_turns=turns, max_chars=chars)
    except Exception as exc:  # noqa: BLE001 - 历史读取失败不能阻断问答
        logger.warning(f"读取有界会话历史失败（本次不带历史）: {type(exc).__name__}: {exc}")
        return []


def _supports_history() -> bool:
    """``answer_question`` 是否接受 ``history`` 关键字（兼容尚未升级的实现）。"""
    import inspect

    try:
        parameters = inspect.signature(knowledge_service.answer_question).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return "history" in parameters


def _history_warning(session_id: str) -> None:
    """结构化告警：说明本次降级为“不带历史”，便于运维一眼定位。"""
    logger.warning(
        "knowledge_service.answer_question 不接受 history 参数，"
        "本次按旧行为回答（多轮上下文未使用）: "
        + json.dumps(
            {"event": "chat_history_not_supported", "session_id": session_id},
            ensure_ascii=False,
        )
    )


async def _answer(question: str, session_id: str, run_id: str = "",
                  run_context: dict[str, Any] | None = None,
                  history: list[tuple[str, str]] | None = None
                  ) -> AsyncGenerator[dict[str, Any], None]:
    """跑一次知识问答；run_context 追加在问题里但会单独回报。

    ``history`` 是有界的历史窗口（role/content 对）。它只作为**对话上下文**
    传给 ``knowledge_service``，绝不能当成检索证据；实现里若还没有这个形参，
    就降级到旧行为并打一条结构化告警，绝不因为参数缺失而失败。
    """
    prompt = _question_with_run_context(question, run_context)
    kwargs: dict[str, Any] = {
        "retriever": _retriever(), "model": _chat_model,
        "run_id": run_id or uuid.uuid4().hex,
    }
    if history:
        if _supports_history():
            kwargs["history"] = list(history)
        else:
            _history_warning(session_id)
    async for event in knowledge_service.answer_question(prompt, session_id, **kwargs):
        yield event


def _claim_session(session_id: str, principal: acl.Principal) -> None:
    """把会话归属记到当前 principal（先到先得，不覆盖）。

    已属于别的 principal 时**拒绝本次写入**（403），而不是只在日志里告警：
    否则知道别人 session_id 的人可以把自己的提问混进别人的历史。

    归属记录本身失败（磁盘/权限问题）只告警、不阻断问答——那是我方故障，
    不是调用方的越权，不该把用户关在门外。
    """
    owner = chat_history.owner(session_id) or acl.resource_owner("session", session_id)
    try:
        acl.authorize(principal, owner, action="session:write")
    except acl.PermissionDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    try:
        if not acl.claim_owner("session", session_id, principal.principal_id):
            # claim 失败意味着“已经属于别人”，但上面的 authorize 已放行过：只能
            # 发生在本进程外的并发写入，保守拒绝，避免两边交替写同一份历史。
            raise HTTPException(status_code=403, detail="会话已被其它 principal 占用")
        if not chat_history.claim(session_id, principal.principal_id):
            logger.warning(f"会话归属未能写入历史文件（已写入侧表）: {session_id}")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 归属记录失败不能变成 500
        logger.error(f"记录会话归属失败: {session_id}: {type(exc).__name__}: {exc}")


def _check_session(session_id: str, principal: acl.Principal, *, action: str) -> None:
    """读/清会话前的归属校验；不符时抛 403（不是 500，也不静默返回空）。"""
    owner = chat_history.owner(session_id) or acl.resource_owner("session", session_id)
    try:
        acl.authorize(principal, owner, action=action)
    except acl.PermissionDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


@router.post("/chat")
async def chat(request: ChatRequest,
               x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    """非流式对话：返回完整回答（内部仍走"检索→门控→回答"）。"""
    logger.info(f"[会话 {request.id}] 收到快速对话请求")
    principal = _principal_for(x_api_key)
    _claim_session(request.id, principal)

    run_context = None
    if request.run_id:
        run_context = await _load_context_async(request.run_id, request.id, principal)

    answer = ""
    citations: list[dict[str, Any]] = []
    retrieval_status = ""
    outcome = "failed"
    try:
        async for event in _answer(request.question, request.id, request.run_id or "",
                                   run_context, _bound_history(request.id)):
            if event["type"] == "retrieval":
                retrieval_status = str(event.get("status") or "")
            elif event["type"] == "done":
                answer = str(event.get("content") or "")
                citations = list(event.get("citations") or [])
                outcome = str(event.get("outcome") or "")
    except Exception as exc:  # noqa: BLE001 - 统一错误形状，不泄漏内部细节
        logger.error(f"对话接口错误: {exc}")
        raise HTTPException(status_code=500, detail="知识问答失败，请稍后重试") from exc

    chat_history.append_turn(
        request.id, question=request.question, answer=answer,
        citations=citations, retrieval_status=retrieval_status, outcome=outcome,
    )

    return {
        "code": 200,
        "message": "success",
        "data": {
            "success": outcome == "answered",
            "answer": answer,
            "errorMessage": None if outcome == "answered" else outcome,
            "citations": citations,
            "retrieval_status": retrieval_status,
            "run_context": run_context,
        },
    }


@router.post("/chat_stream")
async def chat_stream(request: ChatRequest,
                      x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    """流式对话：SSE 事件见模块文档。"""
    logger.info(f"[会话 {request.id}] 收到流式对话请求")
    principal = _principal_for(x_api_key)
    _claim_session(request.id, principal)

    async def event_generator():
        run_id = request.run_id or uuid.uuid4().hex
        seq = 0
        run_context = None
        context_note = ""
        history = _bound_history(request.id)
        if request.run_id:
            run_context = await _load_context_async(request.run_id, request.id, principal)
            if run_context is None:
                # 归属不符/不存在：明确说明，绝不退化成“随便找一次运行”。
                context_note = (
                    "追问的 run_id 不属于当前会话或不存在，本次只按知识库回答。"
                )
                seq += 1
                yield _event_frame(
                    {"type": "status", "stage": "run_context", "message": context_note},
                    run_id, request.id, seq,
                )

        terminal_sent = False
        try:
            async for event in _answer(request.question, request.id, request.run_id or "",
                                       run_context, history):
                seq += 1
                kind = event.get("type")
                if kind == "done":
                    terminal_sent = True
                    event = {
                        **event,
                        "run_context": run_context,
                        "tool_calls": [],
                    }
                    if context_note and not run_context:
                        event["reason"] = (
                            f"{context_note} {event.get('reason') or ''}"
                        ).strip()
                    # 只有拿到完整终态才落历史：中断的半截回答不进上下文。
                    chat_history.append_turn(
                        request.id, question=request.question,
                        answer=str(event.get("content") or ""),
                        citations=list(event.get("citations") or []),
                        retrieval_status=str(event.get("retrieval_status") or ""),
                        outcome=str(event.get("outcome") or ""),
                    )
                yield _event_frame(event, run_id, request.id, seq)
                if terminal_sent:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.error(f"流式对话接口错误: {exc}")
            if terminal_sent:
                return
            seq += 1
            yield _event_frame(
                {"type": "done", "outcome": "failed", "reason": "知识问答失败",
                 "content": "知识问答失败，请稍后重试。", "citations": [],
                 "removed_citations": [], "run_context": run_context, "tool_calls": []},
                run_id, request.id, seq,
            )

    return EventSourceResponse(event_generator())


@router.post("/chat/clear", response_model=ApiResponse)
async def clear_session(request: ClearRequest,
                        x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    """清空会话历史（只能清自己的会话）。"""
    principal = _principal_for(x_api_key)
    _check_session(request.session_id, principal, action="session:delete")
    try:
        success = chat_history.clear(request.session_id)
        logger.info(f"清空会话: {request.session_id}, 结果: {success}")
        return ApiResponse(
            status="success" if success else "error",
            message="会话已清空" if success else "清空会话失败",
            data=None,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"清空会话错误: {e}")
        raise HTTPException(status_code=500, detail="清空会话失败") from e


@router.get("/chat/session/{session_id}", response_model=SessionInfoResponse)
async def get_session_info(session_id: str,
                           x_api_key: str | None = Header(default=None,
                                                         alias="X-API-Key")
                           ) -> SessionInfoResponse:
    """查询会话历史（只能读自己的会话）。

    历史来自 ``chat_history``（有界、有 TTL、可持久化），每条包含 role/content
    与当时的引用、检索状态；不再依赖 checkpoint 的内部结构。归属不符返回 403，
    不是“空历史”——后者会让越权读取看起来像“这个会话没内容”。
    """
    principal = _principal_for(x_api_key)
    _check_session(session_id, principal, action="session:read")
    try:
        history = chat_history.load(session_id)
        return SessionInfoResponse(
            session_id=session_id,
            message_count=len(history),
            history=history[:MAX_HISTORY_MESSAGES],
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取会话信息错误: {e}")
        raise HTTPException(status_code=500, detail="读取会话历史失败") from e
