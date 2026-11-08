"""测试共用的构造工具。"""

from __future__ import annotations

from app.schemas import ChatRequest, HistoryMessage, MessageRole, SessionContext


def make_request(
    message: str,
    *,
    history: list[tuple[str, str]] | None = None,
    space_id: int = 1,
    conversation_id: int = 1,
    session: SessionContext | None = None,
    auth_token: str | None = None,
) -> ChatRequest:
    """`history` 用 `[("user", "..."), ("assistant", "...")]` 这种紧凑写法。

    `conversation_id` / `session` 默认与旧调用保持一致（1 / None），
    但可以显式指定，用来模拟跨 Conversation / 跨 Session 的请求边界。
    """
    return ChatRequest(
        runId=1,
        userId=1,
        conversationId=conversation_id,
        learningSpaceId=space_id,
        userMessage=message,
        history=[
            HistoryMessage(
                role=MessageRole.USER if role == "user" else MessageRole.ASSISTANT,
                content=content,
            )
            for role, content in (history or [])
        ],
        session=session,
        authToken=auth_token,
    )
