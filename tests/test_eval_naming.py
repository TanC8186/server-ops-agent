"""``--limit`` 的命名口径（G7 P1-1「清理旧的开发取样命名」）。

``--limit`` 只表示"本次处理多少题"，不是一种评测类型。所以：

- 给了 ``--limit``：产物 ``kind`` 以 ``_retrieval_milvus_sampled`` 结尾，
  文件名前缀带 ``-sampled``；
- 没给（全量）：``kind`` 是 ``_retrieval_milvus``，前缀不带 ``sampled``。

这两条断言的意义是防止命名回退：一旦拼 kind/前缀的逻辑又按条数分档，
同一份题集换个 ``limit`` 就会被记录成两类结果，跨次比较失去可比性。

不连 Milvus：``run_rows`` / ``load_doc_map`` 全部打桩，只校验 main 写出的
产物名与 payload 里的 kind。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from scripts import eval_blind  # noqa: E402 - 需要仓库根在 sys.path


#: 最小可跑的行集合：main 只读 ``category`` 与 ``expect_no_answer`` 做计数，
#: 检索本身由 ``run_rows`` 打桩，因此不需要真实题集内容。
_FAKE_ROWS = [
    {"id": "B001", "category": "exact_command", "expect_no_answer": False},
    {"id": "B002", "category": "no_answer", "expect_no_answer": True},
    {"id": "B003", "category": "zh_to_en_manual", "expect_no_answer": False},
]


@pytest.fixture
def patched_main(tmp_path, monkeypatch):
    """把 main 的外部依赖全部换掉，只在 tmp_path 里写产物。"""
    monkeypatch.setattr(eval_blind, "ROOT", tmp_path)
    monkeypatch.setattr(eval_blind, "RESULT_DIR", tmp_path)
    monkeypatch.setattr(eval_blind, "load_rows", lambda _path: list(_FAKE_ROWS))
    monkeypatch.setattr(eval_blind, "validate", lambda _rows, **_kw: [])
    monkeypatch.setattr(eval_blind, "load_doc_map",
                        lambda _store: ({"doc-1": "SD-01"}, {}))
    monkeypatch.setattr(eval_blind, "run_rows", lambda _rows, _thr, _map, _to: ([], 0.0))
    # build_manifest 会读真实 manifest/descriptor 并跑 git，这里只留 main 打印用的键。
    monkeypatch.setattr(eval_blind, "build_manifest",
                        lambda *_a, **_kw: {"milvus": {}})
    return tmp_path


def _run_written_payload(tmp_path: Path, argv: list[str]) -> tuple[Path, dict]:
    store = ROOT / "data" / "acceptance" / "milvus-e2e-store.json"
    assert eval_blind.main(["--threshold", "0.5", "--store", str(store), *argv]) == 0
    written = sorted(tmp_path.glob("*.json"))
    assert len(written) == 1
    return written[0], json.loads(written[0].read_text(encoding="utf-8"))


# ── 直接测拼名字的两个小函数 ──────────────────────────────


def test_result_kind_uses_sampled_suffix_when_limit_given():
    assert eval_blind.result_kind("blind", 5) == "blind_retrieval_milvus_sampled"
    assert eval_blind.result_kind("calibration", 3).endswith("_retrieval_milvus_sampled")


def test_result_kind_stays_full_when_no_limit():
    assert eval_blind.result_kind("blind", 0) == "blind_retrieval_milvus"
    assert "sampled" not in eval_blind.result_kind("calibration", 0)


def test_result_file_prefix_matches_kind_naming():
    assert eval_blind.result_file_prefix("blind", 5) == "blind-sampled"
    assert eval_blind.result_file_prefix("blind", 0) == "blind"
    # calibration 的拟合产物叫 calibration-<UTC>.json，检索产物必须能区分开。
    assert eval_blind.result_file_prefix("calibration", 5) == "calibration-retrieval-sampled"
    assert eval_blind.result_file_prefix("calibration", 0) == "calibration-retrieval"


# ── 端到端：main 实际写出的文件名与 kind ─────────────────


def test_main_writes_sampled_kind_and_prefix_with_limit(patched_main):
    path, payload = _run_written_payload(patched_main, ["--limit", "2"])
    assert path.name.startswith("blind-sampled-")
    assert payload["kind"] == "blind_retrieval_milvus_sampled"
    assert payload["kind"].endswith("_sampled")
    assert "smoke" not in payload["kind"]
    # 只处理前 2 道题，但记录下来的质量口径仍是"抽样"，不是另一种评测。
    assert len(payload["rows"]) == 0  # run_rows 被打桩成空


def test_main_writes_full_kind_and_prefix_without_limit(patched_main):
    path, payload = _run_written_payload(patched_main, [])
    assert path.name.startswith("blind-") and "sampled" not in path.name
    assert payload["kind"] == "blind_retrieval_milvus"
    assert "sampled" not in payload["kind"]
    assert "smoke" not in payload["kind"]
