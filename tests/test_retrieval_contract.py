"""RAG 检索契约（RAG-02 / RAG-03 / RAG-07）。

检索结果必须是可核验的结构，而不是一段拼接文本：

- ``RetrievalStatus``：hit / miss / unavailable / timeout / invalid_index 分开
- ``RetrievedChunk``：稳定 chunk_id、document_id、revision、标题路径、原文定位
- ``AnswerCitation``：本轮授权命中的短引用 ID（K1、K2…），由服务端分配
- ``CitationRegistry``：拒绝伪造/未授权的引用 ID，只暴露本轮授权的片段

引用 ID 由服务端为本轮命中分配；模型自带的 URL 或 ID 一律不信任。
"""

from __future__ import annotations

import pytest

from app.core.retrieval import (
    AnswerCitation,
    CitationRegistry,
    RetrievalResult,
    RetrievalStatus,
    RetrievedChunk,
    build_citations,
    render_context,
)


def _chunk(index: int, *, score: float = 0.8, document_id: str = "doc-1",
           revision: str = "rev-1", text: str = "内容") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"doc-1:rev-1:{index}",
        document_id=document_id,
        revision=revision,
        text=text,
        title="磁盘空间不足",
        heading_path=("运维手册", "磁盘"),
        source_uri="https://example.invalid/manual#disk",
        line_start=10 + index,
        line_end=20 + index,
        score=score,
        source_kind="manual",
    )


def test_status_values_are_distinct():
    assert {status.value for status in RetrievalStatus} == {
        "hit", "miss", "unavailable", "timeout", "invalid_index",
    }


def test_hit_result_requires_chunks():
    with pytest.raises(ValueError):
        RetrievalResult(status=RetrievalStatus.HIT)


def test_miss_result_cannot_carry_chunks():
    with pytest.raises(ValueError):
        RetrievalResult(status=RetrievalStatus.MISS, chunks=[_chunk(1)])


def test_unavailable_result_is_not_a_miss():
    result = RetrievalResult(status=RetrievalStatus.UNAVAILABLE, reason="milvus down")
    assert result.status != RetrievalStatus.MISS
    assert result.to_dict()["status"] == "unavailable"
    assert result.to_dict()["reason"] == "milvus down"


def test_citation_registry_assigns_short_ids_in_order():
    result = RetrievalResult(status=RetrievalStatus.HIT,
                             chunks=[_chunk(1), _chunk(2, score=0.5)])
    registry = CitationRegistry.from_result(result)
    citations = registry.citations()

    assert [citation.id for citation in citations] == ["K1", "K2"]
    assert citations[0].chunk_id == "doc-1:rev-1:1"
    assert citations[0].document_id == "doc-1"
    assert citations[0].revision == "rev-1"
    assert citations[0].source_uri.endswith("#disk")
    assert citations[0].locator == "L11-L21"


def test_registry_rejects_forged_citation_ids():
    result = RetrievalResult(status=RetrievalStatus.HIT, chunks=[_chunk(1)])
    registry = CitationRegistry.from_result(result)

    assert registry.resolve("K1") is not None
    assert registry.resolve("K2") is None
    assert registry.resolve("https://evil.invalid") is None
    assert registry.resolve("E1") is None


def test_registry_only_accepts_ids_used_this_turn():
    first = CitationRegistry.from_result(
        RetrievalResult(status=RetrievalStatus.HIT, chunks=[_chunk(1)]))
    second = CitationRegistry.from_result(
        RetrievalResult(status=RetrievalStatus.HIT, chunks=[_chunk(2)]))

    assert first.resolve("K1").chunk_id.endswith(":1")
    assert second.resolve("K1").chunk_id.endswith(":2")


def test_sanitize_answer_removes_unknown_citation_markers():
    result = RetrievalResult(status=RetrievalStatus.HIT,
                             chunks=[_chunk(1), _chunk(2)])
    registry = CitationRegistry.from_result(result)

    text, removed = registry.sanitize_answer("先看磁盘 [K1]，再看 inode [K9]。")

    assert "[K1]" in text
    assert "[K9]" not in text
    assert removed == ["K9"]


def test_sanitize_answer_keeps_markdown_links_intact():
    result = RetrievalResult(status=RetrievalStatus.HIT, chunks=[_chunk(1)])
    registry = CitationRegistry.from_result(result)

    text, removed = registry.sanitize_answer("参考 [文档](https://example.invalid) [K1]")
    assert "https://example.invalid" in text
    assert removed == []


def test_miss_registry_has_no_citations():
    registry = CitationRegistry.from_result(RetrievalResult(status=RetrievalStatus.MISS))
    assert registry.citations() == []
    assert registry.resolve("K1") is None


def test_answer_citation_serializes_locator():
    citation = AnswerCitation.from_chunk("K1", _chunk(3))
    payload = citation.to_dict()
    assert payload["id"] == "K1"
    assert payload["locator"] == "L13-L23"
    assert payload["heading_path"] == ["运维手册", "磁盘"]
    assert payload["snippet"]


def test_render_context_marks_documents_as_untrusted_data():
    text = render_context([_chunk(1, text="先执行 df -h")])
    assert "[K1]" in text
    assert "不可信" in text
    assert "df -h" in text
    assert "doc-1" in text and "rev-1" in text
    assert "#disk" in text


def test_render_context_is_empty_for_non_hit():
    assert render_context([]) == ""


def test_build_citations_only_for_granted_hits():
    result = RetrievalResult(status=RetrievalStatus.HIT, chunks=[_chunk(1), _chunk(2)])
    registry = CitationRegistry.from_result(result)

    citations = build_citations(registry, used_ids=["K1", "K7"])

    assert [citation.id for citation in citations] == ["K1"]


def test_retrieved_chunk_requires_locator_or_uri():
    with pytest.raises(ValueError):
        RetrievedChunk(chunk_id="c", document_id="d", revision="r", text="t", title="")
