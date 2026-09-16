"""测试共用的构造工具。"""

from __future__ import annotations

from app.schemas import ChatRequest, HistoryMessage, MessageRole


def make_request(
    message: str,
    *,
    history: list[tuple[str, str]] | None = None,
    space_id: int = 1,
    auth_token: str | None = None,
) -> ChatRequest:
    """`history` 用 `[("user", "..."), ("assistant", "...")]` 这种紧凑写法。"""
    return ChatRequest(
        runId=1,
        userId=1,
        conversationId=1,
        learningSpaceId=space_id,
        userMessage=message,
        history=[
            HistoryMessage(
                role=MessageRole.USER if role == "user" else MessageRole.ASSISTANT,
                content=content,
            )
            for role, content in (history or [])
        ],
        authToken=auth_token,
    )
