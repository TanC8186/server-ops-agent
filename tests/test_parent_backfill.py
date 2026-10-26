"""A13：parent 回填必须真正进入检索上下文与引用（不只是元数据里有字段）。

缺陷：``document_splitter_service`` 早已在 child 分片上写入 ``_parent_id`` /
``_parent_text``，但检索适配器没有读出它们，模型与引用只能看到被切碎的 child，
完整命令与前提条件丢失。
"""

from __future__ import annotations

from langchain_core.documents import Document

from app.core.retrieval import render_context
from app.services.knowledge_service import VectorStoreRetriever


class _Vectors:
    score_kind = "similarity"

    def __init__(self, rows: list[tuple[str, dict]]) -> None:
        self._rows = [
            Document(page_content=text, metadata=metadata) for text, metadata in rows
        ]

    def similarity_search_with_score(self, query, k=3, **kwargs):
        return [(document, 0.9) for document in self._rows][:k]


def _visible_all():
    return {"doc-1": "rev-1"}


CHILD_METADATA = {
    "_chunk_id": "doc-1:rev-1:1",
    "_document_id": "doc-1",
    "_revision": "rev-1",
    "_source": "source://aiops-docs/disk.md",
    "_parent_id": "doc-1:rev-1:pabc123",
    "_parent_text": "## 磁盘满排查\n\n1. 执行 df -h 查看使用率\n2. 检查 inode：df -i\n3. 清理后确认 inodes 释放",
    "_line_start": 12,
    "_line_end": 18,
}


def test_retriever_carries_parent_identity_and_text():
    retriever = VectorStoreRetriever(_Vectors([("检查 inode", CHILD_METADATA)]),
                                     active_revisions=_visible_all)

    chunks = retriever.search("磁盘", 3)

    assert len(chunks) == 1
    assert chunks[0].parent_id == "doc-1:rev-1:pabc123"
    assert "df -i" in chunks[0].parent_text


def test_retrieved_chunk_dict_exposes_parent_fields():
    retriever = VectorStoreRetriever(_Vectors([("检查 inode", CHILD_METADATA)]),
                                     active_revisions=_visible_all)

    payload = retriever.search("磁盘", 3)[0].to_dict()

    assert payload["parent_id"] == "doc-1:rev-1:pabc123"
    assert "df -h" in payload["parent_text"]


def test_render_context_uses_full_parent_instead_of_fragment():
    """给模型的上下文必须包含完整父段落，而不是只有命中的半句。"""
    retriever = VectorStoreRetriever(_Vectors([("检查 inode", CHILD_METADATA)]),
                                     active_revisions=_visible_all)
    chunks = retriever.search("磁盘", 3)

    context = render_context(chunks)

    assert "df -h" in context and "df -i" in context
    assert "命中片段" in context


def test_parent_text_is_bounded():
    """父段落过长时必须有界截断，避免把整节塞进事件载荷。"""
    metadata = dict(CHILD_METADATA, _parent_text="x" * 10_000)
    retriever = VectorStoreRetriever(_Vectors([("命中", metadata)]),
                                     active_revisions=_visible_all)

    chunk = retriever.search("磁盘", 3)[0]

    assert len(chunk.parent_text) <= 2100
    assert chunk.parent_text.endswith("…[truncated]")


def test_missing_parent_metadata_degrades_to_child_text():
    """没有 parent 元数据的旧索引仍按 child 工作，不报错。"""
    metadata = {key: value for key, value in CHILD_METADATA.items()
                if key not in {"_parent_id", "_parent_text"}}
    retriever = VectorStoreRetriever(_Vectors([("检查 inode", metadata)]),
                                     active_revisions=_visible_all)

    chunk = retriever.search("磁盘", 3)[0]

    assert chunk.parent_id == ""
    assert chunk.parent_text == ""
    context = render_context([chunk])
    assert "检查 inode" in context
