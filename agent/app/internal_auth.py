"""内部服务鉴权（Agent HTTP 端点共用）。

两层凭据，职责分开：

1. `X-Internal-Token`：证明调用方是受信内部服务；未配置时 fail closed（503）。
2. `X-Internal-Identity`：Spring Boot 用独立密钥签发的短期声明，绑定
   `sub`（用户）、`aud`（接收方）、`iat` / `exp`（有效期）和 `jti`。
   Agent 只从**签名校验通过**的声明里取 userId。

`X-Internal-User-Id` 仍可携带，但只用于交叉核对；它本身不构成身份，
单独出现时必须拒绝。这样即使共享 token 泄露，没有签名密钥也不能冒充任意用户。
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Header, HTTPException, Request, status

from app.config import Settings
from app.internal_identity import (
    IDENTITY_HEADER,
    IdentityError,
    verify_identity,
)

log = logging.getLogger(__name__)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_service_token(request: Request, token: str | None) -> None:
    settings = _settings(request)
    if not settings.internal_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AILS_INTERNAL_TOKEN 未配置，内部接口已禁用",
        )
    if not token or not secrets.compare_digest(token, settings.internal_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="内部凭据无效",
        )


async def require_internal_service(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """只校验服务身份，不绑定用户；用于诊断等无用户数据的接口。"""
    _require_service_token(request, x_internal_token)


async def require_internal_user(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_internal_identity: str | None = Header(default=None, alias=IDENTITY_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias="X-Internal-User-Id"),
) -> int:
    """校验服务凭据 + 短期签名身份，返回可信 userId。"""
    _require_service_token(request, x_internal_token)
    settings = _settings(request)
    if not settings.internal_identity_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AILS_INTERNAL_IDENTITY_SECRET 未配置，用户级内部接口已禁用",
        )
    try:
        claims = verify_identity(
            x_internal_identity,
            secret=settings.internal_identity_secret,
            audience=settings.internal_identity_audience,
            issuer=settings.internal_identity_issuer,
            max_ttl_seconds=settings.internal_identity_max_ttl_seconds,
            clock_skew_seconds=settings.internal_identity_clock_skew_seconds,
        )
    except IdentityError as exc:
        # 不把具体失败点回给调用方，避免变成签名/过期时间的探测 oracle。
        log.warning("内部身份声明校验失败: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="内部身份声明无效",
        ) from exc

    if x_internal_user_id is not None and x_internal_user_id.strip():
        try:
            header_user_id = int(x_internal_user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-Internal-User-Id 必须是正整数",
            ) from exc
        if header_user_id != claims.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="X-Internal-User-Id 与签名身份不一致",
            )
    return claims.user_id


def require_matching_user(payload_user_id: int, internal_user_id: int) -> None:
    """请求体里的 userId 不能越过签名身份。"""
    if payload_user_id != internal_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="请求 userId 与内部身份不一致",
        )
