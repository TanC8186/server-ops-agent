"""RAG-05 回归：按真实 tokenizer 切分，保留 parent/child、标题、代码与命令。

当前 embedding 模型 ``paraphrase-multilingual-MiniLM-L12-v2`` 的官方
``max_seq_length`` 是 128。180 字符不等于 128 tokens：中文约 1.1 token/字，
英文约 0.25 token/字，代码与 URL 更不稳定。这里直接调用真实 tokenizer 校验。

要求：
- 每个 child 编码后不超过预算（含特殊 token）。
- 代码围栏不得被拆开；`\` 续行命令、缩进、YAML 结构保持原样。
- child 能回到 parent（完整段落/代码）与原文行号。
- 标题路径保留，合并不得跨越章节。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.document_splitter_service import document_splitter_service

DOCS_DIR = Path(__file__).resolve().parents[1] / "aiops-docs"


@pytest.fixture(scope="module")
def tokenizer():
    transformers = pytest.importorskip("transformers")
    try:
        return transformers.AutoTokenizer.from_pretrained(
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            local_files_only=True,
        )
    except Exception:  # pragma: no cover - 离线缓存缺失时跳过（不计为通过）
        pytest.skip("本地 embedding tokenizer 不可用")


def _tokens(tokenizer, text: str) -> int:
    return len(tokenizer.encode(text, add_special_tokens=True))


def _all_chunks(tokenizer):
    chunks = []
    for path in sorted(DOCS_DIR.glob("*.md")):
        chunks.extend(
            document_splitter_service.split_document(
                path.read_text(encoding="utf-8"), str(path)
            )
        )
    return chunks


def test_chunks_respect_real_tokenizer_budget(tokenizer):
    chunks = _all_chunks(tokenizer)
    assert chunks, "知识库没有产生任何分块"
    budget = document_splitter_service.token_budget
    oversized = [
        (doc.metadata.get("_file_name"), _tokens(tokenizer, doc.page_content))
        for doc in chunks
        if _tokens(tokenizer, doc.page_content) > budget
    ]
    assert not oversized, f"{len(oversized)} 个分块超过 {budget} tokens: {oversized[:5]}"


def test_long_chinese_paragraph_is_split_by_tokens_not_chars(tokenizer):
    """中文约 1.1 token/字：280 字远低于字符切分阈值，却超过 128 tokens。"""
    paragraph = "磁盘空间不足时" * 40  # 280 汉字
    assert _tokens(tokenizer, paragraph) > document_splitter_service.token_budget

    docs = document_splitter_service.split_text(paragraph, "inline.md")

    assert len(docs) >= 2
    for doc in docs:
        assert _tokens(tokenizer, doc.page_content) <= document_splitter_service.token_budget
    assert "".join(doc.page_content for doc in docs).replace("\n", "") == paragraph


def test_short_chinese_text_stays_one_chunk(tokenizer):
    text = "先执行 df -h 查看磁盘使用率。"
    docs = document_splitter_service.split_text(text, "inline.md")
    assert len(docs) == 1


def test_code_fence_is_never_split_across_chunks():
    code = "```bash\ndf -h\n```"
    content = f"# 磁盘检查\n\n## 步骤\n\n{code}\n\n## 说明\n\n检查完成后记录结果。\n"

    docs = document_splitter_service.split_document(content, "kb.md")

    holders = [doc for doc in docs if "df -h" in doc.page_content]
    assert len(holders) == 1
    assert holders[0].page_content.count("```") == 2


def test_long_code_block_keeps_parent_and_child_locator():
    lines = [f"line_{index} = {index}" for index in range(200)]
    code = "```python\n" + "\n".join(lines) + "\n```"
    content = f"# 大段代码\n\n{code}\n"

    docs = document_splitter_service.split_document(content, "kb.md")

    assert len(docs) >= 2
    for doc in docs:
        metadata = doc.metadata
        assert metadata["_parent_id"]
        assert metadata["_heading_path"]
        assert metadata["_line_start"] >= 1
        assert metadata["_line_end"] >= metadata["_line_start"]
        # child 的每一行代码都必须是原文中的原行（围栏由切分器补回）
        for line in doc.page_content.splitlines():
            if line and not line.strip().startswith("```"):
                assert line in lines, line
        # 围栏必须成对，且代码块不会被拆成“有头无尾”
        assert doc.page_content.count("```") % 2 == 0
    # 完整代码仍保留在 parent 中，答案引用时可回填
    parent_text = docs[0].metadata["_parent_text"]
    assert "line_0 = 0" in parent_text and "line_199 = 199" in parent_text


def test_line_numbers_point_back_to_source():
    content = "# 标题\n\n第一段内容。\n\n## 小节\n\n第二段内容。\n"
    docs = document_splitter_service.split_document(content, "kb.md")
    original = content.splitlines()

    for doc in docs:
        start = doc.metadata["_line_start"]
        end = doc.metadata["_line_end"]
        window = "\n".join(original[start - 1:end])
        for fragment in doc.page_content.splitlines():
            if fragment.strip():
                assert fragment in window, (fragment, window)


def test_continuation_commands_and_indentation_are_preserved():
    command = (
        "```bash\n"
        "journalctl -u nginx \\\n"
        "  --since '1 hour ago' \\\n"
        "  --no-pager | tail -n 100\n"
        "```"
    )
    content = f"# 日志\n\n## 命令\n\n{command}\n"

    docs = document_splitter_service.split_document(content, "kb.md")
    joined = "\n".join(doc.page_content for doc in docs)

    assert "journalctl -u nginx \\" in joined
    assert "  --since '1 hour ago' \\" in joined
    assert "  --no-pager | tail -n 100" in joined


def test_yaml_indentation_survives():
    yaml_block = "```yaml\nserver:\n  listen: 80\n  locations:\n    - path: /\n      proxy: http://app\n```"
    content = f"# 配置\n\n## YAML\n\n{yaml_block}\n"

    docs = document_splitter_service.split_document(content, "kb.md")
    joined = "\n".join(doc.page_content for doc in docs)

    assert "    - path: /" in joined
    assert "      proxy: http://app" in joined


def test_merge_does_not_cross_section_boundaries():
    for path in sorted(DOCS_DIR.glob("*.md")):
        chunks = document_splitter_service.split_document(
            path.read_text(encoding="utf-8"), str(path)
        )
        for doc in chunks:
            heading = doc.metadata.get("h2")
            h2_count = sum(
                1 for line in doc.page_content.splitlines()
                if line.startswith("## ") and line[3:].strip() != heading
            )
            assert h2_count == 0, f"{path.name}: 一个 chunk 跨越了多个 H2 段落"


def test_metadata_contains_stable_identity_fields():
    content = "# 主题\n\n## 小节\n\n内容内容内容。\n"
    docs = document_splitter_service.split_document(
        content, str(DOCS_DIR / "kb.md"),
        document_id="doc-abc", revision="rev-1",
    )
    assert docs
    for doc in docs:
        metadata = doc.metadata
        assert metadata["_document_id"] == "doc-abc"
        assert metadata["_revision"] == "rev-1"
        assert metadata["_chunk_id"].startswith("doc-abc:rev-1:")
        assert metadata["_heading_path"] == ["主题", "小节"]


def test_chunk_ids_are_unique_and_stable():
    content = "# A\n\n## B\n\n第一段。\n\n第二段。\n"
    first = document_splitter_service.split_document(content, "kb.md",
                                                     document_id="d", revision="r")
    second = document_splitter_service.split_document(content, "kb.md",
                                                      document_id="d", revision="r")
    ids_first = [doc.metadata["_chunk_id"] for doc in first]
    ids_second = [doc.metadata["_chunk_id"] for doc in second]
    assert ids_first == ids_second
    assert len(set(ids_first)) == len(ids_first)


def test_parent_text_available_for_context():
    content = "# 标题\n\n## 章节\n\n" + ("啊" * 300) + "\n"
    docs = document_splitter_service.split_document(content, "kb.md")
    assert len(docs) >= 2
    parents = {doc.metadata["_parent_id"]: doc.metadata["_parent_text"] for doc in docs}
    for parent_id, parent_text in parents.items():
        assert parent_id
        assert len(parent_text) >= max(len(doc.page_content) for doc in docs)
