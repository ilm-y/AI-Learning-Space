"""Context Builder 层：把检索结果变成一次真正发给模型的上下文。"""

from app.context.builder import (
    PRIORITY_EVIDENCE,
    PRIORITY_HISTORY,
    PRIORITY_KNOWLEDGE,
    PRIORITY_MEMORY,
    PRIORITY_UNDERSTANDING,
    PRIORITY_USER_INPUT,
    BuiltContext,
    ContextBuilder,
    ContextSection,
)
from app.context.tokens import estimate_tokens, truncate_to_tokens

__all__ = [
    "PRIORITY_EVIDENCE",
    "PRIORITY_HISTORY",
    "PRIORITY_KNOWLEDGE",
    "PRIORITY_MEMORY",
    "PRIORITY_UNDERSTANDING",
    "PRIORITY_USER_INPUT",
    "BuiltContext",
    "ContextBuilder",
    "ContextSection",
    "estimate_tokens",
    "truncate_to_tokens",
]
