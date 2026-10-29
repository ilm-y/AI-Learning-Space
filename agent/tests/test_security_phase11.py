"""Phase 1.1 B07：Agent HTTP 路由的真实鉴权与身份绑定测试。

这些用例直接请求 `app.main.app`（FastAPI 实际 ASGI 应用），不是检查路由装饰器
或函数签名。跨用户部分用一个本地 HTTP stub 模拟 Spring Boot 只读接口，断言
Agent 发给后端的 `X-Internal-User-Id` 与请求体里的 `userId` 一致，且不匹配时
压根到不了检索层。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.internal_identity import (
    IDENTITY_AUDIENCE,
    mint_identity,
    verify_identity,
)

TOKEN = "phase11-test-internal-token"
IDENTITY_SECRET = "phase11-test-identity-secret"
# 只在本测试里模拟 Spring 的委托密钥。Agent 进程配置中**没有**这一项；
# 生产环境它只存在于 Spring，Agent 只能原样转发签好的声明。
DELEGATION_SECRET = "phase11-test-delegation-secret"
BACKEND_AUDIENCE = "learning-backend"
DEMO_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "demo_space.json"

CHAT_BODY = {
    "runId": 1,
    "traceId": "phase11-security",
    "userId": 9001,
    "conversationId": 9401,
    "learningSpaceId": 9101,
    "userMessage": "TLB 是什么？",
}
ANALYZE_BODY = {
    "runId": 2,
    "traceId": "phase11-security",
    "userId": 9001,
    "conversationId": 9401,
    "learningSpaceId": 9101,
    "messageId": 1,
    "userMessage": "TLB 是什么？",
    "assistantMessage": "TLB 是页表项的高速缓存。",
}
PREVIEW_BODY = dict(CHAT_BODY)


def _set_base_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        "AILS_INTERNAL_TOKEN": TOKEN,
        "AILS_INTERNAL_IDENTITY_SECRET": IDENTITY_SECRET,
        "AILS_INTERNAL_IDENTITY_AUDIENCE": IDENTITY_AUDIENCE,
        "AILS_INTERNAL_IDENTITY_ISSUER": "learning-backend",
        "AILS_LLM_PROVIDER": "mock",
        "AILS_RETRIEVER_MODE": "fixture",
        "AILS_FIXTURE_PATH": str(DEMO_FIXTURE),
        "AILS_EMBEDDING_PROVIDER": "hash",
        "AILS_EMBEDDING_DIMENSION": "8",
        # 不让测试意外连接开发机上的 Qdrant；知识检索不会在 fixture 模式触发。
        "AILS_QDRANT_URL": "http://127.0.0.1:1",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _client(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> TestClient:
    _set_base_env(monkeypatch, **overrides)
    return TestClient(main_module.app)


def _identity(
    user_id: int,
    *,
    secret: str = IDENTITY_SECRET,
    audience: str = IDENTITY_AUDIENCE,
    now: int | None = None,
    ttl_seconds: int = 60,
) -> str:
    return mint_identity(
        user_id,
        secret=secret,
        audience=audience,
        ttl_seconds=ttl_seconds,
        now=now,
    )


def _delegation(user_id: int, *, ttl_seconds: int = 900) -> str:
    """模拟 Spring 放进同步请求 / MQ 任务的委托声明。"""
    return mint_identity(
        user_id,
        secret=DELEGATION_SECRET,
        audience=BACKEND_AUDIENCE,
        issuer="learning-backend",
        scope="analysis:read",
        ttl_seconds=ttl_seconds,
    )


def _headers(
    user_id: int | str | None = 9001,
    token: str | None = TOKEN,
    identity: str | None = "auto",
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["X-Internal-Token"] = token
    if user_id is not None:
        headers["X-Internal-User-Id"] = str(user_id)
        if identity == "auto":
            identity = _identity(int(user_id))
    if identity not in (None, "auto"):
        headers["X-Internal-Identity"] = identity
    return headers


# ---------- 无凭据 / 错误凭据 ----------


@pytest.mark.parametrize(
    "path,body",
    [
        ("/agent/chat", CHAT_BODY),
        ("/agent/analyze", ANALYZE_BODY),
        ("/agent/context/preview", PREVIEW_BODY),
        ("/health/diagnostics", None),
    ],
)
def test_sensitive_routes_reject_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict | None
) -> None:
    with _client(monkeypatch) as client:
        response = client.post(path, json=body) if body is not None else client.get(path)

    assert response.status_code == 401, (path, response.status_code, response.text)


@pytest.mark.parametrize(
    "path,body",
    [
        ("/agent/chat", CHAT_BODY),
        ("/agent/analyze", ANALYZE_BODY),
        ("/agent/context/preview", PREVIEW_BODY),
        ("/health/diagnostics", None),
    ],
)
def test_sensitive_routes_reject_wrong_token(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict | None
) -> None:
    with _client(monkeypatch) as client:
        if body is None:
            response = client.get(path, headers=_headers(token="wrong-token"))
        else:
            response = client.post(path, json=body, headers=_headers(token="wrong-token"))

    assert response.status_code == 401, (path, response.status_code, response.text)


def test_missing_internal_user_header_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as client:
        response = client.post("/agent/chat", json=CHAT_BODY, headers=_headers(user_id=None))

    assert response.status_code == 401


def test_missing_signed_identity_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只有共享 token + 普通 userId Header：旧实现会接受，现在必须 fail closed。
    with _client(monkeypatch) as client:
        response = client.post("/agent/chat", json=CHAT_BODY, headers=_headers(identity=None))

    assert response.status_code == 401
    assert response.json()["detail"] == "内部身份声明无效"


def test_tampered_signed_identity_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _identity(9001)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with _client(monkeypatch) as client:
        response = client.post(
            "/agent/chat", json=CHAT_BODY, headers=_headers(identity=tampered)
        )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "identity",
    [
        # 已过期
        _identity(9001, now=1_600_000_000, ttl_seconds=60),
        # 接收方不是 Agent
        _identity(9001, audience="some-other-service"),
        # 用错误密钥签发
        _identity(9001, secret="attacker-secret"),
    ],
)
def test_expired_wrong_audience_or_wrong_key_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/agent/chat", json=CHAT_BODY, headers=_headers(identity=identity)
        )

    assert response.status_code == 401


def test_identity_and_plain_user_header_must_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/agent/chat",
            json=CHAT_BODY,
            headers=_headers(user_id=9002, identity=_identity(9001)),
        )

    assert response.status_code == 403


def test_document_endpoints_stay_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as client:
        response = client.post(
            "/agent/documents/parse",
            data={"documentId": "1", "learningSpaceId": "9101", "userId": "9001"},
            files={"file": ("notes.md", b"# T\n\nhello", "text/markdown")},
        )

    assert response.status_code == 401


# ---------- 身份绑定：请求体 userId 不能越过凭据身份 ----------


@pytest.mark.parametrize("path", ["/agent/chat", "/agent/analyze", "/agent/context/preview"])
def test_body_user_id_cannot_override_internal_identity(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    body = dict(CHAT_BODY if path != "/agent/analyze" else ANALYZE_BODY)
    body["userId"] = 9002

    with _client(monkeypatch) as client:
        response = client.post(path, json=body, headers=_headers(user_id=9001))

    assert response.status_code == 403, (path, response.status_code, response.text)
    assert response.json()["detail"].startswith("请求 userId 与内部身份不一致")


def test_correct_credential_reaches_chat_and_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as client:
        chat = client.post("/agent/chat", json=CHAT_BODY, headers=_headers())
        preview = client.post("/agent/context/preview", json=PREVIEW_BODY, headers=_headers())

    assert chat.status_code == 200
    assert "text/event-stream" in chat.headers["content-type"]
    assert "event: done" in chat.text

    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["summary"]["userId"] == 9001
    assert data["retriever"].startswith("fixture:")


def test_analyze_without_delegation_token_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有共享 token 和入站签名身份，但没有 Spring 的后端委托声明。

    旧实现会用 Agent 自己的 identity-secret 现签一份后端身份；现在必须直接失败，
    既不访问后端，也不退回 Fixture。
    """
    with _client(
        monkeypatch,
        AILS_RETRIEVER_MODE="http",
        AILS_BACKEND_BASE_URL="http://127.0.0.1:1",
    ) as client:
        response = client.post("/agent/analyze", json=ANALYZE_BODY, headers=_headers())

    assert response.status_code == 503, response.text
    assert "backendIdentity" in response.text


def test_correct_credential_reaches_analyze_but_fixture_guard_blocks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """鉴权通过后，analyze 仍必须被 fixture 保护拦住，而不是把样例分析当真实结果。"""
    with _client(monkeypatch) as client:
        response = client.post("/agent/analyze", json=ANALYZE_BODY, headers=_headers())

    assert response.status_code == 503
    assert response.json()["code"] == 503


# ---------- /health 拆分：安全存活探针 vs 受保护诊断 ----------


def test_health_is_safe_liveness_only(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_diagnostics_requires_internal_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(monkeypatch) as client:
        response = client.get("/health/diagnostics", headers=_headers())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["llm_provider"] == "mock"
    assert data["internal_auth_configured"] is True


# ---------- 跨用户数据访问：真实 HTTP stub 证明身份绑定 ----------


class _StubBackend:
    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                # 后端过滤器现在只从签名声明取身份；stub 用同一套校验证明
                # Agent 发出的 aud 与 sub 正确，而不是只看明文 Header。
                try:
                    claims = verify_identity(
                        self.headers.get("X-Internal-Identity"),
                        secret=DELEGATION_SECRET,
                        audience=BACKEND_AUDIENCE,
                        issuer="learning-backend",
                        max_ttl_seconds=3600,
                    )
                    if claims.scope != "analysis:read":
                        raise ValueError("scope 不匹配")
                    user_id = claims.user_id
                except Exception:  # noqa: BLE001 —— stub 只回 401，不模拟业务
                    self.send_response(401)
                    self.end_headers()
                    return
                declared = int(self.headers.get("X-Internal-User-Id", "0") or 0)
                if declared != user_id:
                    self.send_response(403)
                    self.end_headers()
                    return
                stub.requests.append((parsed.path, user_id))
                data = stub._data(parsed.path, user_id, parse_qs(parsed.query))
                payload = json.dumps({"code": 0, "data": data}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "_StubBackend":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _data(self, path: str, user_id: int, query: dict) -> object:
        if path.startswith("/api/spaces/") and path.endswith("/concepts"):
            if user_id == 9001:
                return [{"id": 9201, "canonicalName": "tlb", "displayName": "TLB"}]
            if user_id == 9002:
                return [{"id": 9301, "canonicalName": "tcp", "displayName": "TCP"}]
            return []
        if path == "/api/understandings/9201":
            return (
                {
                    "id": 1201,
                    "conceptId": 9201,
                    "conceptName": "TLB",
                    "summary": "Alice 的 TLB 记录",
                    "status": "MISCONCEPTION",
                }
                if user_id == 9001
                else None
            )
        if path == "/api/understandings/9301":
            return (
                {
                    "id": 1211,
                    "conceptId": 9301,
                    "conceptName": "TCP",
                    "summary": "Bob 的 TCP 记录",
                    "status": "MISCONCEPTION",
                }
                if user_id == 9002
                else None
            )
        if path.startswith("/api/memories/search") or path.endswith("/evidence") or path.endswith("/revisions"):
            return []
        if path.endswith("/documents/ready-ids"):
            return []
        if path.startswith("/api/evidence/"):
            return None
        return None


def test_cross_user_preview_uses_bound_internal_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    with _StubBackend() as stub:
        with _client(
            monkeypatch,
            AILS_RETRIEVER_MODE="http",
            AILS_BACKEND_BASE_URL=stub.base_url,
        ) as client:
            alice = client.post(
                "/agent/context/preview",
                json={
                    **PREVIEW_BODY,
                    "userId": 9001,
                    "userMessage": "TLB 是什么？",
                    "backendIdentity": _delegation(9001),
                },
                headers=_headers(user_id=9001),
            )
            bob = client.post(
                "/agent/context/preview",
                json={
                    **PREVIEW_BODY,
                    "userId": 9002,
                    "learningSpaceId": 9102,
                    "userMessage": "TCP 是什么？",
                    "backendIdentity": _delegation(9002),
                },
                headers=_headers(user_id=9002),
            )

        assert alice.status_code == 200, alice.text
        assert bob.status_code == 200, bob.text
        alice_text = alice.json()["data"]["context"]["systemInstruction"]
        bob_text = bob.json()["data"]["context"]["systemInstruction"]
        assert "Alice 的 TLB 记录" in alice_text
        assert "Bob 的 TCP 记录" not in alice_text
        assert "Bob 的 TCP 记录" in bob_text
        assert "Alice 的 TLB 记录" not in bob_text

        bound_users = {user for _, user in stub.requests}
        assert bound_users == {9001, 9002}


def test_agent_identity_secret_cannot_authorize_backend_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent 用自己的 identity-secret 签 aud=learning-backend 也过不了后端。

    旧路由就是在这里自签后端身份。现在请求体里的 backendIdentity 必须来自
    Spring 的 delegation-secret；拿 Agent 密钥伪造的去后端只会拿到 401，
    Agent 不降级、不落数据。
    """
    forged = mint_identity(
        9001,
        secret=IDENTITY_SECRET,
        audience=BACKEND_AUDIENCE,
        issuer="learning-backend",
        scope="analysis:read",
    )
    with _StubBackend() as stub:
        with _client(
            monkeypatch,
            AILS_RETRIEVER_MODE="http",
            AILS_BACKEND_BASE_URL=stub.base_url,
        ) as client:
            response = client.post(
                "/agent/context/preview",
                json={**PREVIEW_BODY, "backendIdentity": forged},
                headers=_headers(),
            )

        assert response.status_code >= 500, response.text
        assert stub.requests == []


def test_body_user_id_mismatch_never_reaches_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    with _StubBackend() as stub:
        with _client(
            monkeypatch,
            AILS_RETRIEVER_MODE="http",
            AILS_BACKEND_BASE_URL=stub.base_url,
        ) as client:
            response = client.post(
                "/agent/context/preview",
                json={**PREVIEW_BODY, "userId": 9002},
                headers=_headers(user_id=9001),
            )

        assert response.status_code == 403
        assert stub.requests == []


def test_invalid_identity_never_reaches_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    with _StubBackend() as stub:
        with _client(
            monkeypatch,
            AILS_RETRIEVER_MODE="http",
            AILS_BACKEND_BASE_URL=stub.base_url,
        ) as client:
            response = client.post(
                "/agent/context/preview",
                json=PREVIEW_BODY,
                headers=_headers(identity=_identity(9001, audience="wrong-audience")),
            )

        assert response.status_code == 401
        assert stub.requests == []
