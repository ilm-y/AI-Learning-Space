"""阶段 5.3：用真实 LLM 跑四个跨会话学习场景。

这个脚本是**评测工具**，不是生产入口：
- 检索与上下文组装复用 `AgentRuntime.prepare()` / `ContextBuilder`，走正式后端读接口；
- 模型调用直接使用 OpenAI 兼容 `/chat/completions`，只为记录 HTTP 状态、延迟与 usage；
- 场景 C 的 Current-only 上下文由同一个 `AgentState` 去掉 trajectory / 关联 Evidence
  后重新组装，除轨迹外其余输入不变；不写数据库、不伪造历史。

用法（从仓库根目录）：
    AILS_LLM_PROVIDER=openai \
    AILS_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
    AILS_LLM_API_KEY=<从 agent/.env 注入> \
    AILS_LLM_MODEL=qwen-plus \
    python agent/scripts/phase53_real_model_eval.py <summary.json> [summary-clean.json]

第二个参数可选：为场景 B/C 提供“已纠正且无冲突 Memory”的干净数据集，
避免 Current-only 通过 Memory 读到旧误区、污染场景 C 的对照。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT))

from app.config import Settings  # noqa: E402
from app.context.builder import ContextBuilder  # noqa: E402
from app.embedding import build_embedding_provider  # noqa: E402
from app.retriever.knowledge import KnowledgeSearchService  # noqa: E402
from app.runtime import AgentRuntime  # noqa: E402
from app.schemas import ChatRequest  # noqa: E402
from app.vectorstore.qdrant_store import QdrantDocumentStore  # noqa: E402

BACKEND = os.environ.get("AILS_BACKEND_BASE_URL", "http://localhost:8080").rstrip("/")


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


async def build_runtime(settings: Settings, llm=None):
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
    # prepare() 不需要 LLM；默认用 Mock 占位，本脚本不调用 generate()。
    # 传入真实 adapter 时也只复用 prepare()，生成仍由 call_model() 直接发起。
    from app.llm.mock import MockLLMAdapter

    runtime = AgentRuntime(settings, llm or MockLLMAdapter(), knowledge_search=knowledge)
    return runtime, embedding, store


async def build_context(runtime: AgentRuntime, settings: Settings, *, token: str,
                        user_id: int, conversation_id: int, space_id: int,
                        question: str, run_id: int):
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


def current_only(prepared, settings: Settings):
    """同一个 state 去掉轨迹与轨迹关联证据后重新组装，其他输入不变。"""
    state = copy.copy(prepared.state)
    state.trajectories = []
    state.evidences = []
    state.context = None
    builder = ContextBuilder(
        max_context_tokens=settings.max_context_tokens,
        recent_history_limit=settings.recent_history_limit,
    )
    return builder.build(state), state


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


async def call_model(client: httpx.AsyncClient, settings: Settings,
                     context, call_id: str) -> dict[str, Any]:
    payload = {
        "model": settings.llm_model,
        "messages": [message.as_dict() for message in context.messages],
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "stream": False,
    }
    started = time.perf_counter()
    result: dict[str, Any] = {"callId": call_id, "model": settings.llm_model}
    try:
        response = await client.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=payload,
            timeout=settings.llm_timeout_seconds,
        )
        result["httpStatus"] = response.status_code
        result["latencyMs"] = round((time.perf_counter() - started) * 1000)
        result["responseBytes"] = len(response.content)
        if response.status_code >= 400:
            result["error"] = response.text[:500]
            result["answer"] = None
            return result
        body = response.json()
        result["usage"] = body.get("usage")
        result["answer"] = body["choices"][0]["message"]["content"]
        return result
    except Exception as exc:  # noqa: BLE001 —— 评测需要记录失败类别，而不是重试
        result["httpStatus"] = None
        result["latencyMs"] = round((time.perf_counter() - started) * 1000)
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        result["answer"] = None
        return result


def compare_contexts(full, only) -> dict[str, Any]:
    full_record = context_record(full)
    only_record = context_record(only)
    full_sections = full_record["sections"]
    only_sections = only_record["sections"]
    full_base = [s for s in full_sections if not s["name"].startswith("trajectory:")]
    only_base = only_sections
    identical = [(a["name"], a["content"]) for a in full_base] == [
        (b["name"], b["content"]) for b in only_base
    ]
    # messages[0] 是 system instruction，按设计会因为轨迹区块而不同；
    # 对话历史与本轮输入必须完全一致。
    full_dialogue = [m for m in full_record["messages"] if m["role"] != "system"]
    only_dialogue = [m for m in only_record["messages"] if m["role"] != "system"]
    return {
        "trajectorySections": [s["name"] for s in full_sections if s["name"].startswith("trajectory:")],
        "nonTrajectoryIdentical": identical,
        "fullSectionNames": [s["name"] for s in full_sections],
        "currentOnlySectionNames": [s["name"] for s in only_sections],
        "dialogueMessagesIdentical": full_dialogue == only_dialogue,
        "systemInstructionDiffers": full_record["systemInstruction"] != only_record["systemInstruction"],
        "tokenDelta": full_record["estimatedTokens"] - only_record["estimatedTokens"],
    }


async def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("用法：phase53_real_model_eval.py <summary.json> [summary-clean.json]",
              file=sys.stderr)
        return 2
    summary = load_summary(sys.argv[1])
    bc_summary = load_summary(sys.argv[2]) if len(sys.argv) == 3 else summary
    settings = Settings()
    if settings.llm_provider != "openai" or not settings.llm_api_key:
        print("真实评测需要 AILS_LLM_PROVIDER=openai 且提供 AILS_LLM_API_KEY。", file=sys.stderr)
        return 2
    if settings.llm_model in {"", "deepseek-chat"} and "dashscope" in settings.llm_base_url:
        # 不擅自改配置；只提示当前组合可能不是用户想测的模型。
        print(f"注意：当前模型配置为 {settings.llm_model} @ {settings.llm_base_url}", file=sys.stderr)

    suffix = "real-model-clean" if len(sys.argv) == 3 else "real-model"
    out_dir = Path(summary["out_dir"]) / suffix
    out_dir.mkdir(parents=True, exist_ok=True)
    runtime, embedding, store = await build_runtime(settings)
    records: dict[str, Any] = {
        "model": {
            "provider": settings.llm_provider,
            "baseUrl": settings.llm_base_url,
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "maxTokens": settings.llm_max_tokens,
            "embeddingProvider": settings.embedding_provider,
            "embeddingModel": settings.embedding_model,
        },
        "datasets": {
            "ab_d": summary.get("out_dir"),
            "bc": bc_summary.get("out_dir"),
            "bc_has_conflicting_memory": False if len(sys.argv) == 3 else True,
        },
        "scenarios": {},
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            token_a = await login(client, summary["user_a_name"], summary["password"])
            token_b = await login(client, bc_summary["user_c_name"], bc_summary["password"])
            token_d = await login(client, summary["user_b_name"], summary["password"])
            cases = {
                "A": (summary["user_a_id"], summary["space_a_id"], summary["tlb_a"],
                      summary["questions"]["A"], token_a, "A 当前误区"),
                "B": (bc_summary["user_c_id"], bc_summary["space_c_id"], bc_summary["tlb_c"],
                      bc_summary["questions"]["B"], token_b, "B 纠正后普通问题（无冲突 Memory）"),
                "C": (bc_summary["user_c_id"], bc_summary["space_c_id"], bc_summary["tlb_c"],
                      bc_summary["questions"]["C"], token_b, "C 主动复盘（无冲突 Memory）"),
                "D": (summary["user_b_id"], summary["space_b_id"], summary["tlb_b"],
                      summary["questions"]["D"], token_d, "D Memory 冲突"),
            }
            run_id = 930000
            for key, (user_id, space_id, concept_id, question, token, title) in cases.items():
                conversation_id = await create_conversation(client, token, space_id, f"5.3 {title}")
                prepared = await build_context(
                    runtime, settings, token=token, user_id=int(user_id),
                    conversation_id=conversation_id, space_id=int(space_id),
                    question=question, run_id=run_id,
                )
                run_id += 1
                detected = [item.concept.label for item in prepared.state.detected_concepts]
                if not any("TLB" in label.upper() for label in detected):
                    raise RuntimeError(f"场景 {key} 未识别到 TLB 概念：{detected}")

                item: dict[str, Any] = {
                    "scenario": key,
                    "title": title,
                    "userId": int(user_id),
                    "spaceId": int(space_id),
                    "conceptId": int(concept_id),
                    "conversationId": conversation_id,
                    "question": question,
                    "detectedConcepts": detected,
                    "retriever": prepared.retriever_name,
                    "stateSummary": prepared.state.summary(),
                    "trace": prepared.trace.as_dict(),
                    "context": context_record(prepared.context),
                }
                if key == "C":
                    only_context, only_state = current_only(prepared, settings)
                    item["comparison"] = compare_contexts(prepared.context, only_context)
                    item["currentOnlyContext"] = context_record(only_context)
                    item["currentOnlyStateSummary"] = only_state.summary()
                    if not item["comparison"]["nonTrajectoryIdentical"]:
                        raise RuntimeError("场景 C 两种上下文除轨迹外不一致，对照无效")
                    if not item["comparison"]["dialogueMessagesIdentical"]:
                        raise RuntimeError("场景 C 两种上下文的对话消息不一致，对照无效")
                    if not item["comparison"]["systemInstructionDiffers"]:
                        raise RuntimeError("场景 C 两种上下文的 system instruction 竟然相同，轨迹未生效")

                item["trajectoryCall"] = await call_model(
                    client, settings, prepared.context, f"{key}-trajectory"
                )
                if key == "C":
                    item["currentOnlyCall"] = await call_model(
                        client, settings, only_context, "C-current-only"
                    )
                records["scenarios"][key] = item
                (out_dir / f"{key}.json").write_text(
                    json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                # 单次调用失败就停止，避免在鉴权/限流错误上继续消耗额度。
                if item["trajectoryCall"].get("answer") is None:
                    records["blockedAt"] = key
                    break
                if key == "C" and item.get("currentOnlyCall", {}).get("answer") is None:
                    records["blockedAt"] = key
                    break
    finally:
        await runtime.aclose()
        await embedding.aclose()
        await store.aclose()

    (out_dir / "responses.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"写出：{out_dir / 'responses.json'}")
    for key, item in records["scenarios"].items():
        for call_key in ("trajectoryCall", "currentOnlyCall"):
            call = item.get(call_key)
            if not call:
                continue
            print(
                f"[{key}/{call_key}] status={call.get('httpStatus')} "
                f"latency={call.get('latencyMs')}ms chars={len(call.get('answer') or '')}"
            )
    return 0 if not records.get("blockedAt") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
