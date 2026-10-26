"""内部服务鉴权。

与后端 `InternalServiceAuthenticationFilter` 使用同一组请求头：
`X-Internal-Token` + `X-Internal-User-Id`。

安全策略：

* `AILS_INTERNAL_TOKEN` 未配置时 **fail closed**（503），绝不把内部接口裸奔；
* token 使用 `secrets.compare_digest` 常量时间比较；
* 目标 userId 必须存在且为正整数，业务请求里的 userId 还必须与它一致。
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status

from app.config import Settings


async def require_internal_user(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_internal_user_id: str | None = Header(default=None, alias="X-Internal-User-Id"),
) -> int:
    settings: Settings = request.app.state.settings
    if not settings.internal_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AILS_INTERNAL_TOKEN 未配置，内部文档接口已禁用",
        )
    if not x_internal_token or not secrets.compare_digest(
        x_internal_token, settings.internal_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="内部凭据无效",
        )
    if not x_internal_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 X-Internal-User-Id",
        )
    try:
        user_id = int(x_internal_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Internal-User-Id 必须是正整数",
        ) from exc
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Internal-User-Id 必须是正整数",
        )
    return user_id
