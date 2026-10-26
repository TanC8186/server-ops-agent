"""对话接口：知识问答固定流水线（RAG-01 / RAG-02）+ 追问上下文（M4 / RAG-10）。

与旧实现的差别：

- 旧实现把 ``create_agent`` 交给模型自己决定是否检索，并且可以调用全部 MCP
  运维工具；知识问答因此既不可核验、也没有权限边界。
- 现在固定 ``retrieve → 门控 → answer``（``app.services.knowledge_service``），
  知识模式没有运维工具权限，事件带服务端分配的引用 ID。
- ``run_id`` 是可选追问：服务端校验该 run 属于**当前会话**后才返回受控摘要，
  否则 ``run_context=null`` 并说明原因，绝不串到别的运行。

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

from fastapi import APIRouter, HTTPException
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.config import config
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


async def _answer(question: str, session_id: str, run_id: str = "",
                  run_context: dict[str, Any] | None = None
                  ) -> AsyncGenerator[dict[str, Any], None]:
    """跑一次知识问答；run_context 追加在问题里但会单独回报。"""
    prompt = _question_with_run_context(question, run_context)
    async for event in knowledge_service.answer_question(
        prompt, session_id, retriever=_retriever(), model=_chat_model,
        run_id=run_id or uuid.uuid4().hex,
    ):
        yield event


@router.post("/chat")
async def chat(request: ChatRequest):
    """非流式对话：返回完整回答（内部仍走"检索→门控→回答"）。"""
    logger.info(f"[会话 {request.id}] 收到快速对话请求")
    run_context = None
    if request.run_id:
        run_context = await load_run_context_async(request.run_id, request.id)

    answer = ""
    citations: list[dict[str, Any]] = []
    retrieval_status = ""
    outcome = "failed"
    try:
        async for event in _answer(request.question, request.id, request.run_id or "",
                                   run_context):
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
async def chat_stream(request: ChatRequest):
    """流式对话：SSE 事件见模块文档。"""
    logger.info(f"[会话 {request.id}] 收到流式对话请求")

    async def event_generator():
        run_id = request.run_id or uuid.uuid4().hex
        seq = 0
        run_context = None
        context_note = ""
        if request.run_id:
            run_context = await load_run_context_async(request.run_id, request.id)
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
                                       run_context):
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
async def clear_session(request: ClearRequest):
    """清空会话历史。"""
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
async def get_session_info(session_id: str) -> SessionInfoResponse:
    """查询会话历史。

    历史来自 ``chat_history``（有界、有 TTL、可持久化），每条包含 role/content
    与当时的引用、检索状态；不再依赖 checkpoint 的内部结构。
    """
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
