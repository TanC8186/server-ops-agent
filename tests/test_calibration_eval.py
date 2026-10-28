"""G5 分域校准与生成式评测的回归测试。

这些测试保护三件容易被悄悄破坏的事：

1. **calibration 集本身可审计**：每行锚点逐字存在于 normalized 正文、域字段与
   manifest 一致、有参考答案要点。锚点失效的题集会让"事实正确率"变成自说自话。
2. **frozen blind 没被改动**：sha256 与已知值比对。阈值/题集是两套东西，
   如果有人为了调阈值去改 blind，这个测试会立刻失败。
3. **阈值拟合行为可解释**：桶内样本不足必须回退全局并标注；阈值必须最大化
   balanced accuracy；``--dry-run`` 必须真的不联网。

不测"指标数值好不好看"——那取决于真实 Milvus 与真实模型，属于脚本产物而不是
单元测试的职责。
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest

from scripts import eval_blind, eval_calibration, eval_generation

ROOT = Path(__file__).resolve().parents[1]
BLIND_FILE = ROOT / "eval" / "rag" / "blind.jsonl"
CALIBRATION_FILE = ROOT / "eval" / "rag" / "calibrate-dev2.jsonl"

#: frozen blind 集在 G2.2 定稿时的 sha256。任何字节变化都说明它被改过。
BLIND_SHA256 = "a42aed35caa5e88db2b907a82867a8f4c2af905d398187ec5d11780f5b183253"


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _answerable_row(rows: list[dict]) -> dict:
    return next(row for row in rows if not row["expect_no_answer"])


# ── (a) calibration 集自身通过校验 ────────────────────────


def test_calibration_set_passes_validation():
    rows = _rows(CALIBRATION_FILE)
    assert eval_blind.validate(rows, label="calibration") == []


def test_calibration_size_and_answerable_ratio():
    rows = _rows(CALIBRATION_FILE)
    answerable = [row for row in rows if not row["expect_no_answer"]]
    assert 60 <= len(rows) <= 100, f"calibration 行数 {len(rows)} 不在 60–100"
    ratio = len(answerable) / len(rows)
    assert 0.66 <= ratio <= 0.74, f"可答题占比 {ratio:.3f} 偏离 70% 太多"


def test_calibration_covers_all_categories():
    """8 个类别都要有样本，否则分域/category 桶在某个类别上无数据。"""
    categories = {row["category"] for row in _rows(CALIBRATION_FILE)}
    assert categories == eval_blind.ALLOWED_CATEGORIES


def test_calibration_rows_carry_domain_and_reference_points():
    for row in _rows(CALIBRATION_FILE):
        if row["expect_no_answer"]:
            continue
        assert row.get("reference_points"), f"{row['id']}: 缺少 reference_points"
        assert row.get("product"), f"{row['id']}: 缺少 product（分域校准需要）"
        assert row.get("os_family"), f"{row['id']}: 缺少 os_family"


# ── (b) 脏行必须被拒 ──────────────────────────────────────


def test_validation_rejects_anchor_not_in_corpus():
    rows = _rows(CALIBRATION_FILE)
    dirty = dict(_answerable_row(rows))
    dirty["notes"] = "锚点=SD-01「这句话在正文里根本不存在，属于编造的锚点」"
    problems = eval_blind.validate([dirty], label="calibration")
    assert any("锚点短语不在" in problem for problem in problems)


def test_validation_rejects_anchor_source_outside_expected_ids():
    rows = _rows(CALIBRATION_FILE)
    dirty = dict(_answerable_row(rows))
    dirty["notes"] = "锚点=WS-04「This cmdlet is only available on the Windows platform.」"
    assert dirty["expected_ids"] != ["WS-04"]
    problems = eval_blind.validate([dirty], label="calibration")
    assert any("不在 expected_ids 内" in problem for problem in problems)


def test_validation_rejects_no_answer_row_with_expected_ids():
    rows = _rows(CALIBRATION_FILE)
    row = next(row for row in rows if row["expect_no_answer"])
    dirty = dict(row)
    dirty["expected_ids"] = ["SD-01"]
    problems = eval_blind.validate([dirty], label="calibration")
    assert any("expect_no_answer=true 却给了 expected_ids" in problem
               for problem in problems)


def test_validation_rejects_domain_field_mismatching_manifest():
    """product 写错会让 gate 用别家产品的阈值，必须在校验阶段拦住。"""
    rows = _rows(CALIBRATION_FILE)
    dirty = dict(_answerable_row(rows))
    dirty["product"] = "not-a-real-product"
    problems = eval_blind.validate([dirty], label="calibration")
    assert any("与 manifest" in problem for problem in problems)


def test_validation_rejects_missing_reference_points():
    rows = _rows(CALIBRATION_FILE)
    dirty = dict(_answerable_row(rows))
    dirty.pop("reference_points", None)
    problems = eval_blind.validate([dirty], label="calibration")
    assert any("reference_points" in problem for problem in problems)


def test_validation_rejects_reference_point_not_in_corpus():
    """reference_points 必须来自锚点所在正文，否则事实正确率变成"抄句子"检测。"""
    rows = _rows(CALIBRATION_FILE)
    dirty = dict(_answerable_row(rows))
    dirty["reference_points"] = ["--flag-that-does-not-exist-anywhere"]
    problems = eval_blind.validate([dirty], label="calibration")
    assert any("reference_point" in problem for problem in problems)


def test_blind_is_not_accepted_as_calibration():
    """blind 行没有域字段/参考答案要点，不能被当作 calibration 拟合来源。"""
    problems = eval_blind.validate(_rows(BLIND_FILE), label="calibration")
    assert any("缺少可选域字段" in problem for problem in problems)


def test_dataset_label_distinguishes_blind_from_calibration():
    assert eval_blind.dataset_label(BLIND_FILE) == "blind"
    assert eval_blind.dataset_label(CALIBRATION_FILE) == "calibration"


def test_calibration_tool_refuses_to_fit_on_blind():
    with pytest.raises(SystemExit):
        eval_calibration.main(["--set", str(BLIND_FILE), "--dry-run"])


# ── (c) blind 必须逐字节不变 ──────────────────────────────


def test_blind_file_sha256_unchanged():
    raw = BLIND_FILE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != BLIND_SHA256:
        # 区分“题被改了”和“checkout 把行尾换了”：两者都让字节 hash 变，但原因与
        # 处置完全不同。.gitattributes 已强制 `*.jsonl eol=lf`，正常 clone 不会
        # 触发这一条；触发时说明本地 git 配置或旧 clone 把文件转成了 CRLF。
        normalised = hashlib.sha256(
            raw.replace(b"\r\n", b"\n")).hexdigest()
        assert normalised == BLIND_SHA256, (
            "blind.jsonl 被修改了。它是 frozen 盲测集：改题等于把答案抄进考题，"
            "已发布的 blind 结论也会随之失效。新题请写进 calibrate-dev2.jsonl。")
        raise AssertionError(
            "blind.jsonl 的内容没变，但工作区把它存成了 CRLF（git 的 "
            "core.autocrlf/eol 配置导致）。字节 sha256 因此对不上：请在仓库根执行 "
            "`git config core.autocrlf false` 后重新 checkout 该文件"
            "（仓库已用 .gitattributes 固定 `*.jsonl eol=lf`）。"
            f"raw={digest[:12]}… expected={BLIND_SHA256[:12]}…")


# ── (d) 分桶拟合的纯函数行为 ──────────────────────────────


def test_fit_threshold_maximizes_balanced_accuracy():
    # 两类完全可分：最优切点必须落在 0.7，两个比率都是 1.0。
    fit = eval_calibration.fit_threshold([0.7, 0.8, 0.9], [0.2, 0.3, 0.4],
                                        min_samples=3)
    assert fit is not None
    assert fit["threshold"] == pytest.approx(0.7)
    assert fit["recall"] == 1.0
    assert fit["false_answer_rate"] == 0.0
    assert fit["balanced_accuracy"] == 1.0


def test_fit_threshold_prefers_higher_cut_on_tie():
    """重叠样本上多解平局时取更高阈值（误答代价高于漏答）。"""
    # 0.6 / 0.65 / 0.7 三个切点的 balanced accuracy 都是 0.75；
    # 取最高的 0.7：宁可漏掉 0.6 那条可答题，也不放行 0.65 的无答案题。
    fit = eval_calibration.fit_threshold([0.7, 0.6], [0.65, 0.1], min_samples=2)
    assert fit is not None
    assert fit["balanced_accuracy"] == pytest.approx(0.75)
    assert fit["threshold"] == pytest.approx(0.7)
    assert fit["recall"] == pytest.approx(0.5)
    assert fit["false_answer_rate"] == 0.0


def test_fit_threshold_default_rejects_tiny_buckets():
    """默认下限 8 条：1 条样本的"完美切点"不构成证据。"""
    assert eval_calibration.fit_threshold([0.9], [0.2]) is None


def test_fit_threshold_returns_none_when_samples_insufficient():
    assert eval_calibration.fit_threshold([0.9, 0.8], [0.2, 0.3], min_samples=3) is None
    assert eval_calibration.fit_threshold([0.9, 0.8, 0.7], [], min_samples=3) is None


def test_fit_bucket_falls_back_to_global_when_bucket_is_small():
    rows = [
        {"category": "exact_command", "expected_ids": ["SD-01"], "top_score": 0.9},
        {"category": "no_answer", "expected_ids": [], "expect_no_answer": True,
         "top_score": 0.2},
    ]
    bucket = eval_calibration.fit_bucket(
        [rows[0]], global_threshold=0.55, global_negatives=[0.2, 0.4],
        min_samples=3)
    assert bucket["fallback"] is True
    assert bucket["fallback_reason"] == "insufficient_answerable_samples"
    assert bucket["threshold"] == pytest.approx(0.55)
    assert bucket["fitted_threshold"] is None
    assert bucket["negatives_source"] == "global_no_answer_rows"


def test_fit_bucket_marks_no_answer_only_bucket_as_fallback():
    rows = [{"category": "no_answer", "expected_ids": [],
             "expect_no_answer": True, "top_score": 0.3}]
    bucket = eval_calibration.fit_bucket(
        rows, global_threshold=0.5, global_negatives=[0.3], min_samples=1)
    assert bucket["fallback"] is True
    assert bucket["fallback_reason"] == "no_answerable_rows"
    assert bucket["recall"] is None
    assert bucket["false_answer_rate"] == 0.0


def test_fit_bucket_uses_own_fit_when_enough_samples():
    rows = [{"category": "exact_command", "expected_ids": ["SD-01"],
             "top_score": score} for score in (0.71, 0.75, 0.8)]
    bucket = eval_calibration.fit_bucket(
        rows, global_threshold=0.5, global_negatives=[0.2, 0.3], min_samples=3)
    assert bucket["fallback"] is False
    assert bucket["threshold"] == pytest.approx(0.71)
    assert bucket["recall"] == 1.0
    assert bucket["false_answer_rate"] == 0.0


def test_build_profile_emits_production_readable_shape():
    rows = []
    for index in range(10):
        rows.append({"category": "exact_command", "product": "systemd",
                     "expected_ids": ["SD-01"], "top_score": 0.6 + index * 0.01})
    for index in range(10):
        rows.append({"category": "no_answer", "product": "",
                     "expected_ids": [], "expect_no_answer": True,
                     "top_score": 0.1 + index * 0.01})
    fitted = eval_calibration.build_profile(rows, min_samples=8)
    profile = fitted["gate_profile"]
    assert profile["mode"] == "domain"
    assert profile["fallback"] == "global"
    assert set(profile) >= {"mode", "global", "by_category", "by_product",
                            "fallback", "fitted_on"}
    assert profile["by_product"]["systemd"] >= profile["global"]
    # category 桶只是诊断口径：它不能冒充运行时可选项。
    assert fitted["buckets"]["by_category"]["no_answer"]["runtime_selectable"] is False
    assert fitted["buckets"]["by_product"]["systemd"]["runtime_selectable"] is True


def test_apply_profile_prefers_product_bucket_over_global():
    profile = {"mode": "domain", "global": 0.5, "by_category": {},
               "by_product": {"docker": 0.8}, "fallback": "global"}
    rows = [
        {"product": "docker", "expected_ids": ["DK-02"], "top_score": 0.7},
        {"product": "systemd", "expected_ids": ["SD-01"], "top_score": 0.7},
    ]
    result = eval_calibration.apply_profile(rows, profile)
    # docker 桶阈值 0.8 → 0.7 被拒；systemd 走全局 0.5 → 被回答。
    assert result["recall"] == 0.5
    assert result["answerable_rows"] == 2


# ── (e) --dry-run 真的不联网 ──────────────────────────────


@pytest.fixture()
def no_network(monkeypatch):
    """任何 socket 连接尝试都直接失败，确保 dry-run 没有偷偷联网。"""

    def _blocked(*_args, **_kwargs):
        raise AssertionError("dry-run 不允许建立网络连接")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def test_eval_blind_dry_run_does_not_touch_milvus(monkeypatch, no_network):
    def _boom():
        raise AssertionError("--dry-run 不应读取 Milvus 事实")

    monkeypatch.setattr(eval_blind, "milvus_facts", _boom)
    assert eval_blind.main(["--dry-run"]) == 0


def test_eval_blind_dry_run_accepts_calibration_set(monkeypatch, no_network):
    def _boom():
        raise AssertionError("--dry-run 不应读取 Milvus 事实")

    monkeypatch.setattr(eval_blind, "milvus_facts", _boom)
    assert eval_blind.main(["--set", str(CALIBRATION_FILE), "--dry-run"]) == 0


def test_eval_calibration_dry_run_does_not_touch_milvus(monkeypatch, no_network):
    def _boom():
        raise AssertionError("--dry-run 不应读取 Milvus 事实")

    monkeypatch.setattr(eval_calibration, "milvus_facts", _boom)
    assert eval_calibration.main(["--dry-run"]) == 0


def test_eval_generation_dry_run_does_not_call_model(monkeypatch, no_network):
    from app.core import llm_factory

    def _boom(**_kwargs):
        raise AssertionError("--dry-run 不应构造模型")

    monkeypatch.setattr(llm_factory, "create_chat_model", _boom)

    def _no_run(*_args, **_kwargs):
        raise AssertionError("--dry-run 不应执行真实评测循环")

    monkeypatch.setattr(eval_generation, "_run", _no_run)
    assert eval_generation.main(["--dry-run", "--limit", "6"]) == 0


# ── 指标口径的纯函数 ──────────────────────────────────────


def test_citation_support_uses_tokens_from_evidence():
    answer = "最小允许值是 `6m`[K1]。另外 [K9] 是伪造引用。"
    supported = eval_generation.judge_citation(
        answer, "K1", 'the minimum allowed value is 6m (6 megabytes)')
    assert supported["supported"] is True
    assert "6m" in supported["matched"]

    unsupported = eval_generation.judge_citation(
        answer, "K1", "the container can use 300m of memory and 700m swap")
    assert unsupported["supported"] is False


def test_citation_without_token_is_undecided_not_supported():
    """抽不到 token 时记 None（无法判定），不能算成"支持"来虚高指标。"""
    answer = "根据资料，这样做是可行的[K1]。"
    result = eval_generation.judge_citation(answer, "K1", "任意正文")
    assert result["supported"] is None


def test_citation_not_used_in_answer_is_ignored():
    answer = "答案里没有引用标记。"
    assert eval_generation.citation_sentences(answer, "K1") == ""


def test_summarize_keeps_three_metrics_separate():
    rows = [
        {
            "id": "C039", "category": "code_config", "expect_no_answer": False,
            "outcome": "answered", "removed_citations": ["K7"],
            "citation_checks": [
                {"citation_id": "K1", "authorized": True, "resolves_to_chunk": True,
                 "supported": True},
                {"citation_id": "K2", "authorized": True, "resolves_to_chunk": True,
                 "supported": False},
            ],
            "deterministic_coverage": {"coverage": 0.5, "all_present": False},
            "fact_judge": {"ok": True, "covers": True,
                           "usage": {"input_tokens": 10, "output_tokens": 5,
                                     "total_tokens": 15}},
            "seconds": 1.0,
        },
        {
            "id": "C071", "category": "lifecycle_refusal", "expect_no_answer": True,
            "outcome": "answered", "removed_citations": [],
            "citation_checks": [], "deterministic_coverage": None,
            "fact_judge": None, "seconds": 2.0,
        },
        {"id": "C072", "category": "lifecycle_refusal", "expect_no_answer": True,
         "failure": "ReadTimeout: 超时", "seconds": 3.0},
    ]
    summary = eval_generation.summarize(rows)
    citation = summary["citation_support_rate"]
    assert citation["supported"] == 1
    assert citation["unsupported"] == 1
    assert citation["rate"] == 0.5
    assert citation["removed_citations_count"] == 1
    assert summary["factual_correct_rate"]["deterministic_mean_coverage"] == 0.5
    assert summary["factual_correct_rate"]["judge_correct_rate"] == 1.0
    false_answers = summary["no_answer_false_answer_rate"]
    assert false_answers["no_answer_rows"] == 1
    assert false_answers["rate"] == 1.0
    assert summary["failure_denominators"] == {
        "total": 3, "succeeded": 2, "failed": 1,
        "failures": [{"id": "C072", "category": "lifecycle_refusal",
                      "failure": "ReadTimeout: 超时"}],
    }
    assert summary["judge_tokens"]["total_tokens"] == 15


def test_select_rows_round_robins_categories():
    rows = _rows(CALIBRATION_FILE)
    picked = eval_generation.select_rows(rows, 8)
    assert len(picked) == 8
    assert len({row["category"] for row in picked}) == 8


def test_gate_profile_loader_skips_files_without_profile(tmp_path, monkeypatch):
    """实测踩到的坑：``calibration-retrieval-*.json`` 与拟合产物同前缀。

    只按名字取"最新 calibration-*"会读到检索评测产物（没有 gate_profile），
    阈值静默退化成 config 的 0，于是无答案题被"回答"。这里锁定：没有
    gate_profile 的文件必须被跳过并记录原因。
    """
    retrieval_like = tmp_path / "calibration-retrieval-20260101T000000Z.json"
    retrieval_like.write_text(json.dumps({"kind": "calibration_retrieval_milvus"}),
                              encoding="utf-8")
    fitted = tmp_path / "calibration-20260101T000100Z.json"
    fitted.write_text(json.dumps({"kind": "calibration_gate_profile",
                                  "gate_profile": {"mode": "domain",
                                                   "global": 0.5,
                                                   "by_product": {},
                                                   "by_category": {}}}),
                      encoding="utf-8")
    monkeypatch.setattr(eval_generation, "RESULT_DIR", tmp_path)
    profile, skipped = eval_generation._load_gate_profile("")
    assert profile is not None
    assert profile["global"] == 0.5
    assert profile["_source"].endswith("calibration-20260101T000100Z.json")
    assert any("没有 gate_profile" in reason for reason in skipped)


def test_gate_profile_loader_returns_none_when_only_retrieval_outputs(
        tmp_path, monkeypatch):
    (tmp_path / "calibration-retrieval-20260101T000000Z.json").write_text(
        json.dumps({"kind": "calibration_retrieval_milvus"}), encoding="utf-8")
    monkeypatch.setattr(eval_generation, "RESULT_DIR", tmp_path)
    profile, skipped = eval_generation._load_gate_profile("")
    assert profile is None
    assert len(skipped) == 1


# ── G6：profile 快照一致性 ────────────────────────────────


def _current_snapshot_evidence() -> dict:
    """按当前磁盘状态构造一份"应当通过"的 store 证据。"""
    descriptor = json.loads((ROOT / "data" / "embedding_descriptor.json")
                            .read_text(encoding="utf-8"))
    store_path = ROOT / "data" / "acceptance" / "milvus-e2e-store.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    return {
        "corpus_manifest_sha256": hashlib.sha256(
            (ROOT / "knowledge" / "manifests" / "normalized-2026-10-26.json")
            .read_bytes()).hexdigest(),
        "embedding_descriptor_sha256": hashlib.sha256(
            (ROOT / "data" / "embedding_descriptor.json").read_bytes()).hexdigest(),
        "embedding_descriptor_hash": descriptor["hash"],
        "knowledge_store_file": "data/acceptance/milvus-e2e-store.json",
        "knowledge_store_sha256": hashlib.sha256(store_path.read_bytes()).hexdigest(),
        "store_document_ids": sorted(str(key) for key in store["documents"]),
        "store_document_descriptor_hashes": sorted({
            doc["embedding_descriptor_hash"]
            for doc in store["documents"].values()
            if doc.get("embedding_descriptor_hash")}),
    }


#: 快照一致性用例需要的本机运行期文件。它们由真实检索/索引产生，`data/` 在
#: `.gitignore` 里（与具体环境绑定，不入库），所以干净的 CI checkout 没有它们。
_SNAPSHOT_DATA = (
    ROOT / "data" / "embedding_descriptor.json",
    ROOT / "data" / "acceptance" / "milvus-e2e-store.json",
)


@pytest.fixture()
def snapshot_data():
    """没有本机快照时跳过并说明原因，而不是让 CI 读到 FileNotFoundError。

    跳过是诚实的边界：这些用例验证的是"profile 与**当前磁盘快照**比对"的逻辑，
    没有快照就没有可比对的对象。校验逻辑本身另有用例（构造 tmp store）覆盖。
    """
    missing = [path for path in _SNAPSHOT_DATA if not path.is_file()]
    if missing:
        names = ", ".join(path.relative_to(ROOT).as_posix() for path in missing)
        pytest.skip(f"本机快照缺失（{names}）：data/ 不入库，需先跑一次真实检索/索引")
    return _current_snapshot_evidence()


def test_profile_consistency_accepts_matching_snapshot(snapshot_data):
    profile = {"global": 0.5,
               "fitted_on": {"sha256": hashlib.sha256(
                   CALIBRATION_FILE.read_bytes()).hexdigest(),
                   "store_evidence": snapshot_data}}
    differences = eval_generation._verify_profile_consistency(
        {}, profile, ROOT / "data" / "acceptance" / "milvus-e2e-store.json")
    assert differences == []


def test_profile_consistency_reports_dataset_hash_mismatch(snapshot_data):
    """题集被改过时不能静默复用阈值：必须给出非空差异说明。"""
    profile = {"global": 0.5,
               "fitted_on": {"sha256": "0" * 64, "store_evidence": snapshot_data}}
    differences = eval_generation._verify_profile_consistency(
        {}, profile, ROOT / "data" / "acceptance" / "milvus-e2e-store.json")
    assert differences
    assert any("题集 sha256 不一致" in item for item in differences)


def test_profile_consistency_reports_store_descriptor_mismatch(tmp_path, snapshot_data):
    """换 collection/重跑 embedding 后，store 里的 descriptor hash 会对不上。"""
    store_path = tmp_path / "store.json"
    store_path.write_text(json.dumps({"documents": {
        "doc-x": {"embedding_descriptor_hash": "sha256:deadbeef"}}}),
        encoding="utf-8")
    profile = {"global": 0.5,
               "fitted_on": {"sha256": hashlib.sha256(
                   CALIBRATION_FILE.read_bytes()).hexdigest(),
                   "store_evidence": snapshot_data}}
    differences = eval_generation._verify_profile_consistency(
        {}, profile, store_path)
    assert any("store 文档的 embedding descriptor" in item
               for item in differences)


def test_profile_without_evidence_reports_missing_not_silent(tmp_path):
    """旧产物只写 gate_profile（没有 manifest/store 证据）：明确报缺失。

    store 路径用临时文件：这里要证明的是"证据缺失会被说出来"，不该因为本机
    没有 `data/` 快照而失败（CI 上 `data/` 不入库，没有这个文件）。
    """
    store_path = tmp_path / "store.json"
    store_path.write_text(json.dumps({"documents": {}}), encoding="utf-8")
    profile = {"global": 0.5, "fitted_on": {"rows": 3}}
    differences = eval_generation._verify_profile_consistency(
        {}, profile, store_path)
    assert differences
    assert any("证据缺失" in item for item in differences)


def test_gate_profile_loader_reports_missing_explicit_path(tmp_path, monkeypatch):
    """显式指定的 profile 不存在时必须说明，不能静默退回 config 阈值。"""
    missing = tmp_path / "calibration-TYPO.json"
    profile, skipped = eval_generation._load_gate_profile(str(missing))
    assert profile is None
    assert any("指定的 gate profile 不存在" in reason for reason in skipped)


def test_effective_threshold_records_explicit_override():
    """显式 --threshold 覆盖 profile 时，manifest 记的必须是真正生效的值。"""
    profile = {"global": 0.9, "by_product": {"docker": 0.606}}
    assert eval_generation._effective_threshold(profile, 0.5) == {
        "value": 0.5, "source": "explicit",
        "fitted_on": "eval/rag/calibrate-dev2.jsonl", "fitted_on_blind": False}

    by_product = eval_generation._effective_threshold(profile, None)
    assert by_product["source"] == "gate_profile_by_product"
    assert by_product["by_product"] == {"docker": 0.606}

    # 没有 profile、没有显式阈值：如实记 config 的值与来源。
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.config.config.rag_min_score", 0.42)
    try:
        fallback = eval_generation._effective_threshold(None, None)
    finally:
        monkeypatch.undo()
    assert fallback["value"] == 0.42
    assert fallback["source"] == "config.rag_min_score"


def test_gate_profile_loader_keeps_parent_manifest(tmp_path, monkeypatch):
    fitted = tmp_path / "calibration-20260101T000100Z.json"
    fitted.write_text(json.dumps({
        "kind": "calibration_gate_profile",
        "gate_profile": {"mode": "domain", "global": 0.5,
                         "by_product": {}, "by_category": {}},
        "manifest": {"dataset_sha256": "a" * 64}}), encoding="utf-8")
    monkeypatch.setattr(eval_generation, "RESULT_DIR", tmp_path)
    profile, _skipped = eval_generation._load_gate_profile("")
    assert profile["_manifest"]["dataset_sha256"] == "a" * 64


# ── G6：产物 kind ────────────────────────────────────────


def test_generation_payload_kind_is_sampled(tmp_path, monkeypatch, snapshot_data):
    """所有抽样生成评测都用同一个 kind，不再按 --limit 分出 smoke。"""
    async def _fake_run(_args, _rows, _profile):
        return [], 0.0

    monkeypatch.setattr(eval_generation, "_run", _fake_run)
    # 产物必须写在 ROOT 之内：main 打印结果路径时用 relative_to(ROOT)。
    monkeypatch.setattr(eval_generation, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(eval_generation, "ROOT", tmp_path)
    monkeypatch.setattr(eval_generation, "milvus_facts", lambda: {"connected": False})
    monkeypatch.setattr(eval_generation, "_load_gate_profile",
                        lambda _explicit: (None, []))

    store = ROOT / "data" / "acceptance" / "milvus-e2e-store.json"
    assert eval_generation.main(
        ["--limit", "3", "--threshold", "0.5", "--store", str(store)]) == 0
    written = sorted(tmp_path.glob("generation-*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["kind"] == "generation_eval_llm_sampled"
    assert payload["gate"]["consistency"]["status"] == "not_applicable"


# ── G6：expr 兼容分支 ────────────────────────────────────


class _TypeErrorStore:
    """签名接受 expr、内部抛 TypeError 的真实故障替身。"""

    score_kind = "similarity"

    def __init__(self):
        self.calls = 0

    def similarity_search_with_score(self, query, k=3, **kwargs):
        self.calls += 1
        raise TypeError("'NoneType' object is not subscriptable")


class _NoExprStore:
    """只接受 (query, k) 的旧适配器：没有预过滤能力。"""

    score_kind = "similarity"

    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    def similarity_search_with_score(self, query, k=3):
        self.calls += 1
        return list(self._rows)[:k]


def _metadata(document_id: str, revision: str) -> dict:
    return {"_chunk_id": f"{document_id}:{revision}:1",
            "_document_id": document_id, "_revision": revision,
            "_source": "https://example.invalid/doc"}


def test_search_does_not_mask_type_error_as_unfiltered_retry():
    from app.services.knowledge_service import (
        RetrievalUnavailable, VectorStoreRetriever)

    store = _TypeErrorStore()
    retriever = VectorStoreRetriever(store, active_revisions=lambda: {"doc-1": "rev-1"})
    with pytest.raises(RetrievalUnavailable):
        retriever.search("q", 3)
    assert store.calls == 1


def test_search_falls_back_only_for_signature_without_expr():
    from langchain_core.documents import Document

    from app.services.knowledge_service import VectorStoreRetriever

    rows = [
        (Document(page_content="越权分片", metadata=_metadata("doc-2", "rev-2")), 0.9),
        (Document(page_content="可见分片", metadata=_metadata("doc-1", "rev-1")), 0.8),
    ]
    store = _NoExprStore(rows)
    retriever = VectorStoreRetriever(store, active_revisions=lambda: {"doc-1": "rev-1"})
    chunks = retriever.search("q", 3)
    assert store.calls == 1
    assert [chunk.document_id for chunk in chunks] == ["doc-1"]


def test_profile_consistency_detects_different_document_set(tmp_path):
    """同一 embedder、同名 store 文件、换了一批文档：必须报不一致。

    这是"换 collection 后静默复用旧阈值"最容易漏掉的形态：descriptor 一个字节
    都没变，只有文档集合变了。旧实现只比 descriptor-hash 集合（同 embedder 下
    恒为 1 个元素），因此完全看不出差别。
    """
    store_a = tmp_path / "acceptance" / "milvus-e2e-store.json"
    store_a.parent.mkdir(parents=True)
    store_a.write_text(json.dumps({"documents": {
        "doc-old-1": {"embedding_descriptor_hash": "sha256:same"},
        "doc-old-2": {"embedding_descriptor_hash": "sha256:same"},
        "doc-gone": {"embedding_descriptor_hash": "sha256:same"},
    }}), encoding="utf-8")
    store_b = tmp_path / "acceptance" / "other" / "milvus-e2e-store.json"
    store_b.parent.mkdir(parents=True)
    store_b.write_text(json.dumps({"documents": {
        "doc-old-1": {"embedding_descriptor_hash": "sha256:same"},
        "doc-old-2": {"embedding_descriptor_hash": "sha256:same"},
        "doc-new": {"embedding_descriptor_hash": "sha256:same"},
    }}), encoding="utf-8")

    profile = {"global": 0.5, "fitted_on": {"store_evidence": {
        "knowledge_store_file": "acceptance/milvus-e2e-store.json",
        "store_document_ids": ["doc-gone", "doc-old-1", "doc-old-2"],
        "store_document_descriptor_hashes": ["sha256:same"],
    }}}
    differences = eval_generation._verify_profile_consistency({}, profile, store_b)
    assert any("文档集合与拟合时不同" in item for item in differences)
    assert any("拟合用的 store 与本次不同" in item for item in differences)
