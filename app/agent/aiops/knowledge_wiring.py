"""把内置知识检索工具接入两个诊断运行器（M4 / RAG-10）。

运行器的工具列表来自 MCP；``retrieve_knowledge`` 是进程内工具，需要显式加入。
这里只做接线，不改运行器自身的编排逻辑：

- 知识检索结果进入证据列表，但 ``kind="knowledge"``、``live=False``，
  质量评估不会把它当“当前观测”（见 ``app.core.run_status``）。
- 工具不可用/超时不会中断诊断：``RunTools.call`` 已把异常转成证据条目。
"""

from __future__ import annotations

from typing import Any

from app.config import config
from app.tools.knowledge_retrieval_tool import create_knowledge_tool


def knowledge_tool_enabled() -> bool:
    """知识检索是否可接入。

    Milvus 未配置时也允许接入：工具自身会返回 ``unavailable``，比“悄悄没有这个
    能力”更可解释。仅在显式关闭时跳过（``RAG_TOOL_ENABLED=0``）。
    """
    return bool(getattr(config, "rag_tool_enabled", True))


def knowledge_tools(*, retriever: Any = None, execution_mode: str = "") -> list:
    """返回可加入 ``RunTools`` 的内置工具列表（当前只有知识检索）。

    ``execution_mode`` 只用于说明边界（A03）：两个模式用的是**同一份**服务端
    向量库，因此差别不在于“远端/本地”，而在于**不会因为模式切换去改全局配置**。
    本地模式的承诺是“不调用远端 LLM/embedding”；检索本身由 Milvus 完成、不会
    触发 embedding 调用，所以这里注入同一检索器不会破坏该承诺——但 embedding
    的远端回退由 ``create_embedding_service(strict_local=True)`` 单独保证。
    """
    if not knowledge_tool_enabled():
        return []
    return [create_knowledge_tool(retriever=retriever)]
