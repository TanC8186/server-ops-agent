"""AutoDL 语料入库后的 metadata 与 target_kind 过滤回归。

这一组锁定两个实测踩到的缺陷，都属于"看起来做了、实际没生效"：

1. **target_kind 只加进字段白名单却没落盘**
   `METADATA_TEXT_FIELDS` 加了 `target_kind`，但文档记录构造处只写了
   `os_family`/`product`，没有写 `target_kind`。结果 snapshot 里永远是 None，
   过滤条件永远匹配不上。

2. **没有 principal 时过滤被静默绕过**
   `VectorStoreRetriever._visible()` 以前只在 `principal_id` 非空时才应用
   filters，否则直接返回全部 active revision。实测
   `filters=RetrievalFilters(target_kind="cloud")` 仍然返回 autodl 的 ssh 文档
   —— 等于过滤功能不存在。
"""

from __future__ import annotations

import pytest

from app.services.knowledge_service import (
    RetrievalFilters,
    _filters_are_specified,
)
from app.services.knowledge_store import normalize_document_metadata


# ── metadata 契约 ────────────────────────────────────────────


def test_target_kind_and_source_kind_survive_normalization():
    """任务书 §3.3 要求的两个标注必须能被保存，未知字段仍被忽略。"""
    meta = normalize_document_metadata({
        "product": "autodl",
        "os_family": "linux",
        "target_kind": "ssh",
        "source_kind": "manual",
        "license_id": "autodl-tos",
        "unknown_field": "should-be-dropped",
    })
    assert meta["target_kind"] == "ssh"
    assert meta["source_kind"] == "manual"
    assert meta["product"] == "autodl"
    assert "unknown_field" not in meta


def test_empty_target_kind_is_kept_as_unset():
    """空串表示"未标注"，不能变成"匹配任意值"。"""
    meta = normalize_document_metadata({"target_kind": "  "})
    assert meta["target_kind"] == ""


# ── 过滤条件是否被识别 ────────────────────────────────────────


def test_filters_are_specified_detects_each_dimension():
    """只要有一个维度非空就必须算"给了条件"，否则会被跳过过滤。"""
    assert _filters_are_specified(RetrievalFilters()) is False
    for field in ("os_family", "product", "product_version", "at_date", "target_kind"):
        filters = RetrievalFilters(**{field: "x"})
        assert _filters_are_specified(filters) is True, f"{field} 未被识别为过滤条件"


# ── 真实 store 上的过滤行为 ──────────────────────────────────


class _FakeStore:
    """最小知识库替身：只提供 filtered_revisions 需要的结构。"""

    def __init__(self):
        self._data = {
            "documents": {
                "doc-ssh": {
                    "active_revision": "rev-ssh",
                    "product": "autodl",
                    "os_family": "linux",
                    "target_kind": "ssh",
                    "product_version": "",
                    "valid_from": "",
                    "valid_until": "",
                    "revisions": {"rev-ssh": {"status": "active"}},
                },
                "doc-cloud": {
                    "active_revision": "rev-cloud",
                    "product": "tencent",
                    "os_family": "linux",
                    "target_kind": "cloud",
                    "product_version": "",
                    "valid_from": "",
                    "valid_until": "",
                    "revisions": {"rev-cloud": {"status": "active"}},
                },
            }
        }
        import threading

        self._lock = threading.RLock()


def test_filtered_revisions_applies_each_dimension():
    from app.services.knowledge_store import KnowledgeStore

    fake = _FakeStore()
    # 直接调用真实实现，绑定假数据
    result_ssh = KnowledgeStore.filtered_revisions(fake, target_kind="ssh")
    assert result_ssh == {"doc-ssh": "rev-ssh"}

    result_cloud = KnowledgeStore.filtered_revisions(fake, target_kind="cloud")
    assert result_cloud == {"doc-cloud": "rev-cloud"}

    result_autodl = KnowledgeStore.filtered_revisions(fake, product="autodl")
    assert result_autodl == {"doc-ssh": "rev-ssh"}

    # 没有任何条件 = 全部 active
    result_all = KnowledgeStore.filtered_revisions(fake)
    assert set(result_all) == {"doc-ssh", "doc-cloud"}


def test_filtered_revisions_skips_inactive_revisions():
    """staged/superseded 一律不可见，与 active_revisions 保持一致。"""
    from app.services.knowledge_store import KnowledgeStore

    fake = _FakeStore()
    fake._data["documents"]["doc-ssh"]["revisions"]["rev-ssh"]["status"] = "staged"
    assert KnowledgeStore.filtered_revisions(fake, target_kind="ssh") == {}


def test_submit_document_persists_target_kind(tmp_path, monkeypatch):
    """真实 submit 路径必须把 target_kind/source_kind 写进文档记录。

    前一条用假 store 只验了过滤逻辑，绕过了落盘；把
    `"target_kind": meta.get(...)` 临时删掉它照样通过——说明是空测试。
    这条走真实 store，锁住"字段真的落盘"。
    """
    import json as _json
    from app.config import config
    from app.services import knowledge_store as store_api

    class _FakeVS:
        metric_type = "COSINE"
        score_kind = "similarity"
        collection_name = "biz"

        def _ensure_initialized(self):
            return True

        def add_documents(self, documents, *, ids=None):
            return list(ids or [])

        def delete_by_ids(self, ids):
            return 0

        def delete_by_document(self, document_id):
            return 0

    monkeypatch.setenv(store_api.STORE_FILE_ENV, str(tmp_path / "knowledge" / "store.json"))
    monkeypatch.setattr(config, "knowledge_tenant_mode", "single")
    fake = _FakeVS()
    monkeypatch.setattr(store_api, "vector_store_manager", fake)
    monkeypatch.setattr("app.services.knowledge_store.vector_store_manager", fake)
    store_api.reset_store()
    try:
        store = store_api.get_store()
        accepted = store.submit_document(
            source_uri="https://www.autodl.com/docs/env/",
            title="AutoDL 环境",
            content=b"# t\n\nbody\n",
            source_kind="manual",
            metadata={"product": "autodl", "os_family": "linux",
                      "target_kind": "ssh", "source_kind": "manual"},
        )
        doc_id = accepted["document_id"]
        record = store.snapshot()["documents"][doc_id]
        assert record["target_kind"] == "ssh", "target_kind 必须落盘"
        assert record["source_kind"] == "manual"
        assert record["product"] == "autodl"
    finally:
        store_api.reset_store()
