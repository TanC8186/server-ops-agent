"""离线检索基线：不依赖 Milvus 的 dense 检索评测（M5 / OPS-03）。

目的：在 Milvus 不可用或未入库时，仍然能给出**可复现的检索质量证据**。
做法：

1. 读 ``knowledge/manifests/normalized-*.json``（``scripts/normalize_corpus.py``
   的产物），用同一个 ``DocumentSplitterService`` 切分（与入库路径一致）；
2. 用真实的本地 embedding（CPU，``EMBEDDING_PROVIDER=local``）编码；
3. 在内存里做 cosine 检索，跑 ``eval/rag/dev.jsonl`` 的标注问题；
4. 输出 recall@k / MRR / 无答案判定，写入 ``eval/rag/results/<timestamp>.json``。

它**不是** Milvus 入库验证：不验证 collection 描述符、索引度量、写入原子性。
这些必须由真实 Milvus 环境单独验证（见 VALIDATION_RESULTS.md 的未完成项）。

用法::

    ./.venv/Scripts/python.exe scripts/eval_retrieval.py [--k 3] [--limit-docs 0]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "knowledge"
MANIFEST_DIR = KNOWLEDGE / "manifests"
EVAL_DIR = ROOT / "eval" / "rag"
RESULT_DIR = EVAL_DIR / "results"

#: 无答案判定阈值。本地 MiniLM 的 cosine 分布很集中：真相关问题 top1 约
#: 0.63–0.82，无关问题也有 0.47–0.57，因此固定 0.35 之类的“常识阈值”没有意义。
#: 默认由 dev 集拟合（分开两类的最优切点），并明确标注“已在该集上拟合，
#: 不能当作盲测结论”；--threshold 可显式指定。
DEFAULT_THRESHOLD: float | None = None


def load_normalized(limit_docs: int = 0, explicit_manifest: str = "") -> list[dict]:
    """加载全部 normalized manifest 的并集。

    以前只读 ``paths[-1]``（最新那个）。G8 追加 AutoDL 语料后，最新 manifest
    只含 AD-01..AD-09，于是 ``eval/rag/dev.jsonl`` 里 11 道可答题的
    ``expected_source_ids``（SD/LK/RH/DK/WS）**全部不在语料里**，
    hit@k 恒为 0 —— 评测照常跑完、照常写出结果，但什么都没测到。

    改为按 id 合并所有 manifest：追加新语料不应该让旧基线失效。
    ``--manifest`` 仍可显式指定单一文件。
    """
    if explicit_manifest:
        paths = [Path(explicit_manifest)]
    else:
        paths = sorted(MANIFEST_DIR.glob("normalized-*.json"))
    if not paths:
        raise SystemExit("缺少 knowledge/manifests/normalized-*.json；先运行 "
                         "scripts/normalize_corpus.py")
    merged: dict[str, dict] = {}
    for path in paths:
        for item in json.loads(path.read_text(encoding="utf-8")).get("entries", []):
            if item.get("status") != "normalized":
                continue
            doc_id = str(item.get("id") or "")
            if doc_id:
                merged[doc_id] = item
    entries = list(merged.values())
    return entries[:limit_docs] if limit_docs else entries


def build_chunks(entries: list[dict]) -> list[dict]:
    from app.services.document_splitter_service import DocumentSplitterService

    splitter = DocumentSplitterService()
    chunks: list[dict] = []
    for entry in entries:
        path = KNOWLEDGE / entry["normalized_path"]
        text = path.read_text(encoding="utf-8")
        docs = splitter.split_document(
            text, entry["normalized_path"],
            document_id=entry["document_id"], revision=entry["revision"],
        )
        for doc in docs:
            metadata = doc.metadata
            chunks.append({
                "chunk_id": metadata.get("_chunk_id", ""),
                "document_id": entry["document_id"],
                "source_id": entry["id"],
                "revision": entry["revision"],
                "text": doc.page_content,
                "heading_path": list(metadata.get("_heading_path") or []),
                "line_start": metadata.get("_line_start"),
                "line_end": metadata.get("_line_end"),
            })
    return chunks


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def embed_all(texts: list[str], batch: int = 32) -> list[list[float]]:
    from app.services.vector_embedding_service import create_embedding_service

    service = create_embedding_service()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch):
        vectors.extend(_normalize(vector) for vector in
                       service.embed_documents(texts[start:start + batch]))
    return vectors


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def load_questions() -> list[dict]:
    path = EVAL_DIR / "dev.jsonl"
    if not path.is_file():
        raise SystemExit(f"缺少标注问题集: {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _fit_threshold(rows: list[dict]) -> float | None:
    """在 dev 集上找使 balanced accuracy 最大的切点（仅用于诊断，非盲测）。"""
    candidates = sorted({row["top_scores"][0] for row in rows if row["top_scores"]})
    best, best_score = None, -1.0
    for cut in candidates:
        correct = sum(
            1 for row in rows
            if (row["top_scores"] and row["top_scores"][0] >= cut) == row["answerable"]
        )
        score = correct / len(rows)
        if score > best_score:
            best, best_score = cut, score
    return best


def evaluate(k: int, docs_limit: int, threshold: float | None = None) -> dict:
    entries = load_normalized(docs_limit)
    started = time.time()
    chunks = build_chunks(entries)
    if not chunks:
        raise SystemExit("没有切分产物；检查 normalized manifest")
    chunk_vectors = embed_all([chunk["text"] for chunk in chunks])
    questions = load_questions()
    query_vectors = embed_all([row["question"] for row in questions])

    rows = []
    for question, vector in zip(questions, query_vectors):
        scored = sorted(
            ({"score": _dot(vector, chunk_vector), "chunk": chunk}
             for chunk, chunk_vector in zip(chunks, chunk_vectors)),
            key=lambda item: item["score"], reverse=True,
        )[:k]
        expected = set(question.get("expected_source_ids") or [])
        ranks = [index for index, item in enumerate(scored, 1)
                 if item["chunk"]["source_id"] in expected]
        hit = bool(ranks)
        rows.append({
            "id": question.get("id"),
            "question": question["question"],
            "expected_source_ids": sorted(expected),
            "answerable": bool(expected),
            "top_source_ids": [item["chunk"]["source_id"] for item in scored],
            "top_scores": [round(item["score"], 4) for item in scored],
            "rank": ranks[0] if ranks else None,
            "reciprocal_rank": round(1 / ranks[0], 4) if ranks else 0.0,
            "hit_at_k": hit,
        })

    cut = threshold if threshold is not None else _fit_threshold(rows)
    for row in rows:
        row["predicted_answerable"] = bool(row["top_scores"]) and (
            cut is not None and row["top_scores"][0] >= cut
        )

    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]
    min_answerable = min((row["top_scores"][0] for row in answerable), default=None)
    max_unanswerable = max((row["top_scores"][0] for row in unanswerable), default=None)
    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "offline_dense_baseline",
        "note": ("内存 cosine 检索，不是 Milvus 入库验证；仅证明切分+embedding+"
                 "检索契约可用，不代表 collection/索引已验收。"),
        "k": k,
        "documents": len(entries),
        "chunks": len(chunks),
        "elapsed_seconds": round(time.time() - started, 1),
        "metrics": {
            "recall_at_k": round(
                sum(1 for row in answerable if row["hit_at_k"]) / len(answerable), 3
            ) if answerable else 0.0,
            "mrr": round(sum(row["reciprocal_rank"] for row in answerable)
                         / len(answerable), 3) if answerable else 0.0,
            "no_answer_precision": round(
                sum(1 for row in unanswerable if not row["predicted_answerable"])
                / len(unanswerable), 3
            ) if unanswerable else None,
            "answerable": len(answerable),
            "unanswerable": len(unanswerable),
        },
        "threshold": cut,
        "threshold_source": ("explicit" if threshold is not None
                             else "fitted_on_dev_set(not a blind claim)"),
        "separation": {
            "min_answerable_top_score": min_answerable,
            "max_unanswerable_top_score": max_unanswerable,
            "overlap": (min_answerable is not None and max_unanswerable is not None
                        and max_unanswerable >= min_answerable),
        },
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线 dense 检索基线")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--limit-docs", type=int, default=0,
                        help="只评测前 N 个文档（调试用）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="无答案判定阈值；不给则在 dev 集上拟合")
    args = parser.parse_args(argv)

    result = evaluate(args.k, args.limit_docs, args.threshold)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RESULT_DIR / f"offline-{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")

    metrics = result["metrics"]
    print(f"documents={result['documents']} chunks={result['chunks']} "
          f"elapsed={result['elapsed_seconds']}s")
    print(f"recall@{args.k}={metrics['recall_at_k']} mrr={metrics['mrr']} "
          f"no_answer_precision={metrics['no_answer_precision']}")
    print(f"threshold={result['threshold']} ({result['threshold_source']}) "
          f"separation={result['separation']}")
    for row in result["rows"]:
        status = "hit" if row["hit_at_k"] else ("n/a" if not row["answerable"] else "MISS")
        print(f"  [{status:>4}] {row['id']} rank={row['rank']} "
              f"top={row['top_source_ids'][:2]} {row['question'][:40]}")
    print(f"\n结果: {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
