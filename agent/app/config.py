"""Agent 运行时配置。

所有配置项都可以不填：默认值是「Mock LLM + Fixture Retriever」，
也就是不连任何外部服务也能把整条流水线跑通。
真实服务的地址与密钥写进 `agent/.env`（已被 .gitignore 排除）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# agent/ 目录，用于把相对路径（fixtures、.env）解析成绝对路径
AGENT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AILS_",
        env_file=AGENT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- HTTP 服务自身 ----------
    host: str = "127.0.0.1"
    port: int = 8000

    # ---------- 后端（Spring Boot）----------
    backend_base_url: str = "http://localhost:8080"
    backend_timeout_seconds: float = 10.0
    # 异步分析 Worker 不回传用户 JWT。它用这个共享密钥 + 目标 userId 调用后端的
    # 只读检索接口（请求头 X-Internal-Token / X-Internal-User-Id）。
    # 必须与后端 ails.internal.token 一致；为空时异步分析的检索会被后端拒绝。
    internal_token: str = ""

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
    evidence_top_k: int = 3

    # ---------- 上下文预算 ----------
    recent_history_limit: int = 12
    max_context_tokens: int = 3000

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
            "rabbitmq_url": _mask_url(self.rabbitmq_url),
            "max_context_tokens": self.max_context_tokens,
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
