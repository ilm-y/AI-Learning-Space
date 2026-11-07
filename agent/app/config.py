"""Agent 运行时配置。

所有配置项都可以不填：默认值是「Mock LLM + Fixture Retriever」，
也就是不连任何外部服务也能把整条流水线跑通。
真实服务的地址与密钥写进 `agent/.env`（已被 .gitignore 排除）。
"""

from __future__ import annotations

import math
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# agent/ 目录，用于把相对路径（fixtures、.env）解析成绝对路径
AGENT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AILS_",
        env_file=AGENT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 校验失败时不要回显输入值：配置里可能有密钥。
        hide_input_in_errors=True,
    )

    # ---------- HTTP 服务自身 ----------
    host: str = "127.0.0.1"
    port: int = 8000

    # ---------- 后端（Spring Boot）----------
    backend_base_url: str = "http://localhost:8080"
    backend_timeout_seconds: float = 10.0
    # 异步分析 Worker 不回传用户 JWT。它用这个共享密钥访问后端内部接口
    # （请求头 X-Internal-Token）。必须与后端 ails.internal.token 一致。
    internal_token: str = ""
    # Spring → Agent 的签名校验密钥（aud=learning-agent）。Agent 只用它校验入站身份；
    # 后端委托声明（aud=learning-backend）由 Spring 用另一把 delegation-secret 签发，
    # 该密钥<b>不在 Agent 配置里</b>。未配置本项时用户级内部路由 fail closed。
    internal_identity_secret: str = ""
    internal_identity_audience: str = "learning-agent"
    internal_identity_issuer: str = "learning-backend"
    internal_identity_ttl_seconds: int = 60
    internal_identity_max_ttl_seconds: int = 300
    internal_identity_clock_skew_seconds: int = 5
    # 仅用于 fail-closed 守卫：委托声明（aud=learning-backend）只能由 Spring 用
    # delegation-secret 签发，Agent 不得持有该密钥。字段默认空；一旦从环境变量或
    # .env 读到非空值，Settings 构造失败，HTTP/Worker 都无法启动。不要在任何地方
    # 把它当合法输入使用，也不要写进 describe()/日志。
    internal_delegation_secret: str = Field(default="", exclude=True, repr=False)

    # ---------- RabbitMQ ----------
    # 只有 `python -m app.worker` 会使用这些配置；HTTP 服务不依赖 RabbitMQ。
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    rabbitmq_prefetch: int = 1
    # 拓扑名称必须与后端 ails.analysis.mq.* 一致（两边都会幂等声明）。
    analysis_exchange: str = "learning.analysis.exchange"
    analysis_dlx: str = "learning.analysis.dlx"
    analysis_request_queue: str = "learning.analysis.request"
    analysis_request_routing_key: str = "analysis.request"
    analysis_result_queue: str = "learning.analysis.result"
    analysis_result_routing_key: str = "analysis.result"
    analysis_retry_queue: str = "learning.analysis.retry"
    analysis_retry_routing_key: str = "analysis.retry"
    analysis_dead_queue: str = "learning.analysis.dlq"
    analysis_dead_routing_key: str = "analysis.dead"
    analysis_retry_ttl_ms: int = 15000
    analysis_max_retries: int = 3
    # 当连「重试副本 / 死信副本」都发不出去时，先退避再 nack(requeue=True)。
    # 目的是避免 broker 半可用状态下形成无上限的快速重投；此时绝不 ACK。
    analysis_publish_failure_backoff_seconds: float = 15.0

    # ---------- LLM ----------
    llm_provider: str = "mock"  # mock | openai
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.6
    llm_max_tokens: int = 1024
    llm_timeout_seconds: float = 60.0

    # ---------- 检索 ----------
    retriever_mode: str = "auto"  # auto | http | fixture
    fixture_path: str = "fixtures/demo_space.json"
    memory_top_k: int = 5
    knowledge_top_k: int = 5
    # 0.35 只是 Hash/真实模型的初始可调起点，不是「答案正确」的阈值。
    # 不同 Embedding 模型的相似度分布不同，部署后必须用自己的语料校准。
    knowledge_min_score: float = 0.35
    # 一次知识检索（白名单 + 查询向量 + Qdrant）的总时限；超时按依赖错误降级为空知识。
    knowledge_timeout_seconds: float = 15.0
    evidence_top_k: int = 3

    # ---------- 上下文预算 ----------
    recent_history_limit: int = 12
    max_context_tokens: int = 3000

    # ---------- 文档解析 / 切块 ----------
    # 后端 ails.document.storage.max-size-bytes 默认 10 MiB；两边必须一致或这里更小。
    document_max_bytes: int = 10 * 1024 * 1024
    document_chunk_max_chars: int = 1200
    document_chunk_overlap_chars: int = 150

    # ---------- Embedding ----------
    # hash = 确定性离线向量（只用于开发/测试）；openai = OpenAI 兼容 /embeddings。
    embedding_provider: str = "hash"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    # 必须与真实模型输出维度一致；Hash provider 的集合也按此维度创建。
    embedding_dimension: int = 256
    embedding_timeout_seconds: float = 30.0
    embedding_max_retries: int = 2
    embedding_retry_backoff_seconds: float = 1.0
    # 单次 /embeddings 请求的最大文本数。百炼 text-embedding-v4 上限为 10，
    # 因此默认取 10；使用其他兼容服务可以按服务端限制调大。
    embedding_batch_size: int = 10

    # ---------- Qdrant ----------
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    qdrant_timeout_seconds: float = 30.0
    # collection 最终名称为 <prefix>_<model>_<dimension>，Hash 与真实模型不会混用。
    qdrant_collection_prefix: str = "ails_documents"
    qdrant_upsert_batch_size: int = 64

    # ---------- Memory 向量（内部接口）----------
    # 与文档 collection 刻意分开：记忆向量不应被知识检索召回。
    memory_collection_prefix: str = "ails_memories"
    # 调用方不传 recallK 时的默认召回数；上限由请求模型限制为 200。
    memory_search_top_k: int = 20
    # 0 表示不做阈值过滤。RRF 只用排名，阈值会删掉唯一能召回的那条。
    memory_search_min_score: float = 0.0
    # 单次记忆向量检索的总时限；Spring 侧 HttpClient 还会再设一层超时。
    memory_search_timeout_seconds: float = 2.0

    @field_validator("internal_delegation_secret")
    @classmethod
    def _reject_delegation_signing_secret(cls, value: str) -> str:
        """Agent 进程不能持有后端委托签发密钥。

        这是运行配置的 fail-closed 校验，不是身份协议的一部分，也不能阻止
        拥有修改代码或启动参数权限的人。错误信息只说明发现了该配置，不回显值。
        """
        if value.strip():
            raise ValueError(
                "检测到禁止的 AILS_INTERNAL_DELEGATION_SECRET："
                "Agent 不得持有后端委托签发密钥"
            )
        return ""

    @model_validator(mode="after")
    def _validate_document_pipeline(self) -> "Settings":
        if self.document_max_bytes <= 0:
            raise ValueError("document_max_bytes 必须为正数")
        if self.document_chunk_max_chars <= 0:
            raise ValueError("document_chunk_max_chars 必须为正数")
        if not 0 <= self.document_chunk_overlap_chars < self.document_chunk_max_chars:
            raise ValueError("document_chunk_overlap_chars 必须 >= 0 且小于 max_chars")
        if self.embedding_dimension <= 0:
            raise ValueError("embedding_dimension 必须为正数")
        if self.embedding_max_retries < 0:
            raise ValueError("embedding_max_retries 不能为负数")
        if self.embedding_batch_size <= 0:
            raise ValueError("embedding_batch_size 必须为正数")
        if self.qdrant_upsert_batch_size <= 0:
            raise ValueError("qdrant_upsert_batch_size 必须为正数")
        if not self.memory_collection_prefix.strip():
            raise ValueError("memory_collection_prefix 不能为空")
        if self.memory_search_top_k <= 0:
            raise ValueError("memory_search_top_k 必须为正数")
        if not math.isfinite(self.memory_search_min_score):
            raise ValueError("memory_search_min_score 必须是有限数值")
        if not -1.0 <= self.memory_search_min_score <= 1.0:
            raise ValueError("memory_search_min_score 必须在 [-1, 1] 之间")
        if self.memory_search_timeout_seconds <= 0:
            raise ValueError("memory_search_timeout_seconds 必须为正数")
        if self.knowledge_top_k <= 0:
            raise ValueError("knowledge_top_k 必须为正数")
        if not math.isfinite(self.knowledge_min_score):
            raise ValueError("knowledge_min_score 必须是有限数值")
        if not -1.0 <= self.knowledge_min_score <= 1.0:
            raise ValueError("knowledge_min_score 必须在 [-1, 1] 之间")
        if self.knowledge_timeout_seconds <= 0:
            raise ValueError("knowledge_timeout_seconds 必须为正数")
        if self.internal_identity_ttl_seconds <= 0:
            raise ValueError("internal_identity_ttl_seconds 必须为正数")
        if self.internal_identity_max_ttl_seconds < self.internal_identity_ttl_seconds:
            raise ValueError("internal_identity_max_ttl_seconds 不能小于 ttl_seconds")
        if self.internal_identity_clock_skew_seconds < 0:
            raise ValueError("internal_identity_clock_skew_seconds 不能为负数")
        if not self.internal_identity_audience.strip():
            raise ValueError("internal_identity_audience 不能为空")
        if not self.internal_identity_issuer.strip():
            raise ValueError("internal_identity_issuer 不能为空")
        if (self.internal_token and self.internal_identity_secret
                and self.internal_token == self.internal_identity_secret):
            raise ValueError(
                "internal_token 不能与 internal_identity_secret 相同："
                "共享 bearer token 不能同时充当签名密钥"
            )
        return self

    @property
    def fixture_file(self) -> Path:
        path = Path(self.fixture_path)
        return path if path.is_absolute() else AGENT_ROOT / path

    def describe(self) -> dict[str, object]:
        """给 /health 用：说明当前实际生效的是哪套适配器，不泄露密钥。"""
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model if self.llm_provider != "mock" else "mock",
            "llm_configured": bool(self.llm_api_key) or self.llm_provider == "mock",
            "retriever_mode": self.retriever_mode,
            "backend_base_url": self.backend_base_url,
            "internal_auth_configured": bool(self.internal_token),
            "internal_identity_configured": bool(self.internal_identity_secret),
            "internal_identity_audience": self.internal_identity_audience,
            "internal_identity_issuer": self.internal_identity_issuer,
            "rabbitmq_url": _mask_url(self.rabbitmq_url),
            "max_context_tokens": self.max_context_tokens,
            "knowledge_top_k": self.knowledge_top_k,
            "knowledge_min_score": self.knowledge_min_score,
            "knowledge_timeout_seconds": self.knowledge_timeout_seconds,
            "embedding_provider": self.embedding_provider,
            "embedding_model": "hash-v1"
            if self.embedding_provider == "hash"
            else self.embedding_model,
            "embedding_dimension": self.embedding_dimension,
            "embedding_batch_size": self.embedding_batch_size,
            "qdrant_url": self.qdrant_url,
            "qdrant_collection_prefix": self.qdrant_collection_prefix,
            "memory_collection_prefix": self.memory_collection_prefix,
            "memory_search_top_k": self.memory_search_top_k,
            "memory_search_min_score": self.memory_search_min_score,
            "memory_search_timeout_seconds": self.memory_search_timeout_seconds,
        }


def _mask_url(url: str) -> str:
    """健康检查里不要回显 RabbitMQ 密码。"""
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
