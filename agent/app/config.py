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
            "max_context_tokens": self.max_context_tokens,
        }
