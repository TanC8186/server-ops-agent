"""文档分割服务：token 预算 + parent/child + 原文定位。

为什么不用“180 字符”：
当前 embedding 模型 ``paraphrase-multilingual-MiniLM-L12-v2`` 的官方
``max_seq_length`` 是 128 tokens。中文约 1.1 token/字，180 个汉字已经远超
窗口；英文 180 字符又可能远低于窗口。字符数不是 token 数。

切分策略（先结构、后 token）：

1. 按 Markdown 标题切出 **parent 段落**（保留标题路径、原始行号）。
2. 代码围栏整体作为一个不可分割单元；过长时按行切成 child，但 parent 保留
   完整代码（缩进、续行、YAML 结构原样字节）。
3. 按真实 tokenizer 计算预算，超长段落按句子/行边界二等分，保证每个 child
   编码后（含特殊 token）不超过 ``embedding_max_tokens``。
4. 合并小片段时只在同一标题下、且 token 预算内合并。

元数据（写入 Milvus 的 ``metadata`` JSON 字段）：

``_source`` / ``_file_name`` / ``_extension`` / ``_document_id`` / ``_revision``
``_chunk_id`` / ``_parent_id`` / ``_parent_text`` / ``_heading_path``
``_line_start`` / ``_line_end`` / ``h1`` / ``h2``。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from langchain_core.documents import Document
from loguru import logger

from app.config import config

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_END = tuple("。！？!?；;\n")


@lru_cache(maxsize=1)
def _tokenizer() -> Callable[[str], int] | None:
    """加载真实 tokenizer；不可用时返回 None 并记录原因。"""
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            config.embedding_model, local_files_only=True
        )
    except Exception as exc:  # noqa: BLE001 - 离线缓存缺失时退化为字符估算
        logger.warning(f"embedding tokenizer 不可用，改用保守字符估算: {exc}")
        return None

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=True))

    return count


def _estimate_tokens(text: str) -> int:
    """无 tokenizer 时的保守估算：中文按 1.2 token/字，其余按 0.35。"""
    chinese = sum(1 for char in text if "一" <= char <= "鿿")
    others = len(text) - chinese
    return int(chinese * 1.2 + others * 0.35) + 2


class TokenCounter:
    """token 计数（带缓存），供切分与测试共用。"""

    def __init__(self, counter: Callable[[str], int] | None = None):
        self._counter = counter or _tokenizer()
        self.estimate = self._counter is None

    def __call__(self, text: str) -> int:
        if self._counter is not None:
            return self._counter(text)
        return _estimate_tokens(text)


@dataclass
class _Block:
    """parent 层面的结构单元。"""

    text: str
    kind: str  # prose | code | table
    line_start: int
    line_end: int


@dataclass
class _Section:
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    blocks: list[_Block]


class DocumentSplitterService:
    """按 token 预算切分文档，保留结构与原文定位。"""

    def __init__(self) -> None:
        self.token_counter = TokenCounter()
        #: 单条 child 的 token 上限（含特殊 token）
        self.embedding_max_tokens = int(config.embedding_max_tokens)
        self.token_budget = self.embedding_max_tokens
        #: 字符软上限：仅用于发现异常膨胀（例如硬切失败），不是模型约束。
        self.char_soft_limit = max(4, self.embedding_max_tokens * 4)

    # ── 兼容旧接口 ─────────────────────────────────────────────

    def split_document(self, content: str, file_path: str = "", *,
                       document_id: str = "", revision: str = "") -> list[Document]:
        if not content or not content.strip():
            logger.warning(f"文档内容为空: {file_path}")
            return []
        doc_id = document_id or _default_document_id(file_path)
        rev = revision or _content_hash(content)
        sections = [section for section in self._parse_sections(content)
                    if not _heading_only(section)]
        docs: list[Document] = []
        child_index = 0
        for section in sections:
            produced = self._split_section(section, file_path, doc_id, rev, child_index)
            child_index += len(produced)
            docs.extend(produced)
        merged = self._merge_small_chunks(docs)
        for doc in merged:
            doc.metadata.setdefault("h1", "")
            doc.metadata.setdefault("h2", "")
        logger.info(f"分割完成: {file_path} -> {len(merged)} 个 child（parent 保留）")
        return merged

    def split_markdown(self, content: str, file_path: str = "") -> list[Document]:
        return self.split_document(content, file_path)

    def split_text(self, content: str, file_path: str = "", *,
                   document_id: str = "", revision: str = "") -> list[Document]:
        """无结构的纯文本切分；与 :meth:`split_document` 共用同一套 ID 语义。"""
        if not content or not content.strip():
            return []
        doc_id = document_id or _default_document_id(file_path)
        rev = revision or _content_hash(content)
        section = _Section(heading_path=(), line_start=1,
                           line_end=len(content.splitlines()),
                           blocks=[_Block(text=content, kind="prose",
                                          line_start=1,
                                          line_end=len(content.splitlines()))])
        return self._merge_small_chunks(
            self._split_section(section, file_path, doc_id, rev, 0)
        )

    # ── 解析：标题 → 段落 → 代码块 ───────────────────────────

    def _parse_sections(self, content: str) -> list[_Section]:
        lines = content.splitlines()
        sections: list[_Section] = []
        heading_path: list[str] = []
        current = _Section(heading_path=(), line_start=1, line_end=1, blocks=[])
        buffer: list[str] = []
        buffer_start = 1
        fence = ""
        fence_kind = ""

        def flush(end_line: int) -> None:
            nonlocal buffer, buffer_start
            text = "\n".join(buffer).strip("\n")
            if text.strip():
                kind = fence_kind or ("table" if _looks_like_table(text) else "prose")
                current.blocks.append(
                    _Block(text=text, kind=kind,
                           line_start=buffer_start, line_end=end_line)
                )
            buffer = []
            buffer_start = end_line + 1

        def close_section(end_line: int) -> None:
            nonlocal current
            flush(end_line)
            current.line_end = end_line
            if current.blocks:
                sections.append(current)

        for index, line in enumerate(lines, 1):
            if fence:
                buffer.append(line)
                if line.strip().startswith(fence):
                    fence = ""
                    flush(index)
                    fence_kind = ""
                continue
            if _FENCE_RE.match(line):
                flush(index - 1)
                fence = _FENCE_RE.match(line).group(1)
                fence_kind = "code"
                buffer_start = index
                buffer.append(line)
                continue
            heading = _HEADING_RE.match(line)
            if heading:
                close_section(index - 1)
                level = len(heading.group(1))
                title = heading.group(2).strip()
                heading_path = heading_path[: level - 1]
                while len(heading_path) < level - 1:
                    heading_path.append("")
                heading_path.append(title)
                current = _Section(heading_path=tuple(path for path in heading_path if path),
                                   line_start=index, line_end=index, blocks=[])
                buffer = [line]
                buffer_start = index
                continue
            if not buffer:
                buffer_start = index
            buffer.append(line)

        close_section(len(lines))
        return sections

    # ── 切分：结构优先，token 兜底 ────────────────────────────

    def _split_section(self, section: _Section, file_path: str,
                       document_id: str, revision: str,
                       child_offset: int = 0) -> list[Document]:
        title = " > ".join(section.heading_path)
        parent_id = f"{document_id}:{revision}:p{_short_hash(title + str(section.line_start))}"
        parent_text = "\n\n".join(block.text for block in section.blocks)
        docs: list[Document] = []
        child_index = child_offset

        # 纯标题块不单独成 child（标题路径已在元数据里），但仍保留在 parent 文本中。
        blocks = [
            block for block in section.blocks
            if not (block.kind == "prose" and _only_headings(block.text))
        ] or section.blocks

        for block in blocks:
            for piece, start, end in self._split_block(block):
                child_index += 1
                metadata = {
                    "_source": file_path,
                    "_extension": Path(file_path).suffix or ".md",
                    "_file_name": Path(file_path).name,
                    "_document_id": document_id,
                    "_revision": revision,
                    "_chunk_id": f"{document_id}:{revision}:{child_index}",
                    "_parent_id": parent_id,
                    "_parent_text": parent_text,
                    "_heading_path": list(section.heading_path),
                    "_line_start": start,
                    "_line_end": end,
                    "h1": section.heading_path[0] if section.heading_path else "",
                    "h2": section.heading_path[1] if len(section.heading_path) > 1 else "",
                    "_kind": block.kind,
                }
                docs.append(Document(page_content=piece, metadata=metadata))
        return docs

    def _split_block(self, block: _Block) -> list[tuple[str, int, int]]:
        """把 block 切成若干 token 合规的片段，返回 (文本, 起始行, 结束行)。"""
        if self.token_counter(block.text) <= self.embedding_max_tokens:
            return [(block.text, block.line_start, block.line_end)]

        if block.kind == "code":
            return self._split_code(block)
        return self._split_prose(block)

    def _split_code(self, block: _Block) -> list[tuple[str, int, int]]:
        """代码按行切，每片补回围栏，保持可读与缩进。"""
        lines = block.text.splitlines()
        fence = lines[0] if lines and _FENCE_RE.match(lines[0]) else "```"
        body = lines[1:-1] if len(lines) > 2 and _FENCE_RE.match(lines[-1]) else lines[1:]
        pieces: list[tuple[str, int, int]] = []
        current: list[str] = []
        start_line = block.line_start + 1

        def emit(end_line: int) -> None:
            nonlocal current
            if not current:
                return
            text = "\n".join([fence, *current, fence])
            pieces.append((text, start_line, end_line))
            current = []

        for offset, line in enumerate(body):
            candidate = "\n".join([fence, *current, line, fence])
            if current and self.token_counter(candidate) > self.embedding_max_tokens:
                emit(block.line_start + offset)
                start_line = block.line_start + offset + 1
            if not current:
                # 单行本身就超过 token 上限（压缩过的 JS/JSON、超长命令）。
                # 旧实现此时 current 为空，跳过拆分直接整行放入，产出的 child
                # 远超 embedding 的 max_seq_length，会被模型静默截断。
                for part, part_start, part_end in self._hard_split_line(
                        line, block.line_start + offset):
                    pieces.append((part, part_start, part_end))
                start_line = block.line_start + offset + 1
                continue
            current.append(line)
        emit(block.line_end)
        return pieces or [(block.text, block.line_start, block.line_end)]

    def _hard_split_line(self, line: str, line_number: int) -> list[tuple[str, int, int]]:
        """把一行超长文本按 token 预算硬切成多段（行号保持不变）。

        片段仍是纯文本而不是完整代码块：单行无法在不破坏语义的前提下补围栏，
        这里只保证 token 不超限，并在文本上保留原始缩进。
        """
        pieces: list[tuple[str, int, int]] = []
        remaining = line
        while remaining:
            low, high = 1, len(remaining)
            while low < high:
                middle = (low + high + 1) // 2
                if self.token_counter(remaining[:middle]) <= self.embedding_max_tokens:
                    low = middle
                else:
                    high = middle - 1
            piece = remaining[:low]
            remaining = remaining[low:]
            pieces.append((piece, line_number, line_number))
        return pieces

    def _split_prose(self, block: _Block) -> list[tuple[str, int, int]]:
        """按段落/句子边界二分，直到每个片段满足 token 预算。"""
        pieces: list[tuple[str, int, int]] = []
        text = block.text
        total_lines = max(1, block.line_end - block.line_start + 1)

        def split(start_offset: int, chunk: str) -> None:
            if self.token_counter(chunk) <= self.embedding_max_tokens:
                end_offset = start_offset + chunk.count("\n")
                pieces.append((chunk, block.line_start + start_offset,
                               block.line_start + end_offset))
                return
            left, right = _best_split(chunk)
            if not right.strip():
                # 无法再分（极长的单行）：按字符硬切，仍保持 token 上限
                for part in _hard_split(chunk, self.token_counter,
                                        self.embedding_max_tokens):
                    end_offset = start_offset + part.count("\n")
                    pieces.append((part, block.line_start + start_offset,
                                   block.line_start + end_offset))
                    start_offset = end_offset
                return
            split(start_offset, left)
            split(start_offset + left.count("\n"), right)

        split(0, text)
        if not pieces:
            pieces.append((text, block.line_start, block.line_end))
        # 行号兜底：确保区间合法且不含 0
        normalized = []
        for piece, start, end in pieces:
            start = max(1, start)
            end = max(start, min(end, block.line_start + total_lines))
            normalized.append((piece, start, end))
        return normalized

    # ── 合并：同章节、同预算 ──────────────────────────────────

    def _merge_small_chunks(self, documents: list[Document],
                            min_tokens: int = 30) -> list[Document]:
        merged: list[Document] = []
        for doc in documents:
            if not merged:
                merged.append(doc)
                continue
            previous = merged[-1]
            same_section = previous.metadata.get("_heading_path") == doc.metadata.get("_heading_path")
            combined = f"{previous.page_content}\n\n{doc.page_content}"
            small = self.token_counter(previous.page_content) < min_tokens
            if (same_section and small and previous.metadata.get("_kind") == doc.metadata.get("_kind")
                    and self.token_counter(combined) <= self.embedding_max_tokens):
                previous.page_content = combined
                previous.metadata["_line_end"] = doc.metadata["_line_end"]
                continue
            merged.append(doc)
        return merged


def _best_split(text: str) -> tuple[str, str]:
    """在句子/行/空白边界二分，尽量保持语义完整。"""
    middle = len(text) // 2
    window = max(40, len(text) // 4)
    for offset in range(0, window):
        for position in (middle - offset, middle + offset):
            if 0 < position < len(text) and text[position - 1] in _SENTENCE_END:
                return text[:position], text[position:]
    for offset in range(0, window):
        for position in (middle - offset, middle + offset):
            if 0 < position < len(text) and text[position] == " ":
                return text[:position], text[position:]
    return text[:middle], text[middle:]


def _hard_split(text: str, counter: Callable[[str], int],
                budget: int) -> Iterable[str]:
    step = max(1, len(text) // max(1, counter(text) // budget + 1))
    for start in range(0, len(text), step):
        yield text[start:start + step]


def _looks_like_table(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) >= 2 and all(line.count("|") >= 2 for line in lines[:3])


def _heading_only(section: _Section) -> bool:
    """只有标题行、没有任何正文的 section 不单独产出 child。"""
    if not section.blocks:
        return True
    return all(_only_headings(block.text) for block in section.blocks)


def _only_headings(text: str) -> bool:
    return all(
        _HEADING_RE.match(line) or not line.strip() for line in text.splitlines()
    )


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _default_document_id(file_path: str) -> str:
    name = Path(file_path).name if file_path else "document"
    return f"doc-{_short_hash(name, 12)}"


# 全局单例
document_splitter_service = DocumentSplitterService()
