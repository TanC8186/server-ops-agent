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


def knowledge_tools(*, retriever: Any = None) -> list:
    """返回可加入 ``RunTools`` 的内置工具列表（当前只有知识检索）。"""
    if not knowledge_tool_enabled():
        return []
    return [create_knowledge_tool(retriever=retriever)]
