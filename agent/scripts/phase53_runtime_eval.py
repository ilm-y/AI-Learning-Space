"""阶段 5.3.2：通过真实 `AgentRuntime.generate()` 做生产链路复验。

与 `phase53_real_model_eval.py` 的区别：
- 这里**不直接调用** `/chat/completions`，而是用真实 LLM Adapter 装配 Runtime，
  走 `prepare() → generate()` 的正常路径；
- 5.3.2 起显式复盘回答会先完整生成、做证据校验，可能触发**一次受限重生成**，
  因此 RecordingAdapter 记录**每一次模型调用**：
  * `modelAttempts`：每次调用的原始文本与错误；
  * `rawAnswer`：第一次调用的模型原始输出；
  * `finalAnswer`：应用最终输出（重生成结果或确定性回退）；
  * `historyGuard`：校验发现的问题、是否重生成、是否回退；
  * `firstDeltaMs`：用户可见首字延迟（复盘缓冲后才会出现）。
- `generate()` 末尾的 evidence guard 仍真实执行；非复盘问题还会给出
  `guardNotice`（知识状态兜底）。
- C 的 Current-only 仍用同一个 ContextBuilder 去掉 trajectory 后重新组装，
  再交给同一个 `generate()`，保证两种模式只有轨迹输入不同。

用法（模型配置从本地环境注入，不写 .env）：
    AILS_LLM_PROVIDER=openai \
    AILS_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
    AILS_LLM_API_KEY=<本地注入> \
    AILS_LLM_MODEL=qwen-plus \
    python agent/scripts/phase53_runtime_eval.py <summary.json> <summary-clean.json>

可选环境变量：
    AILS_RUNTIME_CASES=B,C,C-only     # 默认；5.3.2 不改 Memory/Context 数据逻辑，
                                      # 因此不复测 D，A 也不在默认集合里
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT))

from app.config import Settings  # noqa: E402
from app.context.builder import ContextBuilder  # noqa: E402
from app.context.tokens import estimate_tokens  # noqa: E402
from app.embedding import build_embedding_provider  # noqa: E402
from app.llm.base import ChatMessage, LLMAdapter  # noqa: E402
from app.llm.factory import build_llm_adapter  # noqa: E402
from app.llm.mock import MockLLMAdapter  # noqa: E402
from app.retriever.knowledge import KnowledgeSearchService  # noqa: E402
from app.runtime import AgentRuntime, PreparedRun  # noqa: E402
from app.schemas import ChatRequest  # noqa: E402
from app.vectorstore.qdrant_store import QdrantDocumentStore  # noqa: E402

BACKEND = os.environ.get("AILS_BACKEND_BASE_URL", "http://localhost:8080").rstrip("/")
DEFAULT_CASES = "B,C,C-only"


class PlainMockAdapter:
    """离线验证 guard 用的哑模型：只给一段普通回答，不回读系统提示。

    `MockLLMAdapter` 会把状态区块原文读回来，导致 `already_discloses()` 误以为
    模型已经主动说明；这个适配器用来验证「模型完全忽略知识状态」时生产兜底是否生效。
    """

    @property
    def name(self) -> str:
        return "plain-mock"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        for chunk in ("TLB 是页表项的高速缓存。", "它缓存最近用过的页表项，", "不是整个页表。"):
            yield chunk

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return "TLB 是页表项的高速缓存。它缓存最近用过的页表项，不是整个页表。"

    async def aclose(self) -> None:
        return None


class RecordingLLMAdapter:
    """把真实适配器的流式输出原样转出，同时保留每一次调用的原文。

    复盘校验可能触发一次受限重生成，所以不能只保留「最后一次」：
    `attempts` 是模型原始行为的证据，`raw` 仍指第一次调用。
    """

    def __init__(self, inner: LLMAdapter) -> None:
        self._inner = inner
        self.chunks: list[str] = []
        self.error: str | None = None
        self.attempts: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def last_status_code(self) -> int | None:
        return getattr(self._inner, "last_status_code", None)

    @property
    def raw(self) -> str:
        return "".join(self.chunks)

    def begin(self) -> None:
        """每个评测场景开始时清空，避免上一场景的调用混进本场景。"""
        self.chunks = []
        self.error = None
        self.attempts = []

    def _begin_attempt(self, temperature: float | None) -> dict[str, Any]:
        self.chunks = []
        self.error = None
        record: dict[str, Any] = {
            "chunks": self.chunks,
            "error": None,
            "temperature": temperature,
        }
        self.attempts.append(record)
        return record

    def attempts_record(self) -> list[dict[str, Any]]:
        return [
            {
                "raw": "".join(attempt["chunks"]),
                "chars": len("".join(attempt["chunks"])),
                "error": attempt["error"],
                "temperature": attempt["temperature"],
                "tokensEstimated": estimate_tokens("".join(attempt["chunks"])),
            }
            for attempt in self.attempts
        ]

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        record = self._begin_attempt(temperature)
        try:
            async for delta in self._inner.stream_chat(
                messages, temperature=temperature, max_tokens=max_tokens
            ):
                self.chunks.append(delta)
                yield delta
        except Exception as exc:  # noqa: BLE001 —— 评测要记录失败类别，不在这里重试
            self.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            record["error"] = self.error
            raise

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return await self._inner.complete(
            messages, temperature=temperature, max_tokens=max_tokens
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def load_summary(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


async def login(client: httpx.AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        f"{BACKEND}/api/auth/login",
        json={"username": username, "password": password},
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"登录失败：{username}")
    return body["data"]["token"]


async def create_conversation(client: httpx.AsyncClient, token: str, space_id: int, title: str) -> int:
    response = await client.post(
        f"{BACKEND}/api/spaces/{space_id}/conversations",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": title},
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"创建会话失败：{body}")
    return int(body["data"]["id"])


async def build_runtime(settings: Settings, llm: LLMAdapter):
    embedding = build_embedding_provider(settings)
    store = QdrantDocumentStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        collection_prefix=settings.qdrant_collection_prefix,
        upsert_batch_size=settings.qdrant_upsert_batch_size,
    )
    knowledge = KnowledgeSearchService(
        provider=embedding,
        store=store,
        collection_prefix=settings.qdrant_collection_prefix,
        timeout_seconds=settings.knowledge_timeout_seconds,
    )
    return AgentRuntime(settings, llm, knowledge_search=knowledge), embedding, store


async def build_context(runtime: AgentRuntime, *, token: str, user_id: int,
                        conversation_id: int, space_id: int,
                        question: str, run_id: int) -> PreparedRun:
    request = ChatRequest(
        run_id=run_id,
        user_id=user_id,
        conversation_id=conversation_id,
        learning_space_id=space_id,
        user_message=question,
        history=[],
        session=None,
        auth_token=token,
    )
    prepared = await runtime.prepare(request)
    if prepared.degraded:
        raise RuntimeError("检索降级到 Fixture，拒绝把样例数据用于真实模型验收")
    return prepared


def current_only(prepared: PreparedRun, settings: Settings) -> PreparedRun:
    """同一个 state 去掉轨迹与轨迹关联证据后重新组装，其他输入不变。"""
    state = copy.copy(prepared.state)
    state.trajectories = []
    state.evidences = []
    state.context = None
    builder = ContextBuilder(
        max_context_tokens=settings.max_context_tokens,
        recent_history_limit=settings.recent_history_limit,
    )
    return PreparedRun(
        state=state,
        context=builder.build(state),
        retriever_name=prepared.retriever_name,
        degraded=prepared.degraded,
    )


def context_record(context) -> dict[str, Any]:
    return {
        "estimatedTokens": context.estimated_tokens,
        "droppedSections": context.dropped_sections,
        "warnings": context.warnings,
        "systemInstruction": context.system_instruction,
        "messages": [message.as_dict() for message in context.messages],
        "sections": [
            {
                "name": section.name,
                "priority": section.priority,
                "sourceIds": section.source_ids,
                "estimatedTokens": section.estimated_tokens,
                "truncated": section.truncated,
                "dropped": section.dropped,
                "content": section.content,
            }
            for section in context.sections
        ],
    }


def compare_contexts(full, only) -> dict[str, Any]:
    full_record = context_record(full)
    only_record = context_record(only)
    full_base = [s for s in full_record["sections"] if not s["name"].startswith("trajectory:")]
    only_base = only_record["sections"]
    full_dialogue = [m for m in full_record["messages"] if m["role"] != "system"]
    only_dialogue = [m for m in only_record["messages"] if m["role"] != "system"]
    return {
        "trajectorySections": [
            s["name"] for s in full_record["sections"] if s["name"].startswith("trajectory:")
        ],
        "nonTrajectoryIdentical": [(a["name"], a["content"]) for a in full_base]
        == [(b["name"], b["content"]) for b in only_base],
        "fullSectionNames": [s["name"] for s in full_record["sections"]],
        "currentOnlySectionNames": [s["name"] for s in only_record["sections"]],
        "dialogueMessagesIdentical": full_dialogue == only_dialogue,
        "systemInstructionDiffers": full_record["systemInstruction"]
        != only_record["systemInstruction"],
        "tokenDelta": full_record["estimatedTokens"] - only_record["estimatedTokens"],
    }


def trajectory_facts(prepared: PreparedRun) -> list[dict[str, Any]]:
    """评测产物的可追溯映射：用户可见文本不再带内部 id，但这里保留完整字段。"""
    return [
        {
            "conceptId": trajectory.concept_id,
            "currentStatus": trajectory.current_status.value,
            "revisions": [
                revision.model_dump(mode="json") for revision in trajectory.revisions
            ],
            "evidences": [
                evidence.model_dump(mode="json") for evidence in trajectory.evidences
            ],
        }
        for trajectory in prepared.state.trajectories
    ]


def _trace_event(prepared: PreparedRun, event_type: str) -> dict[str, Any] | None:
    for event in prepared.trace.as_dict()["events"]:
        if event.get("type") == event_type:
            return event
    return None


def guard_trace(prepared: PreparedRun) -> dict[str, Any] | None:
    return _trace_event(prepared, "EVIDENCE_GUARD")


def history_guard_trace(prepared: PreparedRun) -> dict[str, Any] | None:
    return _trace_event(prepared, "HISTORY_GUARD")


async def run_case(runtime: AgentRuntime, recorder: RecordingLLMAdapter,
                   prepared: PreparedRun, call_id: str) -> dict[str, Any]:
    recorder.begin()
    started = time.perf_counter()
    result: dict[str, Any] = {"callId": call_id, "model": runtime.llm_name}
    deltas: list[str] = []
    first_delta_ms: int | None = None
    try:
        async for delta in runtime.generate(prepared):
            if first_delta_ms is None:
                first_delta_ms = round((time.perf_counter() - started) * 1000)
            deltas.append(delta)
        result["latencyMs"] = round((time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 —— 记录失败，不重试
        result["latencyMs"] = round((time.perf_counter() - started) * 1000)
        result["error"] = recorder.error or f"{type(exc).__name__}: {str(exc)[:300]}"
        result["httpStatus"] = recorder.last_status_code
        result["llmCallCount"] = len(recorder.attempts)
        result["modelAttempts"] = recorder.attempts_record()
        result["rawAnswer"] = recorder.raw
        result["finalAnswer"] = prepared.state.response
        result["firstDeltaMs"] = first_delta_ms
        return result

    attempts = recorder.attempts_record()
    raw = attempts[0]["raw"] if attempts else recorder.raw
    final = prepared.state.response
    streamed = "".join(deltas)
    history_guard = history_guard_trace(prepared)
    guard_notice = final[len(raw):] if final.startswith(raw) else None
    result.update(
        {
            "httpStatus": recorder.last_status_code,
            "llmCallCount": len(attempts),
            "modelAttempts": attempts,
            "rawAnswer": raw,
            "finalAnswer": final,
            "firstDeltaMs": first_delta_ms,
            "streamedMatchesFinal": streamed == final,
            "rawPreservedInFinal": final.startswith(raw),
            "fallbackUsed": bool(history_guard and history_guard.get("fallback")),
            "historyGuard": history_guard,
            "guardNotice": guard_notice,
            "guardTrace": guard_trace(prepared),
            "promptTokensEstimated": prepared.context.estimated_tokens,
            "rawTokensEstimated": estimate_tokens(raw),
            "allAttemptTokensEstimated": sum(
                attempt["tokensEstimated"] for attempt in attempts
            ),
            "guardTokensEstimated": estimate_tokens(guard_notice or ""),
            "stateSummary": prepared.state.summary(),
            "trace": prepared.trace.as_dict(),
        }
    )
    runtime.validate_citations(prepared)
    return result


async def main() -> int:
    if len(sys.argv) != 3:
        print(
            "用法：phase53_runtime_eval.py <summary.json> <summary-clean.json>",
            file=sys.stderr,
        )
        return 2
    summary = load_summary(sys.argv[1])
    clean = load_summary(sys.argv[2])
    settings = Settings()
    mock_mode = os.environ.get("AILS_RUNTIME_MOCK", "")
    if mock_mode not in {"", "1", "plain"}:
        print("AILS_RUNTIME_MOCK 只接受 1 或 plain。", file=sys.stderr)
        return 2
    if not mock_mode and (settings.llm_provider != "openai" or not settings.llm_api_key):
        print("需要 AILS_LLM_PROVIDER=openai 且提供 AILS_LLM_API_KEY。", file=sys.stderr)
        return 2
    cases_requested = [
        item.strip() for item in os.environ.get("AILS_RUNTIME_CASES", DEFAULT_CASES).split(",")
        if item.strip()
    ]

    out_dir = Path(summary["out_dir"]) / (
        "runtime-eval-plain-mock" if mock_mode == "plain"
        else "runtime-eval-mock" if mock_mode
        else "runtime-eval"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = (
        PlainMockAdapter() if mock_mode == "plain"
        else MockLLMAdapter() if mock_mode
        else build_llm_adapter(settings)
    )
    recorder = RecordingLLMAdapter(llm)
    runtime, embedding, store = await build_runtime(settings, recorder)
    records: dict[str, Any] = {
        "model": {
            "provider": "mock" if mock_mode else settings.llm_provider,
            "baseUrl": None if mock_mode else settings.llm_base_url,
            "model": "mock" if mock_mode else settings.llm_model,
            "temperature": settings.llm_temperature,
            "maxTokens": settings.llm_max_tokens,
            "embeddingProvider": settings.embedding_provider,
            "embeddingModel": settings.embedding_model,
        },
        "datasets": {
            "ab_d": summary.get("out_dir"),
            "bc": clean.get("out_dir"),
        },
        "cases": {},
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            token_a = await login(client, summary["user_a_name"], summary["password"])
            token_bc = await login(client, clean["user_c_name"], clean["password"])
            token_d = await login(client, summary["user_b_name"], summary["password"])
            specs = {
                "A": (summary["user_a_id"], summary["space_a_id"], summary["questions"]["A"],
                      token_a, "A 当前误区"),
                "B": (clean["user_c_id"], clean["space_c_id"], clean["questions"]["B"],
                      token_bc, "B 纠正后普通问题"),
                "C": (clean["user_c_id"], clean["space_c_id"], clean["questions"]["C"],
                      token_bc, "C 主动复盘（Trajectory-enabled）"),
                "D": (summary["user_b_id"], summary["space_b_id"], summary["questions"]["D"],
                      token_d, "D Memory 冲突"),
            }
            run_id = 940000
            for key in cases_requested:
                if key not in {"A", "B", "C", "C-only", "D"}:
                    raise RuntimeError(f"未知场景：{key}")
                base_key = "C" if key == "C-only" else key
                user_id, space_id, question, token, title = specs[base_key]
                conversation_id = await create_conversation(
                    client, token, int(space_id), f"5.3.1 {title}"
                )
                full = await build_context(
                    runtime, token=token, user_id=int(user_id),
                    conversation_id=conversation_id, space_id=int(space_id),
                    question=question, run_id=run_id,
                )
                run_id += 1
                detected = [item.concept.label for item in full.state.detected_concepts]
                if not any("TLB" in label.upper() for label in detected):
                    raise RuntimeError(f"场景 {key} 未识别到 TLB：{detected}")

                item: dict[str, Any] = {
                    "case": key,
                    "title": title,
                    "userId": int(user_id),
                    "spaceId": int(space_id),
                    "conversationId": conversation_id,
                    "question": question,
                    "detectedConcepts": detected,
                    "retriever": full.retriever_name,
                    "knowledgeStatus": full.state.knowledge_status,
                    "context": context_record(full.context),
                    "trajectoryFacts": trajectory_facts(full),
                }

                prepared = full
                if key == "C-only":
                    prepared = current_only(full, settings)
                    item["comparison"] = compare_contexts(full.context, prepared.context)
                    item["currentOnlyContext"] = context_record(prepared.context)
                    if not item["comparison"]["nonTrajectoryIdentical"]:
                        raise RuntimeError("C-only 与 C 除轨迹外上下文不一致，对照无效")
                    if not item["comparison"]["dialogueMessagesIdentical"]:
                        raise RuntimeError("C-only 与 C 的对话消息不一致，对照无效")
                    if not item["comparison"]["systemInstructionDiffers"]:
                        raise RuntimeError("C-only 与 C 的 system instruction 相同，轨迹未生效")

                item["call"] = await run_case(runtime, recorder, prepared, key)
                records["cases"][key] = item
                (out_dir / f"{key}.json").write_text(
                    json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                if item["call"].get("error"):
                    records["blockedAt"] = key
                    break
    finally:
        await runtime.aclose()
        await embedding.aclose()
        await store.aclose()

    (out_dir / "runtime-responses.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"写出：{out_dir / 'runtime-responses.json'}")
    for key, item in records["cases"].items():
        call = item["call"]
        print(
            f"[{key}] status={call.get('httpStatus')} latency={call.get('latencyMs')}ms "
            f"calls={call.get('llmCallCount')} firstDelta={call.get('firstDeltaMs')}ms "
            f"raw={len(call.get('rawAnswer') or '')}chars "
            f"final={len(call.get('finalAnswer') or '')}chars "
            f"fallback={call.get('fallbackUsed')} "
            f"guard={bool(call.get('guardNotice'))} error={call.get('error')}"
        )
    return 0 if not records.get("blockedAt") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
