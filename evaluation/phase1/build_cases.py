"""生成 Phase 1 的 40 条固定评测用例。

用例是**审阅对象**：``docs/testing/agent-evaluation-cases.jsonl`` 应当能直接
diff。这个脚本只负责把重复的字段（请求形状、rubric 结构）写顺，不生成
任何运行时结果。运行 ``python -m evaluation.phase1.build_cases`` 可重新生成。

设计约束：

- 40 条，A10 / B8 / C6 / D8 / E8，不复制凑数；
- 每条至少有 1 条硬断言与 1 条固定 rubric；
- 只使用隔离 fixture 号段（9001/9002 用户，9101/9102/9104 空间）；
- 需要后端/数据库的用例标记 ``designed_only``，状态只会是 SKIPPED。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CASES_PATH = Path("docs/testing/agent-evaluation-cases.jsonl")


def assertion(path: str, op: str, value: Any, note: str = "") -> dict[str, Any]:
    return {"path": path, "op": op, "value": value, "note": note}


def rubric(
    key: str,
    description: str,
    score2: str,
    score1: str,
    score0: str,
    *,
    signals2: tuple[tuple[str, ...], ...] = (),
    signals1: tuple[str, ...] = (),
    forbidden: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": key,
        "description": description,
        "score2": score2,
        "score1": score1,
        "score0": score0,
        "signals2": [list(group) for group in signals2],
        "signals1": list(signals1),
        "forbidden": list(forbidden),
    }


def request(
    message: str,
    *,
    user: int = 9001,
    space: int = 9101,
    conversation: int = 9401,
    run_id: int = 1,
    history: list[dict[str, str]] | None = None,
    session: dict[str, Any] | None = None,
    message_id: int | None = None,
    assistant_message: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "runId": run_id,
        "traceId": f"phase1-{run_id:02d}",
        "userId": user,
        "conversationId": conversation,
        "learningSpaceId": space,
        "userMessage": message,
        "history": history or [],
    }
    if session is not None:
        data["session"] = session
    if message_id is not None:
        data["messageId"] = message_id
    if assistant_message is not None:
        data["assistantMessage"] = assistant_message
    return data


def case(
    case_id: str,
    category: str,
    title: str,
    severity: str,
    *,
    mode: str,
    probe: str,
    profile: str,
    req: dict[str, Any],
    assertions: list[dict[str, Any]],
    rubrics: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    llm: str = "mock",
    preconditions: tuple[str, ...] = (),
    steps: tuple[str, ...] = (),
    expected: tuple[str, ...] = (),
    forbidden: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    cleanup: str = "进程内 fixture 深拷贝；执行后 aclose，无持久化副作用。",
    targets_defect: str | None = None,
    contract_status: str = "confirmed",
    scripted_chat: str | None = None,
    scripted_analyze: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": case_id,
        "category": category,
        "title": title,
        "severity": severity,
        "executionMode": mode,
        "probe": probe,
        "profile": profile,
        "request": req,
        "settings": settings or {},
        "preconditions": list(preconditions),
        "steps": list(steps),
        "expectedBehavior": list(expected),
        "forbiddenBehavior": list(forbidden),
        "hardAssertions": assertions,
        "semanticRubric": rubrics,
        "dependencies": list(dependencies),
        "cleanup": cleanup,
        "contractStatus": contract_status,
        "llm": llm,
        "notes": notes,
    }
    if targets_defect:
        item["targetsDefect"] = targets_defect
    if scripted_chat is not None:
        item["scriptedChat"] = scripted_chat
    if scripted_analyze is not None:
        item["scriptedAnalyze"] = scripted_analyze
    return item


HISTORY_LONG = [
    {"role": "user", "content": "我先看了地址翻译的整体流程，想弄清楚虚拟地址怎样一步步变成物理地址，以及每一层查表失败时会发生什么。"},
    {"role": "assistant", "content": "可以从页表基址寄存器开始，先拆分虚拟页号和页内偏移，再逐级查页表；如果页表项不存在，就会触发缺页异常。"},
]


def build() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # ===================== A：跨会话学习与状态演化 =====================
    cases.append(
        case(
            "A01",
            "A",
            "历史误区被识别为过去，而不是当前状态",
            "P0",
            mode="fixture_mock",
            probe="chat",
            profile="alice_corrected",
            req=request(
                "我之前哪里搞错了 TLB 和缺页？帮我复盘一下。",
                run_id=1,
            ),
            assertions=[
                assertion("run.historyReviewRequested", "eq", True, "显式复盘应缓冲校验"),
                assertion("retrieval.trajectoryRevisionNos", "subset", [1, 2, 3]),
                assertion("retrieval.trajectoryEvidenceIds", "subset", [1701, 1702, 1703]),
                assertion("retrieval.understandingStatuses", "contains", "DEMONSTRATED"),
                assertion("context.systemInstruction", "contains", "已纠正，历史"),
                assertion("context.systemInstruction", "contains", "把 TLB Miss 当成缺页异常"),
                assertion("context.falseNoHistoryClaim", "eq", False),
                assertion("answer.internalIdLeak", "eq", False),
                assertion("isolation.violationCount", "eq", 0),
            ],
            rubrics=[
                rubric(
                    "temporal_framing",
                    "把旧误区说成过去发生过、后来被纠正，而不是当前仍在。",
                    "同时出现过去时与纠正结果，且不把旧误区当现状。",
                    "提到旧误区，但没有明确它是已纠正的历史。",
                    "把旧误区描述成当前仍然存在的问题。",
                    signals2=(("过去", "曾经", "历史", "已纠正"), ("纠正", "改过来", "后来")),
                    signals1=("TLB Miss", "缺页"),
                    forbidden=("现在仍然把 TLB Miss 当成缺页", "目前仍认为 TLB Miss 就是缺页"),
                )
            ],
            expected=("复盘引用 revision 1/2/3 与关联证据", "旧误区标注为已纠正的历史"),
            forbidden=("把已纠正误区说成当前状态", "暴露内部 revision/evidence id"),
            dependencies=("真实 AgentRuntime 的 History Guard 路径",),
        )
    )
    cases.append(
        case(
            "A02",
            "A",
            "当前 Understanding 覆盖旧的冲突状态",
            "P0",
            mode="fixture_mock",
            probe="chat",
            profile="alice_corrected",
            req=request("我之前是不是把 TLB Miss 当成缺页了？现在呢？", run_id=2),
            assertions=[
                assertion("retrieval.understandingStatuses", "contains", "DEMONSTRATED"),
                assertion("context.systemInstruction", "contains", "已能讲清"),
                assertion("context.systemInstruction", "contains", "已纠正，历史"),
                assertion("context.systemInstruction", "not_contains", "当前仍存在的误区：把 TLB Miss"),
                assertion("context.falseNoHistoryClaim", "eq", False),
            ],
            rubrics=[
                rubric(
                    "current_vs_history",
                    "回答区分「过去曾经」与「现在状态」。",
                    "明确说过去有误区、现在已能区分。",
                    "只描述过去，不确认当前状态。",
                    "把过去的误区说成现在仍然存在。",
                    signals2=(("过去", "之前", "曾经"), ("现在", "当前", "已经")),
                    signals1=("TLB Miss", "缺页"),
                    forbidden=("你现在仍然认为 TLB Miss 就是缺页",),
                )
            ],
            expected=("当前快照优先于历史记录",),
            dependencies=("Context Builder 的 understanding/trajectory 渲染",),
        )
    )
    cases.append(
        case(
            "A03",
            "A",
            "旧 Memory 与当前 Understanding 冲突时的提示",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_conflict_memory",
            req=request("我现在对 TLB 的理解是什么状态？还有旧记忆吗？", run_id=3),
            assertions=[
                assertion("retrieval.understandingStatuses", "contains", "DEMONSTRATED"),
                assertion("retrieval.memoryIds", "contains", 1407),
                assertion("context.systemInstruction", "contains", "学习状态（历史记录）"),
                assertion("context.systemInstruction", "contains", "较早的学习状态记录"),
                assertion("context.systemInstruction", "contains", "当前状态已不同"),
            ],
            rubrics=[
                rubric(
                    "memory_conflict",
                    "说明旧记忆是历史记录，不能覆盖当前认知。",
                    "同时点名旧记忆与当前状态，并说明差异。",
                    "提到旧记忆，但没有说明它不覆盖当前状态。",
                    "直接拿旧记忆断言用户现在的水平。",
                    signals2=(("旧", "较早", "历史"), ("当前", "现在"), ("不同", "已变化")),
                    signals1=("长期记忆", "记录"),
                    forbidden=("你现在还不懂 TLB Miss 和缺页的区别",),
                )
            ],
            expected=("Context 明确标注 learning memory 为历史",),
            dependencies=("Context Builder 的 has_learning_memory 提示",),
        )
    )
    cases.append(
        case(
            "A04",
            "A",
            "复盘轨迹包含版本、时间、来源与完整证据",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_corrected",
            req=request("复盘一下我学 TLB 的完整轨迹。", run_id=4),
            assertions=[
                assertion("retrieval.trajectoryRevisionNos", "subset", [1, 2, 3]),
                assertion("context.systemInstruction", "contains", "版本 1"),
                assertion("context.systemInstruction", "contains", "版本 2"),
                assertion("context.systemInstruction", "contains", "版本 3"),
                assertion("context.systemInstruction", "contains", "首次记录于"),
                assertion("context.systemInstruction", "contains", "来源：消息"),
                assertion("context.systemInstruction", "contains", "来源：批注"),
                assertion("context.systemInstruction", "contains", "来源：测试回答"),
                assertion("context.systemInstruction", "not_contains", "revision:"),
                assertion("context.systemInstruction", "not_contains", "1701"),
            ],
            rubrics=[
                rubric(
                    "record_completeness",
                    "复述可核对事实，不补写动机或心理过程。",
                    "列出至少两个版本与至少两类来源，并说明记录之外的无法确认。",
                    "列出部分版本或来源，但没有说明未记录内容。",
                    "补写「意识到 / 自己推导出」等记录没有写出的过程。",
                    signals2=(("版本",), ("来源",), ("无法确认", "没有记录", "未记录")),
                    signals1=("批注", "测试"),
                    forbidden=("你自己推导出", "你突然意识到", "完全靠自己"),
                )
            ],
            expected=("轨迹完整且不泄露内部 id",),
            dependencies=("build_trajectories / collect_evidence_ids 真实路径",),
        )
    )
    cases.append(
        case(
            "A05",
            "A",
            "跨会话持久化：第二天重新进入仍能看到学习状态",
            "P0",
            mode="designed_only",
            probe="chat",
            profile="alice_corrected",
            req=request("第二天回来继续学 TLB，记录还在吗？", run_id=5),
            assertions=[
                assertion("e2e.messagePersisted", "truthy", True),
                assertion("e2e.understandingStillCurrent", "truthy", True),
                assertion("e2e.noCrossUserLeak", "truthy", True),
            ],
            rubrics=[
                rubric(
                    "cross_session_continuity",
                    "新会话能读到上一会话的 Understanding / Revision。",
                    "回答能指出上一会话留下的具体状态。",
                    "只泛泛说「有记录」。",
                    "声称没有历史或重新从零开始。",
                    signals1=("上一", "上次", "记录"),
                )
            ],
            expected=("后端 MySQL + Agent 读取同一条学习状态",),
            dependencies=("Spring Boot / MySQL / 真实会话切换",),
            contract_status="pending_e2e",
            notes="Phase 1 只登记；未执行，不能计 PASS。",
        )
    )
    cases.append(
        case(
            "A06",
            "A",
            "认知区块被预算丢弃后不得谎称「没有历史」",
            "P1",
            mode="fixture_context",
            probe="context",
            profile="alice_current",
            req=request("请继续讲 TLB。", run_id=6, history=HISTORY_LONG),
            settings={"maxContextTokens": 60},
            assertions=[
                assertion("context.cognitiveSectionsDropped", "eq", True),
                assertion("retrieval.understandingIds", "contains", 1201),
                assertion("context.falseNoHistoryClaim", "eq", False),
                assertion(
                    "context.systemInstruction",
                    "not_contains",
                    "本次没有检索到任何历史认知记录",
                ),
            ],
            rubrics=[
                rubric(
                    "budget_honesty",
                    "预算裁剪后仍如实说明「记录被裁剪」，而不是「没有记录」。",
                    "说明记录存在但被裁剪。",
                    "未说明裁剪原因，但没有否认记录。",
                    "直接说本次没有检索到任何历史认知记录。",
                    forbidden=("本次没有检索到任何历史认知记录",),
                )
            ],
            targets_defect="Context Builder 误报无历史",
            contract_status="confirmed",
            expected=("预算裁剪与「未检索到」必须是两个不同事实",),
            notes="预算丢弃的区块必须显式说明「超出预算/未包含」，不得降级成「没有检索到历史」。",
        )
    )
    cases.append(
        case(
            "A07",
            "A",
            "动态上下文预算必须生效，完整 prompt 估算必须如实报告",
            "P1",
            mode="fixture_context",
            probe="context",
            profile="alice_budget",
            req=request("TLB 是什么？", run_id=7),
            settings={"maxContextTokens": 300},
            assertions=[
                assertion("context.droppedSections", "contains", "memory:1409"),
                # 契约（Phase 1.1 明确）：maxContextTokens 约束动态上下文 —
                # 区块标题/说明、section 内容与实际消息；固定教学契约
                # （BASE_SYSTEM_INSTRUCTION）单独报告，不计入该预算。
                assertion("context.budgetTokens", "eq", 300),
                assertion("context.contextTokens", "lte", 300),
                assertion("context.budgetExceeded", "eq", False),
                # 完整 prompt 仍要如实估算：不能把 2500+ tokens 说成 300。
                assertion("context.estimatedTokens", "gt", 300),
            ],
            rubrics=[
                rubric(
                    "budget_accounting",
                    "动态上下文不得超过预算，完整 prompt 估算必须与之一致地报告。",
                    "动态上下文在预算内、有裁剪说明，且完整 prompt 估算诚实披露超出。",
                    "动态上下文在预算内，但未说明固定系统提示单独占用。",
                    "动态上下文超出预算，或把完整 prompt 伪装成等于预算。",
                )
            ],
            targets_defect="Context Builder 预算漏算 system prompt/headings",
            expected=(
                "contextTokens 是受 maxContextTokens 约束的动态上下文",
                "estimatedTokens 是真正发给模型的完整 prompt，两者分别报告",
            ),
            notes=(
                "Phase 1.1 明确预算语义：300 约束动态上下文（不含固定系统提示），"
                "estimatedTokens 仍为完整 prompt 估算并显式提示超出预算。"
            ),
        )
    )
    cases.append(
        case(
            "A08",
            "A",
            "空历史时不得编造过去",
            "P0",
            mode="fixture_mock",
            probe="chat",
            profile="alice_empty_history",
            req=request("我之前学过 TLB 吗？我哪里搞错了？", run_id=8),
            assertions=[
                assertion("run.historyReviewRequested", "eq", True),
                assertion("retrieval.trajectoryConcepts", "length", 0),
                assertion("retrieval.understandingIds", "length", 0),
                assertion("retrieval.knowledgeStatus", "eq", "NO_VALID_DOCUMENTS"),
                assertion("answer.historyGuard.issues", "length", 0),
                assertion("answer.historyGuard.fallback", "eq", False),
                assertion("context.systemInstruction", "contains", "本次没有检索到任何历史认知记录"),
                assertion("answer.text", "not_contains", "你以前一直"),
            ],
            rubrics=[
                rubric(
                    "no_fabricated_history",
                    "没有历史轨迹时，明确说明当前可见信息不足。",
                    "说明没有可见历史记录，并拒绝推断过去。",
                    "只说不知道，但没有说明记录不足。",
                    "断言用户过去没有错误或过去一定学过。",
                    signals2=(("没有检索到", "没有记录", "可见"), ("无法", "不能", "不足")),
                    forbidden=("你以前没有犯过错", "你过去一定学过", "你从来没有误解"),
                )
            ],
            expected=("History Guard 的 no-history 路径",),
            dependencies=("History Guard 缓冲生成",),
        )
    )
    cases.append(
        case(
            "A09",
            "A",
            "跨用户隔离：Bob 的请求不能读到 Alice 的认知",
            "P0",
            mode="fixture_mock",
            probe="chat",
            profile="bob_current",
            req=request(
                "TCP 四次挥手和三次握手有什么区别？",
                user=9002,
                space=9102,
                conversation=9402,
                run_id=9,
            ),
            assertions=[
                assertion("run.userId", "eq", 9002),
                assertion("retrieval.understandingIds", "eq", [1211]),
                assertion("retrieval.memoryIds", "subset", [1411, 1412]),
                assertion("retrieval.memoryIds", "not_contains", 1401),
                assertion("retrieval.knowledgeDocumentNames", "eq", ["tcp.md"]),
                assertion("context.systemInstruction", "not_contains", "TLB"),
                assertion("context.systemInstruction", "not_contains", "操作系统课"),
                assertion("isolation.violationCount", "eq", 0),
            ],
            rubrics=[
                rubric(
                    "cross_user_isolation",
                    "回答只基于 Bob 自己的记录。",
                    "只出现 TCP/网络相关记录。",
                    "出现无关用户记录但没有据此作答。",
                    "引用 Alice 的 TLB 或操作系统记录。",
                    forbidden=("TLB", "操作系统课"),
                )
            ],
            expected=("Retriever 按 userId 过滤",),
            dependencies=("EvalRetriever 的 scope 过滤",),
        )
    )
    cases.append(
        case(
            "A10",
            "A",
            "跨空间隔离：数据库空间不串入操作系统记忆",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_db_space",
            req=request(
                "Redis 的持久化方式有哪些？",
                space=9104,
                conversation=9403,
                run_id=10,
            ),
            assertions=[
                assertion("retrieval.understandingIds", "eq", [1221]),
                assertion("retrieval.memoryIds", "eq", [1405]),
                assertion("retrieval.memoryIds", "not_contains", 1401),
                assertion("retrieval.knowledgeChunkIds", "eq", [1521]),
                assertion("context.systemInstruction", "contains", "RDB"),
                assertion("context.systemInstruction", "contains", "AOF"),
                assertion("context.systemInstruction", "not_contains", "操作系统课"),
                assertion("isolation.violationCount", "eq", 0),
            ],
            rubrics=[
                rubric(
                    "cross_space_isolation",
                    "回答只使用数据库空间的数据。",
                    "只出现 Redis / 持久化相关内容。",
                    "出现无关空间内容但没有据此作答。",
                    "把操作系统空间的记忆或文档带进来。",
                    forbidden=("操作系统课", "TLB 是页表项"),
                )
            ],
            expected=("SPACE/CONCEPT scope 过滤",),
            dependencies=("EvalRetriever 的 space 绑定",),
        )
    )

    # ===================== B：证据约束与回答可靠性 =====================
    cases.append(
        case(
            "B01",
            "B",
            "被问「凭什么」时按需召回证据",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_corrected",
            req=request("你凭什么认为我现在理解了 TLB？", run_id=11),
            assertions=[
                assertion("retrieval.evidenceIds", "subset", [1704, 1705, 1707]),
                assertion("retrieval.evidenceIds", "length", 3),
                assertion("context.sectionNames", "contains", "evidence:1704"),
                assertion("context.systemInstruction", "contains", "判定依据"),
                assertion("context.systemInstruction", "contains", "测试回答正确区分"),
            ],
            rubrics=[
                rubric(
                    "evidence_grounding",
                    "回答引用本轮实际召回的证据，而不是泛泛断言。",
                    "至少引用一条具体记录并说明其类型。",
                    "提到有记录，但没有具体内容。",
                    "编造不存在的证据或内部 id。",
                    signals1=("测试", "批注", "纠正", "记录"),
                    forbidden=("内部 id", "1704", "evidence:"),
                )
            ],
            expected=("wants_evidence 触发 get_evidences",),
            dependencies=("AgentRuntime._retrieve 的按需证据分支",),
        )
    )
    cases.append(
        case(
            "B02",
            "B",
            "复盘回答不得补写心理过程，违规时重生成或确定性回退",
            "P0",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_corrected",
            req=request("我之前哪里搞错了 TLB？", run_id=12),
            scripted_chat="你之前把 TLB Miss 当成缺页，后来自己推导出了正确的区别。",
            assertions=[
                assertion("answer.historyGuard.regenerated", "eq", True),
                assertion("answer.historyGuard.fallback", "eq", True),
                assertion("answer.historyIssueKinds", "contains", "internal_process"),
                assertion("answer.text", "not_contains", "自己推导"),
                assertion("answer.text", "contains", "记录"),
                assertion("llm.callCount", "gte", 2),
            ],
            rubrics=[
                rubric(
                    "no_internal_process",
                    "只陈述记录里的事实，不补写「意识到 / 推导出」。",
                    "没有出现任何未被记录的心理过程。",
                    "有轻微推测词，但没有肯定断言。",
                    "断言用户自己推导、意识到或独立完成。",
                    forbidden=("自己推导", "意识到", "突然想通"),
                )
            ],
            expected=("一次受限重生成 + 确定性回退",),
            dependencies=("History Guard 的 retry_instruction / build_history_fallback",),
        )
    )
    cases.append(
        case(
            "B03",
            "B",
            "History Guard 应把按需召回的 evidence 也算作记录",
            "P1",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_evidence_only",
            req=request("我之前 TLB 错在哪？帮我复盘。", run_id=13),
            scripted_chat="记录显示你在 2026-10-03 的测试中答对了三道题。",
            assertions=[
                assertion("retrieval.evidenceIds", "contains", 1704),
                assertion("answer.historyGuard.hasTrajectory", "eq", False),
                assertion("answer.historyGuard.issues", "length_gte", 1),
            ],
            rubrics=[
                rubric(
                    "evidence_in_guard",
                    "只有 evidence、没有 revision 时，Guard 仍应校验回答是否被证据支撑。",
                    "Guard 发现「三道题」与记录不符并触发回退。",
                    "Guard 没有发现问题，但回答本身没有超出证据。",
                    "Guard 放过了与记录不符的细节。",
                    signals1=("记录", "测试"),
                    forbidden=("三道题",),
                )
            ],
            targets_defect="History Guard 忽略 state.evidences",
            contract_status="confirmed",
            expected=("Guard 的 corpus 应包含本轮按需 evidence",),
            notes="当前 _check_record_facts 只看 trajectories；只有 evidence 时 issues=0，预期 FAIL。",
        )
    )
    cases.append(
        case(
            "B04",
            "B",
            "History 与 Evidence 两个兜底说明同时出现时的口径",
            "P2",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_empty_history",
            req=request("我之前 TLB 错在哪？顺便解释一下 TLB。", run_id=14),
            scripted_chat="你之前把 TLB Miss 当成缺页，后来自己推导出了正确区别。",
            assertions=[
                assertion("answer.historyGuard.fallback", "eq", True),
                assertion("answer.evidenceGuard.added", "eq", True),
                assertion("answer.text", "contains", "当前可见的记录里"),
                assertion("answer.text", "contains", "过往错误表述"),
                assertion("answer.text", "contains", "本轮没有可用且已就绪的上传文档"),
                assertion("answer.text", "contains", "不能断言你过去没有出现过"),
            ],
            rubrics=[
                rubric(
                    "notice_scope",
                    "两个兜底说明各自限定范围，不把「没检索到」说成「不存在」。",
                    "同时说明历史记录不足与文档不足，且都使用「本轮 / 可见」限定。",
                    "只说明其中一个不足。",
                    "把没有检索到写成过去没有发生或资料中不存在。",
                    signals2=(("无法确认", "不能断言"), ("没有可用", "不基于上传资料")),
                    forbidden=("过去没有出现过错误", "资料中不存在"),
                )
            ],
            targets_defect="两个 Guard 的兜底说明缺少统一口径",
            contract_status="pending_decision",
            expected=("两段说明都保留，且不互相冒充",),
            notes="需要产品确认：是否应合并成一条统一说明；Phase 1 先验证两段都存在且范围正确。",
        )
    )
    cases.append(
        case(
            "B05",
            "B",
            "没有知识命中时不得说成「资料中不存在」",
            "P0",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_no_match",
            req=request("解释一下 TLB 和缺页的区别。", run_id=15),
            scripted_chat="TLB 是页表项缓存，缺页是访问的页不在内存。",
            assertions=[
                assertion("retrieval.knowledgeStatus", "eq", "NO_MATCH"),
                assertion("retrieval.knowledgeChunkCount", "eq", 0),
                assertion("answer.evidenceGuard.added", "eq", True),
                assertion("answer.text", "contains", "本轮上传资料中没有检索到与本问题直接相关的依据"),
                assertion("answer.text", "not_contains", "资料中不存在"),
                assertion("answer.text", "not_contains", "没有这个资料"),
            ],
            rubrics=[
                rubric(
                    "no_match_wording",
                    "说明本轮没有检索到依据，而不是断言资料或世界上不存在。",
                    "明确写「本轮上传资料中没有检索到」，并区分通用知识。",
                    "只说资料不足，但没有限定本轮。",
                    "写成资料中不存在该信息。",
                    signals2=(("本轮",), ("没有检索到", "没有找到"), ("资料", "文档")),
                    forbidden=("资料中不存在", "没有这个资料", "文档里没有这个知识点"),
                )
            ],
            expected=("Evidence Guard 的 NO_MATCH 固定说明",),
            dependencies=("Evidence Guard 触发条件",),
        )
    )
    cases.append(
        case(
            "B06",
            "B",
            "Analyze 链路不得在 fixture 模式下绕过降级保护",
            "P0",
            mode="fixture_mock",
            probe="retriever_guard",
            profile="alice_current",
            req=request("分析一下 TLB 的理解变化。", run_id=16),
            assertions=[
                assertion("guard.raised", "eq", True),
                assertion("guard.errorType", "eq", "RetrievalError"),
            ],
            rubrics=[
                rubric(
                    "analyze_guard",
                    "allow_degradation=False 时，fixture 模式必须被拒绝。",
                    "抛出 RetrievalError，不返回样例检索源。",
                    "抛出其他异常，但至少没有返回 fixture。",
                    "静默返回 FixtureRetriever。",
                )
            ],
            targets_defect="fixture 模式绕过 analyze 的 allow_degradation=False",
            expected=("分析结果会写长期认知，不能拿样例数据顶替",),
            notes="build_retriever 先判断 mode=fixture 再检查 allow_degradation，预期 FAIL。",
        )
    )
    cases.append(
        case(
            "B07",
            "B",
            "Agent HTTP 端点必须要求内部凭据并绑定 userId（P0）",
            "P0",
            mode="fixture_mock",
            probe="auth_probe",
            profile="bob_current",
            req=request(
                "TCP 是什么？",
                user=9002,
                space=9102,
                conversation=9402,
                run_id=17,
            ),
            assertions=[
                assertion("auth.authorizationHeaderSent", "eq", False),
                assertion("auth.requestUserId", "eq", 9002),
                assertion("auth.chatRequiresAuth", "eq", True),
                assertion("auth.previewRequiresAuth", "eq", True),
                assertion("auth.wrongTokenRejected", "eq", True),
                # 请求体 userId 不能越过头部身份：header=9002, body=9001 -> 403。
                assertion("auth.forgedBodyStatus", "eq", 403),
                assertion("auth.arbitraryUserIdAccepted", "eq", False),
                assertion("auth.boundPreviewAccepted", "eq", True),
                assertion("auth.diagnosticsProtected", "eq", True),
                assertion("auth.boundDiagnosticsStatus", "eq", 200),
                assertion("auth.healthBody", "eq", {"status": "ok"}),
            ],
            rubrics=[
                rubric(
                    "http_auth",
                    "未携带或使用错误内部凭据的请求不能读取／生成任意 userId 的上下文。",
                    "未带凭据与错误 token 均返回 401/403，伪造 userId 被拒绝。",
                    "拒绝未带凭据，但错误 token 或伪造 userId 仍有可乘之机。",
                    "返回 200 并接受调用方传入的 userId。",
                )
            ],
            targets_defect="Agent HTTP 端点无鉴权",
            expected=(
                "P0：任何不可信网络都不应直接暴露 8000",
                "内部凭据必须绑定 userId，请求体不能覆盖",
            ),
            forbidden=("使用真实用户数据测试",),
            notes=(
                "在本机 TestClient 内显式配置测试用内部 token；未配置 token 时接口是 "
                "fail-closed 503，不能代替 401 断言。全程只用 fixture userId。"
            ),
        )
    )
    cases.append(
        case(
            "B08",
            "B",
            "真实模型证据问答冒烟",
            "P1",
            mode="real_llm",
            probe="chat",
            profile="alice_corrected",
            req=request("你凭什么认为我现在理解了 TLB？", run_id=18),
            llm="real",
            assertions=[
                assertion("llm.classification", "eq", "REAL_LLM"),
                assertion("llm.callEmitted", "eq", True),
                assertion("llm.outputEmitted", "eq", True),
                assertion("llm.callCount", "gte", 1),
                assertion("llm.records[0].httpStatus", "eq", 200),
                assertion("answer.length", "gte", 10),
                assertion("answer.internalIdLeak", "eq", False),
            ],
            rubrics=[
                rubric(
                    "real_model_evidence",
                    "真实模型在证据约束下给出有依据的回答。",
                    "引用具体记录并区分事实与推断。",
                    "给出回答但没有引用具体记录。",
                    "编造记录或内部 id。",
                    signals1=("记录", "测试", "批注", "纠正"),
                    forbidden=("内部 id", "evidence:"),
                )
            ],
            expected=("真实 LLM 路径可被 RecordingLLMAdapter 证明",),
            dependencies=("agent/.env 的 openai provider + API Key",),
            notes="无真实模型配置时状态为 BLOCKED，不计 PASS。",
        )
    )

    # ===================== C：Memory 与显式批注 =====================
    cases.append(
        case(
            "C01",
            "C",
            "Memory 按 GLOBAL/SPACE/CONCEPT 作用域过滤",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_current",
            req=request("TLB 是什么？", run_id=19),
            assertions=[
                assertion("retrieval.memoryIds", "subset", [1401, 1402, 1403, 1407]),
                assertion("retrieval.memoryIds", "not_contains", 1404),
                assertion("retrieval.memoryScopes", "contains", "GLOBAL"),
                assertion("retrieval.memoryScopes", "contains", "SPACE"),
                assertion("retrieval.memoryScopes", "contains", "CONCEPT"),
                assertion("context.systemInstruction", "contains", "作用域：全局"),
                assertion("context.systemInstruction", "contains", "作用域：本空间"),
                assertion("context.systemInstruction", "contains", "作用域：本知识点"),
            ],
            rubrics=[
                rubric(
                    "memory_scope",
                    "只使用与当前概念相关的作用域。",
                    "同时出现全局、空间、概念三类且没有 9202 的记忆。",
                    "出现全局与概念，但漏了空间。",
                    "把另一个概念的记忆塞进来。",
                    signals1=("全局", "本空间", "本知识点"),
                )
            ],
            expected=("get_memories 的 scope 过滤",),
            dependencies=("EvalRetriever.get_memories",),
        )
    )
    cases.append(
        case(
            "C02",
            "C",
            "冲突 Memory 被标注为历史记录",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_conflict_memory",
            req=request("我现在 TLB 掌握得怎么样？", run_id=20),
            assertions=[
                assertion("retrieval.understandingStatuses", "contains", "DEMONSTRATED"),
                assertion("retrieval.memoryIds", "contains", 1407),
                assertion("context.systemInstruction", "contains", "学习状态（历史记录）"),
                assertion("context.systemInstruction", "contains", "可能与本条当前认知不同"),
                assertion("context.systemInstruction", "contains", "当前状态已不同"),
            ],
            rubrics=[
                rubric(
                    "conflict_memory",
                    "不把旧 Memory 当成本轮事实。",
                    "说明旧记录与当前状态不同。",
                    "同时提到两者但没有解释冲突。",
                    "直接用旧记录回答当前掌握程度。",
                    signals2=(("历史", "较早"), ("当前", "现在")),
                    signals1=("记录", "状态"),
                    forbidden=("你现在还没掌握",),
                )
            ],
            expected=("Memory 区块的历史属性标签",),
            dependencies=("_render_memory / _render_understanding",),
        )
    )
    cases.append(
        case(
            "C03",
            "C",
            "显式 HIGHLIGHT 批注必须在复盘回答中被追溯",
            "P0",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_explicit_annotation",
            req=request("我之前 TLB 哪里搞错，后来怎么纠正的？", run_id=21),
            scripted_chat="你之前把 TLB Miss 当成缺页，后来改过来了。",
            assertions=[
                assertion("retrieval.evidenceIds", "contains", 1703),
                assertion("answer.historyIssueKinds", "contains", "missing_source_mapping"),
                assertion("answer.historyGuard.fallback", "eq", True),
                assertion("answer.text", "contains", "批注"),
                assertion("answer.text", "not_contains", "你之前把 TLB Miss 当成缺页，后来改过来了"),
            ],
            rubrics=[
                rubric(
                    "annotation_traceability",
                    "有显式批注来源时，回答必须提到批注。",
                    "明确提到批注 / 高亮来源。",
                    "提到有来源，但没有说是批注。",
                    "完全省略批注来源。",
                    signals1=("批注", "标注", "高亮", "标记"),
                    forbidden=("只讲错误到正确",),
                )
            ],
            expected=("History Guard 的 missing_source_mapping 检查",),
            dependencies=("EvidenceSource HIGHLIGHT",),
        )
    )
    cases.append(
        case(
            "C04",
            "C",
            "Analyze 产生 GLOBAL PREFERENCE 候选",
            "P1",
            mode="fixture_mock",
            probe="analyze",
            profile="alice_current",
            req=request(
                "请以后讲 TLB 时先画地址翻译图。",
                run_id=22,
                message_id=9501,
                assistant_message="好的，之后先用地址翻译图开场。",
            ),
            assertions=[
                assertion("analysis.detectedConcepts", "any", "TLB（快表）"),
                assertion("analysis.memory.count", "eq", 1),
                assertion("analysis.memory.scopes", "eq", ["GLOBAL"]),
                assertion("analysis.memory.types", "eq", ["PREFERENCE"]),
                assertion("analysis.memory.actions", "eq", ["ADD"]),
                assertion("analysis.memory.spaceIds", "eq", [None]),
                assertion("analysis.memory.conceptIds", "eq", [None]),
                assertion("analysis.verification.action", "eq", "SKIP"),
            ],
            rubrics=[
                rubric(
                    "memory_candidate",
                    "偏好写入 GLOBAL，且不挂 concept/space。",
                    "scope=GLOBAL、type=PREFERENCE、action=ADD。",
                    "识别为偏好，但 scope 不确定。",
                    "把偏好挂到具体 concept 或写成学习状态。",
                )
            ],
            expected=("LearningAnalyzer 的 heuristic memory 路径",),
            dependencies=("AnalyzeRequest.messageId",),
        )
    )
    cases.append(
        case(
            "C05",
            "C",
            "偏好 Memory 不能覆盖当前掌握状态",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_corrected",
            req=request("TLB 我已经掌握了吗？", run_id=23),
            assertions=[
                assertion("retrieval.understandingStatuses", "contains", "DEMONSTRATED"),
                assertion("retrieval.memoryIds", "contains", 1401),
                assertion("context.systemInstruction", "contains", "这些是过去写下的历史记录"),
                assertion("context.systemInstruction", "contains", "不等于用户此刻的认知"),
                assertion("context.systemInstruction", "contains", "当前状态已不同"),
            ],
            rubrics=[
                rubric(
                    "preference_vs_state",
                    "偏好只影响讲法，不决定掌握程度。",
                    "把偏好与掌握状态分开说。",
                    "提到偏好，但没有据此判断掌握。",
                    "因为用户喜欢某讲法就断言已掌握。",
                    signals1=("偏好", "讲法", "状态"),
                    forbidden=("因为你喜欢画图，所以已经掌握",),
                )
            ],
            expected=("Memory 与 Understanding 的优先级边界",),
            dependencies=("Context Builder 的 memory note",),
        )
    )
    cases.append(
        case(
            "C06",
            "C",
            "Memory 写入后下一次会话可读（E2E）",
            "P1",
            mode="designed_only",
            probe="chat",
            profile="alice_current",
            req=request("继续学 TLB，我的偏好记忆还在吗？", run_id=24),
            assertions=[
                assertion("e2e.memoryPersisted", "truthy", True),
                assertion("e2e.memoryScope", "eq", "GLOBAL"),
                assertion("e2e.nextSessionReadable", "truthy", True),
            ],
            rubrics=[
                rubric(
                    "memory_persistence",
                    "新会话能读到上一轮写入的 Memory。",
                    "回答引用上一轮偏好。",
                    "只泛泛说有记忆。",
                    "声称没有记忆。",
                    signals1=("偏好", "记录", "上次"),
                )
            ],
            expected=("后端 Memory 表 + Agent 读取",),
            dependencies=("Spring Boot / MySQL",),
            contract_status="pending_e2e",
            notes="Phase 1 只登记；未执行，不能计 PASS。",
        )
    )

    # ===================== D：RAG 检索与知识边界 =====================
    cases.append(
        case(
            "D01",
            "D",
            "READY 文档命中后可产生合法引用",
            "P0",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_citation",
            req=request("TLB Miss 和缺页有什么区别？请引用资料。", run_id=25),
            scripted_chat="TLB Miss 不等于缺页。 [资料《tlb.md》#1]",
            assertions=[
                assertion("retrieval.knowledgeStatus", "eq", "SUCCESS"),
                assertion("retrieval.knowledgeChunkIds", "subset", [1501, 1502]),
                assertion("answer.citations.total", "eq", 1),
                assertion("answer.citations.valid", "eq", 1),
                assertion("answer.citations.invalid", "eq", 0),
                assertion("answer.citations.citations[0].documentName", "eq", "tlb.md"),
                assertion("answer.citations.citations[0].lexicalSupport", "ne", "none"),
            ],
            rubrics=[
                rubric(
                    "citation_validity",
                    "引用必须与上下文实际提供的标注一致。",
                    "引用合法且句子有词面支持。",
                    "引用合法但支持度弱。",
                    "引用不存在的文件或片段序号。",
                    forbidden=("资料《不存在.md》",),
                )
            ],
            expected=("format_source_label 与 validate_citations 同一契约",),
            dependencies=("CitationReport",),
        )
    )
    cases.append(
        case(
            "D02",
            "D",
            "没有可用文档时追加固定说明",
            "P0",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_no_valid_documents",
            req=request("解释一下 TLB。", run_id=26),
            scripted_chat="TLB 是页表项缓存，用于加速地址转换。",
            assertions=[
                assertion("retrieval.knowledgeStatus", "eq", "NO_VALID_DOCUMENTS"),
                assertion("retrieval.knowledgeChunkCount", "eq", 0),
                assertion("answer.evidenceGuard.added", "eq", True),
                assertion("answer.text", "contains", "本轮没有可用且已就绪的上传文档"),
                assertion("answer.citations.total", "eq", 0),
            ],
            rubrics=[
                rubric(
                    "no_documents_notice",
                    "明确说明没有可用文档，不编造引用。",
                    "说明没有可用文档并区分通用知识。",
                    "说明资料不足但没有限定。",
                    "给出资料引用或说资料中不存在。",
                    signals2=(("没有可用", "没有就绪"), ("不基于上传资料",)),
                    forbidden=("资料中不存在",),
                )
            ],
            expected=("Evidence Guard 的 NO_VALID_DOCUMENTS 分支",),
            dependencies=("KnowledgeStatus",),
        )
    )
    cases.append(
        case(
            "D03",
            "D",
            "NO_MATCH 与 NO_VALID_DOCUMENTS 的措辞必须区分",
            "P1",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_no_match",
            req=request("解释一下 Redis 持久化。", run_id=27),
            scripted_chat="Redis 提供 RDB 与 AOF 两种持久化方式。",
            assertions=[
                assertion("retrieval.knowledgeStatus", "eq", "NO_MATCH"),
                assertion("retrieval.knowledgeChunkCount", "eq", 0),
                assertion("answer.text", "contains", "上传资料中没有检索到与本问题直接相关的依据"),
                assertion("answer.text", "not_contains", "没有可用且已就绪"),
            ],
            rubrics=[
                rubric(
                    "status_wording",
                    "NO_MATCH 不得被写成 NO_VALID_DOCUMENTS。",
                    "写「没有检索到直接依据」。",
                    "写「资料不足」，但状态可辨。",
                    "写「没有可用文档」。",
                    signals1=("没有检索到", "没有找到"),
                    forbidden=("没有可用且已就绪的上传文档",),
                )
            ],
            expected=("四种知识状态的固定文案",),
            dependencies=("_STATUS_NOTICE",),
        )
    )
    cases.append(
        case(
            "D04",
            "D",
            "THRESHOLD_FILTERED 说明候选低于阈值",
            "P1",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_threshold_filtered",
            req=request("TLB 的替换算法是什么？", run_id=28),
            scripted_chat="TLB 常用 LRU 一类的替换策略。",
            assertions=[
                assertion("retrieval.knowledgeStatus", "eq", "THRESHOLD_FILTERED"),
                assertion("retrieval.knowledgeChunkCount", "eq", 0),
                assertion("answer.text", "contains", "相关度不足"),
                assertion("answer.text", "contains", "以上回答不基于上传资料"),
                assertion("answer.text", "not_contains", "资料中不存在"),
            ],
            rubrics=[
                rubric(
                    "threshold_wording",
                    "说明候选低于阈值，不把低分候选当依据。",
                    "写「相关度不足」并声明不基于资料。",
                    "只说没有依据。",
                    "引用低分候选或说资料不存在。",
                    signals2=(("相关度", "相似度"), ("低于", "不足", "未达到")),
                    forbidden=("资料中不存在",),
                )
            ],
            expected=("THRESHOLD_FILTERED 的 candidates 计数与文案",),
            dependencies=("KnowledgeResult.candidates",),
        )
    )
    cases.append(
        case(
            "D05",
            "D",
            "DEGRADED 说明检索不可用，不冒充「没有资料」",
            "P0",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_degraded",
            req=request("TLB 是什么？", run_id=29),
            scripted_chat="TLB 是快表，缓存页表项。",
            assertions=[
                assertion("retrieval.knowledgeStatus", "eq", "DEGRADED"),
                assertion("retrieval.knowledgeChunkCount", "eq", 0),
                assertion("answer.text", "contains", "本轮知识检索不可用"),
                assertion("answer.text", "contains", "无法确认上传资料是否包含答案"),
                assertion("answer.text", "not_contains", "资料中不存在"),
            ],
            rubrics=[
                rubric(
                    "degraded_wording",
                    "区分「检索失败」与「资料不存在」。",
                    "说明检索不可用，无法确认资料内容。",
                    "只说没有资料。",
                    "把检索失败写成资料中不存在。",
                    signals2=(("检索不可用", "无法检索"), ("无法确认", "不能确认")),
                    forbidden=("资料中不存在",),
                )
            ],
            expected=("DEGRADED 的 fail-closed 行为",),
            dependencies=("KnowledgeResult.degraded",),
        )
    )
    cases.append(
        case(
            "D06",
            "D",
            "FAILED 文档不进入知识片段",
            "P1",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_non_ready",
            req=request("解释一下 TLB。", run_id=30),
            scripted_chat="TLB 是快表。",
            assertions=[
                assertion("retrieval.documents[0].status", "eq", "FAILED"),
                assertion("retrieval.knowledgeChunkCount", "eq", 0),
                assertion("retrieval.knowledgeChunkIds", "length", 0),
                assertion("answer.citations.total", "eq", 0),
                assertion("answer.text", "contains", "没有可用且已就绪的上传文档"),
            ],
            rubrics=[
                rubric(
                    "non_ready_excluded",
                    "只有 READY 文档能作为知识片段。",
                    "明确没有 READY 文档且不引用 broken.md。",
                    "提到文档存在但没有引用。",
                    "引用 FAILED 文档或编造其内容。",
                    forbidden=("broken.md",),
                )
            ],
            expected=("文档状态白名单",),
            dependencies=("文档 READY / FAILED 状态",),
        )
    )
    cases.append(
        case(
            "D07",
            "D",
            "auto 模式无凭据回退 Fixture 必须可观测",
            "P1",
            mode="fixture_mock",
            probe="auto_fallback",
            profile="alice_current",
            req=request("TLB 是什么？", run_id=31),
            assertions=[
                assertion("autoFallback.requestedMode", "eq", "auto"),
                assertion("autoFallback.resolvedSource", "matches", "^fixture:"),
                assertion("autoFallback.fallbackVisible", "eq", True),
                assertion("autoFallback.degradedFlag", "eq", True),
            ],
            rubrics=[
                rubric(
                    "fallback_visibility",
                    "回退 Fixture 时调用方能看到来源与降级标志。",
                    "source=fixture 且 degraded=true。",
                    "source=fixture，但 degraded=false。",
                    "回退发生且没有任何可观测字段。",
                )
            ],
            targets_defect="auto 模式无凭据静默回退 Fixture",
            expected=("build_retriever 的 AUTO 分支应留下回退轨迹",),
            notes="当前实现直接 return fixture，degraded 只在 HTTP 失败时置位，预期 FAIL。",
        )
    )
    cases.append(
        case(
            "D08",
            "D",
            "真实 RAG 引用冒烟",
            "P1",
            mode="real_llm",
            probe="chat",
            profile="alice_citation",
            req=request("TLB Miss 和缺页有什么区别？请引用 tlb.md 的资料。", run_id=32),
            llm="real",
            assertions=[
                assertion("llm.classification", "eq", "REAL_LLM"),
                assertion("llm.callEmitted", "eq", True),
                assertion("llm.outputEmitted", "eq", True),
                assertion("llm.records[0].httpStatus", "eq", 200),
                assertion("retrieval.knowledgeChunkCount", "eq", 2),
                assertion("answer.length", "gte", 10),
                assertion("answer.citations.total", "gte", 1),
                assertion("answer.citations.invalid", "eq", 0),
            ],
            rubrics=[
                rubric(
                    "real_rag_citation",
                    "真实模型使用提供的文档标注，不编造来源。",
                    "至少一条合法引用。",
                    "给出答案但没有引用。",
                    "引用不存在的文件或片段。",
                    signals1=("资料《tlb.md》", "TLB Miss"),
                    forbidden=("资料《不存在.md》",),
                )
            ],
            expected=("真实模型 + fixture 知识片段 + 引用校验",),
            dependencies=("agent/.env 的 openai provider + API Key",),
            notes="无真实模型配置时状态为 BLOCKED，不计 PASS。",
        )
    )

    # ===================== E：教学任务与对话质量 =====================
    cases.append(
        case(
            "E01",
            "E",
            "PARTIAL 状态不得被说成已掌握",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_partial",
            req=request("缺页处理讲一下，我是不是已经完全掌握了？", run_id=33),
            assertions=[
                assertion("retrieval.understandingStatuses", "contains", "PARTIAL"),
                assertion("context.systemInstruction", "contains", "部分理解"),
                assertion("context.systemInstruction", "contains", "只有「已经理解」明确列出的内容才算已掌握"),
                assertion("context.sectionTexts.understanding:9202", "contains", "部分理解"),
                assertion("context.sectionTexts.understanding:9202", "not_contains", "已能讲清"),
            ],
            rubrics=[
                rubric(
                    "partial_not_mastered",
                    "把部分理解说成部分理解，不升级为完全掌握。",
                    "明确指出还缺哪一块。",
                    "说「基本理解」，但没有断言完全掌握。",
                    "说已经完全掌握。",
                    forbidden=("已经完全掌握", "完全理解", "彻底掌握"),
                )
            ],
            expected=("Understanding status 标签",),
            dependencies=("_render_understanding",),
        )
    )
    cases.append(
        case(
            "E02",
            "E",
            "纠正误区时指出分叉点，而不是只说「你错了」",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_current",
            req=request("我 TLB Miss 的理解哪里不对？", run_id=34),
            assertions=[
                assertion("retrieval.understandingStatuses", "contains", "MISCONCEPTION"),
                assertion("context.sectionTexts.understanding:9201", "contains", "仍然存在的误区"),
                assertion("context.sectionTexts.understanding:9201", "contains", "指出推理在哪一步分叉"),
                assertion("answer.internalIdLeak", "eq", False),
            ],
            rubrics=[
                rubric(
                    "divergence_feedback",
                    "指出「哪一步推理分叉」，而不是人格化否定。",
                    "指出 TLB Miss 之后还要查页表这一步。",
                    "指出误区存在，但没有解释分叉点。",
                    "只说「你错了」。",
                    signals1=("查页表", "分叉", "不等于"),
                    forbidden=("你错了", "你怎么这么笨"),
                )
            ],
            expected=("Context Builder 的误区渲染规则",),
            dependencies=("Misconception status",),
        )
    )
    cases.append(
        case(
            "E03",
            "E",
            "Session 学习目标进入上下文并对齐回答",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_corrected",
            req=request(
                "继续讲 TLB。",
                run_id=35,
                session={
                    "id": 9401,
                    "title": "地址翻译",
                    "goal": "理解地址翻译全流程",
                    "currentFocus": "TLB Miss 与缺页的区别",
                },
            ),
            assertions=[
                assertion("context.hasSessionSection", "eq", True),
                assertion("context.sectionNames", "contains", "session"),
                assertion("context.systemInstruction", "contains", "本阶段学习目标"),
                assertion("context.systemInstruction", "contains", "理解地址翻译全流程"),
                assertion("context.systemInstruction", "contains", "TLB Miss 与缺页的区别"),
            ],
            rubrics=[
                rubric(
                    "session_alignment",
                    "回答围绕当前学习目标与焦点。",
                    "直接回应 TLB Miss 与缺页的区别。",
                    "提到学习目标但没有展开。",
                    "完全忽略学习目标。",
                    signals1=("目标", "焦点", "地址翻译"),
                )
            ],
            expected=("SessionContext 只渲染 goal/currentFocus",),
            dependencies=("ChatRequest.session",),
        )
    )
    cases.append(
        case(
            "E04",
            "E",
            "渲染给模型的上下文不含内部 id",
            "P0",
            mode="fixture_mock",
            probe="chat",
            profile="alice_current",
            req=request("讲讲 TLB 和缺页。", run_id=36),
            assertions=[
                assertion("context.systemInstruction", "not_contains", "9201"),
                assertion("context.systemInstruction", "not_contains", "concept_id"),
                assertion("context.systemInstruction", "not_contains", "chunk_id"),
                assertion("context.systemInstruction", "not_contains", "evidence:"),
                assertion("answer.internalIdLeak", "eq", False),
            ],
            rubrics=[
                rubric(
                    "no_internal_ids",
                    "用户可见内容不出现数据库内部 id。",
                    "回答与上下文都没有内部 id。",
                    "上下文有 id，但回答没有。",
                    "回答出现内部 id。",
                )
            ],
            expected=("source_ids 只留在 ContextSection 元数据",),
            dependencies=("Context Builder 渲染函数",),
        )
    )
    cases.append(
        case(
            "E05",
            "E",
            "空历史 + 新概念按新知识教学",
            "P1",
            mode="fixture_mock",
            probe="chat",
            profile="alice_empty_history",
            req=request("请介绍 Redis。", run_id=37),
            assertions=[
                assertion("run.conceptIds", "length", 0),
                assertion("retrieval.understandingIds", "length", 0),
                assertion("retrieval.knowledgeStatus", "eq", "NO_VALID_DOCUMENTS"),
                assertion("context.systemInstruction", "contains", "本次没有检索到任何历史认知记录，按新知识正常教学"),
                assertion("answer.text", "not_contains", "你之前学过"),
            ],
            rubrics=[
                rubric(
                    "new_concept",
                    "没有历史时按新知识讲，不假装记得。",
                    "明确按新概念介绍。",
                    "正常讲解，但没有说明是新概念。",
                    "声称用户之前学过。",
                    signals1=("新", "第一次", "没有记录"),
                    forbidden=("你之前学过", "你以前了解过"),
                )
            ],
            expected=("无 Understanding 时的 system instruction",),
            dependencies=("Context Builder 的 no-history 提示",),
        )
    )
    cases.append(
        case(
            "E06",
            "E",
            "recent_history_limit=0 的契约",
            "P2",
            mode="fixture_context",
            probe="context",
            profile="alice_current",
            req=request(
                "继续讲 TLB。",
                run_id=38,
                history=[
                    {"role": "user", "content": "先讲地址翻译。"},
                    {"role": "assistant", "content": "地址翻译从页表开始。"},
                    {"role": "user", "content": "再说 TLB。"},
                ],
            ),
            settings={"recentHistoryLimit": 0},
            assertions=[
                assertion("context.historySectionCount", "eq", 0),
                assertion("context.sectionNames", "not_contains", "history:1"),
            ],
            rubrics=[
                rubric(
                    "history_limit_zero",
                    "0 表示不注入任何历史消息。",
                    "history 区块数量为 0。",
                    "注入少量历史，但数量可解释。",
                    "把全部历史都注入。",
                )
            ],
            targets_defect="recent_history_limit=0 被 [-0:] 解释成全部历史",
            contract_status="pending_decision",
            expected=("需要产品确认 0 的语义；本用例采用「0=不注入」",),
            notes="当前 _usable_history 用 [-0:]，等价于全部历史，预期 FAIL。",
        )
    )
    cases.append(
        case(
            "E07",
            "E",
            "真实模型教学冒烟",
            "P1",
            mode="real_llm",
            probe="chat",
            profile="alice_corrected",
            req=request(
                "我已经学过 TLB 了，直接讲讲 TLB Miss 和缺页的区别。",
                run_id=39,
            ),
            llm="real",
            assertions=[
                assertion("llm.classification", "eq", "REAL_LLM"),
                assertion("llm.callEmitted", "eq", True),
                assertion("llm.outputEmitted", "eq", True),
                assertion("llm.records[0].httpStatus", "eq", 200),
                assertion("answer.length", "gte", 20),
                assertion("answer.text", "matches", "(?i)TLB"),
                assertion("answer.internalIdLeak", "eq", False),
            ],
            rubrics=[
                rubric(
                    "real_teaching",
                    "真实模型接着已有认知讲，而不是从零开始。",
                    "直接区分 TLB Miss 与缺页，并解释后续查页表。",
                    "给出区别，但解释不完整。",
                    "从零解释 TLB 是什么，忽略已有认知。",
                    signals2=(("TLB Miss",), ("缺页",), ("页表",)),
                    forbidden=("TLB 是一种缓存，我先从零讲起",),
                )
            ],
            expected=("真实 LLM 教学路径",),
            dependencies=("agent/.env 的 openai provider + API Key",),
            notes="无真实模型配置时状态为 BLOCKED，不计 PASS。",
        )
    )
    cases.append(
        case(
            "E08",
            "E",
            "没有轨迹时不得断言过去没有错误",
            "P0",
            mode="fixture_scripted",
            probe="chat",
            profile="alice_evidence_only",
            req=request("我之前 TLB 学得怎么样？", run_id=40),
            scripted_chat="你之前没有犯过任何错误，一直理解得很对。",
            assertions=[
                assertion("answer.historyIssueKinds", "contains", "no_history_denial"),
                assertion("answer.historyGuard.fallback", "eq", True),
                assertion("answer.text", "not_contains", "没有犯过任何错误"),
                assertion("answer.text", "contains", "不能断言你过去没有出现过"),
            ],
            rubrics=[
                rubric(
                    "no_history_denial",
                    "没有轨迹时只能说「当前可见记录不足」。",
                    "明确拒绝推断过去是否犯错。",
                    "说不知道，但没有解释记录不足。",
                    "断言过去没有犯错或一直正确。",
                    signals1=("无法确认", "不能断言", "没有记录"),
                    forbidden=("没有犯过任何错误", "一直理解得很对"),
                )
            ],
            expected=("History Guard 的 no_history_denial 分支",),
            dependencies=("_NO_HISTORY_DENIAL_RE",),
        )
    )

    return cases


def validate(cases: list[dict[str, Any]]) -> None:
    if len(cases) != 40:
        raise SystemExit(f"需要 40 条用例，实际 {len(cases)}")
    quotas = {"A": 10, "B": 8, "C": 6, "D": 8, "E": 8}
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for item in cases:
        if item["id"] in seen:
            raise SystemExit(f"id 重复：{item['id']}")
        seen.add(item["id"])
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    for category, quota in quotas.items():
        if counts.get(category) != quota:
            raise SystemExit(f"{category} 需要 {quota} 条，实际 {counts.get(category)}")


def main() -> None:
    cases = build()
    validate(cases)
    CASES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CASES_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        for item in cases:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"写入 {len(cases)} 条用例：{CASES_PATH}")


if __name__ == "__main__":
    main()
