"""语料管线的两条 P0 修复的回归测试（任务书 §3.2）。

之前这两处**完全没有测试**：抓取白名单和 manifest 日期选择一旦被改回旧写法，
CI 不会红，AutoDL 语料就会在无人察觉时重新变得抓不到/读不到。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import fetch_corpus, normalize_corpus


# ── 抓取白名单 ────────────────────────────────────────────────


def test_autodl_hosts_are_allowlisted():
    """两个 AutoDL 官方主机必须在白名单里，否则 9 个来源全部抓不到。"""
    assert "www.autodl.com" in fetch_corpus.ALLOWED_HOSTS
    assert "api.autodl.com" in fetch_corpus.ALLOWED_HOSTS


def test_foreign_host_is_rejected_before_any_request(monkeypatch):
    """非白名单主机必须在**发出请求之前**就被拒绝。

    只断言"抛异常"不够：旧实现是先让 urllib 跟随重定向、拿到响应体之后才校验
    final_url，请求已经打到外站了。这里直接盯住 opener.open 是否被调用。
    """
    called = {"open": 0}

    class _Opener:
        def open(self, *_a, **_k):
            called["open"] += 1
            raise AssertionError("不该真的发请求")

    monkeypatch.setattr(fetch_corpus.urllib.request, "build_opener", lambda *h: _Opener())

    with pytest.raises(ValueError) as excinfo:
        fetch_corpus.fetch("https://evil.example.com/page", proxy="")
    assert "白名单" in str(excinfo.value)
    assert called["open"] == 0, "非白名单主机不得发出任何请求"


# ── manifest 日期选择 ─────────────────────────────────────────


def test_load_manifest_picks_newest_import_file(tmp_path, monkeypatch):
    """必须读目录里最新的 import-*.json，而不是硬编码某个日期。

    旧实现写死 ``import-2026-10-26.json``，于是 fetch 写出的新快照永远读不到，
    normalize 反复处理同一份旧清单。
    """
    (tmp_path / "import-2026-10-26.json").write_text(
        json.dumps({"entries": [{"id": "OLD-01"}]}), encoding="utf-8")
    (tmp_path / "import-2026-10-30.json").write_text(
        json.dumps({"entries": [{"id": "NEW-01"}]}), encoding="utf-8")
    monkeypatch.setattr(normalize_corpus, "MANIFEST_DIR", tmp_path)
    monkeypatch.delenv("AIOPS_CORPUS_MANIFEST", raising=False)

    manifest = normalize_corpus.load_manifest()
    assert [e["id"] for e in manifest["entries"]] == ["NEW-01"]


def test_load_manifest_honours_explicit_path(tmp_path):
    """显式路径优先于自动发现。"""
    target = tmp_path / "custom.json"
    target.write_text(json.dumps({"entries": [{"id": "X"}]}), encoding="utf-8")
    manifest = normalize_corpus.load_manifest(target)
    assert [e["id"] for e in manifest["entries"]] == ["X"]


def test_load_manifest_missing_dir_returns_empty(tmp_path, monkeypatch):
    """目录里没有任何 manifest 时返回空清单，由调用方决定是否报错。"""
    monkeypatch.setattr(normalize_corpus, "MANIFEST_DIR", tmp_path)
    monkeypatch.delenv("AIOPS_CORPUS_MANIFEST", raising=False)
    assert normalize_corpus.load_manifest() == {"entries": []}


# ── 评测语料不能被新语料挤掉 ──────────────────────────────────


def test_eval_retrieval_merges_all_normalized_manifests(tmp_path, monkeypatch):
    """追加新语料不得让旧基线失效。

    实测踩到：新增 AutoDL 的 normalized manifest 后，
    ``eval_retrieval.load_normalized()`` 只读 ``paths[-1]``，语料变成只有
    AD-01..AD-09，于是 eval/rag/dev.jsonl 里 11 道可答题的
    expected_source_ids 全部不在语料里 —— hit@k 恒为 0，评测照常"跑完"。
    """
    from scripts import eval_retrieval

    legacy = tmp_path / "normalized-2026-10-26.json"
    legacy.write_text(json.dumps({"entries": [
        {"id": "SD-01", "status": "normalized"},
        {"id": "RK-01", "status": "normalized"},
    ]}), encoding="utf-8")
    newer = tmp_path / "normalized-2026-10-30.json"
    newer.write_text(json.dumps({"entries": [
        {"id": "AD-01", "status": "normalized"},
    ]}), encoding="utf-8")

    monkeypatch.setattr(eval_retrieval, "MANIFEST_DIR", tmp_path)

    ids = {e["id"] for e in eval_retrieval.load_normalized()}
    assert ids == {"SD-01", "RK-01", "AD-01"}, "合并后必须同时含旧语料与新语料"


def test_eval_retrieval_skips_non_normalized_entries(tmp_path, monkeypatch):
    from scripts import eval_retrieval

    (tmp_path / "normalized-2026-10-30.json").write_text(json.dumps({"entries": [
        {"id": "A", "status": "normalized"},
        {"id": "B", "status": "needs_review"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(eval_retrieval, "MANIFEST_DIR", tmp_path)
    assert {e["id"] for e in eval_retrieval.load_normalized()} == {"A"}
