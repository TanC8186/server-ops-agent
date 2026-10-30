"""把原始快照规范化为可切分的 Markdown（M3）。

输入：``knowledge/raw/<source_id>/*.html``（由 ``scripts/fetch_corpus.py`` 抓取）。
输出：

- ``knowledge/normalized/<source_id>/<revision>.md``：正文（标题层级、代码块、
  表格尽量保留），文件头带来源/许可/版本元数据；
- ``knowledge/notices/<source_family>/<source_id>.md``：署名与许可说明，许可未知
  时明确写 ``needs_review``；
- ``knowledge/manifests/normalized-<date>.json``：每份文档的 input sha256、输出
  sha256、document_id、revision、字符数与提取统计。

实现只用标准库 ``html.parser``：不引入新依赖，避免为一个转换器改变锁文件。
边界：

- **不做全站抓取**，只处理已存在的本地快照；
- 只提取 ``<main>``/``<article>``（找不到时退化为 ``<body>``），去掉导航、脚本、
  样式；
- 保留 ``<pre>`` 原文（去标签但不去空白），代码缩进不丢；
- 提取失败（几乎没有正文）时标记 ``status=needs_review``，不假装成功。

用法::

    ./.venv/Scripts/python.exe scripts/normalize_corpus.py [--only SD-01 ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "knowledge"
SOURCES = ROOT / "docs" / "claude-code-handoff" / "knowledge_sources.json"
MANIFEST_DIR = KNOWLEDGE / "manifests"

#: 低于该正文字符数视为提取失败（页脚/登录墙/目录页）。
#: 正文少于这个字数就判 ``needs_review``（怀疑抓到了空壳或只剩导航）。
#:
#: 原值 400 会把真实但简短的官方页面误判：AutoDL `docs/image/` 的正文
#: 去空白后 388 字，内容是完整的"镜像保存/加载/更换/共享"说明，却被拦下。
#: 这条规则的本意是**拦截空壳**（JS 渲染页往往只剩几十字导航），不是要求
#: 每页都写成长文。200 足以区分"空壳"与"短而完整"。
MIN_BODY_CHARS = 200

#: 整块丢弃的标签（导航/脚本/样式等）。
DROP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "header", "aside",
    "svg", "form", "button", "iframe", "template",
}
#: 标题标签 -> Markdown 级别。
HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
#: 段落级块标签（前后补空行）。
BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "ul", "ol", "dl", "table",
    "tr", "blockquote", "figure", "figcaption", "details", "summary", "br",
}
#: 内联强调。
INLINE_MARK = {"strong": "**", "b": "**", "em": "_", "i": "_", "code": "`"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: 代码围栏用四个反引号：原文里常见的 ``` 片段不会提前结束代码块。
FENCE = "````"


class _Extractor(HTMLParser):
    """把 HTML 转成 Markdown 片段（够用即可，不追求完整语义）。

    ``restrict=True`` 时只保留 ``<main>``/``<article>`` 内部的内容：大多数手册
    页面在正文外还有导航、目录、搜索框和页脚，全收进来会把“目录”当成正文切分。
    """

    def __init__(self, *, restrict: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        self.pre_depth = 0
        self.main_depth = 0
        self.restrict = restrict
        self.buffer: list[str] = []

    # -- 是否接收当前内容 --------------------------------------------------
    def _dropping(self) -> bool:
        if self.skip_depth:
            return True
        return self.restrict and self.main_depth == 0

    # -- 结构 -------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"main", "article"}:
            self.main_depth += 1
        if tag in DROP_TAGS:
            self.skip_depth += 1
            return
        if self._dropping():
            return
        if tag == "pre":
            if self.pre_depth == 0:
                self._flush()
                self.out.append(f"\n{FENCE}\n")
            self.pre_depth += 1
            return
        if self.pre_depth:
            return
        if tag in HEADING_TAGS:
            self._flush()
            self.out.append("\n" + "#" * HEADING_TAGS[tag] + " ")
            return
        if tag in INLINE_MARK:
            self.out.append(INLINE_MARK[tag])
            return
        if tag == "li":
            self._flush()
            self.out.append("\n- ")
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self.out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            if tag in {"main", "article"}:
                self.main_depth = max(0, self.main_depth - 1)
            return
        if tag == "pre" and self.pre_depth:
            self.pre_depth -= 1
            if self.pre_depth == 0:
                self._flush_pre()
                self.out.append(f"\n{FENCE}\n\n")
            if tag in {"main", "article"}:
                self.main_depth = max(0, self.main_depth - 1)
            return
        if self._dropping():
            if tag in {"main", "article"}:
                self.main_depth = max(0, self.main_depth - 1)
            return
        if tag in HEADING_TAGS and not self.pre_depth:
            self._flush()
            self.out.append("\n")
        elif tag in INLINE_MARK and not self.pre_depth:
            self.out.append(INLINE_MARK[tag])
        elif tag in BLOCK_TAGS and not self.pre_depth:
            self._flush()
            self.out.append("\n\n")
        if tag in {"main", "article"}:
            self.main_depth = max(0, self.main_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._dropping():
            return
        self.buffer.append(data)

    # -- 文本缓冲 ---------------------------------------------------------
    def _flush(self) -> None:
        if not self.buffer:
            return
        text = re.sub(r"[ \t\r\n]+", " ", "".join(self.buffer)).strip()
        self.buffer = []
        if text:
            self.out.append(text)

    def _flush_pre(self) -> None:
        if not self.buffer:
            return
        # 代码块保留原始换行与缩进，只去掉首尾空行；内部的反引号序列降级，
        # 避免与原围栏冲突（用零宽连接符打断连续反引号）。
        text = "".join(self.buffer)
        self.buffer = []
        text = re.sub(r"`{3,}", lambda m: m.group(0)[:2] + "\u200b" + m.group(0)[2:], text)
        self.out.append(text.strip("\n"))

    def markdown(self) -> str:
        self._flush()
        if self.pre_depth:
            self._flush_pre()
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = _collapse_stray_backticks(text)
        return text.strip() + "\n"


def _collapse_stray_backticks(text: str) -> str:
    """围栏之外连续的反引号收敛成一个。

    相邻的 ``<code>a</code><code>b</code>`` 会拼出 ``` ``...`` ``` 这种伪围栏，
    下游会把整段正文误判成代码块（因此绕过按句切分，产出超预算片段）。
    代码块内部不动。
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        # 只有“整行就是一个围栏”才是真正的代码块边界。相邻行内 <code> 拼出来的
        # 「````正文…」不是围栏，否则整段正文会被下游当成代码（并因此绕过按句切分）。
        if stripped == FENCE:
            in_fence = not in_fence
            out.append(line)
            continue
        out.append(line if in_fence else re.sub(r"`{2,}", "`", line))
    return "\n".join(out)


_MAIN_HINT = re.compile(r"<(main|article)[\s>]", re.IGNORECASE)


def html_to_markdown(raw: str) -> str:
    """优先只提取正文容器；页面没有 main/article 时回退整页。"""
    restrict = bool(_MAIN_HINT.search(raw))
    parser = _Extractor(restrict=restrict)
    parser.feed(raw)
    text = parser.markdown()
    if restrict and len(re.sub(r"\s+", "", text)) < MIN_BODY_CHARS:
        # 正文容器判错（例如 main 只包了一个加载占位）：退回整页再试。
        parser = _Extractor(restrict=False)
        parser.feed(raw)
        text = parser.markdown()
    return text


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _document_id(source_id: str, url: str) -> str:
    key = f"{source_id}|{url}".encode("utf-8")
    return f"doc-{hashlib.sha256(key).hexdigest()[:12]}"


def load_sources() -> dict[str, dict]:
    data = json.loads(SOURCES.read_text(encoding="utf-8"))
    return {item["id"]: item for item in data.get("sources", [])}


def load_manifest(manifest_path: Path | None = None) -> dict:
    """读取本次要规范化的 import manifest。

    以前这里硬编码 ``import-2026-10-26.json``：fetch_corpus 写的是
    ``import-<UTC 日期>.json``，于是新抓的快照**永远读不到**，normalize 只会
    反复处理那份旧清单（抓取看起来成功、语料却不变）。

    现在的顺序：显式传入 > 环境变量 ``AIOPS_CORPUS_MANIFEST`` > 目录里最新的
    ``import-*.json``。找不到时返回空清单，由调用方决定是否报错。
    """
    if manifest_path is not None:
        path = Path(manifest_path)
    else:
        override = os.environ.get("AIOPS_CORPUS_MANIFEST", "").strip()
        if override:
            path = Path(override)
        else:
            candidates = sorted(MANIFEST_DIR.glob("import-*.json"))
            if not candidates:
                return {"entries": []}
            path = candidates[-1]  # 文件名带 ISO 日期，字典序即时间序
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"entries": []}


def notice_markdown(source: dict, entry: dict) -> str:
    license_info = source.get("license") or {}
    identifier = license_info.get("identifier") or "unknown"
    status = license_info.get("status") or entry.get("license_status") or "needs_review"
    lines = [
        f"# 来源与许可 — {source.get('id')} {source.get('title', '')}",
        "",
        f"- 发布者: {source.get('publisher', '')}",
        f"- 规范页面: {source.get('source_url', '')}",
        f"- 抓取时间 (UTC): {entry.get('fetched_at_utc', '')}",
        f"- 上游版本: {entry.get('upstream_version') or source.get('version_scope', '')}",
        f"- 原始快照: {entry.get('raw_path', '')}",
        f"- 原始 SHA256: {entry.get('sha256', '')}",
        f"- 许可标识: {identifier}",
        f"- 许可状态: {status}",
        f"- 许可依据: {license_info.get('evidence_url') or '未记录'}",
        "",
        "## 使用边界",
        "",
        "- 本文件仅用于检索与引用原文；原文版权归原发布者，不随本项目许可转移。",
        "- 手册描述的是某版本的行为，不能证明当前主机发生了故障。",
    ]
    if status != "repository_or_publisher_license_observed" and not identifier:
        lines += [
            "",
            "> 许可状态为 `needs_review`：入库前必须人工核对具体页面/文件的许可与署名，"
            "在此之前该文档只能作为内部验证数据，不得对外分发。",
        ]
    return "\n".join(lines) + "\n"


def normalize_one(source_id: str, source: dict, entry: dict) -> dict:
    raw_path = KNOWLEDGE / entry["raw_path"]
    if not raw_path.is_file():
        return {"id": source_id, "status": "missing_raw"}
    raw_bytes = raw_path.read_bytes()
    markdown = html_to_markdown(raw_bytes.decode("utf-8", errors="replace"))
    body_chars = len(re.sub(r"\s+", "", markdown))
    if body_chars < MIN_BODY_CHARS:
        return {"id": source_id, "status": "needs_review",
                "reason": f"正文仅 {body_chars} 字符，可能是目录页或提取失败",
                "chars": body_chars}

    document_id = _document_id(source_id, source.get("source_url", ""))
    content_bytes = markdown.encode("utf-8")
    revision = _sha256(content_bytes)[:16]
    out_dir = KNOWLEDGE / "normalized" / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{revision}.md"
    # 固定 LF 换行：manifest 里的 normalized_sha256 是按 LF 字节算的，
    # Windows 默认会把换行写成 CRLF，直接对磁盘字节校验就会失败。
    out_path.write_text(markdown, encoding="utf-8", newline=chr(10))

    notice_dir = KNOWLEDGE / "notices" / (source.get("source_family") or "unknown")
    notice_dir.mkdir(parents=True, exist_ok=True)
    notice_path = notice_dir / f"{source_id}.md"
    notice_path.write_text(notice_markdown(source, entry), encoding="utf-8",
                           newline=chr(10))

    return {
        "id": source_id,
        "status": "normalized",
        "document_id": document_id,
        "revision": revision,
        "source_uri": source.get("source_url", ""),
        "os_family": source.get("os_family", ""),
        "product": source.get("source_family", ""),
        "product_version": entry.get("upstream_version") or source.get("version_scope", ""),
        "license_id": (source.get("license") or {}).get("identifier") or "",
        "license_status": (source.get("license") or {}).get("status") or "needs_review",
        "raw_path": entry["raw_path"],
        "raw_sha256": _sha256(raw_bytes),
        "normalized_path": out_path.relative_to(KNOWLEDGE).as_posix(),
        "normalized_sha256": _sha256(content_bytes),
        "chars": body_chars,
        "notice_path": notice_path.relative_to(KNOWLEDGE).as_posix(),
        "produced_at_utc": _now(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规范化原始快照为 Markdown")
    parser.add_argument("--only", action="append", default=None,
                        help="只处理指定 source id（可重复）")
    args = parser.parse_args(argv)

    sources = load_sources()
    manifest = load_manifest()
    wanted = set(args.only) if args.only else None
    results = []
    for entry in manifest.get("entries", []):
        source_id = entry.get("id")
        if not source_id or source_id not in sources:
            continue
        if wanted and source_id not in wanted:
            continue
        results.append(normalize_one(source_id, sources[source_id], entry))

    out_path = MANIFEST_DIR / f"normalized-{datetime.now(timezone.utc):%Y-%m-%d}.json"
    payload = {
        "generated_at_utc": _now(),
        "note": ("由 scripts/normalize_corpus.py 生成；status=needs_review 的文档"
                 "在人工核对许可前不得对外分发。"),
        "entries": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    ok = [item for item in results if item.get("status") == "normalized"]
    for item in results:
        print(f"[{item['status']:>13}] {item['id']} {item.get('chars', item.get('reason', ''))}")
    print(f"\n完成: {len(ok)}/{len(results)}；manifest: {out_path.relative_to(ROOT).as_posix()}")
    return 0 if len(ok) == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
