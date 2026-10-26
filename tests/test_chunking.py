"""语料级切分回归（替换旧的“180 字符”断言）。

旧测试断言 `len(page_content) <= 180`，它的前提是错的：embedding 模型的限制是
128 **tokens**，而中文约 1.1 token/字——180 个汉字已经远超窗口。真实的窗口校验
在 `tests/test_token_chunking.py`（调用真实 tokenizer）；这里检查仓库自带的
`aiops-docs/` 语料在真实切分器下是否满足 token 预算与结构保真。
"""

from pathlib import Path

import pytest

from app.services.document_splitter_service import document_splitter_service

DOCS_DIR = Path(__file__).resolve().parents[1] / "aiops-docs"


def _all_chunks():
    chunks = []
    for path in sorted(DOCS_DIR.glob("*.md")):
        chunks.extend(
            document_splitter_service.split_document(
                path.read_text(encoding="utf-8"), str(path)
            )
        )
    return chunks


def _tokenizer():
    transformers = pytest.importorskip("transformers")
    try:
        return transformers.AutoTokenizer.from_pretrained(
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            local_files_only=True,
        )
    except Exception:  # pragma: no cover - 离线缓存缺失时跳过（不计为通过）
        pytest.skip("本地 embedding tokenizer 不可用")


def test_chunks_do_not_exceed_configured_char_budget():
    """字符预算仍作为保守上界保留：token 合规通常也意味着字符不会无限膨胀。"""
    chunks = _all_chunks()
    assert chunks, "知识库没有产生任何分块"
    oversized = [doc for doc in chunks
                 if len(doc.page_content) > document_splitter_service.char_soft_limit]
    assert not oversized, (
        f"{len(oversized)} 个分块超过 {document_splitter_service.char_soft_limit} 字符软上限"
    )


def test_chunks_do_not_exceed_embedding_token_limit():
    tokenizer = _tokenizer()
    chunks = _all_chunks()
    budget = document_splitter_service.embedding_max_tokens
    too_long = [
        (len(tokenizer.encode(doc.page_content, add_special_tokens=True)),
         doc.metadata.get("_file_name"))
        for doc in chunks
        if len(tokenizer.encode(doc.page_content, add_special_tokens=True)) > budget
    ]
    assert not too_long, f"{len(too_long)} 个分块超过 {budget} tokens: {too_long[:5]}"


def test_every_chunk_has_traceable_locator():
    for doc in _all_chunks():
        metadata = doc.metadata
        assert metadata["_document_id"]
        assert metadata["_revision"]
        assert metadata["_chunk_id"].startswith(metadata["_document_id"])
        assert metadata["_line_start"] >= 1
        assert metadata["_line_end"] >= metadata["_line_start"]
        assert metadata["_heading_path"] or metadata["_kind"] == "code"


def test_merge_does_not_cross_section_boundaries():
    """合并小分片时不能把两个 H2 段落拼进同一个 chunk。"""
    for path in sorted(DOCS_DIR.glob("*.md")):
        chunks = document_splitter_service.split_document(
            path.read_text(encoding="utf-8"), str(path)
        )
        for doc in chunks:
            headings = {
                line for line in doc.page_content.splitlines() if line.startswith("## ")
            }
            if len(headings) > 1:
                pytest.fail(f"{path.name}: 一个 chunk 跨越了 {len(headings)} 个 H2 段落")


def test_code_fences_are_balanced_in_every_chunk():
    for doc in _all_chunks():
        assert doc.page_content.count("```") % 2 == 0, doc.metadata.get("_file_name")
