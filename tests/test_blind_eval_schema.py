"""盲测集 schema 与可追溯性测试（G2.2）。

这套测试的目的不是"跑一遍脚本"，而是**阻止别人往 blind.jsonl 里塞不可核验的行**：

- 盲测集的每一行都必须能在语料里找到锚点；只有 ``expect_no_answer`` 的行
  才可以没有锚点（它们的"依据"恰恰是语料里没有）。
- ``expected_ids`` 只能是 11 个 core 来源之一，防止把未入库的来源写进标签。
- category 必须在固定集合内，新增类别必须先改这里，避免出现"杂项"桶。
- 无答案行至少 8 条、总行数 50–100：低于这个规模，指标不足以支撑任何结论。

失败信息直接指出行 id 和原因，方便定位是哪一行被塞进来的。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BLIND_FILE = ROOT / "eval" / "rag" / "blind.jsonl"
NORMALIZED_DIR = ROOT / "knowledge" / "normalized"

#: 11 个 core 来源（与 normalized manifest 的 status=normalized 条目一致）。
CORE_IDS = frozenset({
    "SD-01", "SD-02", "LK-01", "RH-01",
    "DK-01", "DK-02", "DK-03",
    "WS-01", "WS-02", "WS-03", "WS-04",
})

#: 允许的 category。新增需同时改 scripts/eval_blind.py 的 ALLOWED_CATEGORIES。
ALLOWED_CATEGORIES = frozenset({
    "zh_to_en_manual",
    "exact_command",
    "os_version_conflict",
    "no_answer",
    "misleading_trap",
    "code_config",
    "prompt_injection",
    "lifecycle_refusal",
})

REQUIRED_KEYS = frozenset({
    "id", "question", "expected_ids", "expect_no_answer", "category", "notes",
})

#: 这些类别按定义就是"语料里没有依据 / 应当拒答"，因此允许无锚点。
NO_ANCHOR_CATEGORIES = frozenset({"no_answer", "lifecycle_refusal"})

MIN_ROWS = 50
MAX_ROWS = 100
MIN_NO_ANSWER = 8

#: notes 里像 "锚点=<SOURCE-ID>" 的显式声明。
_ANCHOR_TAG = re.compile(r"锚点\s*=\s*([A-Z]{2}-\d{2})")


def _load_rows() -> list[dict]:
    assert BLIND_FILE.is_file(), f"缺少盲测集: {BLIND_FILE}"
    rows: list[dict] = []
    for lineno, line in enumerate(
            BLIND_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - 失败即报错
            raise AssertionError(f"blind.jsonl 第 {lineno} 行不是合法 JSON: {exc}") from exc
    return rows


ROWS = _load_rows()


def _corpus_text(source_id: str) -> str | None:
    paths = sorted((NORMALIZED_DIR / source_id).glob("*.md"))
    return paths[-1].read_text(encoding="utf-8") if paths else None


def test_blind_file_has_expected_size():
    assert MIN_ROWS <= len(ROWS) <= MAX_ROWS, (
        f"盲测集 {len(ROWS)} 行，超出 {MIN_ROWS}–{MAX_ROWS} 区间。"
        "若确实无法凑够可审计的题目，必须在 eval/rag/README.md 写明原因并"
        "同步调整这里的上下限，而不是塞进不可核验的行。"
    )


def test_every_row_has_required_keys():
    for row in ROWS:
        missing = REQUIRED_KEYS - set(row)
        assert not missing, f"{row.get('id', '<无 id>')}: 缺少字段 {sorted(missing)}"


def test_row_ids_are_unique():
    counts = Counter(row["id"] for row in ROWS)
    duplicates = sorted(rid for rid, count in counts.items() if count > 1)
    assert not duplicates, f"重复 id: {duplicates}"


def test_expected_ids_subset_of_core_ids():
    for row in ROWS:
        expected = row["expected_ids"]
        assert isinstance(expected, list), f"{row['id']}: expected_ids 必须是数组"
        assert all(isinstance(item, str) for item in expected), (
            f"{row['id']}: expected_ids 只能包含字符串 id"
        )
        extra = set(expected) - CORE_IDS
        assert not extra, (
            f"{row['id']}: expected_ids 含非 core 来源 {sorted(extra)}；"
            f"允许的 11 个是 {sorted(CORE_IDS)}"
        )


def test_category_is_allowed():
    for row in ROWS:
        assert row["category"] in ALLOWED_CATEGORIES, (
            f"{row['id']}: 未知 category {row['category']!r}；"
            f"允许值 {sorted(ALLOWED_CATEGORIES)}"
        )


def test_no_answer_flag_consistent_with_expected_ids():
    for row in ROWS:
        if row["expect_no_answer"]:
            assert not row["expected_ids"], (
                f"{row['id']}: expect_no_answer=true 却给了 expected_ids"
            )
        else:
            assert row["expected_ids"], f"{row['id']}: 可答题必须有 expected_ids"


def test_enough_no_answer_rows():
    count = sum(1 for row in ROWS if row["expect_no_answer"])
    assert count >= MIN_NO_ANSWER, (
        f"expect_no_answer 行只有 {count} 条，至少需要 {MIN_NO_ANSWER} 条"
    )


def test_every_row_explains_itself():
    """notes 必须非空；这是"题目来源可审计"的最低要求。"""
    for row in ROWS:
        assert str(row["notes"]).strip(), f"{row['id']}: notes 为空，题目来源不可审计"


def test_no_answer_rows_use_no_answer_categories():
    for row in ROWS:
        if row["expect_no_answer"]:
            assert row["category"] in NO_ANCHOR_CATEGORIES, (
                f"{row['id']}: expect_no_answer=true 但 category={row['category']!r}；"
                f"只能是 {sorted(NO_ANCHOR_CATEGORIES)}"
            )


def test_answerable_rows_declare_a_verifiable_anchor():
    """可答题必须在 notes 里给出 ``锚点=<SOURCE-ID>`` 且该锚点属于 expected_ids。"""
    for row in ROWS:
        if row["expect_no_answer"]:
            continue
        tags = _ANCHOR_TAG.findall(str(row["notes"]))
        assert tags, (
            f"{row['id']}: notes 里缺少 '锚点=<SOURCE-ID>' 声明，无法审计题目来源"
        )
        assert set(tags) <= set(row["expected_ids"]), (
            f"{row['id']}: notes 锚点 {tags} 不在 expected_ids "
            f"{row['expected_ids']} 内"
        )


def test_anchor_documents_exist_in_corpus():
    """锚点指向的来源必须有 normalized 正文，否则题目无法复核。"""
    for row in ROWS:
        for source_id in _ANCHOR_TAG.findall(str(row["notes"])):
            assert _corpus_text(source_id) is not None, (
                f"{row['id']}: 锚点来源 {source_id} 没有 knowledge/normalized/ 正文"
            )


def test_categories_cover_the_required_gaps():
    """G2.2 要求的覆盖项一个都不能少。"""
    present = {row["category"] for row in ROWS}
    required = {
        "zh_to_en_manual",      # 中文问题检索英文手册
        "exact_command",        # 精确命令/参数/错误码
        "os_version_conflict",  # Linux vs Windows / 版本冲突
        "no_answer",            # 无答案
        "misleading_trap",      # 相似但不支持结论
        "code_config",          # 代码/YAML/完整命令
        "prompt_injection",     # 提示注入
        "lifecycle_refusal",    # active revision / ACL / 索引不可用
    }
    assert required <= present, f"缺少覆盖类别: {sorted(required - present)}"


def test_prompt_injection_rows_contain_an_injection_marker():
    """注入类题目本身必须真的含有注入文本，否则测不到该行为。

    标记分两类：覆盖/越权类（否定既有规则）与元数据篡改类（改写来源、
    许可、引用等旁路信息）。两类都属于"问题文本里夹带指令"，必须至少命中一个。
    """
    markers = (
        # 覆盖规则 / 伪造身份
        "忽略", "无视", "作废", "SYSTEM:", "无限制模式", "unrestricted",
        # 篡改来源/许可等旁路指令
        "删掉", "删除", "先执行", "先做", "不要引用",
    )
    rows = [row for row in ROWS if row["category"] == "prompt_injection"]
    assert rows, "缺少 prompt_injection 行"
    for row in rows:
        assert any(marker in row["question"] for marker in markers), (
            f"{row['id']}: prompt_injection 行的 question 里没有注入标记"
        )


def test_questions_are_unique_enough():
    """完全重复的题干会让指标虚高；要求题干两两不同。"""
    counts = Counter(row["question"] for row in ROWS)
    duplicates = sorted(question for question, count in counts.items() if count > 1)
    assert not duplicates, f"重复题干: {duplicates}"


@pytest.mark.parametrize("bad_row", [
    # 不可核验的可答题：有 expected_ids 但 notes 空 → 必须被拒
    {"id": "X1", "question": "q", "expected_ids": ["SD-01"],
     "expect_no_answer": False, "category": "exact_command", "notes": ""},
    # 未入库来源
    {"id": "X2", "question": "q", "expected_ids": ["NX-01"],
     "expect_no_answer": False, "category": "exact_command",
     "notes": "锚点=NX-01 随便一句"},
    # 未知 category
    {"id": "X3", "question": "q", "expected_ids": ["SD-01"],
     "expect_no_answer": False, "category": "misc", "notes": "锚点=SD-01 x"},
])
def test_guard_rejects_unverifiable_rows(bad_row):
    """回归：这三类行必须被判定为不合格（防止以后有人放宽校验）。"""
    problems = []
    if not str(bad_row["notes"]).strip():
        problems.append("notes")
    if set(bad_row["expected_ids"]) - CORE_IDS:
        problems.append("core_ids")
    if bad_row["category"] not in ALLOWED_CATEGORIES:
        problems.append("category")
    if not bad_row["expect_no_answer"] and not _ANCHOR_TAG.findall(bad_row["notes"]):
        problems.append("anchor")
    assert problems, f"校验器放过了不该放过的行: {bad_row['id']}"
