"""向量嵌入服务模块 - 多提供商 Embedding 支持

支持 local / dashscope / openai 三种 provider。

关键约束（RAG-04）：

1. **描述符一致性**：每个 embedding 配置有唯一 ``EmbeddingDescriptor``（provider、
   模型、revision、维度、normalization、distance、tokenizer、最大长度、前缀），
   其哈希写入 collection 元数据。索引与查询必须使用同一描述符；同维不同模型、
   不同 normalization 一律拒绝，不同维度更早拒绝。
2. **本地模式 fail closed**：``embedding_provider=local`` 时只从本地缓存加载
   （``local_files_only=True``），失败即报错——绝不联网下载、绝不回退远端
   provider。这样才能兑现“本地诊断不依赖远端 embedding”的承诺。
3. **不做零向量兜底**：零向量会让所有查询等距，RAG 会静默返回任意文档。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List

# 国内 HuggingFace 镜像，必须在任何 huggingface 导入前设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from langchain_core.embeddings import Embeddings
from loguru import logger
from openai import OpenAI

from app.config import config

#: 各 provider 的默认向量维度。必须与 Milvus collection 的 dim 一致。
PROVIDER_DIMENSIONS = {"local": 384, "openai": 1536, "dashscope": 1024}
LOCAL_MODEL_DIMENSIONS = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
}


class DescriptorMismatch(RuntimeError):
    """embedding 描述符与 collection 不一致，或描述符缺失/损坏。"""


@dataclass(frozen=True)
class EmbeddingDescriptor:
    """embedding 配置的完整身份。任何字段变化都必须新建 collection/索引。"""

    provider: str
    model: str
    revision: str = ""
    dimension: int = 384
    normalization: str = "l2"
    distance: str = "cosine"
    tokenizer: str = ""
    max_tokens: int = 0
    prefix: str = ""
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def hash(self) -> str:
        payload = {key: value for key, value in asdict(self).items() if key != "extra"}
        payload["extra"] = self.extra or {}
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["hash"] = self.hash
        return payload


def current_descriptor() -> EmbeddingDescriptor:
    """从配置推导当前描述符。"""
    provider = (config.embedding_provider or "local").strip().lower()
    model = config.embedding_model or ""
    if provider == "local":
        dimension = LOCAL_MODEL_DIMENSIONS.get(model, PROVIDER_DIMENSIONS["local"])
        tokenizer = model
        max_tokens = int(config.embedding_max_tokens)
    elif provider == "openai":
        dimension = PROVIDER_DIMENSIONS["openai"]
        tokenizer = "cl100k_base"
        max_tokens = 8191
    else:
        dimension = PROVIDER_DIMENSIONS["dashscope"]
        tokenizer = "dashscope"
        max_tokens = 8192
    return EmbeddingDescriptor(
        provider=provider,
        model=model or "unknown",
        revision="",
        dimension=dimension,
        normalization="l2",
        distance="cosine",
        tokenizer=tokenizer,
        max_tokens=max_tokens,
        prefix="",
    )


def metric_type_for(distance: str) -> str:
    """描述符的 ``distance`` -> Milvus ``metric_type``（建库与查询必须一致）。"""
    value = (distance or "cosine").strip().lower()
    return {
        "cosine": "COSINE",
        "ip": "IP",
        "inner_product": "IP",
        "l2": "L2",
        "euclidean": "L2",
    }.get(value, "COSINE")


def assert_descriptor_matches(expected: EmbeddingDescriptor,
                              stored: EmbeddingDescriptor | None) -> None:
    """collection 必须与当前 embedding 描述符完全一致。"""
    if stored is None:
        raise DescriptorMismatch(
            "collection 缺少 embedding 描述符：无法证明索引与查询使用同一向量空间。"
            "请重建索引（不要静默复用旧数据）。"
        )
    if expected.dimension != stored.dimension:
        raise DescriptorMismatch(
            f"向量维度不匹配：collection 是 {stored.dimension} 维，"
            f"当前配置是 {expected.dimension} 维。请显式重建 collection，"
            "不要自动 drop 或复用。"
        )
    if expected.hash != stored.hash:
        changed = [
            key for key in ("provider", "model", "revision", "normalization",
                            "distance", "tokenizer", "max_tokens", "prefix")
            if getattr(expected, key) != getattr(stored, key)
        ]
        raise DescriptorMismatch(
            "embedding 描述符与 collection 不一致（字段: "
            + ", ".join(changed or ["hash"])
            + "）。同维不同模型也不能混用；请新建 revision/collection 并重建索引。"
        )


def write_descriptor(path: Path, descriptor: EmbeddingDescriptor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(descriptor.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    os.replace(temp, path)


def read_descriptor(path: Path, *, strict: bool = False) -> EmbeddingDescriptor | None:
    """读取描述符；损坏时在 strict 模式报错，否则返回 None（表示缺失）。"""
    if not path.exists():
        if strict:
            raise DescriptorMismatch(f"描述符文件不存在: {path}")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise DescriptorMismatch(f"embedding 描述符损坏: {exc}") from exc
        logger.warning(f"embedding 描述符损坏，已忽略: {exc}")
        return None
    if not isinstance(payload, dict):
        if strict:
            raise DescriptorMismatch("embedding 描述符格式非法")
        return None
    payload.pop("hash", None)
    extra = payload.pop("extra", {}) or {}
    try:
        return EmbeddingDescriptor(extra=extra, **payload)
    except TypeError as exc:
        if strict:
            raise DescriptorMismatch(f"embedding 描述符字段不完整: {exc}") from exc
        return None


def _load_local_embeddings(model_name: str, *, local_only: bool = True) -> Embeddings:
    """加载本地 sentence-transformers 模型（CPU）。"""
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(
        model_name=model_name,
        cache_folder=config.hf_cache_dir or None,
        model_kwargs={"device": "cpu", "local_files_only": local_only},
        encode_kwargs={"normalize_embeddings": True},
    )


class LazyLocalEmbeddings(Embeddings):
    """延迟加载的本地 embedding（A03）。

    为什么不在构造时就加载：``create_embedding_service(strict_local=True)`` 是
    “这个请求只允许本地能力”的声明，很多调用方（例如只做检索、只查询索引状态）
    在整条路径上根本不会真正 embedding。构造时就加载模型会：把模型下载/加载
    时机塞进请求线程、在依赖缺失时把“未使用”变成“失败”，并且让一次本地请求
    在不知情的情况下等待磁盘/网络。

    因此：
    - 构造函数只记下模型名与加载策略，**不做任何 I/O、不 import HF、不联网**；
    - 第一次真正 ``embed_documents``/``embed_query`` 时才 ``local_files_only=True``
      加载，失败即抛错——绝不回退远端 provider（fail closed）。
    """

    def __init__(self, model_name: str, *, local_only: bool = True) -> None:
        self._model_name = model_name
        self._local_only = bool(local_only)
        self._inner: Embeddings | None = None
        self._load_error: Exception | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self) -> Embeddings:
        if self._inner is not None:
            return self._inner
        if self._load_error is not None:
            raise self._load_error
        try:
            self._inner = _load_local_embeddings(self._model_name, local_only=self._local_only)
            return self._inner
        except Exception as exc:  # noqa: BLE001 - 本地不可用必须明确失败
            self._load_error = RuntimeError(
                "本地 embedding 模型不可用：请预先下载模型后离线运行。"
                "本地模式不会联网下载，也不会回退到远端 embedding provider。"
                f"（模型: {self._model_name}；{type(exc).__name__}: {exc}）"
            )
            raise self._load_error from exc

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._load().embed_documents(texts)

    def embed_query(self, text: str) -> List[float]:
        return self._load().embed_query(text)


def _create_openai_embeddings(
    api_key: str,
    base_url: str,
    model: str,
    dimensions: int = 1024,
) -> "Embeddings":
    """通过 OpenAI 兼容 API 创建 Embeddings 实例"""

    class OpenAICompatibleEmbeddings(Embeddings):
        """OpenAI 兼容的 Embeddings 实现"""

        def __init__(self, api_key: str, base_url: str, model: str, dimensions: int):
            if not api_key or api_key in (
                "your-api-key-here", "your-dashscope-api-key-here",
                "sk-your-deepseek-api-key-here", "sk-your-openai-key",
            ):
                raise ValueError(
                    "请设置 EMBEDDING_API_KEY 或 EMBEDDING_PROVIDER=local。"
                    f"当前 provider={config.embedding_provider}，base_url={base_url}"
                )
            self.client = OpenAI(api_key=api_key, base_url=base_url)
            self.model = model
            self.dimensions = dimensions
            masked = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
            logger.info(
                f"Embeddings 初始化: provider={config.embedding_provider}, "
                f"model={model}, dim={dimensions}, key={masked}"
            )

        def embed_documents(self, texts: List[str]) -> List[List[float]]:
            if not texts:
                return []
            try:
                logger.info(f"批量嵌入 {len(texts)} 个文档")
                kwargs = dict(model=self.model, input=texts, encoding_format="float")
                if self.dimensions:
                    kwargs["dimensions"] = self.dimensions
                response = self.client.embeddings.create(**kwargs)
                return [item.embedding for item in response.data]
            except Exception as e:
                logger.error(f"批量嵌入失败: {e}")
                raise RuntimeError(f"批量嵌入失败: {e}") from e

        def embed_query(self, text: str) -> List[float]:
            if not text or not text.strip():
                raise ValueError("查询文本不能为空")
            try:
                kwargs = dict(model=self.model, input=text, encoding_format="float")
                if self.dimensions:
                    kwargs["dimensions"] = self.dimensions
                response = self.client.embeddings.create(**kwargs)
                return response.data[0].embedding
            except Exception as e:
                logger.error(f"查询嵌入失败: {e}")
                raise RuntimeError(f"查询嵌入失败: {e}") from e

    return OpenAICompatibleEmbeddings(api_key, base_url, model, dimensions)


def local_model_name() -> str:
    """strict local 使用的本地模型名（配置为空时回退到内置小模型）。"""
    return config.embedding_model or (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )


def create_embedding_service(*, strict_local: bool | None = None) -> Embeddings:
    """根据配置创建 Embedding 服务。

    Args:
        strict_local: ``True`` 表示调用方声明“本次请求只允许本地能力”：即使
            配置里写着远端 provider（openai/dashscope），也**只**返回本地
            embedding，绝不选择远端工厂、绝不发 HTTP。其余取值跟随配置
            （``embedding_provider=local`` 本身就是 strict）。

    Raises:
        RuntimeError: 没有可用 provider（绝不返回零向量，也绝不静默换 provider）
    """
    provider = (config.embedding_provider or "local").strip().lower()

    if strict_local is True and provider != "local":
        # A03：strict_local 必须真正约束 provider。旧实现把它当成“说明调用意图”
        # 直接忽略，于是本地运行可以用远端 embedding（并且会真的构造远端客户端）。
        model_name = local_model_name()
        logger.info(
            f"strict_local 请求覆盖远端 embedding provider={provider}，"
            f"改用本地模型（离线，按需加载）: {model_name}"
        )
        return LazyLocalEmbeddings(model_name, local_only=True)

    if provider == "local":
        model_name = local_model_name()
        try:
            logger.info(f"加载本地 Embedding 模型（CPU，离线）: {model_name}")
            return _load_local_embeddings(model_name, local_only=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"本地 Embedding 模型加载失败: {exc}")
            raise RuntimeError(
                "本地 embedding 模型不可用：请预先下载模型后离线运行。"
                "本地模式不会联网下载，也不会回退到远端 embedding provider。"
                f"（模型: {model_name}；{type(exc).__name__}: {exc}）"
            ) from exc

    if provider == "openai":
        return _create_openai_embeddings(
            api_key=config.embedding_api_key or config.llm_api_key,
            base_url=config.embedding_base_url or "https://api.openai.com/v1",
            model=config.embedding_model or "text-embedding-3-small",
            dimensions=PROVIDER_DIMENSIONS["openai"],
        )

    if provider == "dashscope":
        return _create_openai_embeddings(
            api_key=config.embedding_api_key or config.llm_api_key,
            base_url=config.embedding_base_url
            or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=config.embedding_model or "text-embedding-v4",
            dimensions=PROVIDER_DIMENSIONS["dashscope"],
        )

    raise RuntimeError(
        f"未知的 EMBEDDING_PROVIDER: {provider}（支持 local / openai / dashscope）"
    )


# 懒加载全局单例（避免 import 时因模型缺失导致整个应用崩溃）
_vector_embedding_service: Embeddings | None = None


def get_embedding_service() -> Embeddings:
    global _vector_embedding_service
    if _vector_embedding_service is None:
        _vector_embedding_service = create_embedding_service()
    return _vector_embedding_service


def reset_embedding_service() -> None:
    """测试/重配置用：清空缓存，让下一次调用重新按配置创建。"""
    global _vector_embedding_service
    _vector_embedding_service = None


# 兼容旧代码的模块级 __getattr__（Python 3.7+）
def __getattr__(name: str):
    if name == "vector_embedding_service":
        return get_embedding_service()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
