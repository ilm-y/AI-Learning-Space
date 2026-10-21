"""在线用例执行器：真后端 + 真 Python Agent。

与 offline 的分工：

- offline 证明「分析器拿到这段输入会输出什么」；
- live 证明「这些输出经过后端事务后，用户看到的状态确实变了」。

只有 live 能覆盖 Evidence 去重、Understanding Revision、Memory 作用域这类
**存在数据库里**的行为。它需要 MySQL + Spring Boot + Python Agent 同时在线，
所以默认不跑；`--live` 显式开启，环境不可用时整类标记 skipped 而不是 failed。

每个用例用独立用户隔离（用户名带时间戳 + case id），互不影响，也不清理数据 ——
与 scripts/regress-closed-loop.sh 的做法一致：只增不删，靠唯一用户名隔开。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .checks import CheckOutcome, evaluate
from .models import STATUS_FAILED, STATUS_PASSED, STATUS_SKIPPED, CaseResult, LiveCase

DEFAULT_TIMEOUT = 60.0
POLL_INTERVAL_SECONDS = 0.2


class LiveError(RuntimeError):
    """在线用例执行失败（HTTP 非 2xx / 响应不是约定结构 / 引用了未定义的变量）。"""


@dataclass
class LiveClient:
    backend: str
    agent: str
    timeout: float = DEFAULT_TIMEOUT
    _http: httpx.Client = field(init=False)
    _token: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(timeout=self.timeout, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    @property
    def token(self) -> str | None:
        return self._token

    # ---------- 通用 ----------

    def api(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        response = self._http.request(
            method, self.backend + path, json=json_body, params=params, headers=headers
        )
        return response.status_code, _as_json(response)

    def agent_post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        response = self._http.post(self.agent + path, json=body)
        return response.status_code, _as_json(response)

    def login(self, username: str, password: str) -> str:
        self.api("POST", "/api/auth/register",
                 json_body={"username": username, "password": password, "email": f"{username}@eval.local"})
        status, payload = self.api("POST", "/api/auth/login",
                                   json_body={"username": username, "password": password})
        token = (payload.get("data") or {}).get("token")
        if not token:
            raise LiveError(f"登录失败（HTTP {status}）：{payload}")
        self._token = token
        return token

    def stream_message(self, conversation_id: int, content: str) -> dict[str, Any]:
        """POST 一条消息并把 SSE 事件读完。

        返回的是**规范化后的事件摘要**，不是原始 SSE 字符串：用例只关心
        「首事件是不是 start / 有没有 end / 拼出来的回复是什么」。
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        events: list[str] = []
        deltas: list[str] = []
        event_name = ""
        end_payload: dict[str, Any] = {}
        error_payload: dict[str, Any] | None = None

        with self._http.stream(
            "POST",
            f"{self.backend}/api/conversations/{conversation_id}/messages",
            json={"content": content},
            headers={**headers, "Accept": "text/event-stream"},
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise LiveError(f"发送消息失败：HTTP {response.status_code}")
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                    events.append(event_name)
                elif line.startswith("data:"):
                    raw = line[len("data:"):].strip()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event_name == "message.delta":
                        deltas.append(str(data.get("delta", "")))
                    elif event_name == "message.end":
                        end_payload = data
                    elif event_name == "message.error":
                        error_payload = data

        return {
            "events": events,
            "delta": "".join(deltas),
            "ended": "message.end" in events,
            "started": bool(events) and events[0] == "message.start",
            "error": error_payload,
            "assistantMessageId": end_payload.get("messageId"),
        }


def run_live_cases(cases: list[LiveCase], *, backend: str, agent: str) -> list[CaseResult]:
    problem = _environment_problem(backend, agent)
    if problem is not None:
        return [
            CaseResult(
                case_id=case.id,
                category=case.category,
                mode="live",
                status=STATUS_SKIPPED,
                description=case.description,
                error=problem,
            )
            for case in cases
        ]

    client = LiveClient(backend=backend, agent=agent)
    try:
        return [_run_one(case, client) for case in cases]
    finally:
        client.close()


def _environment_problem(backend: str, agent: str) -> str | None:
    """只探测「端口后面有没有东西」，不要求任何接口返回 200。

    后端根路径 401/404 都说明进程活着；连不上才是环境问题。
    这样探测不会因为安全配置变化而误报。
    """
    for name, base in (("后端", backend), ("Agent", agent)):
        try:
            httpx.get(base + ("/actuator/health" if name == "后端" else "/health"), timeout=3)
        except httpx.HTTPError as exc:
            return f"{name} 不可达（{base}）：{type(exc).__name__}；启动服务后重试，或用 --offline 只跑进程内用例"
    return None


# ===================== 单个用例 =====================


def _run_one(case: LiveCase, client: LiveClient) -> CaseResult:
    started = time.perf_counter()
    result = CaseResult(
        case_id=case.id,
        category=case.category,
        mode="live",
        status=STATUS_FAILED,
        description=case.description,
    )
    try:
        context = _bootstrap(case, client)
        step_details: list[dict[str, Any]] = []
        for index, step in enumerate(case.steps, start=1):
            action = step.get("action", "")
            try:
                data = _perform(context, step)
            except Exception as exc:  # noqa: BLE001 —— 单步失败要落到报告，而不是中断整个套件
                result.checks.append(CheckOutcome(
                    path=f"step{index}/{action}",
                    op="action",
                    expected="执行成功",
                    actual=f"{type(exc).__name__}: {exc}",
                    passed=False,
                ))
                break

            step_details.append({"index": index, "action": action, "data": _brief(data)})
            # 期望值里也可以引用前面步骤产生的变量，例如 "${conversation.a.sessionId}"
            checks = evaluate(_template(step.get("expect", {}), context.variables), data)
            result.checks.extend(
                CheckOutcome(
                    path=f"step{index}/{action}: {check.path}",
                    op=check.op,
                    expected=check.expected,
                    actual=check.actual,
                    passed=check.passed,
                    message=check.message,
                )
                for check in checks
            )
            if any(not check.passed for check in checks):
                # 后续步骤通常依赖这一步建立的状态，继续跑只会产生大量连锁失败
                break
        result.detail = {"steps": step_details}
        if not result.checks:
            result.error = "用例没有任何断言 —— 请为至少一个 step 添加 expect"
            return result
        result.status = STATUS_PASSED if all(check.passed for check in result.checks) else STATUS_FAILED
        return result
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result.duration_ms = int((time.perf_counter() - started) * 1000)


@dataclass
class _Context:
    client: LiveClient
    variables: dict[str, Any]
    space_id: int
    concepts: dict[str, int]
    conversations: dict[str, int] = field(default_factory=dict)
    last_user_message: dict[str, int] = field(default_factory=dict)


def _bootstrap(case: LiveCase, client: LiveClient) -> _Context:
    """注册独立用户 → 建空间 → 建知识点。失败直接抛，报告里能看到是哪一步。"""
    suffix = hashlib.sha1(case.id.encode("utf-8")).hexdigest()[:8]
    username = f"eval{int(time.time())}{suffix}"[:40]
    client.login(username, "pass123456")

    space_setup = case.setup.get("space", {})
    status, payload = client.api("POST", "/api/spaces", json_body={
        "name": _template(space_setup.get("name", f"评测空间-{case.id}"), {}),
        "description": "evaluation harness",
    })
    space = _data_or_raise(status, payload, "创建空间")
    space_id = int(space["id"])

    concepts: dict[str, int] = {}
    for item in case.setup.get("concepts", []):
        key = item["key"]
        status, payload = client.api("POST", f"/api/spaces/{space_id}/concepts", json_body={
            "canonicalName": item["canonicalName"],
            "displayName": item.get("displayName", item["canonicalName"]),
            "description": item.get("description", ""),
        })
        concepts[key] = int(_data_or_raise(status, payload, f"创建知识点 {key}")["id"])

    variables = {
        "user": {"username": username},
        "space": {"id": space_id, "name": space_setup.get("name", "")},
        "concept": {key: {"id": value, "name": key} for key, value in concepts.items()},
        "conversation": {},
    }
    return _Context(client=client, variables=variables, space_id=space_id, concepts=concepts)


# ===================== 动作 =====================


def _perform(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    action = step.get("action")
    handler = _ACTIONS.get(action)
    if handler is None:
        raise LiveError(f"未知 action：{action!r}")
    return handler(context, {key: _template(value, context.variables) for key, value in step.items()})


def _send_message(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    key = step.get("conversation", "default")
    conversation_id = _conversation(context, key)
    stream = context.client.stream_message(conversation_id, step["content"])
    context.last_user_message[key] = _last_user_message_id(context, conversation_id)
    # sessionId 是跨对话归属的观测点：下一条用例可以用
    # "${conversation.a.sessionId}" 断言两个对话归入同一个学习活动。
    session_id = _conversation_session(context, conversation_id)
    context.variables.setdefault("conversation", {}).setdefault(key, {})["sessionId"] = session_id
    return {"data": {**stream, "conversationId": conversation_id,
                     "userMessageId": context.last_user_message[key], "sessionId": session_id}}


def _highlight(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    key = step.get("conversation", "default")
    message_id = step.get("messageId") or context.last_user_message.get(key)
    if message_id is None:
        raise LiveError("highlight 之前必须先 sendMessage（或显式给 messageId）")
    status, payload = context.client.api("POST", f"/api/messages/{message_id}/highlights", json_body={
        "markType": step["markType"],
        "selectedText": step.get("selectedText", ""),
        "startOffset": step.get("startOffset", 0),
        "endOffset": step.get("endOffset", 0),
        "note": step.get("note"),
    })
    return {"httpStatus": status, "data": _data_or_raise(status, payload, "创建批注")}


def _context_preview(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    conversation_id = _conversation(context, step.get("conversation", "default"))
    body = {
        "runId": 1,
        "userId": 1,
        "conversationId": conversation_id,
        "learningSpaceId": context.space_id,
        "userMessage": step["content"],
        "authToken": context.client.token,
    }
    status, payload = context.client.agent_post("/agent/context/preview", body)
    data = _data_or_raise(status, payload, "组装上下文")
    sections = data.get("context", {}).get("sections", [])
    names = [item.get("name", "") for item in sections]
    understanding_previews = [
        item.get("preview", "") for item in sections if item.get("name", "").startswith("understanding:")
    ]
    return {"httpStatus": status, "data": {
        "sections": names,
        "hasUnderstanding": any(name.startswith("understanding:") for name in names),
        "hasSession": "session" in names,
        "understandingPreview": "\n".join(understanding_previews),
        "systemInstruction": data.get("context", {}).get("systemInstruction", ""),
        "warnings": data.get("context", {}).get("warnings", []),
        "retriever": data.get("retriever"),
        "degraded": data.get("degraded", False),
    }}


def _get_understanding(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    concept_id = _concept(context, step["concept"])
    status, payload = context.client.api("GET", f"/api/understandings/{concept_id}")
    return {"httpStatus": status, "data": _data_or_raise(status, payload, "查询理解")}


def _wait_for_understanding(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    """轮询到条件满足或超时。

    对话链路在 `emitter.complete()` 之后才做分析（不占用户的等待时间），
    所以读到 message.end 不代表 Evidence 已经写完。轮询比固定 sleep 稳，
    也让「分析慢了」和「分析根本没发生」在结果上可区分（timedOut）。
    """
    concept_id = _concept(context, step["concept"])
    until = step.get("until", {})
    timeout = float(step.get("timeoutMs", 8000)) / 1000
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    last_status = 0
    while True:
        last_status, payload = context.client.api("GET", f"/api/understandings/{concept_id}")
        candidate = payload.get("data")
        if candidate:
            last = candidate
            if _understanding_matches(candidate, until):
                break
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    return {
        "httpStatus": last_status,
        "data": last,
        "timedOut": last is None or not _understanding_matches(last, until),
    }


def _get_revisions(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    concept_id = _concept(context, step["concept"])
    status, payload = context.client.api("GET", f"/api/understandings/{concept_id}/revisions")
    items = _data_or_raise(status, payload, "查询 Revision") or []
    statuses = [item.get("status") for item in items]
    return {
        "httpStatus": status,
        "data": {
            "count": len(items),
            "statuses": statuses,
            # 逐状态布尔值，避免同一 expect 对象里重复 key
            "hasExposed": "EXPOSED" in statuses,
            "hasPartial": "PARTIAL" in statuses,
            "hasDemonstrated": "DEMONSTRATED" in statuses,
            "hasMisconception": "MISCONCEPTION" in statuses,
            "revisionNos": [item.get("revisionNo") for item in items],
            "items": items,
        },
    }


def _get_learning_state(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    concept_id = _concept(context, step["concept"])
    status, payload = context.client.api("GET", f"/api/learning-state/concepts/{concept_id}")
    return {"httpStatus": status, "data": _data_or_raise(status, payload, "查询学习状态")}


def _create_memory(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    # step 已经整体做过模板替换（_perform 里），这里不要再替换一遍。
    status, payload = context.client.api("POST", "/api/memories", json_body=step.get("body", {}))
    return {"httpStatus": status, "code": payload.get("code"), "data": payload.get("data"),
            "message": payload.get("message")}


def _search_memories(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if step.get("space"):
        params["spaceId"] = context.space_id
    elif "spaceId" in step:
        # 显式给一个别的空间，用于验证「非 GLOBAL 记忆不会泄漏」
        params["spaceId"] = step["spaceId"]
    if step.get("concept"):
        params["conceptId"] = _concept(context, step["concept"])
    status, payload = context.client.api("GET", "/api/memories/search", params=params)
    items = _data_or_raise(status, payload, "检索记忆") or []
    scopes = [item.get("scopeType") for item in items]
    return {"httpStatus": status, "data": {
        "count": len(items),
        "scopes": scopes,
        # 逐作用域的布尔值：同一个期望对象里不能重复写 "data.scopes" 这个 key
        "hasGlobal": "GLOBAL" in scopes,
        "hasSpace": "SPACE" in scopes,
        "hasConcept": "CONCEPT" in scopes,
        "keys": [item.get("memoryKey") for item in items],
        "statuses": [item.get("status") for item in items],
        "contents": [item.get("content") for item in items],
        "items": items,
    }}


def _pending_tests(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    status, payload = context.client.api("GET", "/api/tests/pending")
    items = _data_or_raise(status, payload, "查询待答测试") or []
    return {"httpStatus": status, "data": {"count": len(items), "items": items}}


def _answer_test(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    status, payload = context.client.api(
        "POST", f"/api/tests/{step['testId']}/answer", json_body={"userAnswer": step.get("answer", "")}
    )
    return {"httpStatus": status, "data": _data_or_raise(status, payload, "提交答案")}


def _sleep(context: _Context, step: dict[str, Any]) -> dict[str, Any]:
    time.sleep(float(step.get("ms", 500)) / 1000)
    return {"data": {"sleptMs": step.get("ms", 500)}}


_ACTIONS = {
    "sendMessage": _send_message,
    "highlight": _highlight,
    "contextPreview": _context_preview,
    "getUnderstanding": _get_understanding,
    "waitForUnderstanding": _wait_for_understanding,
    "getRevisions": _get_revisions,
    "getLearningState": _get_learning_state,
    "createMemory": _create_memory,
    "searchMemories": _search_memories,
    "pendingTests": _pending_tests,
    "answerTest": _answer_test,
    "sleep": _sleep,
}


# ===================== 工具 =====================


def _conversation(context: _Context, key: str) -> int:
    if key not in context.conversations:
        status, payload = context.client.api(
            "POST", f"/api/spaces/{context.space_id}/conversations", json_body={}
        )
        conversation = _data_or_raise(status, payload, "创建对话")
        context.conversations[key] = int(conversation["id"])
        context.variables.setdefault("conversation", {})[key] = {"id": conversation["id"]}
    return context.conversations[key]


def _concept(context: _Context, key: str) -> int:
    if key not in context.concepts:
        raise LiveError(f"未定义的知识点 key：{key!r}（setup.concepts 里没有）")
    return context.concepts[key]


def _conversation_session(context: _Context, conversation_id: int) -> int | None:
    status, payload = context.client.api("GET", f"/api/conversations/{conversation_id}")
    conversation = _data_or_raise(status, payload, "读取对话") or {}
    return conversation.get("sessionId")


def _last_user_message_id(context: _Context, conversation_id: int) -> int | None:
    status, payload = context.client.api("GET", f"/api/conversations/{conversation_id}/messages")
    messages = _data_or_raise(status, payload, "读取消息列表") or []
    user_messages = [item for item in messages if item.get("role") == "USER"]
    return int(user_messages[-1]["id"]) if user_messages else None


def _understanding_matches(understanding: dict[str, Any], until: dict[str, Any]) -> bool:
    """`waitForUnderstanding` 的终止条件。

    只支持理解状态里真正稳定的几个维度；更复杂的断言交给后续 step 的 checks。
    """
    if not until:
        return True
    status = understanding.get("status")
    if "status" in until and status != until["status"]:
        return False
    if "statusIn" in until and status not in until["statusIn"]:
        return False
    if until.get("knownPartsNonEmpty") and not understanding.get("knownParts"):
        return False
    if until.get("misconceptionsNonEmpty") and not understanding.get("misconceptions"):
        return False
    contains = until.get("latestUserExplanationContains")
    if contains and contains not in (understanding.get("latestUserExplanation") or ""):
        return False
    return True


def _template(value: Any, variables: dict[str, Any]) -> Any:
    """把 `${concept.tlb.id}` 换成实际 id。

    整串就是一个引用时保留原类型（int），方便直接当 conceptId 用；
    嵌入在更长字符串里时做字符串替换。
    """
    if isinstance(value, dict):
        return {key: _template(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_template(item, variables) for item in value]
    if isinstance(value, str):
        if value.startswith("${") and value.endswith("}") and value.count("${") == 1:
            return _lookup(variables, value[2:-1])
        return _interpolate(value, variables)
    return value


def _interpolate(text: str, variables: dict[str, Any]) -> str:
    for _ in range(10):  # 防止模板里再套模板造成死循环
        start = text.find("${")
        end = text.find("}", start + 2) if start >= 0 else -1
        if start < 0 or end < 0:
            return text
        name = text[start + 2:end]
        replacement = _lookup(variables, name)
        text = text[:start] + str(replacement) + text[end + 1:]
    return text


def _lookup(variables: dict[str, Any], path: str) -> Any:
    current: Any = variables
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise LiveError(f"未定义的变量：${{{path}}}")
        current = current[segment]
    return current


def _as_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"raw": response.text[:500]}
    return payload if isinstance(payload, dict) else {"data": payload}


def _data_or_raise(status: int, payload: dict[str, Any], action: str) -> Any:
    if status >= 400 or payload.get("code") not in (0, None):
        raise LiveError(f"{action}失败（HTTP {status}, code={payload.get('code')}）：{payload.get('message')}")
    return payload.get("data")


def _brief(value: Any, limit: int = 800) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= limit else {"truncated": text[:limit] + "…"}
