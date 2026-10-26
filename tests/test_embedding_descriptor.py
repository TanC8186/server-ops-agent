"""RAG-04 回归：embedding 描述符与 collection 必须严格匹配。

原始问题：

- collection 固定 384 维，但 embedding 还有 OpenAI 1536 / DashScope 1024 分支；
- 同维不同模型也能写进同一个 collection；
- 本地模式加载失败时会悄悄回退到远端 provider（``provider = "dashscope"``）；
- 本地加载先 ``local_files_only=True``，失败后再联网下载。

本文件只做单元级验证（不连 Milvus、不下载模型）。
"""

from __future__ import annotations

import pytest

from app.config import config
from app.services import vector_embedding_service as emb


def test_local_mode_failure_never_falls_back_to_remote(monkeypatch):
    """本地模式（strict）失败必须 fail closed，而不是偷偷换成 dashscope。"""
    monkeypatch.setattr(config, "embedding_provider", "local")

    def boom(*args, **kwargs):
        raise RuntimeError("模型未预置")

    monkeypatch.setattr(emb, "_load_local_embeddings", boom, raising=False)

    with pytest.raises(RuntimeError) as exc:
        emb.create_embedding_service(strict_local=True)
    message = str(exc.value)
    assert "本地" in message
    assert "不会" in message and "回退" in message


def test_local_mode_never_retries_online_download(monkeypatch):
    calls: list[dict] = []

    def fake_load(model_name, local_only):
        calls.append({"model": model_name, "local_only": local_only})
        raise RuntimeError("missing cache")

    monkeypatch.setattr(emb, "_load_local_embeddings", fake_load, raising=False)
    monkeypatch.setattr(config, "embedding_provider", "local")

    with pytest.raises(RuntimeError):
        emb.create_embedding_service(strict_local=True)

    assert calls, "必须尝试加载本地模型"
    assert all(call["local_only"] is True for call in calls), (
        "strict 本地模式不得联网下载模型"
    )


def test_non_strict_local_also_never_silently_uses_remote(monkeypatch):
    """即使非 strict，也不能把本地失败伪装成远端成功——必须显式报错。"""
    monkeypatch.setattr(config, "embedding_provider", "local")
    monkeypatch.setattr(
        emb, "_load_local_embeddings",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("missing")),
        raising=False,
    )
    monkeypatch.setattr(config, "embedding_api_key", "")

    with pytest.raises(RuntimeError):
        emb.create_embedding_service(strict_local=False)


def test_descriptor_captures_all_identity_fields():
    descriptor = emb.EmbeddingDescriptor(
        provider="local",
        model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        revision="e8f8c211226b894fcb81acc59f3b34baef85cb40",
        dimension=384,
        normalization="l2",
        distance="cosine",
        tokenizer="xlm-roberta-base-sentencepiece",
        max_tokens=128,
        prefix="",
    )
    payload = descriptor.to_dict()
    assert set(payload) >= {
        "provider", "model", "revision", "dimension", "normalization",
        "distance", "tokenizer", "max_tokens", "prefix", "hash",
    }
    assert len(payload["hash"]) == 64


def test_descriptor_hash_changes_when_any_field_changes():
    base = dict(provider="local", model="m", revision="r", dimension=384,
                normalization="l2", distance="cosine", tokenizer="t",
                max_tokens=128, prefix="")
    a = emb.EmbeddingDescriptor(**base)
    b = emb.EmbeddingDescriptor(**{**base, "model": "m2"})
    c = emb.EmbeddingDescriptor(**{**base, "normalization": "none"})
    d = emb.EmbeddingDescriptor(**{**base, "distance": "ip"})

    assert len({a.hash, b.hash, c.hash, d.hash}) == 4


def test_collection_descriptor_mismatch_is_rejected():
    base = dict(provider="local", model="m", revision="r", dimension=384,
                normalization="l2", distance="cosine", tokenizer="t",
                max_tokens=128, prefix="")
    current = emb.EmbeddingDescriptor(**base)
    other_model = emb.EmbeddingDescriptor(**{**base, "model": "other"})

    with pytest.raises(emb.DescriptorMismatch) as exc:
        emb.assert_descriptor_matches(current, other_model)
    assert "model" in str(exc.value)

    emb.assert_descriptor_matches(current, current)  # 相同描述符通过


def test_same_dimension_different_model_is_rejected():
    """同维不同模型不能共用向量空间（原始缺陷）。"""
    base = dict(provider="local", revision="r", dimension=384,
                normalization="l2", distance="cosine", tokenizer="t",
                max_tokens=128, prefix="")
    a = emb.EmbeddingDescriptor(model="model-a", **base)
    b = emb.EmbeddingDescriptor(model="model-b", **base)

    with pytest.raises(emb.DescriptorMismatch):
        emb.assert_descriptor_matches(a, b)


def test_dimension_mismatch_is_rejected_before_query():
    base = dict(provider="openai", model="text-embedding-3-small", revision="r",
                normalization="l2", distance="cosine", tokenizer="t",
                max_tokens=8191, prefix="")
    a = emb.EmbeddingDescriptor(dimension=1536, **base)
    b = emb.EmbeddingDescriptor(dimension=384, **base)

    with pytest.raises(emb.DescriptorMismatch) as exc:
        emb.assert_descriptor_matches(a, b)
    assert "384" in str(exc.value) or "dimension" in str(exc.value)


def test_missing_collection_descriptor_fails_closed():
    descriptor = emb.EmbeddingDescriptor(
        provider="local", model="m", revision="r", dimension=384,
        normalization="l2", distance="cosine", tokenizer="t", max_tokens=128, prefix="",
    )
    with pytest.raises(emb.DescriptorMismatch):
        emb.assert_descriptor_matches(descriptor, None)


def test_descriptor_is_persisted_and_read_back(tmp_path):
    descriptor = emb.EmbeddingDescriptor(
        provider="local", model="m", revision="r", dimension=384,
        normalization="l2", distance="cosine", tokenizer="t", max_tokens=128, prefix="",
    )
    path = tmp_path / "descriptor.json"
    emb.write_descriptor(path, descriptor)

    loaded = emb.read_descriptor(path)
    assert loaded is not None
    assert loaded.hash == descriptor.hash


def test_corrupt_descriptor_file_is_reported_not_ignored(tmp_path):
    path = tmp_path / "descriptor.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(emb.DescriptorMismatch) as exc:
        emb.read_descriptor(path, strict=True)
    assert "描述符" in str(exc.value)


def test_local_embedding_dimension_matches_collection_constant():
    """本地模型输出维度必须与 collection 的向量维度一致。"""
    from app.core.milvus_client import MilvusClientManager

    assert MilvusClientManager.VECTOR_DIM == 384


def test_current_descriptor_reflects_config(monkeypatch):
    monkeypatch.setattr(config, "embedding_provider", "local")
    monkeypatch.setattr(
        config, "embedding_model",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    monkeypatch.setattr(config, "embedding_max_tokens", 128)

    descriptor = emb.current_descriptor()
    assert descriptor.provider == "local"
    assert descriptor.dimension == 384
    assert descriptor.max_tokens == 128
    assert descriptor.normalization == "l2"
