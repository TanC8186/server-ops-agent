"""真实 LLM 生成层评测（G5）：引用支持率、事实正确率、无答案误答率。

**为什么不复用检索评测的结论**：``eval_blind.py``/``eval_calibration.py`` 测的是
"资料找得准不准"，与"模型有没有照着资料答、有没有编"是两件事。0.98 的 Recall@3
不能写成问答准确率。本脚本走完整流水线
（``knowledge_service.answer_question``：同一检索 → 同一门控 → 同一 SYSTEM_PROMPT
→ 同一 ``_build_messages`` → 服务端引用消毒），只把 LLM 换成真实模型。

**三层指标，禁止合成一个数**：

1. ``citation_support_rate``：分母是答案里**被使用**的引用数（``[K#]`` 出现在
   服务端消毒后的答案里）。每个引用再跑一次确定性检查（引用所在句子里的
   数值/命令/路径 token 是否真的出现在该片段正文中）。可选的第二次 LLM 判定
   写在 ``citation_judge`` 里，和确定性结果并列，不合并。被服务端移除的伪造
   引用单列 ``removed_citations_count``。
2. ``factual_correct_rate``：分母是被评的**可答题**数。``reference_points`` 来自
   calibration 行的锚点（写题时就核对过在正文里），确定性 token 覆盖率与 LLM
   judge 分别报告。
3. ``no_answer_false_answer_rate``：分母是 ``expect_no_answer=true`` 的行；
   ``outcome=answered`` 即一次误答。

**失败不丢弃**：超时、网关过滤、题集外的异常都进 ``failures`` 并在各自分母里
扣掉，同时写 ``failure_denominators``。token 用量按每次调用累加。

**固定真实模型与固定快照**：模型从 ``LLM_*``（``app.config``）读取，只打印
provider/model，不打印 key；Milvus 用 ``data/acceptance/milvus-e2e-store.json``
映射 document_id，collection/实体数进 ``manifest.milvus``。

用法::

    # 只校验题集 + prompt 组装，不发任何 LLM 请求、不连 Milvus
    ./.venv/Scripts/python.exe scripts/eval_generation.py --dry-run

    # 抽样生成评测（默认 12 条；--limit 3 只用于快速自检）
    ./.venv/Scripts/python.exe scripts/eval_generation.py --limit 3 --judge off
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_blind import (  # noqa: E402 - 需要先修好 sys.path
    CALIBRATION_FILE,
    DEFAULT_STORE,
    RESULT_DIR,
    _sha256_file,
    build_manifest,
    dataset_label,
    load_doc_map,
    load_rows,
    milvus_facts,
    validate,
)

RESULT_PREFIX = "generation"

#: 产物 kind：所有抽样生成评测都用这个值。以前按 ``--limit <= 3`` 分出一个
#: "smoke" 名字，但 limit 只是取样条数，不是评测类型；同一脚本换个 limit 就
#: 换 kind，会让跨次比较与检索看起来像两种东西。
GENERATION_KIND = "generation_eval_llm_sampled"

#: 一致性校验里"证据缺失"条目的前缀。缺失与"对不上"要分开：前者说明这次
#: 校验覆盖不到，后者说明确实换了快照。
_EVIDENCE_MISSING = "证据缺失："

#: 默认评测条数。全量 80 条会让一次评测花费数十次模型调用；12 条覆盖 8 个类别
#: 各至少 1 条，足以暴露"引用不支持/事实错误/误答"三类问题。
DEFAULT_LIMIT = 12

#: 默认从最新的 calibration 结果里读分域 gate；读不到就用显式 --threshold。
CALIBRATION_GLOB = "calibration-*.json"

#: 从答案句子里抽"可核验 token"：反引号内容、带值的选项、数值（可带单位）、
#: 形态像路径/配置键的 token。抽不到 token 时该引用记为"无法判定"，
#: 而不是算成"不支持"——两者含义完全不同。
_CODE_SPAN = re.compile(r"`([^`\n]{1,120})`")
_FLAG = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*(?:=[\w./-]+)?)")
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?\s?(?:[mMgGkK]b?|ms|s|%|microseconds?|bytes?)?)")
_PATH = re.compile(r"(/[\w./-]{3,}|[A-Za-z_]+(?:\.[A-Za-z_]+){1,3})")
_CITATION = re.compile(r"\[(K\d{1,3})\]")

#: 模型在“answered”分支里仍可能写出明确的拒答句（门控放行了、但模型自己判断
#: 资料不足）。这类回答没有给出任何事实性结论，把它直接算进“无答案误答率”
#: 会把“模型守住了边界”记成“模型答错了”。这里只做**文本层**的识别，并把两类
#: 数量分开报告：否定式拒答单列，不与真正给了结论的误答混算。
_REFUSAL_MARKERS = (
    "知识库中没有足够依据",
    "没有足够依据",
    "无法据此",
    "不能仅凭",
    "资料不足",
    "没有提到",
    "无法回答",
)


def looks_like_refusal(answer: str) -> bool:
    """答案是否实质上在拒答（没有任何结论，只说明资料不足）。

    判据刻意保守：必须命中拒答标记，且答案里**没有**给出可执行命令/配置片段。
    给了命令再补一句“仅供参考”的回答不算拒答——那种回答确实可能答错。
    """
    text = (answer or "").strip()
    if not text:
        return False
    if not any(marker in text for marker in _REFUSAL_MARKERS):
        return False
    return not _CODE_SPAN.search(text)


_JUDGE_PROMPT = """你是严格的评测员。给你一个问题的参考答案要点、以及被测模型给出的答案。
只判断"答案是否覆盖了要点里的关键事实"，不要因为措辞、格式或额外内容扣分。
要点中的事实如果与答案矛盾，视为未覆盖。

只输出 JSON，不要任何解释文字：
{"covers": true 或 false, "covered_points": ["<命中的要点原文>"], "reason": "<一句话>"}
"""


# ── 纯函数：取样、token、指标 ──────────────────────────────


def select_rows(rows: Sequence[dict], limit: int) -> list[dict]:
    """按类别轮转取样，保证每个类别都有代表、无答案行不被挤掉。

    直接取前 N 行会取决于文件顺序（本集前段全是可答题），把无答案误答率
    算在 0 条分母上，等于没测。轮转是确定性的，同样的 limit 得到同样的样本。
    """
    if limit <= 0 or limit >= len(rows):
        return list(rows)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["category"], []).append(row)
    order = sorted(grouped)
    picked: list[dict] = []
    index = 0
    while len(picked) < limit:
        added = False
        for category in order:
            bucket = grouped[category]
            if index < len(bucket):
                picked.append(bucket[index])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        index += 1
    return picked


def evidence_tokens(text: str) -> list[str]:
    """从一段答案文字里抽可核验 token（去重、保序）。"""
    tokens: list[str] = []
    for pattern in (_CODE_SPAN, _FLAG, _PATH, _NUMBER):
        for match in pattern.finditer(text or ""):
            token = match.group(1).strip()
            if len(token) < 2:
                continue
            if token not in tokens:
                tokens.append(token)
    return tokens


def citation_sentences(answer: str, citation_id: str) -> str:
    """返回引用标记所在的句子（按行 + 中文句末标点切），用于抽 token。"""
    sentences: list[str] = []
    for line in (answer or "").splitlines():
        if f"[{citation_id}]" not in line:
            continue
        for sentence in re.split(r"(?<=[。！？;；])", line):
            if f"[{citation_id}]" in sentence:
                sentences.append(sentence)
    return "\n".join(sentences)


def judge_citation(answer: str, citation_id: str, evidence: str) -> dict:
    """确定性引用支持检查。返回 supported/None（无法判定）与使用的 token。"""
    sentence = citation_sentences(answer, citation_id)
    tokens = evidence_tokens(sentence)
    if not tokens:
        return {"supported": None, "tokens": [], "matched": [],
                "reason": "引用所在句子里没有可核验 token"}
    haystack = (evidence or "").lower()
    matched = [token for token in tokens if token.lower() in haystack]
    return {
        "supported": bool(matched),
        "tokens": tokens,
        "matched": matched,
        "reason": "" if matched else "引用片段里找不到句中任何数值/命令/路径 token",
    }


def point_coverage(answer: str, points: Sequence[str]) -> dict:
    """确定性事实覆盖率：reference_points 中出现在答案里的比例。"""
    text = (answer or "").lower()
    hit = [point for point in points if point.lower() in text]
    return {
        "points": list(points),
        "matched": hit,
        "coverage": round(len(hit) / len(points), 4) if points else None,
        "all_present": bool(points) and len(hit) == len(points),
    }


def _rate(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


# ── 真实流水线 ────────────────────────────────────────────


class _RecordingRetriever:
    """记录最后一次检索的完整片段，供确定性引用检查使用。

    只做委托 + 记录，不改检索行为：流水线里仍然是同一个
    ``VectorStoreRetriever``。直接读截断后的 400 字 snippet 会把"片段太长所以
    token 没截到"误判成"引用不支持"，那是对模型的冤枉。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last_chunks: list[Any] = []

    def search(self, query: str, k: int) -> Any:
        chunks = self._inner.search(query, k)
        self.last_chunks = list(chunks)
        return chunks


def _chunk_evidence(chunks: Sequence[Any]) -> dict[str, str]:
    """``K# -> 渲染给模型的正文``（parent_text 优先，与 render_context 一致）。"""
    evidence: dict[str, str] = {}
    for index, chunk in enumerate(chunks, 1):
        # 与 render_context 一致：有 parent 时给 parent + 命中 child，两段都算证据。
        parent = getattr(chunk, "parent_text", "") or ""
        text = getattr(chunk, "text", "") or ""
        evidence[f"K{index}"] = f"{parent}\n{text}" if parent else text
    return evidence


def _usage(response: Any) -> dict:
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


async def _ask_judge(model: Any, question: str, points: Sequence[str],
                     answer: str, timeout: float) -> dict:
    """第二次 LLM 判定，返回独立字段；失败也如实返回，不静默吞掉。"""
    from app.agent.aiops.cloud_multi import _extract_json

    payload = (
        f"问题：{question}\n"
        f"参考答案要点：{json.dumps(list(points), ensure_ascii=False)}\n"
        f"被测答案：\n{answer}\n"
    )
    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            model.ainvoke([("system", _JUDGE_PROMPT), ("user", payload)]), timeout)
    except Exception as exc:  # noqa: BLE001 - 超时/网关过滤都要计数
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                "seconds": round(time.perf_counter() - started, 2)}
    text = getattr(response, "content", response)
    if not isinstance(text, str):
        text = str(text)
    try:
        data = json.loads(_extract_json(text))
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": f"judge JSON 解析失败: {exc}",
                "raw": text[:300], "usage": _usage(response),
                "seconds": round(time.perf_counter() - started, 2)}
    return {
        "ok": True,
        "covers": bool(data.get("covers")) if "covers" in data else None,
        "covered_points": [str(item) for item in (data.get("covered_points") or [])],
        "reason": str(data.get("reason") or "")[:300],
        "usage": _usage(response),
        "seconds": round(time.perf_counter() - started, 2),
    }


async def evaluate_row(row: dict, *, retriever: Any, model: Any,
                       threshold: float, answer_timeout: float,
                       retrieval_timeout: float, judge: bool,
                       judge_timeout: float) -> dict:
    """跑一条问答（真实检索 + 真实模型），并把三层原始证据一起返回。"""
    from app.services.knowledge_service import answer_question

    recorder = _RecordingRetriever(retriever)
    started = time.perf_counter()
    final: dict | None = None
    retrieval_event: dict | None = None
    try:
        async for event in answer_question(
                row["question"], f"gen-{row['id']}", retriever=recorder,
                model=model, min_score=threshold,
                retrieval_timeout=retrieval_timeout, answer_timeout=answer_timeout):
            if event["type"] == "retrieval":
                retrieval_event = event
            elif event["type"] == "done":
                final = event
    except Exception as exc:  # noqa: BLE001
        return {
            "id": row["id"],
            "category": row["category"],
            "question": row["question"],
            "expect_no_answer": bool(row["expect_no_answer"]),
            "expected_ids": list(row.get("expected_ids") or []),
            "failure": f"{type(exc).__name__}: {exc}"[:300],
            "seconds": round(time.perf_counter() - started, 2),
        }

    evidence = _chunk_evidence(recorder.last_chunks)
    outcome = (final or {}).get("outcome", "unknown")
    answer = (final or {}).get("content") or ""
    citations = list((final or {}).get("citations") or [])
    removed = list((final or {}).get("removed_citations") or [])

    # 被使用的引用 = 消毒后答案里出现、且服务端授权返回的 [K#]。
    used_ids: list[str] = []
    for match in _CITATION.finditer(answer):
        if match.group(1) not in used_ids:
            used_ids.append(match.group(1))
    authorized = {item.get("id"): item for item in citations}

    checks: list[dict] = []
    for citation_id in used_ids:
        item = authorized.get(citation_id)
        body = evidence.get(citation_id, "")
        check = judge_citation(answer, citation_id, body)
        checks.append({
            "citation_id": citation_id,
            "authorized": item is not None,
            "resolves_to_chunk": bool(item and item.get("chunk_id")),
            **check,
        })

    coverage = None
    judge_result = None
    if not row["expect_no_answer"] and row.get("reference_points"):
        coverage = point_coverage(answer, row["reference_points"])
        if judge and outcome == "answered":
            judge_result = await _ask_judge(
                model, row["question"], row["reference_points"], answer, judge_timeout)

    return {
        "id": row["id"],
        "category": row["category"],
        "question": row["question"],
        "expect_no_answer": bool(row["expect_no_answer"]),
        "expected_ids": list(row.get("expected_ids") or []),
        "product": row.get("product", ""),
        "outcome": outcome,
        "retrieval_status": (retrieval_event or {}).get("status"),
        "retrieval_reason": (retrieval_event or {}).get("reason", ""),
        "answer": answer,
        "answer_chars": len(answer),
        "used_citation_ids": used_ids,
        "authorized_citation_ids": sorted(authorized),
        "removed_citations": removed,
        "citation_checks": checks,
        "reference_points": list(row.get("reference_points") or []),
        "deterministic_coverage": coverage,
        "fact_judge": judge_result,
        "seconds": round(time.perf_counter() - started, 2),
    }


def summarize(rows: Sequence[dict]) -> dict:
    """三层指标按各自分母计算；失败与"无法判定"分开计数。"""
    failures = [row for row in rows if row.get("failure")]
    ok_rows = [row for row in rows if not row.get("failure")]

    # ① 引用支持率
    supported = unsupported = undecided = 0
    unauthorized = 0
    removed_total = 0
    for row in ok_rows:
        removed_total += len(row.get("removed_citations") or [])
        for check in row.get("citation_checks") or []:
            if not check["authorized"] or not check["resolves_to_chunk"]:
                unauthorized += 1
                continue
            if check["supported"] is None:
                undecided += 1
            elif check["supported"]:
                supported += 1
            else:
                unsupported += 1
    decided = supported + unsupported

    # ② 事实正确率（只算可答题）
    answerable = [row for row in ok_rows if not row["expect_no_answer"]]
    coverage_values = [row["deterministic_coverage"]["coverage"]
                       for row in answerable
                       if row.get("deterministic_coverage")]
    all_present = [1.0 if row["deterministic_coverage"]["all_present"] else 0.0
                   for row in answerable if row.get("deterministic_coverage")]
    judge_rows = [row for row in answerable
                  if (row.get("fact_judge") or {}).get("ok")]
    judge_covers = [1.0 if row["fact_judge"]["covers"] else 0.0 for row in judge_rows]

    # ③ 无答案误答率
    no_answer_rows = [row for row in ok_rows if row["expect_no_answer"]]
    answered_no_answer = [row for row in no_answer_rows if row["outcome"] == "answered"]
    # 门控放行但模型自己明确拒答的行单列：它们不是“答错了”，但也不能算成
    # 门控的功劳——两种口径必须分别可见，否则会把模型行为记到门控头上。
    refusals = [row for row in answered_no_answer
                if looks_like_refusal(str(row.get("answer") or ""))]
    false_answers = [row for row in answered_no_answer if row not in refusals]

    # judge 的 token 用量单独累计：回答本身的用量由模型侧日志负责，
    # 这里只回答"这次评测额外花了多少 judge 调用"。
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for row in ok_rows:
        judge_usage = (row.get("fact_judge") or {}).get("usage") or {}
        for key in usage:
            usage[key] += int(judge_usage.get(key) or 0)

    return {
        "citation_support_rate": {
            "supported": supported,
            "unsupported": unsupported,
            "undecided_no_token": undecided,
            "unauthorized_or_unresolved": unauthorized,
            "denominator_used_citations": supported + unsupported + undecided + unauthorized,
            "denominator_decided": decided,
            "rate": round(supported / decided, 4) if decided else None,
            "removed_citations_count": removed_total,
            "note": "分母是答案里被使用的引用；被服务端移除的伪造引用不计入分母，"
                    "单列 removed_citations_count。",
        },
        "factual_correct_rate": {
            "answerable_rows": len(answerable),
            "deterministic_rows_with_points": len(coverage_values),
            "deterministic_mean_coverage": _rate(coverage_values),
            "deterministic_rows_all_points_present": _rate(all_present),
            "judge_rows": len(judge_rows),
            "judge_failures": sum(
                1 for row in answerable
                if row.get("fact_judge") and not row["fact_judge"].get("ok")),
            "judge_correct_rate": _rate(judge_covers),
            "note": "确定性覆盖率与 LLM judge 分别报告，不合成单一数字。",
        },
        "no_answer_false_answer_rate": {
            "no_answer_rows": len(no_answer_rows),
            "gated_answered": len(answered_no_answer),
            "false_answers": len(false_answers),
            "rate": (round(len(false_answers) / len(no_answer_rows), 4)
                     if no_answer_rows else None),
            "rows": [row["id"] for row in false_answers],
            "model_refusals_in_answered": len(refusals),
            "model_refusal_rows": [row["id"] for row in refusals],
            "note": "rate 只统计真正给出结论的误答；门控放行但模型自己明确拒答的行"
                    "单列 model_refusals_in_answered（它们没有事实性结论，不记作答错，"
                    "也不算门控的功劳）。",
        },
        "outcomes": {
            outcome: sum(1 for row in ok_rows if row["outcome"] == outcome)
            for outcome in sorted({row["outcome"] for row in ok_rows})
        },
        "failure_denominators": {
            "total": len(rows),
            "succeeded": len(ok_rows),
            "failed": len(failures),
            "failures": [{"id": row["id"], "category": row["category"],
                          "failure": row["failure"]} for row in failures],
        },
        "judge_tokens": usage,
        "latency_seconds": {
            "rows_p50": (round(statistics.median([row["seconds"] for row in ok_rows]), 2)
                         if ok_rows else None),
            "rows_max": (round(max(row["seconds"] for row in ok_rows), 2)
                         if ok_rows else None),
        },
    }


# ── 入口 ──────────────────────────────────────────────────


def _load_gate_profile(explicit: str) -> tuple[dict | None, list[str]]:
    """读最新 calibration 产物里的 ``gate_profile``；返回 (profile, 跳过原因)。

    只认带 ``gate_profile`` 的文件：``eval_blind.py --set calibration`` 也会在
    同一目录产出 ``calibration-retrieval-*.json``，它没有这个字段。按文件名
    取"最新 calibration-*"会恰好读到它，然后静默退化成 ``config.rag_min_score``
    （默认 0）——门控形同虚设，无答案误答率被算成很低。这是实测踩到的坑：
    C071 因此在 threshold=0 时被"回答"。
    """
    skipped: list[str] = []
    if explicit:
        candidates = [Path(explicit)]
        explicit_missing = [path for path in candidates if not path.is_file()]
        if explicit_missing:
            # 显式指定的产物不存在（路径拼错/文件被删）必须说出来：静默退回
            # config 阈值会让"我指定了 profile"与"实际用的阈值"完全脱节。
            for path in explicit_missing:
                skipped.append(f"指定的 gate profile 不存在: {path}")
            return None, skipped
    else:
        candidates = sorted(RESULT_DIR.glob(CALIBRATION_GLOB), reverse=True)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            skipped.append(f"{path.name}: 不可读（{exc}）")
            continue
        profile = payload.get("gate_profile")
        if isinstance(profile, dict) and profile:
            profile["_source"] = (path.relative_to(ROOT).as_posix()
                                  if path.is_relative_to(ROOT) else str(path))
            # 父 payload 的 manifest 才是"阈值是在哪份快照上拟合的"证据；
            # 只返回 gate_profile 会让调用方无法核对，只能重新读文件。
            parent_manifest = payload.get("manifest")
            if isinstance(parent_manifest, dict):
                profile["_manifest"] = parent_manifest
            return profile, skipped
        skipped.append(f"{path.name}: 没有 gate_profile（kind={payload.get('kind')}）")
    return None, skipped


def _verify_profile_consistency(profile_payload: dict, profile: dict,
                                store_file: Path) -> list[str]:
    """核对阈值 profile 与**当前**题集/语料/descriptor/store 是否是同一份快照。

    返回差异说明列表；空列表表示一致或无可核对证据（后者写在 ``skipped`` 里，
    由调用方打印）。为什么不静默使用：换 collection 或重跑 embedding 后，旧
    profile 的阈值仍然"能读"，但它是在另一份向量上拟合的——拿它跑生成评测，
    误答率既不是新 store 的性质，也不是旧 profile 的性质。

    证据来源优先级：``fitted_on.store_evidence``（G6 新产物）→ 父 payload 的
    ``manifest``（G5 产物）→ ``profile["fitted_on"]`` 里的题集 sha256（最老产物）。
    取不到的字段单独记为 skipped，不当作"一致"。
    """
    from scripts.eval_blind import DESCRIPTOR_FILE, MANIFEST_FILE, _sha256_file

    fitted_on = profile.get("fitted_on") or {}
    evidence = dict(fitted_on.get("store_evidence") or {})
    manifest = dict(profile_payload.get("manifest") or profile.get("_manifest") or {})

    def expected(name: str) -> str:
        for source in (evidence, manifest, fitted_on):
            value = source.get(name)
            if isinstance(value, str) and value:
                return value
        return ""

    differences: list[str] = []
    skipped: list[str] = []

    # ① 题集：profile 记的 sha256 必须等于当前磁盘上的题集字节。
    fitted_sha = str(fitted_on.get("sha256") or "") or expected("dataset_sha256")
    dataset_path = str(manifest.get("dataset_file") or fitted_on.get("dataset") or "")
    current_dataset = Path(dataset_path) if dataset_path else CALIBRATION_FILE
    if not current_dataset.is_absolute():
        current_dataset = ROOT / current_dataset
    if not fitted_sha:
        skipped.append("profile 未记录题集 sha256")
    elif not current_dataset.is_file():
        differences.append(f"题集文件不存在：{current_dataset}")
    elif _sha256_file(current_dataset) != fitted_sha:
        differences.append(
            f"题集 sha256 不一致：profile={fitted_sha[:12]}… "
            f"当前={_sha256_file(current_dataset)[:12]}…（{current_dataset.name}）")

    # ② 语料 manifest：决定"这份阈值见过哪些文档"。
    expected_manifest = expected("corpus_manifest_sha256")
    if not expected_manifest:
        skipped.append("profile 未记录语料 manifest sha256")
    elif not MANIFEST_FILE.is_file():
        differences.append(f"语料 manifest 缺失：{MANIFEST_FILE}")
    elif _sha256_file(MANIFEST_FILE) != expected_manifest:
        differences.append(
            f"语料 manifest sha256 不一致：profile={expected_manifest[:12]}… "
            f"当前={_sha256_file(MANIFEST_FILE)[:12]}…")

    # ③ descriptor：文件字节与内部 hash 都要对上（文件可能只是换了注释）。
    expected_descriptor_sha = expected("embedding_descriptor_sha256")
    expected_descriptor_hash = expected("embedding_descriptor_hash")
    current_descriptor_hash = ""
    if DESCRIPTOR_FILE.is_file():
        try:
            current_descriptor_hash = str(json.loads(
                DESCRIPTOR_FILE.read_text(encoding="utf-8")).get("hash", ""))
        except ValueError:
            current_descriptor_hash = ""
    if not expected_descriptor_sha and not expected_descriptor_hash:
        skipped.append("profile 未记录 embedding descriptor")
    else:
        if expected_descriptor_sha:
            if not DESCRIPTOR_FILE.is_file():
                differences.append(f"embedding descriptor 缺失：{DESCRIPTOR_FILE}")
            elif _sha256_file(DESCRIPTOR_FILE) != expected_descriptor_sha:
                differences.append(
                    f"embedding descriptor sha256 不一致："
                    f"profile={expected_descriptor_sha[:12]}… "
                    f"当前={_sha256_file(DESCRIPTOR_FILE)[:12]}…")
        if expected_descriptor_hash and \
                current_descriptor_hash != expected_descriptor_hash:
            differences.append(
                f"embedding descriptor hash 不一致：profile={expected_descriptor_hash} "
                f"当前={current_descriptor_hash or '缺失'}")

    # ④ 当前 store 里 documents 实际写入的 descriptor hash 集合：这是"阈值当时
    # 看的向量"与"这次要检索的向量"是否同一套编码器的直接证据。
    document_hashes: set[str] = set()
    document_ids: list[str] = []
    if store_file.is_file():
        try:
            store = json.loads(store_file.read_text(encoding="utf-8"))
            documents = store.get("documents") or {}
            document_hashes = {
                str(doc.get("embedding_descriptor_hash") or "")
                for doc in documents.values()
                if doc.get("embedding_descriptor_hash")
            }
            document_ids = sorted(str(key) for key in documents)
        except ValueError:
            document_hashes = set()
            document_ids = []
    if not document_hashes:
        skipped.append(f"store {store_file.name} 里没有 documents 的 descriptor hash")
    elif expected_descriptor_hash:
        mismatched = sorted(h for h in document_hashes
                            if h not in (expected_descriptor_hash,
                                         f"sha256:{expected_descriptor_hash}"))
        if mismatched:
            differences.append(
                "store 文档的 embedding descriptor 与 profile 不一致："
                f"{', '.join(h[:20] + '…' for h in mismatched)}")

    # ⑤ 快照身份：descriptor 相同**不代表**是同一份 store。换 collection、重建
    # 索引、换一批文档时 descriptor hash 完全可能一字不差，而阈值是在另一批
    # 向量上拟合的。这里比对 profile 记下的 store 路径与 documents 的
    # descriptor-hash 集合；store 文件字节 sha256 只作快照标识（上传/重试会改写
    # jobs 与时间戳），不参与硬判定。
    # G5 产物的 store 路径记在父 payload 的 manifest 里（那时还没有 store_evidence），
    # 所以两处都要看；取不到才记为"证据缺失"。
    recorded_store = str(evidence.get("knowledge_store_file")
                         or manifest.get("knowledge_store_file") or "")
    if recorded_store:
        try:
            current_rel = store_file.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            current_rel = str(store_file)
        # 比**相对仓库根的完整路径**，不是 basename：同名不同目录的两份 store
        # 是两批不同的文档。
        if recorded_store.replace("\\", "/") != current_rel.replace("\\", "/"):
            differences.append(
                f"拟合用的 store 与本次不同：profile={recorded_store} 当前={current_rel}")
    else:
        skipped.append("profile 未记录拟合用的 store 路径")

    # 文档身份：descriptor-hash 集合在"同一个 embedder"下永远只有 1 个元素，
    # 增删文档都看不出差别。真正能区分"同一批文档"的是文档集合本身，所以
    # profile 记录 ``store_document_ids``，这里逐项比对。
    recorded_ids = evidence.get("store_document_ids")
    current_ids = sorted(document_ids)
    if isinstance(recorded_ids, list) and recorded_ids:
        if current_ids and sorted(map(str, recorded_ids)) != current_ids:
            missing = sorted(set(map(str, recorded_ids)) - set(current_ids))
            added = sorted(set(current_ids) - set(map(str, recorded_ids)))
            differences.append(
                "store 的文档集合与拟合时不同："
                f"缺少 {len(missing)} 份{missing[:3]}，新增 {len(added)} 份{added[:3]}")
    else:
        skipped.append("profile 未记录拟合时 store 的文档 ID 集合")

    recorded_doc_hashes = evidence.get("store_document_descriptor_hashes")
    if isinstance(recorded_doc_hashes, list) and recorded_doc_hashes:
        if document_hashes and set(map(str, recorded_doc_hashes)) != document_hashes:
            differences.append(
                "store 里 documents 的 descriptor-hash 集合与拟合时不同："
                f"profile={sorted(map(str, recorded_doc_hashes))[:3]}… "
                f"当前={sorted(document_hashes)[:3]}…")
    elif not recorded_doc_hashes:
        skipped.append("profile 未记录拟合时 store 文档的 descriptor-hash 集合")

    if skipped:
        # 证据缺失不等于"一致"：单独成条返回，调用方据此记录 status=partial，
        # 而不是把它混进"对不上"里，也不是默默当通过。
        differences.extend(f"{_EVIDENCE_MISSING}{reason}" for reason in skipped)
    return differences


def _resolve_threshold(row: dict, profile: dict | None, explicit: float | None) -> float:
    """按 gate_profile 选阈值：product 桶 > global；都没有时用显式/config 值。"""
    if explicit is not None:
        return float(explicit)
    if profile:
        product = row.get("product") or ""
        if product and product in (profile.get("by_product") or {}):
            return float(profile["by_product"][product])
        if profile.get("global") is not None:
            return float(profile["global"])
    from app.config import config

    return float(getattr(config, "rag_min_score", 0.0) or 0.0)


def _effective_threshold(profile: dict | None, explicit: float | None) -> dict:
    """产物 manifest 里记录的**实际生效**阈值与来源。

    以前无条件写 `profile.global` + `source="gate_profile"`：显式 `--threshold`
    覆盖 profile、或 profile 被跳过而落到 `config.rag_min_score` 时，manifest
    记的阈值与 `rows[*].threshold` 完全不同。审计按 manifest 引用"阈值来源"就会
    引到错的数字，所以这里按 `_resolve_threshold` 的同一优先级如实记录。

    product 桶是**逐行**取值，不存在单一全局值：那种情况下如实写
    `source="gate_profile_by_product"`，`value` 为全局回退值，并标出这是分域阈值。
    """
    from app.config import config as _config

    if explicit is not None:
        return {"value": float(explicit), "source": "explicit",
                "fitted_on": "eval/rag/calibrate-dev2.jsonl", "fitted_on_blind": False}
    if profile:
        by_product = profile.get("by_product") or {}
        if by_product:
            return {"value": profile.get("global"),
                    "source": "gate_profile_by_product",
                    "by_product": dict(by_product),
                    "global": profile.get("global"),
                    "fitted_on": "eval/rag/calibrate-dev2.jsonl",
                    "fitted_on_blind": False}
        return {"value": profile.get("global"), "source": "gate_profile",
                "fitted_on": "eval/rag/calibrate-dev2.jsonl", "fitted_on_blind": False}
    return {"value": float(getattr(_config, "rag_min_score", 0.0) or 0.0),
            "source": "config.rag_min_score", "fitted_on": None,
            "fitted_on_blind": False}


def _dry_run(rows: Sequence[dict]) -> int:
    """只校验题集与 prompt 组装：不连 Milvus、不发 LLM 请求。

    上下文用锚点原文代替检索结果——目的是验证 ``SYSTEM_PROMPT`` 与
    ``_build_messages`` 的接入点没断，而不是验证检索。
    """
    from app.services.knowledge_service import SYSTEM_PROMPT, _build_messages

    problems: list[str] = []
    for row in rows:
        anchor = ""
        match = re.search(r"锚点=[A-Z]{2}-\d{2}「(.+?)」", row["notes"])
        if match:
            anchor = match.group(1)
        context = f"[K1] 锚点片段\n  内容:\n{anchor}" if anchor else ""
        messages = _build_messages(row["question"], context)
        if messages[0][0] != "system" or messages[0][1] != SYSTEM_PROMPT:
            problems.append(f"{row['id']}: 首条消息不是固定的 SYSTEM_PROMPT")
        if messages[-1][0] != "user" or row["question"] not in messages[-1][1]:
            problems.append(f"{row['id']}: 末条用户消息里没有本轮问题")
        if context and context not in messages[-1][1]:
            problems.append(f"{row['id']}: 参考资料段没有进入用户消息")
        if not row["expect_no_answer"] and not row.get("reference_points"):
            problems.append(f"{row['id']}: 可答题缺少 reference_points，事实正确率无法判定")
    if problems:
        for problem in problems:
            print("  -", problem, file=sys.stderr)
        return 1
    print(f"--dry-run：{len(rows)} 行题集的 prompt 组装与指标前置条件通过；"
          "未连接 Milvus，未调用模型。")
    return 0


async def _run(args: argparse.Namespace, rows: list[dict],
               profile: dict | None) -> tuple[list[dict], float]:
    from app.core.llm_factory import create_chat_model
    from app.services.knowledge_service import VectorStoreRetriever

    model = create_chat_model(streaming=False, temperature=0,
                              max_tokens=args.max_tokens)
    retriever = VectorStoreRetriever()
    started = time.time()
    results: list[dict] = []
    for row in rows:
        threshold = _resolve_threshold(row, profile, args.threshold)
        result = await evaluate_row(
            row, retriever=retriever, model=model, threshold=threshold,
            answer_timeout=args.answer_timeout,
            retrieval_timeout=args.retrieval_timeout,
            judge=args.judge == "on", judge_timeout=args.answer_timeout)
        result["threshold"] = threshold
        results.append(result)
        status = result.get("failure") or f"{result['outcome']} " \
                                          f"({len(result['citation_checks'])} 引用)"
        print(f"  {row['id']} {row['category']:20s} {status}")
    return results, time.time() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实 LLM 生成层评测（引用/事实/误答）")
    parser.add_argument("--set", dest="dataset", type=str, default="",
                        help="题集路径（默认 eval/rag/calibrate-dev2.jsonl）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"最多评测 N 行（按类别轮转取样，默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--threshold", type=float, default=None,
                        help="显式 gate 阈值；不给则读最新 calibration 的 gate_profile")
    parser.add_argument("--gate-profile", type=str, default="",
                        help="指定 calibration 结果文件（默认取最新 calibration-*.json）")
    parser.add_argument("--judge", choices=("on", "off"), default="on",
                        help="是否跑第二次 LLM 判定（默认 on）")
    parser.add_argument("--max-tokens", type=int, default=900, help="回答的 max_tokens")
    parser.add_argument("--answer-timeout", type=float, default=120.0,
                        help="单次模型调用超时（秒）")
    parser.add_argument("--retrieval-timeout", type=float, default=60.0,
                        help="单次检索超时（秒）")
    parser.add_argument("--store", type=str, default="",
                        help="KNOWLEDGE_STORE_FILE（默认 data/acceptance/milvus-e2e-store.json）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验题集与 prompt 组装，不连 Milvus、不调模型")
    args = parser.parse_args(argv)

    dataset_file = Path(args.dataset) if args.dataset else CALIBRATION_FILE
    label = dataset_label(dataset_file)
    if label != "calibration":
        raise SystemExit(
            "生成层评测只跑 calibration 集：blind 是 frozen 集，模型/阈值在这里"
            "调优后再报 blind 会污染盲测。")
    all_rows = load_rows(dataset_file)
    problems = validate(all_rows, label="calibration")
    if problems:
        print(f"{dataset_file.name} 校验失败：", file=sys.stderr)
        for problem in problems:
            print("  -", problem, file=sys.stderr)
        return 1

    rows = select_rows(all_rows, args.limit)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    print(f"{dataset_file.name}: 取样 {len(rows)}/{len(all_rows)} 行 "
          f"(可答 {sum(1 for row in rows if not row['expect_no_answer'])} / "
          f"无答 {sum(1 for row in rows if row['expect_no_answer'])})")
    for category, number in sorted(counts.items()):
        print(f"  {category:20s} {number}")

    if args.dry_run:
        return _dry_run(rows)

    store_file = Path(args.store) if args.store else DEFAULT_STORE
    if not store_file.is_file():
        raise SystemExit(f"store 文件不存在: {store_file}")
    os.environ["KNOWLEDGE_STORE_FILE"] = str(store_file)
    doc_map, store_uris = load_doc_map(store_file)
    if not doc_map:
        raise SystemExit(f"store {store_file} 里没有 document_id 能映射回 11 个 core id")

    from app.config import config

    profile, profile_skipped = _load_gate_profile(args.gate_profile)
    if profile is None and args.threshold is None:
        # 没有分域 profile 又没给显式阈值时，config.rag_min_score 可能是 0：
        # 那等于"所有候选都放行"，无答案误答率会变成"模型有没有自觉拒答"，
        # 与 gate 无关。宁可停下来说清楚，也不要交出一个看着好看的数字。
        from app.config import config as _config

        if float(getattr(_config, "rag_min_score", 0.0) or 0.0) <= 0:
            print("警告：没有可用的 gate_profile，且 config.rag_min_score <= 0。"
                  "本次评测的阈值是 0，等于不门控；产物里的误答率不能读成 gate 的效果。",
                  file=sys.stderr)
    print(f"\n模型: provider={config.llm_provider} model={config.llm_model} "
          f"(key 未打印)")
    print(f"gate: {'profile ' + profile['_source'] if profile else 'config/--threshold'}"
          f"，judge={args.judge}")
    for reason in profile_skipped:
        print(f"  （跳过 {reason}）")

    # 读取 profile 后立刻核对它是哪份快照上拟合的：换 collection / 重跑 embedding
    # 之后，"能读到旧阈值"不等于"旧阈值还有效"。有对不上的证据就停下，而不是
    # 拿它跑出一份既不属于新 store、也不属于旧 profile 的误答率。
    consistency: dict[str, Any] = {"status": "not_checked", "differences": [],
                                   "skipped": []}
    if profile is not None:
        parent_payload = {"manifest": profile.get("_manifest") or {}}
        found = _verify_profile_consistency(parent_payload, profile, store_file)
        differences = [item for item in found
                       if not item.startswith(_EVIDENCE_MISSING)]
        skipped_evidence = [item[len(_EVIDENCE_MISSING):] for item in found
                            if item.startswith(_EVIDENCE_MISSING)]
        for reason in skipped_evidence:
            print(f"  （一致性校验跳过：{reason}）")
        if differences:
            consistency = {"status": "mismatch", "differences": differences,
                           "skipped": skipped_evidence}
            if args.threshold is not None:
                # 显式阈值会覆盖 profile（`_resolve_threshold` 的 explicit 分支），
                # 此时不一致只影响"profile 元数据"，不影响本次实际门控。阻断没有
                # 依据，但必须让调用方看见"这份 profile 已经不能用"。
                consistency["status"] = "mismatch_ignored_explicit_threshold"
                print("gate profile 与当前快照不一致，已按 --threshold 继续；"
                      "产物里的 gate.profile 不可引用：", file=sys.stderr)
                for item in differences:
                    print(f"  - {item}", file=sys.stderr)
            else:
                print("gate profile 与当前快照不一致，拒绝复用旧阈值：", file=sys.stderr)
                for item in differences:
                    print(f"  - {item}", file=sys.stderr)
                raise SystemExit(
                    "换 collection/store 后不得静默复用旧阈值；请重新跑 "
                    "scripts/eval_calibration.py，或用 --threshold 显式给出阈值。")
        # 证据齐全才是 ok；只有部分证据可核对时标 partial，避免报告把"没核到"
        # 读成"核对通过"。
        consistency = {
            "status": "partial" if skipped_evidence else "ok",
            "differences": [], "skipped": skipped_evidence}
        # 父产物 manifest 已核对过，不必再整份嵌进 gate.profile 里重复一遍。
        profile.pop("_manifest", None)
    else:
        consistency = {"status": "not_applicable", "differences": [],
                       "skipped": ["没有 gate_profile（使用 --threshold/config）"]}

    started = time.time()
    results, elapsed = asyncio.run(_run(args, rows, profile))
    summary = summarize(results)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": GENERATION_KIND,
        "report_label": "calibration",
        "note": ("真实 LLM + 真实 Milvus 的生成层评测。三层指标各自独立分母，"
                 "不能合成一个'问答准确率'；失败与无法判定分别计数。"),
        "model": {"provider": config.llm_provider, "model": config.llm_model,
                  "base_url": config.llm_base_url, "max_tokens": args.max_tokens,
                  "temperature": 0},
        "dataset": {
            "file": dataset_file.relative_to(ROOT).as_posix()
            if dataset_file.is_relative_to(ROOT) else str(dataset_file),
            "sha256": _sha256_file(dataset_file),
            "rows_total": len(all_rows),
            "rows_evaluated": len(rows),
            "selection": "按 category 轮转取样（确定性）",
        },
        "gate": {"profile": profile, "explicit_threshold": args.threshold,
                 "profile_skipped": profile_skipped,
                 "consistency": consistency},
        "judge": {"enabled": args.judge == "on",
                  "note": "LLM 判定结果单独字段，不与确定性检查合并"},
        "manifest": build_manifest(
            _effective_threshold(profile, args.threshold),
            store_file, dataset_file, "calibration"),
        "milvus": milvus_facts(),
        "id_mapping": {"unmapped_document_ids": sorted(set(store_uris) - set(doc_map))},
        "metrics": summary,
        "elapsed_seconds": round(elapsed, 1),
        "rows": results,
    }

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULT_DIR / f"{RESULT_PREFIX}-{stamp}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    print("\n引用支持率:", summary["citation_support_rate"])
    print("事实正确率:", summary["factual_correct_rate"])
    print("无答案误答率:", summary["no_answer_false_answer_rate"])
    print("失败分母:", summary["failure_denominators"]["total"],
          "成功", summary["failure_denominators"]["succeeded"],
          "失败", summary["failure_denominators"]["failed"])
    print(f"judge tokens: {summary['judge_tokens']}")
    print(f"\n结果: {out_path.relative_to(ROOT).as_posix()}")
    print(f"总耗时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
