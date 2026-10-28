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


def need_principal_scope(principal_id: str) -> bool:
    """是否要按 principal 收窄检索范围（G6 P1-2 诊断路径）。

    判据只有一条：**拿到 principal 就启用**。早先版本为了"保持 dev 行为不变"
    在 single+dev 下返回 False，结果是同一个人在两份实现里看到不同集合——
    ``/api/chat`` 一直传 principal（``_scoped_retriever``），诊断却退回"只看
    active revision"，于是别人 owner 的文档、过期文档在问答里看不见、在诊断
    里能当证据。那是真实的越权面，不是兼容性。

    租户语义仍只从 ``knowledge_store._tenant_mode()`` 读，不在这里复制；传空串
    表示没有身份（旧接线/脚本），保持"只看 active revision"的旧行为。
    """
    return bool(str(principal_id or "").strip())


def local_only_retriever(*, principal_id: str = "", filters=None):
    """本地诊断模式专用的检索器：embedding 只允许本地，绝不触达远端（A03）。

    为什么不能复用默认检索器：默认检索器绑定进程级 ``vector_store_manager``，
    它的 embedding 函数在首次初始化时定型。若配置是远端 provider，本地模式跑一次
    检索就会复用**远端** embedding 客户端（对抗复核实测到远端工厂被调用）。
    这里用一个独立的 manager 实例，只注入 ``LocalOnlyEmbeddings``，并**不修改**
    全局配置或全局单例——云端模式与知识库管理页完全不受影响。

    principal/filters 只在拿到 principal 时下传；空串表示没有身份（脚本/旧接线），
    此时保持"只看 active revision"的旧行为。
    """
    from app.services.vector_store_manager import create_local_vector_store_manager

    store = create_local_vector_store_manager()
    if need_principal_scope(principal_id):
        return knowledge_service.VectorStoreRetriever(
            store, principal_id=principal_id, filters=filters,
        )
    return knowledge_service.VectorStoreRetriever(store=store)


def knowledge_tools(*, retriever: Any = None, execution_mode: str = "",
                    principal_id: str = "", filters=None) -> list:
    """返回可加入 ``RunTools`` 的内置工具列表（当前只有知识检索）。

    ``execution_mode == "local"`` 时注入**只允许本地 embedding** 的检索器；其余
    模式默认仍由工具在调用时解析全局检索器。两条路径都不改全局配置来“切换模式”。

    诊断路径过去完全不传 principal，于是 ``retrieve_knowledge`` 可以命中调用方
    无权读的文档；这里把同一个 principal 交给 chat 用的 ``VectorStoreRetriever``
    可见性规则，不另造过滤器。只有**没有身份**（空 principal）时才保持
    ``retriever=None`` 的旧接线。
    """
    if not knowledge_tool_enabled():
        return []
    if retriever is None and execution_mode == "local":
        retriever = local_only_retriever(principal_id=principal_id, filters=filters)
    elif retriever is None and need_principal_scope(principal_id):
        retriever = knowledge_service.VectorStoreRetriever(
            principal_id=principal_id, filters=filters,
        )
    return [create_knowledge_tool(retriever=retriever)]
