"""端到端：一次请求走完 Python → State → Retriever → Context Builder → Tutor → LLM。

用的是 Fixture 数据 + Mock LLM，所以断言可以很具体 ——
「用户的历史理解有没有真的进到模型看到的消息里」这件事在这里被钉死。
这是整个 MVP 唯一真正要证明的东西：**跨对话的认知确实参与了本次回答的生成**。
"""

from __future__ import annotations

import json

import pytest

from app.llm import build_llm_adapter
from app.llm.base import ChatMessage
from app.runtime import AgentRuntime
from app.trace import TraceEventType
from tests.helpers import make_request


@pytest.fixture
def runtime(settings) -> AgentRuntime:
    return AgentRuntime(settings, build_llm_adapter(settings))


async def collect_events(runtime: AgentRuntime, request) -> list:
    return [event async for event in runtime.stream(request)]


def joined_text(events: list) -> str:
    return "".join(str(event.data["delta"]) for event in events if event.event == "delta")


async def test_history_understanding_reaches_the_model(runtime):
    events = await collect_events(runtime, make_request("TLB Miss 和 Page Fault 是什么关系？"))

    assert [event.event for event in events][-1] == "done"
    output = joined_text(events)
    # Mock LLM 把系统提示里的背景条目回读出来，因此这段文本能证明认知快照确实被组装进去了
    assert "把 TLB Miss 与 Page Fault 混为一谈" in output
    assert "TLB 是页表项的缓存" in output
    assert "Page Table 本身存放在哪里" in output


async def test_detected_concepts_are_reported_in_meta(runtime):
    events = await collect_events(runtime, make_request("TLB 是什么？"))
    meta = next(event for event in events if event.event == "meta")

    assert meta.data["detectedConcepts"] == ["TLB（快表）"]
    assert meta.data["estimatedTokens"] > 0


async def test_done_carries_minimal_context_manifest(runtime):
    """D4-4：Manifest 必须随终态事件到后端，且保持 D4-3 的最小字段边界。"""
    events = await collect_events(runtime, make_request("TLB 是什么？"))
    done = next(event for event in events if event.event == "done")
    manifest = done.data["contextManifest"]

    assert set(manifest) == {
        "schema_version",
        "policy_version",
        "intent",
        "sources",
        "budget",
    }
    assert manifest["schema_version"] == 1
    assert manifest["sources"]

    for source in manifest["sources"]:
        assert set(source) == {
            "type",
            "source_id",
            "retrieval_status",
            "injection_status",
            "reason",
            "estimated_tokens",
        }

    raw = json.dumps(manifest, ensure_ascii=False)
    for forbidden in (
        "preview",
        "system_instruction",
        "systemInstruction",
        "messages",
        "PRIVATE",
    ):
        assert forbidden not in raw


async def test_candidate_memory_is_never_injected(runtime):
    """CANDIDATE 是 AI 推断、用户还没确认的，不能当成事实影响回答。"""
    prepared = await runtime.prepare(make_request("TLB 是什么？"))

    assert "用户可能喜欢看图示" not in prepared.context.messages[0].content
    assert "用户更喜欢从「硬件实际发生了什么」入手" in prepared.context.messages[0].content


async def test_evidence_is_retrieved_only_on_demand(runtime):
    plain = await runtime.prepare(make_request("TLB 是什么？"))
    challenged = await runtime.prepare(make_request("为什么你认为我理解了 TLB？"))

    assert _retrieve_detail(plain)["evidenceRequested"] is False
    assert _retrieve_detail(plain)["evidences"] == 0
    assert _retrieve_detail(challenged)["evidenceRequested"] is True
    assert _retrieve_detail(challenged)["evidences"] == 2
    assert "判定依据" in challenged.context.messages[0].content


async def test_no_concept_hit_still_attempts_knowledge_retrieval(runtime):
    """没有 Concept 只跳过 U / M / E，不能连带跳过用户上传的文档。"""
    prepared = await runtime.prepare(make_request("今天天气怎么样？"))

    assert prepared.state.concept_names == []
    detail = _retrieve_detail(prepared)
    assert detail is not None
    assert detail["conceptIds"] == []
    assert detail["knowledgeStatus"] == "NO_MATCH"
    assert detail["understandings"] == []
    assert detail["evidences"] == 0
    assert detail["evidenceRequested"] is False
    # 没识别到概念 => 这三条检索线根本没有发起。NOT_QUERIED 不能渲染成
    # 「没有记录 / 没学过」，也不能让 preview 的 warnings 暗示结果为空。
    assert detail["understandingStatus"] == "NOT_QUERIED"
    assert detail["memoryStatus"] == "NOT_QUERIED"
    assert detail["revisionStatus"] == "NOT_QUERIED"
    assert all("没有检索到任何 Understanding" not in warning for warning in prepared.context.warnings)
    system = prepared.context.messages[0].content
    # 固定教学契约里本来就可能出现「没有记录的部分不能算已掌握」这类通用规则，
    # 所以这里只检查动态生成的「学习者背景」区块没有被错误添加。
    assert "没有检索到任何历史认知记录" not in system
    assert "学习者背景" not in system


async def test_no_concept_does_not_call_understanding_memory_or_evidence(settings, monkeypatch):
    """用计数 Retriever 钉死：Knowledge 被调用，另外三条线一次都不碰。"""
    from app.retriever.base import (
        EvidenceRetrievalResult,
        KnowledgeResult,
        KnowledgeStatus,
    )
    from app.state.agent_state import AgentState

    calls = {"knowledge": 0, "understanding": 0, "memory": 0, "evidence": 0}

    class CountingRetriever:
        name = "counting"

        async def list_concepts(self, space_id):
            return []

        async def get_knowledge(self, query):
            calls["knowledge"] += 1
            assert query.space_id == 1
            assert query.user_id == 1
            assert query.top_k == settings.knowledge_top_k == 5
            assert query.min_score == settings.knowledge_min_score == 0.35
            return KnowledgeResult(status=KnowledgeStatus.NO_MATCH)

        async def get_understandings(self, *args):
            calls["understanding"] += 1
            return []

        async def get_memories(self, *args):
            calls["memory"] += 1
            return []

        async def get_evidences(self, *args):
            calls["evidence"] += 1
            return EvidenceRetrievalResult()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "app.runtime.build_retriever", lambda *args, **kwargs: CountingRetriever()
    )
    runtime = AgentRuntime(settings, build_llm_adapter(settings))
    prepared = await runtime.prepare(make_request("今天天气怎么样？"))

    assert isinstance(prepared.state, AgentState)
    assert calls == {"knowledge": 1, "understanding": 0, "memory": 0, "evidence": 0}


async def test_knowledge_failure_degrades_without_breaking_chat(settings, monkeypatch):
    """普通知识检索故障只能降级为空知识，不能让整轮对话变成 error / 5xx。"""

    class RaisingKnowledgeRetriever:
        name = "raising"

        async def list_concepts(self, space_id):
            return []

        async def get_knowledge(self, query):
            raise RuntimeError("qdrant down")

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "app.runtime.build_retriever",
        lambda *args, **kwargs: RaisingKnowledgeRetriever(),
    )
    runtime = AgentRuntime(settings, build_llm_adapter(settings))

    prepared = await runtime.prepare(make_request("今天天气怎么样？"))
    assert prepared.state.knowledge == []
    assert prepared.state.knowledge_status == "DEGRADED"
    assert any("知识检索失败" in warning for warning in prepared.context.warnings)

    events = await collect_events(runtime, make_request("今天天气怎么样？"))
    assert events[-1].event == "done"


async def test_trace_records_the_whole_run(runtime):
    events = await collect_events(runtime, make_request("TLB 是什么？"))
    prepared = await runtime.prepare(make_request("TLB 是什么？"))

    assert events[0].event == "meta"
    assert events[-1].event == "done"
    assert all(event.event == "delta" for event in events[1:-1])
    assert prepared.trace.event_types() == [
        TraceEventType.RUN_START.value,
        TraceEventType.CONCEPT_DETECT.value,
        TraceEventType.RETRIEVE.value,
        TraceEventType.CONTEXT_BUILT.value,
    ]
    assert prepared.retriever_name.startswith("fixture:")
    assert prepared.degraded is False


async def test_follow_up_question_after_topic_switch_still_recalls_the_concept(runtime):
    """架构文档第四十三节那个场景：用户换了说法追问，仍要接上已有认知。"""
    prepared = await runtime.prepare(
        make_request(
            "那它没命中的时候呢？",
            history=[("user", "TLB 是什么？"), ("assistant", "TLB 是页表项缓存。")],
        )
    )

    assert prepared.state.concept_names[0] == "TLB（快表）"
    assert prepared.state.understandings


async def test_http_mode_without_token_fails_loudly(settings):
    """配了 http 就必须真读后端，不允许静默退回假数据。"""
    strict = settings.model_copy(update={"retriever_mode": "http"})
    runtime = AgentRuntime(strict, build_llm_adapter(strict))

    events = await collect_events(runtime, make_request("TLB 是什么？"))

    assert events[-1].event == "error"
    assert "authToken" in str(events[-1].data["message"])
    # error 必须带 int 的 code：后端 AgentStreamHandler.onError(int, String) 收的就是 int，
    # 少了它，网关只能自己编一个常量，错误分类就丢在内网这一跳上了
    assert events[-1].data["code"] == 503


async def test_empty_model_output_becomes_an_explicit_error(settings):
    """模型一个字都没吐出来时必须报错，而不是给用户推一条空消息。"""
    runtime = AgentRuntime(settings, _SilentLLM())

    events = await collect_events(runtime, make_request("TLB 是什么？"))

    assert events[-1].event == "error"
    assert "没有返回任何内容" in str(events[-1].data["message"])
    # 模型上游失败与内部异常要能区分开
    assert events[-1].data["code"] == 502


def _retrieve_detail(prepared) -> dict | None:
    for event in prepared.trace.events:
        if event.type is TraceEventType.RETRIEVE:
            return event.detail
    return None


class _SilentLLM:
    """什么都不产出的模型，用于验证错误路径。"""

    @property
    def name(self) -> str:
        return "silent"

    async def stream_chat(self, messages: list[ChatMessage], **kwargs):
        if False:  # pragma: no cover —— 目的是让它成为一个空异步生成器
            yield ""

    async def complete(self, messages: list[ChatMessage], **kwargs) -> str:
        return ""

    async def aclose(self) -> None:
        return None
