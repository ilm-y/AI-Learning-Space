"""LLM Adapter 层：Agent 与模型之间的唯一界面。"""

from app.llm.base import ChatMessage, LLMAdapter, LLMError
from app.llm.factory import build_llm_adapter
from app.llm.mock import MockLLMAdapter
from app.llm.openai_compatible import OpenAICompatibleAdapter

__all__ = [
    "ChatMessage",
    "LLMAdapter",
    "LLMError",
    "MockLLMAdapter",
    "OpenAICompatibleAdapter",
    "build_llm_adapter",
]
