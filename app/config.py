"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

import os
from pathlib import Path
from typing import Annotated, Any, Dict

from pydantic import Field, field_serializer, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# .env 文件路径（相对于项目根目录，不依赖当前工作目录）。
# AI_CONFIG_FILE 允许测试/部署指向另一份 env_file；它本身必须来自进程环境，
# 以免“配置文件自己指定配置文件”的递归。
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_CONFIG_FILE_OVERRIDE = os.environ.get("AI_CONFIG_FILE", "").strip()

# 默认只信任本机前端来源；不要给 "*" 开口子
_DEFAULT_CORS_ORIGINS = ["http://localhost:9900", "http://127.0.0.1:9900"]

# 需要打码的字段：导出配置（model_dump / model_dump_json）时绝不出现明文。
_SECRET_FIELDS = (
    "api_key",
    "llm_api_key",
    "embedding_api_key",
    "tencentcloud_secret_id",
    "tencentcloud_secret_key",
)


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=_CONFIG_FILE_OVERRIDE or str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    app_name: str = "OnCallAgent"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 9900

    # ====== 访问控制 ======
    # dev: 未设置密钥时允许本机直接访问（默认，便于本地演示）
    # deployment: 未设置密钥时所有受保护路由一律 401（fail closed）
    auth_mode: str = "dev"
    # 应用访问密钥；只能来自配置/环境，绝不在日志或接口中回显
    api_key: str = Field(default="", repr=False)

    @field_validator("auth_mode", mode="before")
    @classmethod
    def _normalize_auth_mode(cls, value: Any) -> str:
        # 未提供时用字段默认值 dev（validator 不会被调用）；一旦显式提供，
        # 空串/未知值都拒绝：拼错或“传了空值”不能静默降级成未鉴权。
        if value is None:
            return "dev"
        mode = str(value).strip().lower()
        if mode not in {"dev", "deployment"}:
            raise ValueError("AUTH_MODE 只能是 dev 或 deployment")
        return mode

    # ====== CORS ======
    # 只从 Settings 读取，避免 app/main.py 在导入时用 os.getenv 快照，
    # 导致“只写在 .env 里的值不生效”（与 P0-02 同一类缺陷）。
    # NoDecode: 环境变量仍按逗号分隔解析，而不是要求 JSON 数组。
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(_DEFAULT_CORS_ORIGINS)
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        if value is None:
            return list(_DEFAULT_CORS_ORIGINS)
        if isinstance(value, str):
            origins = [origin.strip() for origin in value.split(",")]
            origins = [origin for origin in origins if origin]
            # 空值/全空白不当作“禁用 CORS”，回退到安全的本地默认值
            return origins or list(_DEFAULT_CORS_ORIGINS)
        return value

    @field_serializer(*_SECRET_FIELDS, when_used="always")
    def _mask_secret(self, value: str) -> str:
        """导出配置时打码：repr=False 只隐藏 repr，dump 仍会带明文。"""
        return "***" if value and value.strip() else ""

    # ====== LLM 配置 ======
    # provider: anthropic（默认，走 Messages API）| openai（任意 OpenAI 兼容端点）
    llm_provider: str = "anthropic"
    llm_api_key: str = Field(default="", repr=False)
    # anthropic provider 下这是 API 根地址，SDK 会自己拼 /v1/messages
    llm_base_url: str = "https://www.autodl.art/api/v1/anthropic"
    llm_model: str = "claude-sonnet-4-6"
    llm_max_tokens: int = 4096

    # 本地小模型（vLLM / llama.cpp / Ollama 的 OpenAI 兼容接口）
    local_llm_base_url: str = ""
    local_llm_model: str = "qwen-ops"
    # AutoDL SSH 目标的专用本地模型（留空 = 所有目标都用 local_llm_model）。
    #
    # 为什么需要它：通用 adapter 在 136 条 AutoDL 训练集之外从没学过
    # "目标没有云监控 API" 这件事，实测会把 CPU 负载高规划成 query_cpu_metrics
    # 而目标机器根本没有腾讯云监控。AutoDL adapter 专门训练过这个语境
    # （system prompt 里带"采集方式：SSH 只读 / 不能选择 query_*_metrics"）。
    #
    # 只按 target.kind=ssh 路由，绝不把它设为默认模型：那会让所有目标的
    # 规划都变成 SSH 语境。两个模型必须共用同一个 base_url（vLLM 以
    # --served-model-name 区分），否则这里就变成了第二个端点配置。
    local_llm_model_ssh: str = ""
    # 训练时工具目录是否带参数签名；必须与训练数据一致
    local_llm_with_params: bool = False
    local_llm_max_tokens: int = 768
    diagnostic_timeout_seconds: float = 180
    diagnostic_tool_timeout_seconds: float = 20
    diagnostic_max_tool_calls: int = 10

    # ====== 跨请求资源边界（BE-06） ======
    # 并发诊断准入：超过并发上限的请求排队 admission_queue_timeout_seconds 后拒绝
    max_concurrent_diagnostics: int = 2
    admission_queue_timeout_seconds: float = 30
    # 单次诊断的硬预算；超过后抛 BudgetExhausted，由调用方降级
    diagnostic_max_llm_calls: int = 12
    diagnostic_max_prompt_chars: int = 20000
    diagnostic_max_evidence_chars: int = 60000
    # 日志查询参数收敛：条数上限、逗号分隔关键词个数上限
    diagnostic_log_limit: int = 100
    diagnostic_log_max_keywords: int = 5

    # Embedding 配置
    embedding_provider: str = "local"  # local | dashscope | openai
    embedding_api_key: str = Field(default="", repr=False)
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 向后兼容旧字段名 (DASHSCOPE_* 环境变量仍可用)
    @property
    def dashscope_api_key(self) -> str:
        """兼容旧代码：优先用 LLM_API_KEY，回退到 DASHSCOPE_API_KEY"""
        return self.llm_api_key

    @property
    def dashscope_model(self) -> str:
        """兼容旧代码"""
        return self.llm_model

    @property
    def dashscope_embedding_model(self) -> str:
        """兼容旧代码"""
        return self.embedding_model

    @property
    def rag_model(self) -> str:
        """RAG 使用的模型"""
        return self.llm_model

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒

    # RAG 配置
    rag_top_k: int = 3
    # 检索适用性门控：低于该分数视为未命中（需用验证集校准，不要当成通用阈值）
    rag_min_score: float = 0.0
    # 单次检索最多返回的片段与上下文预算（字符），防止无限注入 prompt
    rag_max_chunks: int = 8
    rag_context_max_chars: int = 6000
    # 诊断运行时是否接入内置知识检索工具（M4）。关闭后诊断仍可运行，只是
    # 没有“知识依据”这一节；Milvus 不可用时不必关闭，工具会如实报 unavailable。
    rag_tool_enabled: bool = True
    # 知识库租户语义（G5）：
    # - single（默认，单租户 demo）：没有归属记录的旧文档对任何已认证 principal 可见，
    #   与今天的行为一致；
    # - multi：没有归属记录的文档对非 dev principal 一律不可见（fail closed），
    #   避免多租户部署里“没记 owner 就等于所有人可见”。
    # 只来自配置/环境，绝不从请求头或客户端 tenant_id 读取。
    knowledge_tenant_mode: str = "single"

    @field_validator("knowledge_tenant_mode", mode="before")
    @classmethod
    def _normalize_knowledge_tenant_mode(cls, value: Any) -> str:
        if value is None:
            return "single"
        mode = str(value).strip().lower()
        if mode not in {"single", "multi"}:
            # 拼错模式必须显式失败：静默回退到 single 会造成“以为开了隔离”的假象。
            raise ValueError("KNOWLEDGE_TENANT_MODE 只能是 single 或 multi")
        return mode
    # embedding 描述符落盘位置；默认 data/embedding_descriptor.json，测试可覆盖。
    embedding_descriptor_file: str = ""

    # 文档分块配置
    # 真实约束是 embedding 模型的 token 窗口，不是字符数：
    # paraphrase-multilingual-MiniLM-L12-v2 的 max_seq_length = 128。
    # chunk_max_size / chunk_overlap 仅作为历史配置保留，不再参与切分。
    chunk_max_size: int = 180
    chunk_overlap: int = 40
    embedding_max_tokens: int = 128

    # 腾讯云 API 凭证
    tencentcloud_appid: str = ""
    tencentcloud_secret_id: str = Field(default="", repr=False)
    tencentcloud_secret_key: str = Field(default="", repr=False)
    tencentcloud_region: str = "ap-beijing"

    # MCP 服务配置
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"
    mcp_ssh_transport: str = "streamable-http"
    mcp_ssh_url: str = "http://localhost:8005/mcp"

    @property
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        servers = {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            },
            "ssh": {
                "transport": self.mcp_ssh_transport,
                "url": self.mcp_ssh_url,
            },
        }
        return servers


# 全局配置实例
config = Settings()
