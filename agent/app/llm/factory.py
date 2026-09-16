"""按配置装配 LLM 适配器。

`llm_provider = openai` 但没填 api_key 时**不静默退回 mock** —— 那样只会让人以为
模型在工作。这里直接报错，让问题在启动时就暴露出来。
"""

from __future__ import annotations

from app.config import Settings
from app.llm.base import LLMAdapter
from app.llm.mock import MockLLMAdapter
from app.llm.openai_compatible import OpenAICompatibleAdapter

MOCK = "mock"
OPENAI_COMPATIBLE = "openai"


def build_llm_adapter(settings: Settings) -> LLMAdapter:
    provider = settings.llm_provider.strip().lower()

    if provider == MOCK:
        return MockLLMAdapter()

    if provider == OPENAI_COMPATIBLE:
        if not settings.llm_api_key:
            raise ValueError(
                "AILS_LLM_PROVIDER=openai 但没有配置 AILS_LLM_API_KEY。"
                "要么填上 Key，要么把 provider 设回 mock。"
            )
        return OpenAICompatibleAdapter(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )

    raise ValueError(f"未知的 AILS_LLM_PROVIDER：{settings.llm_provider}（可选：mock / openai）")
