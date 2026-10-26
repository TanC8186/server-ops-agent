"""向量存储管理器 - 封装 Milvus VectorStore 操作（懒加载）。

三个真实缺陷在这里修掉（RAG-03 / RAG-04 / RAG-06）：

1. **初始化不能提前成功**：旧实现先置 ``_initialized=True`` 再尝试连接，
   连接失败也被记成“已初始化”，后续调用全部拿到空的 ``vector_store``
   并把故障伪装成“没有命中”。现在只有成功才置位；失败保留首次异常并在
   后续调用继续抛出。
2. **描述符必须在真实 I/O 边界校验**：collection 的向量维度/距离度量与
   ``EmbeddingDescriptor`` 必须一致。描述符文件缺失且 collection 已有数据时
   直接拒绝（不能拿一套没记录的向量空间当索引）；空 collection 首次建库时
   写入描述符。
3. **检索必须返回真实分数**：``similarity_search_with_score`` 是显式方法，
   失败时抛异常而不是返回 ``[]``；分数按 collection 的真实 metric 解释
   （COSINE/IP 越大越好，L2 是距离），不懂 metric 时标记为“分数不可用”，
   绝不伪造 1.0 相似度。
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import List, Sequence

from langchain_core.documents import Document
from langchain_milvus import Milvus
from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.services.vector_embedding_service import (
    DescriptorMismatch,
    assert_descriptor_matches,
    current_descriptor,
    read_descriptor,
    write_descriptor,
)

# 统一使用 biz collection
COLLECTION_NAME = "biz"

#: 描述符落盘位置（与 collection 同生命周期）；可用配置覆盖以便测试隔离。
DESCRIPTOR_FILE = Path(__file__).resolve().parents[2] / "data" / "embedding_descriptor.json"

#: 分数语义：越大越好 / 越小越好 / 不可用。
SCORE_SIMILARITY = "similarity"
SCORE_DISTANCE = "distance"
SCORE_UNAVAILABLE = "unavailable"

#: transient 初始化的有限退避参数（只影响“依赖暂时不可用后何时重连”）。
INIT_RETRY_BASE_SECONDS = 0.05
INIT_RETRY_MAX_SECONDS = 2.0
INIT_RETRY_MAX_ATTEMPTS = 5


class VectorStoreError(RuntimeError):
    """向量库不可用（连接失败、collection 缺失等）。"""


class VectorStoreInvalidIndex(VectorStoreError):
    """索引与当前 embedding 配置不匹配（维度/模型/度量不一致）。"""


class VectorStoreUnavailable(VectorStoreError):
    """向量库暂时不可用（连接失败、查询异常）；与“零命中”严格区分。"""


def descriptor_path() -> Path:
    """描述符文件路径；``EMBEDDING_DESCRIPTOR_FILE`` 可覆盖（测试/部署）。"""
    override = str(getattr(config, "embedding_descriptor_file", "") or "").strip()
    return Path(override) if override else DESCRIPTOR_FILE


def _escape_expr_string(value: str) -> str:
    """转义 Milvus 表达式中的字符串字面量。

    Milvus 的过滤表达式用双引号包裹字符串，反斜杠与双引号必须先转义，
    否则路径里的引号会提前结束字面量，表达式要么非法、要么永远匹配不到，
    删除会静默返回 0 条，留下重复数据。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def metric_of(distance: str) -> str:
    """描述符里的距离度量 -> Milvus 的 ``metric_type``。"""
    value = (distance or "cosine").strip().lower()
    return {
        "cosine": "COSINE",
        "ip": "IP",
        "inner_product": "IP",
        "l2": "L2",
        "euclidean": "L2",
    }.get(value, "COSINE")


def score_kind_for(metric_type: str) -> str:
    """Milvus metric -> 分数语义（决定越大越好还是越小越好）。"""
    metric = (metric_type or "").strip().upper()
    if metric in {"COSINE", "IP"}:
        return SCORE_SIMILARITY
    if metric in {"L2", "EUCLIDEAN"}:
        return SCORE_DISTANCE
    return SCORE_UNAVAILABLE


def _collection_metric(collection) -> str:
    """从 Milvus 读取 vector 字段的真实 metric_type；读不到返回空串。"""
    try:
        indexes = list(collection.indexes or [])
    except Exception:  # noqa: BLE001 - 旧版 pymilvus / 未建索引
        return ""
    for index in indexes:
        params = getattr(index, "params", None) or {}
        metric = params.get("metric_type") if isinstance(params, dict) else None
        if metric:
            return str(metric).upper()
    return ""


class VectorStoreManager:
    """向量存储管理器（懒加载，Milvus 不可用时影响 RAG 但不拖垮诊断）"""

    def __init__(self):
        """初始化向量存储管理器"""
        self.vector_store = None
        self._initialized = False
        self._init_error: Exception | None = None
        self.collection_name = COLLECTION_NAME
        self._metric_type = ""
        #: 初始化互斥：并发调用只允许一个线程真正连接/建库，其余等它完成或失败。
        self._init_guard = threading.Lock()
        #: 上一次初始化失败的类别：``transient``（依赖暂时不可用，可重试）或
        #: ``permanent``（索引/描述符不匹配，必须显式重建，重试无用）。
        self._failure_kind: str | None = None
        #: 连续 transient 失败次数与最近一次尝试时间，用于有限退避。
        self._transient_failures = 0
        self._last_attempt_at = 0.0

    # ── 初始化与描述符校验 ────────────────────────────────────

    @property
    def metric_type(self) -> str:
        """collection 的真实度量（COSINE/IP/L2），未知时为空串。"""
        return self._metric_type

    @property
    def score_kind(self) -> str:
        return score_kind_for(self._metric_type)

    @property
    def init_failure_kind(self) -> str | None:
        """``transient`` / ``permanent`` / ``None``（供 /health 区分展示）。"""
        return self._failure_kind

    def _ensure_initialized(self) -> bool:
        """懒加载：只有连接 + 描述符校验都成功才算初始化完成。

        失败分两类（A08）：

        - **permanent**（描述符/维度/度量不一致）：缓存并永久重抛。重试不会让
          一个错配的索引变正确，继续连接只会拖慢每次请求；必须显式重建索引。
        - **transient**（Milvus 未启动、网络不可达等）：缓存异常但**允许下一次
          调用重新尝试**，并在锁内做有限退避。旧实现把所有失败都当成永久错误，
          于是依赖先停后启时，同进程必须手工 reset/重启才能恢复。
        """
        if self._initialized:
            return True
        if self._failure_kind == "permanent" and self._init_error is not None:
            raise self._init_error

        with self._init_guard:
            # 拿到锁后再次检查：另一线程可能刚刚完成初始化。
            if self._initialized:
                return True
            if self._failure_kind == "permanent" and self._init_error is not None:
                raise self._init_error
            self._backoff_if_needed()
            self._last_attempt_at = time.monotonic()
            return self._attempt_init()

    def _backoff_if_needed(self) -> None:
        """transient 连续失败后的有限退避（指数、封顶）。

        只在“同一次恢复尝试内部”生效：每次新的调用最多等一小段时间再重连，
        不会退化成对依赖的无意义忙等。成功后退避计数清零。
        """
        if self._transient_failures <= 0 or self._init_error is None:
            return
        delay = min(INIT_RETRY_BASE_SECONDS * (2 ** (self._transient_failures - 1)),
                    INIT_RETRY_MAX_SECONDS)
        elapsed = time.monotonic() - self._last_attempt_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        if self._transient_failures > INIT_RETRY_MAX_ATTEMPTS:
            # 连续失败过多：仍抛最近一次异常，但把退避封顶，避免无限拉长。
            self._transient_failures = INIT_RETRY_MAX_ATTEMPTS

    def _attempt_init(self) -> bool:
        try:
            # 必须先建立 ORM 连接（langchain_milvus 内部用 Collection(using=alias)）
            _ = milvus_manager.connect()

            connection_args = {"uri": f"http://{config.milvus_host}:{config.milvus_port}"}
            self.vector_store = Milvus(
                # 实例级注入优先：本地模式的 manager 只允许本地 embedding（A03）。
                embedding_function=getattr(self, "_embedding_function", None)
                or get_embedding_service_lazy(),
                collection_name=self.collection_name,
                connection_args=connection_args,
                auto_id=False,
                drop_old=False,
                text_field="content",
                vector_field="vector",
                primary_field="id",
                metadata_field="metadata",
                index_params={
                    "metric_type": metric_of(current_descriptor().distance),
                    "index_type": "IVF_FLAT",
                    "params": {"nlist": 128},
                },
                search_params={
                    "metric_type": metric_of(current_descriptor().distance),
                    "params": {"nprobe": 10},
                },
            )
            self._metric_type = _collection_metric(milvus_manager.get_collection())
            self._verify_descriptor()
            self._initialized = True
            self._init_error = None
            self._failure_kind = None
            self._transient_failures = 0
            logger.info(
                f"VectorStore 初始化成功: {config.milvus_host}:{config.milvus_port}, "
                f"collection: {self.collection_name}, metric: {self._metric_type or 'unknown'}"
            )
            return True

        except (DescriptorMismatch, VectorStoreInvalidIndex) as exc:
            error = exc if isinstance(exc, VectorStoreInvalidIndex) else VectorStoreInvalidIndex(str(exc))
            self._fail(error, permanent=True)
            raise error from exc
        except Exception as e:
            logger.warning(f"VectorStore 初始化失败（Milvus 不可用？）: {e}")
            error = VectorStoreUnavailable(f"向量库不可用: {e}")
            self._fail(error, permanent=False)
            raise error from e

    def _fail(self, error: Exception, *, permanent: bool) -> None:
        self.vector_store = None
        self._init_error = error
        if permanent:
            self._failure_kind = "permanent"
            self._transient_failures = 0
        else:
            self._failure_kind = "transient"
            self._transient_failures += 1

    def reset(self) -> None:
        """清空初始化状态（配置变更/测试用）；下次调用重新连接并校验。"""
        self.vector_store = None
        self._initialized = False
        self._init_error = None
        self._metric_type = ""
        self._failure_kind = None
        self._transient_failures = 0
        self._last_attempt_at = 0.0

    def _verify_descriptor(self) -> None:
        """在真实 I/O 边界校验描述符：collection metric + 落盘描述符。

        只信任两处事实：Milvus 索引真实的 ``metric_type``、上次建库时写下的
        描述符文件。两者任一缺失/不一致都拒绝检索，而不是静默复用旧向量。
        """
        expected = current_descriptor()
        actual_metric = self._metric_type
        if actual_metric and actual_metric != metric_of(expected.distance):
            raise VectorStoreInvalidIndex(
                f"collection {self.collection_name} 的距离度量是 {actual_metric}，"
                f"而当前 embedding 描述符声明 {expected.distance}"
                f"（{metric_of(expected.distance)}）。请显式重建索引。"
            )

        path = descriptor_path()
        stored = read_descriptor(path)
        if stored is None:
            entities = milvus_manager.collection_count()
            if entities:
                raise VectorStoreInvalidIndex(
                    f"collection {self.collection_name} 已有 {entities} 条向量，"
                    f"但缺少 embedding 描述符（{path}）：无法证明索引与当前查询"
                    "使用同一向量空间。请显式重建索引。"
                )
            write_descriptor(path, expected)
            logger.info(f"已写入新的 embedding 描述符: {path}")
            return

        assert_descriptor_matches(expected, stored)
        logger.info(f"embedding 描述符校验通过: {stored.hash[:12]}")

    # ── 写入 ──────────────────────────────────────────────────

    def add_documents(
        self, documents: List[Document], *, ids: List[str] | None = None
    ) -> List[str]:
        """批量添加文档到向量存储，返回实际写入的 id 列表。

        ``ids`` 由调用方预先生成时优先使用：批量写入中途失败时，调用方
        才知道哪些 id 可能已经落库，从而精确回滚而不是留下孤儿行。
        """
        self._ensure_initialized()
        if self.vector_store is None:
            raise VectorStoreUnavailable("VectorStore 未初始化，无法添加文档")
        if ids is not None and len(ids) != len(documents):
            raise ValueError("ids 数量必须与 documents 数量一致")
        try:
            doc_ids = list(ids) if ids is not None else [str(uuid.uuid4()) for _ in documents]
            result = self.vector_store.add_documents(documents, ids=doc_ids)
            # langchain-milvus 正常情况下原样返回 ids；返回 None 时仍以本地生成的为准。
            return list(result or doc_ids)
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            raise

    def delete_by_source(self, file_path: str, *, keep_ids: List[str] | None = None) -> int:
        """删除指定文件的所有文档，可保留指定 id（新写入的分片）。"""
        self._ensure_initialized()
        if self.vector_store is None:
            raise VectorStoreUnavailable("VectorStore 未初始化，无法删除旧数据")
        collection = milvus_manager.get_collection()
        expr = f'metadata["_source"] == "{_escape_expr_string(file_path)}"'
        if keep_ids:
            quoted = ", ".join(f'"{_escape_expr_string(value)}"' for value in keep_ids if value)
            if quoted:
                expr = f"{expr} and id not in [{quoted}]"
        try:
            result = collection.delete(expr)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0
            logger.info(f"删除文件旧数据: {file_path}, 删除数量: {deleted_count}")
            return deleted_count
        except Exception as e:
            # 不再吞异常：删除失败必须让调用方回滚，否则会静默产生重复数据。
            logger.error(f"删除旧数据失败: {file_path}: {e}")
            raise

    def delete_by_ids(self, ids: List[str]) -> int:
        """按 id 删除（用于写入失败后的回滚）。"""
        self._ensure_initialized()
        if self.vector_store is None or not ids:
            return 0
        collection = milvus_manager.get_collection()
        quoted = ", ".join(f'"{_escape_expr_string(value)}"' for value in ids if value)
        if not quoted:
            return 0
        result = collection.delete(f"id in [{quoted}]")
        return result.delete_count if hasattr(result, "delete_count") else 0

    # ── 查询 ──────────────────────────────────────────────────

    def get_vector_store(self):
        """获取 VectorStore 实例（未初始化时抛出真实原因）。"""
        self._ensure_initialized()
        return self.vector_store

    def similarity_search_with_score(
        self, query: str, k: int = 3, *, expr: str | None = None
    ) -> List[tuple[Document, float]]:
        """返回 ``(Document, 原始分数)``；失败抛异常，绝不返回空列表冒充未命中。

        返回的分数是 Milvus 的原始值：COSINE/IP 是相似度（越大越好），L2 是
        距离（越小越好）。调用方必须结合 :attr:`score_kind` 解释。
        """
        self._ensure_initialized()
        if self.vector_store is None:
            raise VectorStoreUnavailable("VectorStore 未初始化，无法检索")
        try:
            pairs = self.vector_store.similarity_search_with_score(query, k=k, expr=expr)
            logger.debug(f"相似度搜索: query='{query}', 结果数={len(pairs)}")
            return list(pairs or [])
        except Exception as exc:  # noqa: BLE001 - 需要区分“索引不可用”与“查询失败”
            text = str(exc).lower()
            if "dimension" in text or "descriptor" in text or "metric" in text:
                raise VectorStoreInvalidIndex(f"索引与查询不匹配: {exc}") from exc
            logger.error(f"相似度搜索失败: {exc}")
            raise VectorStoreUnavailable(f"向量库检索失败: {exc}") from exc

    def similarity_search(self, query: str, k: int = 3) -> List[Document]:
        """相似度搜索（只要文档）。失败同样抛异常，由调用方决定如何降级。"""
        return [doc for doc, _ in self.similarity_search_with_score(query, k=k)]

    # ── M2：按文档聚合（供文档列表/删除使用） ─────────────────

    def delete_by_document(self, document_id: str) -> int:
        """删除某个 ``document_id`` 的全部 revision 分片。"""
        self._ensure_initialized()
        if self.vector_store is None:
            raise VectorStoreUnavailable("VectorStore 未初始化，无法删除文档")
        if not document_id:
            raise ValueError("document_id 不能为空")
        collection = milvus_manager.get_collection()
        expr = f'metadata["_document_id"] == "{_escape_expr_string(document_id)}"'
        result = collection.delete(expr)
        return result.delete_count if hasattr(result, "delete_count") else 0

    def query_documents(self, *, limit: int = 1000) -> Sequence[dict]:
        """列出 collection 里的文档元数据（按 ``_document_id`` 聚合前的原始行）。"""
        self._ensure_initialized()
        collection = milvus_manager.get_collection()
        try:
            rows = collection.query(
                expr='_document_id != ""',
                output_fields=["id", "metadata"],
                limit=int(limit),
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreUnavailable(f"读取文档元数据失败: {exc}") from exc
        return [row for row in rows or [] if isinstance(row, dict)]


def get_embedding_service_lazy(*, strict_local: bool | None = None):
    """延迟导入 embedding 服务，避免模块导入时加载模型。

    ``strict_local=True`` 时（本地诊断模式）返回**独立的**本地 embedding：
    - 不读取/不修改进程全局配置来“切换模式”（A03 明确禁止这种做法）；
    - 在远端 provider 配置下也不会构造远端客户端、不发 HTTP；
    - 本地模型不可用时明确抛错，而不是悄悄回退远端。
    """
    from app.services.vector_embedding_service import create_embedding_service

    return create_embedding_service(strict_local=strict_local)


class LocalOnlyEmbeddings:
    """本地 mode 专用的 embedding 适配器：把任何远端能力都挡在外面。

    为什么需要单独一层：``VectorStoreManager`` 是进程级单例，它的
    ``embedding_function`` 在首次初始化时绑定。若本地模式的检索复用同一份
    单例，一旦配置是远端 provider，本地运行就会复用**远端** embedding 客户端
    （对抗复核实测：``embed_query`` 打到了远端工厂）。这里给出一个明确只允许
    本地实现的包装，本地模式用它自己的 manager 实例。
    """

    def __init__(self) -> None:
        self._inner = None

    def _service(self):
        if self._inner is None:
            self._inner = get_embedding_service_lazy(strict_local=True)
        return self._inner

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._service().embed_documents(texts)

    def embed_query(self, text: str) -> List[float]:
        return self._service().embed_query(text)


def create_local_vector_store_manager() -> "VectorStoreManager":
    """构造一个**只允许本地 embedding** 的向量库管理器（本地诊断模式专用）。

    不复用模块级 ``vector_store_manager``：那个单例已经被初始化/绑定后不能再改
    embedding 函数，而修改它会污染云端模式与知识库管理页面。独立实例让两种模式
    各自独立，谁也不需要改全局配置。
    """
    manager = VectorStoreManager()
    # 让这个实例在初始化时使用只允许本地的 embedding。
    manager._embedding_function = LocalOnlyEmbeddings()  # type: ignore[attr-defined]
    return manager


# 全局单例（懒加载，不会在导入时崩溃）
vector_store_manager = VectorStoreManager()
