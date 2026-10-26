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
from app.services import knowledge_service
from app.tools.knowledge_retrieval_tool import create_knowledge_tool


def knowledge_tool_enabled() -> bool:
    """知识检索是否可接入。

    Milvus 未配置时也允许接入：工具自身会返回 ``unavailable``，比“悄悄没有这个
    能力”更可解释。仅在显式关闭时跳过（``RAG_TOOL_ENABLED=0``）。
    """
    return bool(getattr(config, "rag_tool_enabled", True))


def local_only_retriever():
    """本地诊断模式专用的检索器：embedding 只允许本地，绝不触达远端（A03）。

    为什么不能复用默认检索器：默认检索器绑定进程级 ``vector_store_manager``，
    它的 embedding 函数在首次初始化时定型。若配置是远端 provider，本地模式跑一次
    检索就会复用**远端** embedding 客户端（对抗复核实测到远端工厂被调用）。
    这里用一个独立的 manager 实例，只注入 ``LocalOnlyEmbeddings``，并**不修改**
    全局配置或全局单例——云端模式与知识库管理页完全不受影响。
    """
    from app.services.vector_store_manager import create_local_vector_store_manager

    return knowledge_service.VectorStoreRetriever(store=create_local_vector_store_manager())


def knowledge_tools(*, retriever: Any = None, execution_mode: str = "") -> list:
    """返回可加入 ``RunTools`` 的内置工具列表（当前只有知识检索）。

    ``execution_mode == "local"`` 时注入**只允许本地 embedding** 的检索器；其余
    模式用默认检索器。两条路径都不改全局配置来“切换模式”。
    """
    if not knowledge_tool_enabled():
        return []
    if retriever is None and execution_mode == "local":
        retriever = local_only_retriever()
    return [create_knowledge_tool(retriever=retriever)]
