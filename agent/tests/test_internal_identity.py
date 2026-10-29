"""短期内部身份声明的确定性单元测试。"""

from __future__ import annotations

import json

import pytest

from app.internal_identity import (
    IdentityError,
    mint_identity,
    verify_identity,
)

SECRET = "unit-test-identity-secret"


def test_round_trip_binds_user_audience_and_jti() -> None:
    token = mint_identity(42, secret=SECRET, audience="learning-agent", now=1000)
    claims = verify_identity(token, secret=SECRET, audience="learning-agent", now=1001, max_ttl_seconds=60)
    assert claims.user_id == 42
    assert claims.audience == "learning-agent"
    assert claims.issued_at == 1000
    assert claims.expires_at == 1060
    assert claims.jti


def test_wrong_secret_audience_and_tampering_are_rejected() -> None:
    token = mint_identity(42, secret=SECRET, now=1000)
    with pytest.raises(IdentityError):
        verify_identity(token, secret="other-secret", now=1001)
    with pytest.raises(IdentityError):
        verify_identity(token, secret=SECRET, audience="other", now=1001)
    with pytest.raises(IdentityError):
        verify_identity(token[:-1] + ("A" if token[-1] != "A" else "B"), secret=SECRET, now=1001)


def test_expired_and_future_tokens_are_rejected() -> None:
    expired = mint_identity(42, secret=SECRET, now=1000, ttl_seconds=60)
    with pytest.raises(IdentityError):
        verify_identity(expired, secret=SECRET, now=1100, clock_skew_seconds=0)

    future = mint_identity(42, secret=SECRET, now=2000, ttl_seconds=60)
    with pytest.raises(IdentityError):
        verify_identity(future, secret=SECRET, now=1000, clock_skew_seconds=0)


def test_expired_delegation_claim_is_rejected_for_backend_audience() -> None:
    """MQ 任务里的委托声明过期后，不能再用它访问后端。"""
    delegation_secret = "unit-test-delegation-secret"
    token = mint_identity(
        42,
        secret=delegation_secret,
        audience="learning-backend",
        scope="analysis:read",
        now=1000,
        ttl_seconds=900,
    )
    # exp=1900，clock skew=5：now=1906 已超出宽限。
    with pytest.raises(IdentityError):
        verify_identity(
            token,
            secret=delegation_secret,
            audience="learning-backend",
            now=1906,
            max_ttl_seconds=3600,
            clock_skew_seconds=5,
        )


def test_clock_skew_tolerates_small_drift_only() -> None:
    token = mint_identity(42, secret=SECRET, now=1000, ttl_seconds=60)
    assert verify_identity(token, secret=SECRET, now=1063, clock_skew_seconds=5).user_id == 42
    with pytest.raises(IdentityError):
        verify_identity(token, secret=SECRET, now=1066, clock_skew_seconds=5)


def test_lifetime_above_maximum_is_rejected() -> None:
    token = mint_identity(42, secret=SECRET, now=1000, ttl_seconds=600)
    with pytest.raises(IdentityError):
        verify_identity(token, secret=SECRET, now=1001, max_ttl_seconds=300)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "v1.only-two-parts",
        "v2.abc.def",
        "v1.not-base64.signature",
        "v1.e30.signature",
    ],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(IdentityError):
        verify_identity(token, secret=SECRET, now=1000)


@pytest.mark.parametrize("subject", [0, -1, "not-a-number", True])
def test_invalid_subject_is_rejected(subject: object) -> None:
    import base64
    import hashlib
    import hmac

    payload = {
        "sub": subject,
        "aud": "learning-agent",
        "iat": 1000,
        "exp": 1060,
        "jti": "j",
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(body.encode()).rstrip(b"=").decode()
    signing_input = f"v1.{encoded}"
    signature = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    with pytest.raises(IdentityError):
        verify_identity(f"{signing_input}.{signature}", secret=SECRET, now=1001)


def test_issuer_is_required_and_must_match() -> None:
    token = mint_identity(42, secret=SECRET, now=1000, issuer="learning-backend")
    claims = verify_identity(
        token, secret=SECRET, issuer="learning-backend", now=1001
    )
    assert claims.issuer == "learning-backend"

    with pytest.raises(IdentityError):
        verify_identity(token, secret=SECRET, issuer="other-issuer", now=1001)

    # 缺少 iss 的 payload 即使签名正确也必须拒绝：iss 是信任边界的一部分。
    forged = _sign_payload(
        {
            "sub": 42,
            "aud": "learning-agent",
            "iat": 1000,
            "exp": 1060,
            "jti": "j",
        }
    )
    with pytest.raises(IdentityError):
        verify_identity(forged, secret=SECRET, now=1001)


def test_scope_is_preserved_and_validated() -> None:
    token = mint_identity(
        42,
        secret=SECRET,
        audience="learning-backend",
        scope="analysis:read",
        now=1000,
        ttl_seconds=900,
    )
    claims = verify_identity(
        token,
        secret=SECRET,
        audience="learning-backend",
        now=1001,
        max_ttl_seconds=3600,
    )
    assert claims.scope == "analysis:read"

    bad_scope = _sign_payload(
        {
            "iss": "learning-backend",
            "sub": 42,
            "aud": "learning-agent",
            "iat": 1000,
            "exp": 1060,
            "jti": "j",
            "scope": 123,
        }
    )
    with pytest.raises(IdentityError):
        verify_identity(bad_scope, secret=SECRET, now=1001)


def test_backend_audience_token_requires_the_delegation_key() -> None:
    """Agent 的 identity-secret 签不出后端接受的委托声明（密钥边界回归）。"""
    agent_secret = SECRET
    delegation_secret = "unit-test-delegation-secret"
    forged = mint_identity(
        42,
        secret=agent_secret,
        audience="learning-backend",
        scope="analysis:read",
        now=1000,
        ttl_seconds=900,
    )
    with pytest.raises(IdentityError):
        verify_identity(
            forged,
            secret=delegation_secret,
            audience="learning-backend",
            now=1001,
            max_ttl_seconds=3600,
        )

    genuine = mint_identity(
        42,
        secret=delegation_secret,
        audience="learning-backend",
        scope="analysis:read",
        now=1000,
        ttl_seconds=900,
    )
    assert (
        verify_identity(
            genuine,
            secret=delegation_secret,
            audience="learning-backend",
            now=1001,
            max_ttl_seconds=3600,
        ).user_id
        == 42
    )


def _sign_payload(payload: dict) -> str:
    import base64
    import hashlib
    import hmac

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(body.encode()).rstrip(b"=").decode()
    signing_input = f"v1.{encoded}"
    signature = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{signing_input}.{signature}"
