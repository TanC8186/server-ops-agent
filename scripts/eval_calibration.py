"""分域/版本感知的门控（gate）校准（G5）。

**为什么需要它**：``scripts/eval_retrieval.py`` 在 14 条 dev 题上拟合出一个全局阈值
0.6315，blind 上却误拒 29/58 可答题；反过来阈值取 0 时 19/19 无答案题被误答。
blind 的分数分布本身有重叠（可答 min 0.3515 < 无答案 max 0.5818），说明**单一全局
阈值在这批语料上不存在干净切点**——不是"再调一点就好"，而是需要按域分别校准。

**为什么用 calibration 而不是 blind**：blind 是 frozen 集，只有在完全没见过的情况下
报告才有意义。在 blind 上选阈值再报告 blind，等于把答案抄进考题里。本脚本因此：

- 输入默认 ``eval/rag/calibrate-dev2.jsonl``（80 行，可答题 56 / 无答案 24）；
- 传入 ``eval/rag/blind.jsonl`` 直接**硬性退出**，并在产物里写
  ``fitted_on.fitted_on_blind: false``；
- 产物的文件名/``kind``/``report_label`` 都标 ``calibration``，且带一句
  "拟合在 calibration 上，不能在 blind 上复报" 的声明。

**桶的设计与限制（诚实说明）**：

- ``by_product`` 是可用的运行时维度：产品/OS 一旦由候选过滤（G5 另一处）确定，
  门控就能按桶取阈值。
- ``by_category`` 目前**只是诊断桶**：category 是评测标注，运行时不携带。除非
  未来有可靠的问题分类器，否则它不能被生产直接按名取用；产物里对每个 category
  桶都写 ``runtime_selectable: false``。
- 无答案行没有 product 归属（它们问的主题不在语料里），所以 product 桶的"负样本"
  复用**全局无答案行**，产物里标 ``negatives_source="global_no_answer_rows"``，
  不假装是"该产品域内的误答率"。
- 桶样本不足（可答题 < ``--min-bucket-samples``，或该桶缺一个类别）时回退全局，
  并写 ``fallback: true`` + ``fallback_reason``。

阈值决策规则：``top_score >= threshold`` 记为"回答"（与
``knowledge_service._gate`` 的比较方向一致）；在候选切点上选**balanced accuracy**
（(TPR+TNR)/2）最大者，平局取**更高**阈值——误答在运维场景的代价高于漏答，
这个取舍写在 ``tie_break`` 字段里而不是藏在代码注释里。

用法::

    # 只校验题集，不连 Milvus
    ./.venv/Scripts/python.exe scripts/eval_calibration.py --dry-run

    # 真实 Milvus 上校准（需要 collection 已导入 11 篇 core 文档）
    ./.venv/Scripts/python.exe scripts/eval_calibration.py \
        --store data/acceptance/milvus-e2e-store.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_blind import (  # noqa: E402 - 需要先修好 sys.path
    CALIBRATION_FILE,
    DEFAULT_STORE,
    DESCRIPTOR_FILE,
    MANIFEST_FILE,
    RESULT_DIR,
    RETRIEVAL_K,
    _sha256_file,
    build_manifest,
    dataset_label,
    load_doc_map,
    load_rows,
    milvus_facts,
    validate,
)

#: 拟合产物文件名前缀。和 blind-*.json 明确分开。
RESULT_PREFIX = "calibration"

#: 桶内可答题样本下限。1 条样本对应 ±0.125 的 recall 波动，低于 8 条时
#: 拟合出来的阈值只是在贴合个别题，不构成证据。
MIN_BUCKET_SAMPLES = 8

TIE_BREAK = ("balanced_accuracy 相同时取更高阈值：运维场景里误答（给出没有依据的"
             "结论）的代价高于漏答（宁可拒答让人去查手册）")


# ── 纯函数：阈值拟合与分桶指标 ──────────────────────────────


def _split(rows: Sequence[dict]) -> tuple[list[float], list[float]]:
    """把行拆成 (可答题 top_score, 无答案题 top_score)，跳过取不到分数的行。"""
    positives = [row["top_score"] for row in rows
                 if row.get("expected_ids") and row.get("top_score") is not None]
    negatives = [row["top_score"] for row in rows
                 if row.get("expect_no_answer") and row.get("top_score") is not None]
    return positives, negatives


def confusion(positives: Sequence[float], negatives: Sequence[float],
              threshold: float) -> dict:
    """在给定阈值下的 TP/FN/FP/TN 与两个比率。分母随字段一起返回，不另算。"""
    tp = sum(1 for score in positives if score >= threshold)
    fn = len(positives) - tp
    fp = sum(1 for score in negatives if score >= threshold)
    tn = len(negatives) - fp
    return {
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "recall": round(tp / len(positives), 4) if positives else None,
        "false_answer_rate": round(fp / len(negatives), 4) if negatives else None,
        "balanced_accuracy": (
            round(0.5 * (tp / len(positives) + tn / len(negatives)), 4)
            if positives and negatives else None),
    }


def fit_threshold(positives: Sequence[float], negatives: Sequence[float], *,
                  min_samples: int = MIN_BUCKET_SAMPLES) -> dict | None:
    """在候选切点上找 balanced accuracy 最大的阈值；样本不足返回 ``None``。

    返回 ``None`` 的两种情况都必须被调用方当作"回退全局"，不能静默用 0：
    - 缺一个类别（只有可答题或只有无答案题）→ balanced accuracy 无定义；
    - 可答题样本 < ``min_samples`` → 拟合结果只是贴合个别题。
    """
    if len(positives) < min_samples or not negatives:
        return None
    candidates = sorted(set(positives) | set(negatives))
    best: dict | None = None
    for cut in candidates:
        metrics = confusion(positives, negatives, cut)
        score = metrics["balanced_accuracy"]
        if score is None:
            continue
        if best is None or score > best["balanced_accuracy"] or (
                score == best["balanced_accuracy"] and cut > best["threshold"]):
            best = {**metrics, "threshold": round(cut, 4)}
    return best


def fit_bucket(rows: Sequence[dict], *, global_threshold: float,
               global_negatives: Sequence[float],
               min_samples: int = MIN_BUCKET_SAMPLES) -> dict:
    """拟合一个桶；样本不足/缺类别时回退全局阈值并标注原因。"""
    positives, own_negatives = _split(rows)
    fit = fit_threshold(positives, own_negatives or list(global_negatives),
                        min_samples=min_samples)
    # 分桶时负样本来自全局（无答案行没有 product/category 之外的可归属域）。
    negatives = list(own_negatives) if own_negatives else list(global_negatives)
    if fit is None:
        reason = ("no_answerable_rows" if not positives
                  else "insufficient_answerable_samples")
        used = global_threshold
        fallback = True
        fitted = None
    else:
        used = fit["threshold"]
        fallback = False
        reason = ""
        fitted = fit["threshold"]
    metrics = confusion(positives, negatives, used)
    return {
        "rows": len(rows),
        "answerable_samples": len(positives),
        "no_answer_samples": len(negatives),
        "negatives_source": ("own_bucket" if own_negatives
                             else "global_no_answer_rows"),
        "threshold": used,
        "fitted_threshold": fitted,
        "fallback": fallback,
        "fallback_reason": reason,
        **{key: metrics[key] for key in
           ("tp", "fn", "fp", "tn", "recall", "false_answer_rate",
            "balanced_accuracy")},
    }


def build_profile(rows: Sequence[dict], *, min_samples: int = MIN_BUCKET_SAMPLES) -> dict:
    """拟合全局 + by_category + by_product，并返回可直接被生产读取的 gate_profile。

    ``gate_profile`` 只放"查表就能用"的阈值；样本数/TP/FN/误答率等证据放在
    ``buckets`` 里。这样生产侧读一个扁平结构，审计侧仍能逐桶核对分母。
    """
    global_positives, global_negatives = _split(rows)
    global_fit = fit_threshold(global_positives, global_negatives,
                               min_samples=min_samples)
    if global_fit is None:
        raise SystemExit(
            "全局阈值无法拟合：可答题或全量无答案题不足"
            f"（可答 {len(global_positives)} / 无答 {len(global_negatives)}，"
            f"下限 {min_samples}）。样本不足时给出的任何阈值都不构成证据。")
    global_threshold = global_fit["threshold"]

    by_category_rows: dict[str, list[dict]] = {}
    by_product_rows: dict[str, list[dict]] = {}
    for row in rows:
        by_category_rows.setdefault(row.get("category", ""), []).append(row)
        if row.get("product"):
            by_product_rows.setdefault(row["product"], []).append(row)

    category_buckets = {
        name: fit_bucket(group, global_threshold=global_threshold,
                         global_negatives=global_negatives,
                         min_samples=min_samples)
        for name, group in sorted(by_category_rows.items())
    }
    product_buckets = {
        name: fit_bucket(group, global_threshold=global_threshold,
                         global_negatives=global_negatives,
                         min_samples=min_samples)
        for name, group in sorted(by_product_rows.items())
    }

    gate_profile = {
        "mode": "domain",
        "global": global_threshold,
        "by_category": {name: bucket["threshold"]
                        for name, bucket in category_buckets.items()},
        "by_product": {name: bucket["threshold"]
                       for name, bucket in product_buckets.items()},
        "fallback": "global",
        "fitted_on": {},  # 由调用方补题集 hash 等事实，避免纯函数依赖 IO
    }
    # 桶是否可被运行时按名取用：product 可以（候选过滤已确定产品），
    # category 只是一个评测标注，运行时没有这个值。
    for bucket in category_buckets.values():
        bucket["runtime_selectable"] = False
    for bucket in product_buckets.values():
        bucket["runtime_selectable"] = True
    return {
        "gate_profile": gate_profile,
        "global_fit": global_fit,
        "buckets": {
            "by_category": category_buckets,
            "by_product": product_buckets,
        },
    }


def apply_profile(rows: Sequence[dict], profile: dict) -> dict:
    """按 profile 判定每一行（用于给出"这套策略在 calibration 上的样子"）。

    这是**样本内**指标：阈值就在同一批数据上拟合，因此不能当作泛化结论。
    """
    def decision(row: dict) -> bool | None:
        score = row.get("top_score")
        if score is None:
            return None
        # 取值优先级：product（运行时可确定）> category（仅诊断口径）> global。
        # 用优先级而不是取多个桶的最大值：多个桶都"适用"时取最大值会让阈值
        # 实际由最严的桶决定，但报告里看不出是哪一桶生效，难以复核。
        product = row.get("product") or ""
        if product and product in profile["by_product"]:
            threshold = profile["by_product"][product]
        else:
            category = row.get("category") or ""
            if row.get("use_category_bucket") and category in profile["by_category"]:
                threshold = profile["by_category"][category]
            else:
                threshold = profile["global"]
        return score >= threshold

    positives, negatives = [], []
    errors = 0
    for row in rows:
        answer = decision(row)
        if answer is None:
            errors += 1
            continue
        if row.get("expected_ids"):
            positives.append(1.0 if answer else 0.0)
        elif row.get("expect_no_answer"):
            negatives.append(1.0 if answer else 0.0)
    return {
        "decision_rule": "product 桶 > （可选）category 桶 > global；"
                         "判定为 answered 当且仅当 top_score >= 命中阈值",
        "in_sample": True,
        "answerable_rows": len(positives),
        "no_answer_rows": len(negatives),
        "recall": round(statistics.fmean(positives), 4) if positives else None,
        "false_answer_rate": round(statistics.fmean(negatives), 4) if negatives else None,
        "errors": errors,
    }


# ── 真实检索 ──────────────────────────────────────────────


def collect_scores(rows: Sequence[dict], doc_map: dict[str, str],
                   timeout: float) -> list[dict]:
    """对每行跑一次真实 ``VectorStoreRetriever.search``，保留 top_score 与分母。

    只做一次检索：校准只需要分数，不需要再跑一遍 gate（gate 的比较规则已经
    在 ``fit_threshold`` 里按同一方向实现）。
    """
    from app.services.knowledge_service import VectorStoreRetriever

    retriever = VectorStoreRetriever()
    collected: list[dict] = []
    for row in rows:
        started = time.perf_counter()
        try:
            chunks = list(retriever.search(row["question"], RETRIEVAL_K))
            error = None
        except Exception as exc:  # noqa: BLE001 - 依赖失败如实记录，不伪造分数
            chunks, error = [], f"{type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - started) * 1000
        doc_ids = [doc_map.get(chunk.document_id, chunk.document_id) for chunk in chunks]
        collected.append({
            "id": row["id"],
            "category": row["category"],
            "question": row.get("question", ""),
            "product": row.get("product", ""),
            "os_family": row.get("os_family", ""),
            "product_version": row.get("product_version", ""),
            "expected_ids": list(row.get("expected_ids") or []),
            "expect_no_answer": bool(row.get("expect_no_answer")),
            "top_score": round(chunks[0].score, 4) if chunks else None,
            "top_doc_ids": doc_ids,
            "hit_at_5": bool(set(doc_ids) & set(row.get("expected_ids") or [])),
            "error": error,
            "search_ms": round(elapsed, 1),
            "timeout_seconds": timeout,
        })
    return collected


def _store_evidence(store_file: Path) -> dict:
    """阈值拟合时所用 store / 语料 / embedding 快照的字节级证据。

    ``fitted_on`` 以前只有题集 sha256：换 collection 或重跑 embedding 后，旧
    profile 的阈值仍会被无差别读走，报告无法判断这个阈值是不是在另一份向量
    上拟合的。这里把三样能够逐字节复核的事实写进 ``fitted_on``，供
    ``eval_generation`` 在读取时对照当前磁盘状态。

    store 文件本身的 sha256 只作快照标识：上传/重试会改写 jobs 与时间戳，
    所以一致性判定必须以 documents 的 descriptor hash 集合为准。
    """
    descriptor_hash = ""
    if DESCRIPTOR_FILE.is_file():
        try:
            descriptor_hash = str(json.loads(
                DESCRIPTOR_FILE.read_text(encoding="utf-8")).get("hash", ""))
        except ValueError:
            descriptor_hash = ""
    document_hashes: set[str] = set()
    if store_file.is_file():
        try:
            store = json.loads(store_file.read_text(encoding="utf-8"))
            for doc in (store.get("documents") or {}).values():
                value = str(doc.get("embedding_descriptor_hash") or "")
                if value:
                    document_hashes.add(value)
        except ValueError:
            document_hashes = set()
    return {
        "store_document_ids": sorted(str(key) for key in (store.get("documents") or {}))
        if store_file.is_file() else [],
        "corpus_manifest_sha256": _sha256_file(MANIFEST_FILE)
        if MANIFEST_FILE.is_file() else "missing",
        "embedding_descriptor_sha256": _sha256_file(DESCRIPTOR_FILE)
        if DESCRIPTOR_FILE.is_file() else "missing",
        "embedding_descriptor_hash": descriptor_hash,
        "knowledge_store_file": store_file.relative_to(ROOT).as_posix()
        if store_file.is_relative_to(ROOT) else str(store_file),
        "knowledge_store_sha256": _sha256_file(store_file)
        if store_file.is_file() else "missing",
        "store_document_descriptor_hashes": sorted(document_hashes),
    }


# ── 入口 ──────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分域/版本感知 gate 校准（真实 Milvus）")
    parser.add_argument("--set", dest="dataset", type=str, default="",
                        help="calibration 题集路径（默认 eval/rag/calibrate-dev2.jsonl）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验题集，不连 Milvus、不写结果")
    parser.add_argument("--store", type=str, default="",
                        help="KNOWLEDGE_STORE_FILE（默认 data/acceptance/milvus-e2e-store.json）")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次检索超时（秒）")
    parser.add_argument("--min-bucket-samples", type=int, default=MIN_BUCKET_SAMPLES,
                        help="桶内可答题样本下限，低于则回退全局")
    args = parser.parse_args(argv)

    dataset_file = Path(args.dataset) if args.dataset else CALIBRATION_FILE
    label = dataset_label(dataset_file)
    if label == "blind":
        raise SystemExit(
            "拒绝在 blind 上拟合：blind 是 frozen 盲测集，在它上面选阈值再报告 "
            "blind 等于抄答案。请用 eval/rag/calibrate-dev2.jsonl。")

    rows = load_rows(dataset_file)
    problems = validate(rows, label="calibration")
    if problems:
        print(f"{dataset_file.name} 校验失败：", file=sys.stderr)
        for problem in problems:
            print("  -", problem, file=sys.stderr)
        return 1
    print(f"{dataset_file.name}: {len(rows)} 行 "
          f"(可答 {sum(1 for row in rows if not row['expect_no_answer'])} / "
          f"无答 {sum(1 for row in rows if row['expect_no_answer'])})")
    if args.dry_run:
        print("\n--dry-run：题集校验通过，未连接 Milvus，未写结果文件。")
        return 0

    store_file = Path(args.store) if args.store else DEFAULT_STORE
    if not store_file.is_file():
        raise SystemExit(f"store 文件不存在: {store_file}")
    os.environ["KNOWLEDGE_STORE_FILE"] = str(store_file)
    doc_map, store_uris = load_doc_map(store_file)
    if not doc_map:
        raise SystemExit(f"store {store_file} 里没有 document_id 能映射回 11 个 core id")
    unmapped = sorted(set(store_uris) - set(doc_map))

    started = time.time()
    collected = collect_scores(rows, doc_map, args.timeout)
    errors = [row for row in collected if row["error"]]
    print(f"检索完成：{len(collected)} 行，失败 {len(errors)} 行，"
          f"{time.time() - started:.1f}s")

    fitted = build_profile(collected, min_samples=args.min_bucket_samples)
    profile = fitted["gate_profile"]
    dataset_meta = {
        "dataset": dataset_file.relative_to(ROOT).as_posix()
        if dataset_file.is_relative_to(ROOT) else str(dataset_file),
        "sha256": _sha256_file(dataset_file),
        "rows": len(collected),
        "answerable_rows": sum(1 for row in collected if row["expected_ids"]),
        "no_answer_rows": sum(1 for row in collected if row["expect_no_answer"]),
        "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fitted_on_blind": False,
        "retrieval": "real Milvus via app.services.knowledge_service.VectorStoreRetriever",
        "model": "embedding descriptor on disk（见 manifest.embedding_descriptor_hash）",
        # 快照证据放在 fitted_on 里而不是只放 payload.manifest：production 侧
        # 读的是 gate_profile，它必须自带"这份阈值是在哪份语料/向量上拟合的"。
        "store_evidence": _store_evidence(store_file),
    }
    profile["fitted_on"] = dataset_meta

    # 分域策略在 calibration 上的样本内表现：category 桶不会自动生效（运行时
    # 没有 category），所以分别给"仅 product"/"product+category"两个口径。
    plain = [{**row, "use_category_bucket": False} for row in collected]
    with_category = [{**row, "use_category_bucket": True} for row in collected]

    payload = {
        "generated_at_utc": dataset_meta["fitted_at_utc"],
        "kind": "calibration_gate_profile",
        "report_label": "calibration",
        "note": ("阈值拟合在 calibration 集上，**不能在 blind 上复报**。"
                 "by_category 桶是诊断口径（运行时不携带 category），"
                 "by_product 桶可直接被生产读取。无答案行没有 product 归属，"
                 "product 桶的负样本取自全局无答案行。"),
        "tie_break": TIE_BREAK,
        "min_bucket_samples": args.min_bucket_samples,
        "gate_profile": profile,
        "global_fit": fitted["global_fit"],
        "buckets": fitted["buckets"],
        "in_sample": {
            "product_and_global": apply_profile(plain, profile),
            "product_category_and_global": apply_profile(with_category, profile),
        },
        "manifest": build_manifest(
            {"value": profile["global"], "source": "fitted_on:calibration",
             "fitted_on": dataset_meta["dataset"], "fitted_on_blind": False},
            store_file, dataset_file, "calibration"),
        "milvus": milvus_facts(),
        "id_mapping": {"unmapped_document_ids": unmapped},
        "denominators": {
            "total": len(collected),
            "answerable_rows": sum(1 for row in collected if row["expected_ids"]),
            "no_answer_rows": sum(1 for row in collected if row["expect_no_answer"]),
            "errors": len(errors),
            "rows_without_score": sum(1 for row in collected if row["top_score"] is None),
        },
        "elapsed_seconds": round(time.time() - started, 1),
        "rows": collected,
    }

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULT_DIR / f"{RESULT_PREFIX}-{stamp}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    global_fit = fitted["global_fit"]
    print(f"\n全局阈值 = {profile['global']} "
          f"(TP={global_fit['tp']} FN={global_fit['fn']} "
          f"FP={global_fit['fp']} TN={global_fit['tn']} "
          f"recall={global_fit['recall']} 误答率={global_fit['false_answer_rate']})")
    print("by_product:")
    for name, bucket in fitted["buckets"]["by_product"].items():
        print(f"  {name:12s} thr={bucket['threshold']:<7} "
              f"n={bucket['answerable_samples']:<3} fallback={bucket['fallback']}")
    print("by_category:")
    for name, bucket in fitted["buckets"]["by_category"].items():
        print(f"  {name:20s} thr={bucket['threshold']:<7} "
              f"n={bucket['answerable_samples']:<3} fallback={bucket['fallback']}")
    print(f"样本内（product+global）：{payload['in_sample']['product_and_global']}")
    print(f"\n结果: {out_path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
