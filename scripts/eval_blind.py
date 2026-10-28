"""Blind RAG 检索评测（G2.2）：用**真实 Milvus**跑独立盲测集，并给出诚实的分母。

与 ``scripts/eval_retrieval.py`` 的区别（两者互补，不互相替代）：

====================  ==============================  ==============================
                      eval_retrieval.py               eval_blind.py（本脚本）
====================  ==============================  ==============================
数据                 在内存里重新切分+编码             已入库的真实 Milvus collection
问题集               eval/rag/dev.jsonl（14 条）        eval/rag/blind.jsonl（盲测集）
阈值                 在 dev 上拟合                       **沿用 dev 拟合的阈值，不在 blind 上拟合**
====================  ==============================  ==============================

为什么必须分开：dev 集的 14 条问题在阈值拟合和人工调试中被反复看过，
在那里得到的满分不能当作泛化结论。blind 集在写题时只对照语料原文
（每行 ``notes`` 抄录一句锚点短语），不看检索结果排序。

指标口径（都会写进结果文件的 ``denominators``）：

- **Recall@k / MRR / nDCG@5** 只在 ``expected_ids`` 非空的行上计算
  （"可答题"分母）。逐 chunk 判定：chunk 所属文档 ∈ expected 即相关。
- **无答案误答率** 只在 ``expect_no_answer=true`` 的行上计算：这些行
  经检索+门控后仍然返回高于阈值的命中，即为一次误答（false positive）。
- **拒答正确率** 是上面两个分母的补集视角：``refused / no_answer_rows``。
- 任一行出错（依赖不可用、超时、索引非法）都单独计数，**不静默丢弃**：
  分母里记 ``errors``，并且该行在可答/无答案统计中都不计入分子。

用法::

    # 只校验 schema，不连 Milvus
    ./.venv/Scripts/python.exe scripts/eval_blind.py --dry-run

    # 真实评测（需要 localhost:19530 有 Milvus 且已导入）
    ./.venv/Scripts/python.exe scripts/eval_blind.py \
        --store data/acceptance/milvus-e2e-store.json

    # 快速冒烟：只跑前 5 行
    ./.venv/Scripts/python.exe scripts/eval_blind.py --limit 5

``--set`` 可以指定别的题集（G5 的 ``calibrate-dev2.jsonl``）。校验规则完全相同，
但产物会换 ``kind``/文件名前缀，并写 ``report_label: calibration``——
calibration 上拟合出来的阈值不能当成 blind 报告引用，这条约束由脚本本身执行，
不依赖使用者自觉。
"""

from __future__ import annotations

import argparse

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BLIND_FILE = ROOT / "eval" / "rag" / "blind.jsonl"
DEV_RESULT_DIR = ROOT / "eval" / "rag" / "results"
RESULT_DIR = DEV_RESULT_DIR
MANIFEST_FILE = ROOT / "knowledge" / "manifests" / "normalized-2026-10-26.json"
DESCRIPTOR_FILE = ROOT / "data" / "embedding_descriptor.json"
DEFAULT_STORE = ROOT / "data" / "acceptance" / "milvus-e2e-store.json"

#: 11 个 core 来源。blind 集的 expected_ids 必须全部落在这个集合内。
CORE_IDS = {
    "SD-01", "SD-02", "LK-01", "RH-01",
    "DK-01", "DK-02", "DK-03",
    "WS-01", "WS-02", "WS-03", "WS-04",
}

#: 允许的 category；新增类别必须同时改这里和 tests/test_blind_eval_schema.py。
ALLOWED_CATEGORIES = {
    "zh_to_en_manual",      # 中文问题检索英文手册
    "exact_command",        # 精确命令/参数/错误码
    "os_version_conflict",  # Linux vs Windows / 版本冲突
    "no_answer",            # 语料中没有依据
    "misleading_trap",      # 相似但不支持结论
    "code_config",          # 代码/YAML/完整命令
    "prompt_injection",     # 问题文本里含注入指令
    "lifecycle_refusal",    # active revision / ACL / 索引不可用 → 期望拒答
}

REQUIRED_KEYS = {"id", "question", "expected_ids", "expect_no_answer",
                 "category", "notes"}

#: 结果文件里必须出现的分母键。缺任何一个都说明统计口径漏了口径。
DENOMINATOR_KEYS = ("total", "answered", "refused", "no_answer_rows",
                    "answerable_rows", "errors", "answered_rows")

RETRIEVAL_K = 5

#: notes 里的锚点声明。格式在 build 脚本与 tests/test_blind_eval_schema.py 中
#: 保持一致；脚本启动时逐条核对短语是否真的出现在语料正文里。
_ANCHOR_PATTERN = re.compile(r"锚点\s*=\s*([A-Z]{2}-\d{2})「(.+?)」")


# ── 输入 ──────────────────────────────────────────────────


#: G5 分域校准集。阈值会在它上面拟合，因此**不是** blind：产物换文件名与 kind，
#: 并由 ``dataset_label`` 标注，禁止把它当 blind 报告复述。
CALIBRATION_FILE = ROOT / "eval" / "rag" / "calibrate-dev2.jsonl"

#: 校准行可选携带的域字段（取值来自 normalized manifest，不是语义推断）。
OPTIONAL_DOMAIN_KEYS = {"os_family", "product", "product_version"}

#: 生成式评测用的参考答案要点；必须能从锚点派生出可核验的 token。
OPTIONAL_REFERENCE_KEY = "reference_points"


def load_rows(path: Path, limit: int = 0) -> list[dict]:
    """读 JSONL 题集；``limit`` 只截取返回条数，不参与校验。"""
    if not path.is_file():
        raise SystemExit(f"缺少题集: {path}")
    rows: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name} 第 {lineno} 行不是合法 JSON: {exc}") from exc
    return rows[:limit] if limit else rows


def load_blind(limit: int = 0) -> list[dict]:
    """兼容旧调用：盲测集。"""
    return load_rows(BLIND_FILE, limit)


def dataset_label(path: Path) -> str:
    """题集身份。只有 frozen blind 才是 ``blind``。

    判据是**内容 hash**（不是文件名）：把 blind 复制成别的文件名再跑，仍然
    会被认成 blind 并套用"不得在其上拟合"的约束。文件名层面把 calibration
    结果叫成 blind，是这次分域校准最容易犯的口径错误（在 calibration 上拟合
    的阈值被当成盲测结论引用），所以由代码而不是文档来保证。
    """
    try:
        if path.is_file() and BLIND_FILE.is_file() and \
                _sha256_file(path) == _sha256_file(BLIND_FILE):
            return "blind"
        resolved = path.resolve()
    except OSError:
        resolved = path
    return "blind" if resolved == BLIND_FILE.resolve() else "calibration"


def _corpus_texts() -> dict[str, str]:
    """11 个 core 来源的 normalized 正文（用于核对 notes 里的锚点短语）。"""
    texts: dict[str, str] = {}
    for source_id in CORE_IDS:
        paths = sorted((ROOT / "knowledge" / "normalized" / source_id).glob("*.md"))
        if paths:
            texts[source_id] = paths[-1].read_text(encoding="utf-8")
    return texts


def _anchor_problems(rid: str, row: dict, corpus: dict[str, str]) -> list[str]:
    """核对 ``notes`` 里的 ``锚点=<SOURCE-ID>「<原文片段>」`` 是否真的存在于正文。"""
    problems: list[str] = []
    matches = _ANCHOR_PATTERN.findall(str(row.get("notes", "")))
    if not matches:
        return [f"{rid}: notes 缺少 '锚点=<SOURCE-ID>「原文片段」'，题目来源不可审计"]
    for source_id, anchor in matches:
        if source_id not in CORE_IDS:
            problems.append(f"{rid}: 锚点来源 {source_id} 不是 11 个 core id 之一")
            continue
        if source_id not in row.get("expected_ids", []):
            problems.append(f"{rid}: 锚点来源 {source_id} 不在 expected_ids 内")
        text = corpus.get(source_id)
        if text is None:
            problems.append(f"{rid}: 锚点来源 {source_id} 没有 normalized 正文")
        elif anchor not in text:
            problems.append(f"{rid}: 锚点短语不在 {source_id} 正文中: {anchor!r}")
    return problems


def _optional_domain_problems(rid: str, row: dict, manifest: dict[str, dict],
                              corpus: dict[str, str]) -> list[str]:
    """校准行的域字段必须来自 manifest、参考答案要点必须来自锚点所在正文。

    gate 会按 product 分桶拟合阈值；如果 product 写错，桶就落到了错误的语料上，
    产出的阈值看起来合理但对着错文档生效。同理，reference_points 若只是自己
    编的短语，事实正确率就变成了"答案里有没有出现这句话"，而不是"有没有答对"。
    所以这里逐条对齐 manifest 与语料正文。
    """
    problems: list[str] = []
    source_id = (row.get("expected_ids") or [""])[0]
    known = manifest.get(source_id, {})
    for key in OPTIONAL_DOMAIN_KEYS:
        if key not in row:
            problems.append(f"{rid}: 缺少可选域字段 {key}（分域校准需要）")
            continue
        value = str(row.get(key) or "")
        if not value:
            problems.append(f"{rid}: 域字段 {key} 为空")
        elif key in known and value != str(known.get(key) or ""):
            problems.append(
                f"{rid}: 域字段 {key}={value!r} 与 manifest 的 "
                f"{known.get(key)!r} 不一致")
    references = row.get(OPTIONAL_REFERENCE_KEY)
    if references is None:
        problems.append(f"{rid}: 缺少 {OPTIONAL_REFERENCE_KEY}（事实正确率的判定依据）")
    elif not isinstance(references, list) or not references:
        problems.append(f"{rid}: {OPTIONAL_REFERENCE_KEY} 必须是非空字符串数组")
    elif not all(isinstance(item, str) and item.strip() for item in references):
        problems.append(f"{rid}: {OPTIONAL_REFERENCE_KEY} 含空项")
    else:
        text = (corpus.get(source_id) or "").lower()
        for point in references:
            if point.lower() not in text:
                problems.append(
                    f"{rid}: reference_point {point!r} 不在 {source_id} 正文中"
                    "（要点必须能从锚点所在原文派生）")
    return problems


def validate(rows: list[dict], *, label: str = "blind") -> list[str]:
    """schema + 可追溯性校验。返回问题列表（空表示通过）。

    ``label="calibration"`` 时追加域字段/参考答案要点检查，并把行数区间
    换成 calibration 的 60–100；其余判定与 blind **完全相同**——校准集同样
    不允许出现语料里查不到的题。
    """
    problems: list[str] = []
    seen: set[str] = set()
    expected_text = _corpus_texts()
    manifest = _manifest_domains()
    for row in rows:
        rid = str(row.get("id") or "<无 id>")
        missing = REQUIRED_KEYS - set(row)
        if missing:
            problems.append(f"{rid}: 缺少字段 {sorted(missing)}")
            continue
        if rid in seen:
            problems.append(f"{rid}: id 重复")
        seen.add(rid)
        if not str(row["question"]).strip():
            problems.append(f"{rid}: question 为空")
        if not str(row["notes"]).strip():
            problems.append(f"{rid}: notes 为空（无法审计题目来源）")
        if row["category"] not in ALLOWED_CATEGORIES:
            problems.append(f"{rid}: 未知 category {row['category']!r}")
        expected = row["expected_ids"]
        if not isinstance(expected, list):
            problems.append(f"{rid}: expected_ids 不是数组")
            continue
        extra = set(expected) - CORE_IDS
        if extra:
            problems.append(f"{rid}: expected_ids 超出 11 个 core id: {sorted(extra)}")
        if row["expect_no_answer"]:
            if expected:
                problems.append(f"{rid}: expect_no_answer=true 却给了 expected_ids")
        elif not expected:
            problems.append(f"{rid}: 可答题却没有 expected_ids")
        # 可追溯性：可答题必须在 notes 里声明锚点，且锚点短语真的在语料正文中。
        if not row["expect_no_answer"]:
            problems.extend(_anchor_problems(rid, row, expected_text))
            if label == "calibration":
                problems.extend(_optional_domain_problems(
                    rid, row, manifest, expected_text))
    if len(rows) > 100 or len(rows) < (60 if label == "calibration" else 50):
        problems.append(
            f"{label} 题集行数 {len(rows)} 不在 "
            f"{60 if label == 'calibration' else 50}–100 区间"
            "（若有缩减理由需在 eval/rag/README.md 记录）")
    no_answer = sum(1 for row in rows if row.get("expect_no_answer"))
    if no_answer < 8:
        problems.append(f"expect_no_answer 行数 {no_answer} < 8（要求至少 8 条）")
    if label == "calibration":
        answerable = len(rows) - no_answer
        ratio = answerable / len(rows) if rows else 0.0
        if not 0.60 <= ratio <= 0.80:
            problems.append(
                f"calibration 可答题占比 {ratio:.3f} 不在 0.60–0.80（分域 gate 需要"
                "可答题与无答案题都有足够样本，偏斜会让阈值只拟合到一边）")
    return problems


def _manifest_domains() -> dict[str, dict]:
    """``source_id -> {os_family, product, product_version}``（来自 normalized manifest）。"""
    if not MANIFEST_FILE.is_file():
        return {}
    entries = json.loads(MANIFEST_FILE.read_text(encoding="utf-8")).get("entries", [])
    return {
        entry["id"]: {
            "os_family": entry.get("os_family", ""),
            "product": entry.get("product", ""),
            "product_version": entry.get("product_version", ""),
        }
        for entry in entries
        if entry.get("id")
    }


def load_doc_map(store_file: Path) -> dict[str, str]:
    """``document_id -> source_id``。

    **不能只读 normalized manifest**：``scripts/normalize_corpus.py`` 用
    ``f"{source_id}|{url}"`` 派生 manifest 里的 ``document_id``，而运行时的
    ``knowledge_store.document_id_for(source_uri)`` 用规范化后的 source_uri
    单键派生，两者不同（例如 SD-01：manifest 是 ``doc-453500c16e01``，
    入库后是 ``doc-4ca55974894e``）。用 manifest 直连 Milvus 会让所有可答题
    的 rank 都算成 None——第一次真实运行就是这么得到 Recall@5=0.0 的，
    那是 join 错误，不是检索质量。

    所以这里以 **store 文件里实际记录的 source_uri** 为准反查 source_id，
    并把无法映射的 document_id 单独返回，由调用方写进 manifest 供审计。
    """
    from app.services.knowledge_store import normalize_source_uri

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    uri_to_source = {
        normalize_source_uri(entry["source_uri"]): entry["id"]
        for entry in manifest.get("entries", [])
        if entry.get("status") == "normalized" and entry.get("source_uri")
    }
    doc_map: dict[str, str] = {}
    store_uris: dict[str, str] = {}
    if store_file.is_file():
        store = json.loads(store_file.read_text(encoding="utf-8"))
        for document_id, doc in (store.get("documents") or {}).items():
            uri = normalize_source_uri(doc.get("source_uri", ""))
            store_uris[document_id] = uri
            source_id = uri_to_source.get(uri)
            if source_id:
                doc_map[document_id] = source_id
    return doc_map, store_uris


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 阈值 ──────────────────────────────────────────────────


def resolve_threshold(explicit: float | None, dev_result: Path | None) -> dict:
    """取阈值并记录来源。**绝不**在 blind 上拟合。

    优先级：命令行 ``--threshold`` > dev 结果文件里的 ``threshold`` >
    ``config.rag_min_score``。若三者都拿不到有意义的值（<=0），直接报错——
    0 会让门控形同虚设，从而把无答案误答率写成 0 这种假结论。
    """
    if explicit is not None:
        return {"value": float(explicit), "source": "explicit:--threshold",
                "fitted_on": "unknown", "fitted_on_blind": False}
    if dev_result is not None and dev_result.is_file():
        payload = json.loads(dev_result.read_text(encoding="utf-8"))
        value = payload.get("threshold")
        if value is not None and float(value) > 0:
            return {
                "value": float(value),
                "source": f"fitted_on_dev_set:{dev_result.relative_to(ROOT).as_posix()}",
                "fitted_on": "eval/rag/dev.jsonl (14 rows, NOT blind)",
                "fitted_on_blind": False,
                "dev_metrics_at_that_threshold": payload.get("metrics"),
            }
    from app.config import config

    value = float(getattr(config, "rag_min_score", 0.0) or 0.0)
    if value > 0:
        return {"value": value, "source": "config:rag_min_score",
                "fitted_on": "unknown", "fitted_on_blind": False}
    raise SystemExit(
        "没有可用的无答案阈值：--threshold 未给、dev 结果文件缺失/无阈值、"
        "config.rag_min_score <= 0。用 0 当阈值会把无答案误答率算成 0，"
        "属于假结论，因此拒绝继续。"
    )


def find_latest_dev_result() -> Path | None:
    if not DEV_RESULT_DIR.is_dir():
        return None
    files = sorted(DEV_RESULT_DIR.glob("offline-*.json"))
    return files[-1] if files else None


# ── 指标 ──────────────────────────────────────────────────


def _ndcg_at_k(doc_ids: list[str], expected: set[str], k: int) -> float:
    """二值增益的 nDCG@k，**按文档去重后的首次出现位置**计增益。

    相关性标注是文档级的（``expected_ids`` 是 source id），而 top-k 是
    chunk 级的：同一文档的 5 个 chunk 都算相关，会把 DCG 累加到超过理想
    DCG（第一次真实运行就得到 nDCG@5=2.18 这种不可能的值）。所以先去重，
    每个文档只在它首次出现的位置贡献一次增益。
    """
    ranked_docs: list[str] = []
    seen: set[str] = set()
    for doc in doc_ids:
        if doc not in seen:
            seen.add(doc)
            ranked_docs.append(doc)
    gains = [1.0 if doc in expected else 0.0 for doc in ranked_docs[:k]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = [1.0] * min(len(expected), k)
    idcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


# ── 真实检索 ──────────────────────────────────────────────


def milvus_facts() -> dict:
    facts: dict = {"connected": False}
    try:
        from app.core.milvus_client import milvus_manager

        milvus_manager.connect()
        collection = milvus_manager.get_collection()
        if collection is None:
            facts["error"] = "collection 不存在"
            return facts
        facts["connected"] = True
        facts["collection"] = collection.name
        facts["num_entities"] = int(collection.num_entities)
        facts["indexes"] = [
            {"field": getattr(index, "field_name", ""),
             "params": dict(getattr(index, "params", {}) or {})}
            for index in (collection.indexes or [])
        ]
        for field in collection.schema.fields:
            if field.name == "vector":
                facts["dim"] = getattr(field.params, "get", lambda *_: None)("dim")
        facts["metric_type"] = next(
            (item["params"].get("metric_type") for item in facts["indexes"]
             if item["params"].get("metric_type")), "")
    except Exception as exc:  # noqa: BLE001 - 采集失败如实报告，不伪造
        facts["error"] = f"{type(exc).__name__}: {exc}"
    return facts


def run_rows(rows: list[dict], threshold: float, doc_map: dict[str, str],
             timeout: float) -> tuple[list[dict], dict]:
    """对每行跑真实 ``VectorStoreRetriever``，并额外走一次检索+门控路径。"""
    from app.config import config
    from app.services import knowledge_service
    from app.services.knowledge_service import VectorStoreRetriever

    retriever = VectorStoreRetriever()
    max_chunks = int(getattr(config, "rag_max_chunks", 8))
    results: list[dict] = []
    for row in rows:
        question = row["question"]
        expected = set(row.get("expected_ids") or [])
        started = time.perf_counter()
        try:
            chunks = list(retriever.search(question, RETRIEVAL_K))
            search_ms = (time.perf_counter() - started) * 1000
            error = None
        except Exception as exc:  # noqa: BLE001
            chunks, search_ms = [], None
            error = f"{type(exc).__name__}: {exc}"

        doc_ids = [doc_map.get(chunk.document_id, chunk.document_id) for chunk in chunks]
        ranks = [index for index, doc in enumerate(doc_ids, 1) if doc in expected]
        top_score = chunks[0].score if chunks else None

        # 检索 + 门控（与 answer_question 内部调用的同一个函数）。
        gate_status: str | None = None
        gate_reason = ""
        gate_ms: float | None = None
        gate_hits = 0
        if error is None:
            gate_started = time.perf_counter()
            try:
                gate = knowledge_service._search(  # noqa: SLF001 - 评测需要同一路径
                    retriever, question, RETRIEVAL_K, timeout, threshold, max_chunks,
                )
                gate_status = str(gate.status)
                gate_reason = gate.reason or ""
                gate_hits = len(gate.chunks)
            except Exception as exc:  # noqa: BLE001
                gate_status = f"error:{type(exc).__name__}"
                gate_reason = str(exc)
            gate_ms = (time.perf_counter() - gate_started) * 1000

        results.append({
            "id": row["id"],
            "category": row["category"],
            "question": question,
            "expected_ids": sorted(expected),
            "expect_no_answer": bool(row["expect_no_answer"]),
            "notes": row["notes"],
            # 分域字段与参考答案要点原样带进逐行结果：calibration 拟合与生成式
            # 评测都直接消费这份逐行数据，避免两处各读一遍题集而口径漂移。
            "os_family": row.get("os_family", ""),
            "product": row.get("product", ""),
            "product_version": row.get("product_version", ""),
            "reference_points": list(row.get("reference_points") or []),
            "error": error,
            "top_doc_ids": doc_ids,
            "top_scores": [round(chunk.score, 4) for chunk in chunks],
            "top_chunk_ids": [chunk.chunk_id for chunk in chunks],
            "rank": ranks[0] if ranks else None,
            "reciprocal_rank": round(1 / ranks[0], 4) if ranks else 0.0,
            "hit_at_1": bool(ranks and ranks[0] <= 1),
            "hit_at_3": bool(ranks and ranks[0] <= 3),
            "hit_at_5": bool(ranks and ranks[0] <= 5),
            "ndcg_at_5": round(_ndcg_at_k(doc_ids, expected, 5), 4) if expected else None,
            "top_score": None if top_score is None else round(top_score, 4),
            "predicted_answerable": gate_status == "hit",
            "gate_status": gate_status,
            "gate_reason": gate_reason,
            "gate_hits": gate_hits,
            "search_ms": None if search_ms is None else round(search_ms, 1),
            "gate_ms": None if gate_ms is None else round(gate_ms, 1),
        })
    return results, {}


def summarize(results: list[dict], threshold: float,
              unmapped_docs: list[str] | None = None) -> dict:
    """按诚实分母算指标；同时给按类别的分解。

    ``unmapped_docs`` 是 store 里存在、但无法映射回 11 个 core source_id 的
    document_id。它们若真的出现在 top-k，会让相关判定失真，因此必须计数上报。
    """
    answerable = [row for row in results if row["expected_ids"]]
    no_answer = [row for row in results if row["expect_no_answer"]]
    errors = [row for row in results if row["error"]]
    unmapped = set(unmapped_docs or ())

    def _block(rows: list[dict]) -> dict:
        ok = [row for row in rows if row["error"] is None]
        return {
            "answerable_recall_at_1": _mean([1.0 if row["hit_at_1"] else 0.0 for row in ok]),
            "answerable_recall_at_3": _mean([1.0 if row["hit_at_3"] else 0.0 for row in ok]),
            "answerable_recall_at_5": _mean([1.0 if row["hit_at_5"] else 0.0 for row in ok]),
            "mrr": _mean([row["reciprocal_rank"] for row in ok]),
            "ndcg_at_5": _mean([row["ndcg_at_5"] or 0.0 for row in ok]),
            "no_answer_false_positive_rate": (
                _mean([1.0 if row["predicted_answerable"] else 0.0 for row in ok])
                if rows and rows[0]["expect_no_answer"] else None),
            "answered_rows": len(ok),
        }

    overall = _block(answerable)
    no_answer_block = _block(no_answer)

    by_category: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        grouped[row["category"]].append(row)
    for category in sorted(grouped):
        rows = grouped[category]
        if rows[0]["expect_no_answer"]:
            ok = [row for row in rows if row["error"] is None]
            by_category[category] = {
                "rows": len(rows),
                "expect_no_answer": True,
                "no_answer_false_positive_rate": _mean(
                    [1.0 if row["predicted_answerable"] else 0.0 for row in ok]),
                "refused": sum(1 for row in ok if not row["predicted_answerable"]),
            }
        else:
            block = _block(rows)
            block["rows"] = len(rows)
            block["expect_no_answer"] = False
            by_category[category] = block

    answered = sum(1 for row in results if row["predicted_answerable"])
    return {
        "metrics": {
            "recall_at_1": overall["answerable_recall_at_1"],
            "recall_at_3": overall["answerable_recall_at_3"],
            "recall_at_5": overall["answerable_recall_at_5"],
            "mrr": overall["mrr"],
            "ndcg_at_5": overall["ndcg_at_5"],
            "no_answer_false_positive_rate":
                no_answer_block["no_answer_false_positive_rate"],
            "refusal_accuracy": (
                round(1.0 - (no_answer_block["no_answer_false_positive_rate"] or 0.0), 4)
                if no_answer_block["no_answer_false_positive_rate"] is not None else None),
            # 端到端（检索+门控）口径：只有这道门之后的命中才会进入回答。
            # 上面的 recall@k 是"纯检索"能力；这两个数字之间的落差就是门控代价。
            "end_to_end_answer_rate": (
                _mean([1.0 if row["predicted_answerable"] else 0.0
                       for row in answerable if row["error"] is None])),
            "end_to_end_hit_and_correct_rate": (
                _mean([1.0 if (row["predicted_answerable"] and row["hit_at_5"]) else 0.0
                       for row in answerable if row["error"] is None])),
        },
        "by_category": by_category,
        "denominators": {
            "total": len(results),
            "answered": answered,
            "refused": len(results) - answered - len(errors),
            "no_answer_rows": len(no_answer),
            "answerable_rows": len(answerable),
            "answered_rows": len([row for row in answerable if row["error"] is None]),
            # 可答题里被门控拒绝的条数：这些是"检索到了但没敢答"的漏答。
            "answerable_refused": len([row for row in answerable
                                       if row["error"] is None
                                       and not row["predicted_answerable"]]),
            "errors": len(errors),
        },
        "id_mapping": {
            "unmapped_document_ids": sorted(unmapped),
            "rows_with_unmapped_top_hits": sorted(
                row["id"] for row in results
                if unmapped & set(row["top_doc_ids"])),
        },
        "latency_ms": {
            "search_p50": _percentile([row["search_ms"] for row in results
                                       if row["search_ms"] is not None], 0.50),
            "search_p95": _percentile([row["search_ms"] for row in results
                                       if row["search_ms"] is not None], 0.95),
            "gate_p50": _percentile([row["gate_ms"] for row in results
                                     if row["gate_ms"] is not None], 0.50),
            "gate_p95": _percentile([row["gate_ms"] for row in results
                                     if row["gate_ms"] is not None], 0.95),
        },
        "score_separation": {
            "min_answerable_top_score": min(
                (row["top_score"] for row in answerable
                 if row["top_score"] is not None), default=None),
            "max_no_answer_top_score": max(
                (row["top_score"] for row in no_answer
                 if row["top_score"] is not None), default=None),
            "threshold": threshold,
        },
    }


def build_manifest(threshold: dict, store_file: Path,
                   dataset_file: Path = BLIND_FILE,
                   label: str = "blind") -> dict:
    facts = milvus_facts()
    commit = dirty = None
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                capture_output=True, text=True, timeout=20).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                    capture_output=True, text=True, timeout=20).stdout.strip())
    except Exception:  # noqa: BLE001 - 没有 git 也要能跑，只是记录缺失
        pass
    descriptor = {}
    if DESCRIPTOR_FILE.is_file():
        descriptor = json.loads(DESCRIPTOR_FILE.read_text(encoding="utf-8"))
    return {
        "git_commit": commit or "unknown",
        "git_dirty": dirty,
        "corpus_manifest": MANIFEST_FILE.relative_to(ROOT).as_posix(),
        "corpus_manifest_sha256": _sha256_file(MANIFEST_FILE)
        if MANIFEST_FILE.is_file() else "missing",
        "embedding_descriptor_sha256": _sha256_file(DESCRIPTOR_FILE)
        if DESCRIPTOR_FILE.is_file() else "missing",
        "embedding_descriptor_hash": descriptor.get("hash", ""),
        "embedding_model": descriptor.get("model", ""),
        "embedding_dimension": descriptor.get("dimension"),
        "milvus": facts,
        "knowledge_store_file": store_file.relative_to(ROOT).as_posix()
        if store_file.is_relative_to(ROOT) else str(store_file),
        "threshold": threshold,
        # 题集身份：blind 与 calibration 各自记自己的 hash，且都带 report_label，
        # 避免把 calibration 的产物当成盲测结论引用。
        "report_label": label,
        "dataset_file": dataset_file.relative_to(ROOT).as_posix()
        if dataset_file.is_relative_to(ROOT) else str(dataset_file),
        "dataset_sha256": _sha256_file(dataset_file) if dataset_file.is_file() else "missing",
        "blind_file": BLIND_FILE.relative_to(ROOT).as_posix(),
        "blind_file_sha256": _sha256_file(BLIND_FILE) if BLIND_FILE.is_file() else "missing",
        "retrieval_k": RETRIEVAL_K,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索评测（真实 Milvus）")
    parser.add_argument("--set", dest="dataset", type=str, default="",
                        help="题集路径（默认 eval/rag/blind.jsonl；"
                             "eval/rag/calibrate-dev2.jsonl 会按 calibration 口径标注）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 行（冒烟）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验 schema，不连 Milvus、不写结果")
    parser.add_argument("--threshold", type=float, default=None,
                        help="显式指定无答案阈值；默认从 dev 结果文件读取")
    parser.add_argument("--dev-result", type=str, default="",
                        help="dev 结果文件路径（默认取 eval/rag/results 下最新 offline-*.json）")
    parser.add_argument("--store", type=str, default="",
                        help="KNOWLEDGE_STORE_FILE（默认 data/acceptance/milvus-e2e-store.json）")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次检索超时（秒）")
    args = parser.parse_args(argv)

    dataset_file = Path(args.dataset) if args.dataset else BLIND_FILE
    label = dataset_label(dataset_file)

    # 先校验**整份**文件：--limit 是冒烟开关，不能让它把行数区间、无答案条数
    # 这些约束绕过掉（否则 --limit 5 会变成合法的正式结果来源）。
    all_rows = load_rows(dataset_file)
    problems = validate(all_rows, label=label)
    if problems:
        print(f"{dataset_file.name} 校验失败（label={label}）：", file=sys.stderr)
        for problem in problems:
            print("  -", problem, file=sys.stderr)
        return 1
    rows = all_rows[:args.limit] if args.limit else all_rows
    counts = Counter(row["category"] for row in rows)

    print(f"{dataset_file.name}: {len(rows)} 行（label={label}）")
    for category, number in sorted(counts.items()):
        print(f"  {category:20s} {number}")
    print(f"  {'expect_no_answer':20s} {sum(1 for row in rows if row['expect_no_answer'])}")

    if args.dry_run:
        print("\n--dry-run：schema 校验通过，未连接 Milvus，未写结果文件。")
        return 0

    store_file = Path(args.store) if args.store else DEFAULT_STORE
    if not store_file.is_file():
        raise SystemExit(f"store 文件不存在: {store_file}（先跑 acceptance_milvus_e2e.py 导入）")
    os.environ["KNOWLEDGE_STORE_FILE"] = str(store_file)

    dev_result = Path(args.dev_result) if args.dev_result else find_latest_dev_result()
    threshold = resolve_threshold(args.threshold, dev_result)
    print(f"\n阈值 = {threshold['value']} 来源 = {threshold['source']} "
          f"(fitted_on_blind={threshold['fitted_on_blind']})")
    if threshold["fitted_on_blind"]:
        raise SystemExit("阈值不能在 blind 上拟合；这是本次评测的硬约束。")

    doc_map, store_uris = load_doc_map(store_file)
    if not doc_map:
        raise SystemExit(
            f"store {store_file} 里没有任何 document_id 能映射回 11 个 core source_id；"
            "无法判定相关性。先确认 store 与 normalized manifest 指向同一批文档。"
        )
    unmapped = sorted(set(store_uris) - set(doc_map))
    if unmapped:
        print(f"注意：{len(unmapped)} 个 store document_id 无法映射到 core source_id："
              f"{unmapped}（通常是验收用的临时文档；其命中会被单独计数）")
    started = time.time()
    results, _ = run_rows(rows, threshold["value"], doc_map, args.timeout)
    summary = summarize(results, threshold["value"], unmapped)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": (f"{label}_retrieval_milvus_smoke" if args.limit
                 else f"{label}_retrieval_milvus"),
        "report_label": label,
        "note": ("真实 Milvus collection 上的检索评测。指标只说明检索/门控行为，"
                 "不说明回答事实正确性。" + (
                     f"阈值来源={threshold['source']}，未在 blind 上拟合"
                     "（若来源是 calibration，则这是'把 calibration 阈值套到 blind "
                     "上看后果'的对照，不是重新拟合）。"
                     if label == "blind" else
                     "本文件是 calibration 集：阈值在 calibration 上拟合，"
                     "不能作为盲测结论复述。")),
        "threshold": threshold,
        "manifest": build_manifest(threshold, store_file, dataset_file, label),
        "elapsed_seconds": round(time.time() - started, 1),
        **summary,
        "rows": results,
    }

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # --limit 是冒烟开关：产物加 smoke- 前缀，避免被当成正式结果引用。
    # calibration 题集用 calibration-retrieval- 前缀：``calibration-<UTC>.json``
    # 这个名字属于 eval_calibration.py 的拟合产物，两者不能互相覆盖。
    prefix = "blind" if label == "blind" else "calibration-retrieval"
    if args.limit:
        prefix = f"{prefix}-smoke"
    out_path = RESULT_DIR / f"{prefix}-{stamp}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    metrics = summary["metrics"]
    denominators = summary["denominators"]
    print(f"\nCollection: {payload['manifest']['milvus'].get('collection')} "
          f"metric={payload['manifest']['milvus'].get('metric_type')} "
          f"entities={payload['manifest']['milvus'].get('num_entities')}")
    print(f"Recall@1={metrics['recall_at_1']} Recall@3={metrics['recall_at_3']} "
          f"Recall@5={metrics['recall_at_5']}")
    print(f"MRR={metrics['mrr']} nDCG@5={metrics['ndcg_at_5']}")
    print(f"无答案误答率={metrics['no_answer_false_positive_rate']} "
          f"拒答正确率={metrics['refusal_accuracy']}")
    print(f"端到端: 应答题被回答比例={metrics['end_to_end_answer_rate']} "
          f"回答且命中={metrics['end_to_end_hit_and_correct_rate']}")
    print(f"分母: {denominators}")
    print(f"延迟: {summary['latency_ms']}")
    print(f"\n结果: {out_path.relative_to(ROOT).as_posix()}")

    misses = [row for row in results
              if row["expected_ids"] and not row["hit_at_5"] and row["error"] is None]
    if misses:
        print(f"\nRecall@5 未命中 {len(misses)} 条：")
        for row in misses:
            print(f"  {row['id']} {row['category']:18s} expect={row['expected_ids']} "
                  f"top={row['top_doc_ids'][:3]} {row['question'][:36]}")
    false_positives = [row for row in results
                       if row["expect_no_answer"] and row["predicted_answerable"]]
    if false_positives:
        print(f"\n无答案误答 {len(false_positives)} 条：")
        for row in false_positives:
            print(f"  {row['id']} {row['category']:18s} score={row['top_score']} "
                  f"top={row['top_doc_ids'][:2]} {row['question'][:36]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
