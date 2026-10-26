"""M4 / RAG-10：诊断运行里的受范围限制的知识检索。

两个真实缺口：

1. 新双模式诊断的工具白名单里没有 Milvus 知识检索，只有 ``retrieve_knowledge``
   的旧 LangChain 工具挂在 RAG Agent 上；
2. 即使拿到知识片段，也没有区分“当前观测”和“手册依据”，容易被写成
   “已确认此服务器发生了该故障”。

本模块提供一个**受范围限制**的知识检索工具：

- 只能读知识库，不绑定也不扩展现有目标，不接受 URL；
- 结果标记为 ``kind="knowledge"``、``live=False``，永远不会进入“当前观测”；
- 检索失败映射成 ``unavailable`` / ``empty``，不伪装成“无匹配”；
- 描述符与索引状态异常映射成 ``invalid_index``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field

from app.services import knowledge_service
from app.services.vector_store_manager import (
    VectorStoreInvalidIndex,
    VectorStoreUnavailable,
)

SCHEMA = {
    "query": (
        "str",
        Field(description="检索关键词或问题（≤400 字符），不要粘贴整段日志"),
    ),
}


class _KnowledgeArgs(BaseModel):
    query: str = Field(min_length=1, max_length=400, description="检索关键词或问题")


class KnowledgeRetrievalTool:
    """``RunTools`` 可调用对象：签名为 ``ainvoke({"query": ...})``。

    之所以不用 ``langchain_core.tools.tool``：运行器按 ``args_schema`` 做 JSON
    Schema 校验，并要求工具只读、可替换；这里保持最小契约，便于测试注入假
    retriever。
    """

    name = "retrieve_knowledge"
    description = (
        "在企业运维知识库中检索操作手册与排查步骤。返回内容属于**知识依据**，"
        "不代表当前主机的实际状态；不能据此断言某故障已经发生。"
    )
    args_schema = _KnowledgeArgs

    def __init__(self, retriever=None, *, timeout: float = 8.0) -> None:
        self._retriever = retriever
        self._timeout = float(timeout)

    def _retriever_or_default(self):
        return self._retriever or knowledge_service.VectorStoreRetriever()

    async def ainvoke(self, args: dict | None = None, **_kwargs: Any) -> dict[str, Any]:
        query = str((args or {}).get("query") or "").strip()
        if not query:
            return {"ok": False, "status": "error", "source": "manual",
                    "error": "query 不能为空"}
        started = time.monotonic()
        try:
            chunks = await asyncio.wait_for(
                asyncio.to_thread(self._retriever_or_default().search, query, 3),
                self._timeout,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "status": "timeout", "source": "unavailable",
                    "error": f"知识检索超过 {self._timeout:g}s"}
        except (knowledge_service.InvalidIndex, VectorStoreInvalidIndex) as exc:
            # 工具契约的 status 集合是 ok/empty/partial/error/timeout/unavailable/
            # unsupported；索引不可用归到 unavailable，并用 code 保留具体原因。
            return {"ok": False, "status": "unavailable", "source": "unavailable",
                    "code": "invalid_index", "error": str(exc)[:300]}
        except (knowledge_service.RetrievalUnavailable, VectorStoreUnavailable) as exc:
            return {"ok": False, "status": "unavailable", "source": "unavailable",
                    "error": str(exc)[:300]}
        except Exception as exc:  # noqa: BLE001 - 检索失败不能中断诊断
            return {"ok": False, "status": "error", "source": "unavailable",
                    "error": f"{type(exc).__name__}: {exc}"[:300]}

        if not chunks:
            # 零命中是 empty，不是 error；也不是“知识库不可用”。
            return {"ok": True, "status": "empty", "source": "manual",
                    "demo": False, "query": query, "chunks": [],
                    "duration_ms": round((time.monotonic() - started) * 1000)}

        return {
            "ok": True,
            "status": "ok",
            "source": "manual",
            "demo": False,
            "query": query,
            "kind": "knowledge",
            "chunks": [chunk.to_dict() for chunk in chunks],
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def create_knowledge_tool(*, retriever=None, timeout: float = 8.0) -> KnowledgeRetrievalTool:
    return KnowledgeRetrievalTool(retriever=retriever, timeout=timeout)
