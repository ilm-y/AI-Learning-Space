"""短期签名内部身份声明（Agent 侧校验）。

共享服务 token 只能证明「调用方是内部服务」，不能证明「调用方代表的用户是谁」。
本模块在服务 token 之外再校验一份由 Spring Boot 用独立密钥签发的短期声明：

    X-Internal-Identity: v1.<base64url(payload)>.<base64url(hmac-sha256)>

payload 绑定签发方、用户、接收方、签发时间、过期时间和关联 id。Agent 只信任
签名校验通过、issuer 与 audience 都匹配的声明；普通 Header 里的 userId 最多用于
交叉核对，不能单独作为身份来源。

密钥边界：

- ``identity-secret``：Spring → Agent 的声明（aud=learning-agent）。Agent 必须持有它
  才能校验，因此也能签发同 audience 的声明；这只影响 Agent 自己的入口。
- ``delegation-secret``：Spring → 后端读接口的声明（aud=learning-backend）。它只在
  Spring 进程，Agent <b>不持有</b>。后端委托声明由 Spring 放入 MQ 任务，Agent 原样转发。

``jti`` 只用于审计关联，不是重放防护；声明在有效期内可多次使用。密钥不进入前端、
URL、日志或仓库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

IDENTITY_VERSION = "v1"
IDENTITY_HEADER = "X-Internal-Identity"
IDENTITY_AUDIENCE = "learning-agent"
IDENTITY_ISSUER = "learning-backend"
DEFAULT_TTL_SECONDS = 60
DEFAULT_MAX_TTL_SECONDS = 300
DEFAULT_CLOCK_SKEW_SECONDS = 5


class IdentityError(ValueError):
    """签名声明不可用；调用方统一按 401 处理，不向客户端回显具体失败点。"""


@dataclass(frozen=True)
class IdentityClaims:
    user_id: int
    audience: str
    issuer: str
    issued_at: int
    expires_at: int
    jti: str
    scope: str | None = None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(secret: str, signing_input: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64url_encode(digest)


def mint_identity(
    user_id: int,
    *,
    secret: str,
    audience: str = IDENTITY_AUDIENCE,
    issuer: str = IDENTITY_ISSUER,
    scope: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
    jti: str | None = None,
) -> str:
    """签发一份声明。

    生产签发方是 Spring Boot。这里保留同构实现供测试/开发脚本使用：Agent 进程
    只有在显式拿到 delegation-secret 时才能生成后端接受的声明，正常部署不提供
    该密钥。Agent 的 ``identity-secret`` 只能签出 aud=learning-agent 的声明。
    """
    if not secret:
        raise IdentityError("内部身份签名密钥未配置")
    if user_id <= 0:
        raise IdentityError("userId 必须是正整数")
    if ttl_seconds <= 0:
        raise IdentityError("ttl_seconds 必须为正数")
    issued_at = int(time.time() if now is None else now)
    payload = {
        "iss": issuer,
        "sub": int(user_id),
        "aud": audience,
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
        "jti": jti or secrets.token_urlsafe(16),
    }
    if scope is not None:
        payload["scope"] = scope
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    encoded = _b64url_encode(body.encode("utf-8"))
    signing_input = f"{IDENTITY_VERSION}.{encoded}"
    return f"{signing_input}.{_sign(secret, signing_input)}"


def verify_identity(
    token: str | None,
    *,
    secret: str,
    audience: str = IDENTITY_AUDIENCE,
    issuer: str = IDENTITY_ISSUER,
    now: int | None = None,
    max_ttl_seconds: int = DEFAULT_MAX_TTL_SECONDS,
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
) -> IdentityClaims:
    """校验签名、签发方、接收方、有效期和声明结构；任何异常都抛 IdentityError。"""
    if not secret:
        raise IdentityError("内部身份签名密钥未配置")
    if not token:
        raise IdentityError("缺少内部身份声明")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != IDENTITY_VERSION:
        raise IdentityError("内部身份声明格式无效")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = _sign(secret, signing_input)
    if not hmac.compare_digest(parts[2], expected):
        raise IdentityError("内部身份签名无效")
    try:
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityError("内部身份声明无法解析") from exc
    if not isinstance(payload, dict):
        raise IdentityError("内部身份声明不是对象")

    claims = _parse_claims(payload)
    current = int(time.time() if now is None else now)
    if claims.issuer != issuer:
        raise IdentityError("内部身份签发方不匹配")
    if claims.audience != audience:
        raise IdentityError("内部身份接收方不匹配")
    if claims.expires_at <= current - clock_skew_seconds:
        raise IdentityError("内部身份声明已过期")
    if claims.issued_at > current + clock_skew_seconds:
        raise IdentityError("内部身份声明签发时间在未来")
    if claims.expires_at <= claims.issued_at:
        raise IdentityError("内部身份声明有效期无效")
    if claims.expires_at - claims.issued_at > max_ttl_seconds:
        raise IdentityError("内部身份声明有效期超过上限")
    return claims


def _parse_claims(payload: dict[str, Any]) -> IdentityClaims:
    issuer = payload.get("iss")
    if not isinstance(issuer, str) or not issuer:
        raise IdentityError("内部身份缺少 issuer")
    subject = payload.get("sub")
    if isinstance(subject, bool) or not isinstance(subject, (int, str)):
        raise IdentityError("内部身份缺少 subject")
    try:
        user_id = int(subject)
    except (TypeError, ValueError) as exc:
        raise IdentityError("内部身份 subject 无效") from exc
    if user_id <= 0:
        raise IdentityError("内部身份 subject 必须是正整数")

    audience = payload.get("aud")
    if not isinstance(audience, str) or not audience:
        raise IdentityError("内部身份缺少 audience")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        isinstance(issued_at, bool)
        or isinstance(expires_at, bool)
        or not isinstance(issued_at, int)
        or not isinstance(expires_at, int)
    ):
        raise IdentityError("内部身份缺少有效时间")
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise IdentityError("内部身份缺少 jti")
    scope = payload.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise IdentityError("内部身份 scope 无效")
    return IdentityClaims(
        user_id=user_id,
        audience=audience,
        issuer=issuer,
        issued_at=issued_at,
        expires_at=expires_at,
        jti=jti,
        scope=scope,
    )


def _main() -> None:
    """给本地脚本签发一次短期声明；密钥只从环境变量读取，不出现在命令行。"""
    import argparse
    import os

    parser = argparse.ArgumentParser(description="签发 Agent 内部身份声明")
    parser.add_argument("--user", type=int, required=True, help="目标用户 id")
    parser.add_argument(
        "--audience",
        default=os.getenv("AILS_INTERNAL_IDENTITY_AUDIENCE", IDENTITY_AUDIENCE),
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="可选权限范围；后端委托声明固定 analysis:read",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=int(os.getenv("AILS_INTERNAL_IDENTITY_TTL_SECONDS", "60")),
    )
    args = parser.parse_args()
    if args.audience == IDENTITY_AUDIENCE:
        secret = os.getenv("AILS_INTERNAL_IDENTITY_SECRET", "")
    else:
        # 后端 audience 只能由 Spring 持有的委托密钥签发；开发脚本也必须显式提供。
        secret = os.getenv("AILS_INTERNAL_DELEGATION_SECRET", "")
    print(
        mint_identity(
            args.user,
            secret=secret,
            audience=args.audience,
            scope=args.scope,
            ttl_seconds=args.ttl,
        )
    )


__all__ = [
    "DEFAULT_CLOCK_SKEW_SECONDS",
    "DEFAULT_MAX_TTL_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "IDENTITY_AUDIENCE",
    "IDENTITY_HEADER",
    "IDENTITY_ISSUER",
    "IdentityClaims",
    "IdentityError",
    "mint_identity",
    "verify_identity",
]


if __name__ == "__main__":
    _main()
