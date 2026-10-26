p = 'app/services/knowledge_service.py'
s = open(p, encoding='utf-8').read()

old = '''def _call_with_timeout(func: Callable[[], Any], timeout: float) -> Any:
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(func)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise RetrievalTimeout(f"检索超过 {timeout:g}s") from exc
'''
new = '''#: 检索线程池：固定小池 + 不阻塞的关闭，避免超时后又等线程结束。
_RETRIEVAL_POOL = None
_RETRIEVAL_POOL_LOCK = None
#: 同时进行的检索上限；超出排队，不会无限创建线程。
RETRIEVAL_WORKERS = 4


def _retrieval_pool():
    """懒建检索线程池（模块级单例，不随请求创建/销毁）。"""
    global _RETRIEVAL_POOL, _RETRIEVAL_POOL_LOCK
    if _RETRIEVAL_POOL_LOCK is None:
        import threading

        _RETRIEVAL_POOL_LOCK = threading.Lock()
    with _RETRIEVAL_POOL_LOCK:
        if _RETRIEVAL_POOL is None:
            import concurrent.futures

            _RETRIEVAL_POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=RETRIEVAL_WORKERS, thread_name_prefix="retrieval"
            )
        return _RETRIEVAL_POOL


def _call_with_timeout(func: Callable[[], Any], timeout: float) -> Any:
    """有界等待检索结果。

    旧实现用 ``with ThreadPoolExecutor(...)``：``future.result`` 超时抛异常后，
    退出 ``with`` 会执行 ``shutdown(wait=True)``，仍要等那个已经卡住的线程跑完，
    于是"超时"根本没有把控制权还给调用方。这里改为模块级线程池 + 只等
    ``timeout``：超时立刻抛 ``RetrievalTimeout``，后台线程自行结束（其结果被
    丢弃），不再阻塞请求。
    """
    import concurrent.futures

    future = _retrieval_pool().submit(func)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise RetrievalTimeout(f"检索超过 {timeout:g}s") from exc
'''
assert s.count(old) == 1, 'timeout block'
s = s.replace(old, new)

old2 = '''class VectorStoreRetriever:
    """把 ``VectorStoreManager`` 适配成 ``Retriever``（保留元数据与分数）。"""

    def __init__(self, store=None):
        self._store = store

    def search(self, query: str, k: int) -> list[RetrievedChunk]:
        from app.services.vector_store_manager import vector_store_manager

        store = self._store or vector_store_manager
        try:
            docs_with_scores = store.similarity_search_with_score(query, k=k)
        except AttributeError:
            docs_with_scores = [(doc, 0.0) for doc in store.similarity_search(query, k=k)]
        except RuntimeError as exc:
            raise RetrievalUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if "dimension" in text or "descriptor" in text:
                raise InvalidIndex(str(exc)) from exc
            raise RetrievalUnavailable(str(exc)) from exc

        chunks: list[RetrievedChunk] = []
        for doc, score in docs_with_scores:
            metadata = dict(getattr(doc, "metadata", {}) or {})
            chunks.append(make_chunk(
                chunk_id=str(metadata.get("_chunk_id") or ""),
                document_id=str(metadata.get("_document_id") or ""),
                revision=str(metadata.get("_revision") or ""),
                text=doc.page_content,
                title=str(metadata.get("h2") or metadata.get("h1") or ""),
                heading_path=tuple(metadata.get("_heading_path") or ()),
                source_uri=str(metadata.get("_source") or ""),
                line_start=metadata.get("_line_start"),
                line_end=metadata.get("_line_end"),
                score=_similarity_from_distance(score),
            ))
        return chunks


def _similarity_from_distance(distance: Any) -> float:
    """Milvus L2 距离 -> [0,1] 相似度（仅用于门控排序，需在验证集上校准）。"""
    try:
        value = float(distance)
    except (TypeError, ValueError):
        return 0.0
    return 1.0 / (1.0 + max(0.0, value))
'''
new2 = '''class VectorStoreRetriever:
    """把 ``VectorStoreManager`` 适配成 ``Retriever``（保留元数据与真实分数）。

    分数语义由 collection 的真实 metric 决定（``store.score_kind``）：

    - ``similarity``（COSINE/IP）：原始分数越大越相关；
    - ``distance``（L2）：原始分数是距离，越小越相关，需转换成相似度才能与
      ``rag_min_score`` 比较；
    - ``unavailable``：读不到 metric。此时**所有分数记 0**，并让所有片段都
      低于任何正的阈值——绝不伪造 ``1.0`` 让候选全部“最高分”。
    """

    def __init__(self, store=None):
        self._store = store

    def search(self, query: str, k: int) -> list[RetrievedChunk]:
        from app.services.vector_store_manager import (
            SCORE_DISTANCE,
            SCORE_SIMILARITY,
            VectorStoreInvalidIndex,
            VectorStoreUnavailable,
        )

        store = self._store or _default_store()
        try:
            pairs = store.similarity_search_with_score(query, k=k)
        except VectorStoreInvalidIndex as exc:
            raise InvalidIndex(str(exc)) from exc
        except VectorStoreUnavailable as exc:
            raise RetrievalUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 适配未知实现（含测试替身）
            text = str(exc).lower()
            if "dimension" in text or "descriptor" in text:
                raise InvalidIndex(str(exc)) from exc
            raise RetrievalUnavailable(str(exc)) from exc

        kind = getattr(store, "score_kind", "") or ""
        chunks: list[RetrievedChunk] = []
        for doc, raw_score in pairs or []:
            metadata = dict(getattr(doc, "metadata", {}) or {})
            score = _score_for_gate(raw_score, kind)
            chunks.append(make_chunk(
                chunk_id=str(metadata.get("_chunk_id") or ""),
                document_id=str(metadata.get("_document_id") or ""),
                revision=str(metadata.get("_revision") or ""),
                text=doc.page_content,
                title=str(metadata.get("h2") or metadata.get("h1") or ""),
                heading_path=tuple(metadata.get("_heading_path") or ()),
                source_uri=str(metadata.get("_source") or ""),
                line_start=metadata.get("_line_start"),
                line_end=metadata.get("_line_end"),
                score=score,
            ))
        return chunks


def _default_store():
    from app.services.vector_store_manager import vector_store_manager

    return vector_store_manager


def _score_for_gate(raw: Any, kind: str) -> float:
    """把 Milvus 原始分数转换成“越大越相关”的门控分数。

    - similarity：原样使用（COSINE 已在 [-1,1]，负数按 0 处理）；
    - distance：``1/(1+d)``，仅用于排序与阈值比较，不是真实相似度；
    - 未知：返回 0.0，表示“分数不可用”，不允许被当成高分。
    """
    if kind == "similarity":
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0
    if kind == "distance":
        try:
            return 1.0 / (1.0 + max(0.0, float(raw)))
        except (TypeError, ValueError):
            return 0.0
    return 0.0
'''
assert s.count(old2) == 1, 'retriever block'
s = s.replace(old2, new2)

open(p, 'w', encoding='utf-8', newline='\\n').write(s)
print('knowledge_service patched')
