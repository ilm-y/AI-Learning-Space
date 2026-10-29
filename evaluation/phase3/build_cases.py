"""从 Phase 1 的 40 条固定用例中构建 Phase 3 的 10 条真实模型语义用例。

原则：

- 不改写 Phase 1 用例文件；Phase 3 另存一份 JSONL，保留 ``sourceCaseId``；
- 硬断言只检查检索/上下文/调用事实和少数确定性禁止表述，不用来冒充语义评分；
- 语义评分维度逐案写锚点，不复用同一套关键词；
- 第 9 条（文档已召回但无法回答）在现有 40 条中没有对应用例，新增 D09，
  其余 9 条全部来自现有固定集。

运行：``python -m evaluation.phase3.build_cases``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SOURCE = Path("docs/testing/agent-evaluation-cases.jsonl")
OUTPUT = Path("docs/testing/phase3-agent-quality-cases.jsonl")

DIMENSIONS = (
    "factual_correctness",
    "task_completion",
    "historical_evidence_use",
    "mastery_calibration",
    "uncertainty_citation_honesty",
    "no_fabricated_personal_history",
)

COMMON_REAL = [
    {"path": "llm.classification", "op": "eq", "value": "REAL_LLM"},
    {"path": "llm.callEmitted", "op": "eq", "value": True, "note": "请求确实发出"},
    {"path": "llm.outputEmitted", "op": "eq", "value": True, "note": "并非空流"},
    {"path": "llm.records[0].httpStatus", "op": "eq", "value": 200},
    {"path": "answer.length", "op": "gte", "value": 20, "note": "至少形成可评审回答"},
    {"path": "answer.internalIdLeak", "op": "eq", "value": False},
    {"path": "isolation.violationCount", "op": "eq", "value": 0},
]


def dim(
    criterion_id: str,
    description: str,
    score2: str,
    score1: str,
    score0: str,
    *,
    applicable: bool = True,
) -> dict[str, Any]:
    return {
        "id": criterion_id,
        "description": description,
        "score2": score2,
        "score1": score1,
        "score0": score0,
        "applicable": applicable,
    }


def dimensions(**overrides: dict[str, Any]) -> list[dict[str, Any]]:
    """按固定六维顺序返回；未覆盖的维度用 N/A（必须写明不适用的理由）。"""
    result: list[dict[str, Any]] = []
    for name in DIMENSIONS:
        if name in overrides:
            result.append(dim(name, **overrides[name]))
        else:
            result.append(
                dim(
                    name,
                    "本案例不涉及该维度。",
                    "不适用。",
                    "不适用。",
                    "不适用。",
                    applicable=False,
                )
            )
    return result


CASE_SPECS: dict[str, dict[str, Any]] = {
    "A01": {
        "requirements": [1, 2, 4],
        "hard": [
            {"path": "run.historyReviewRequested", "op": "eq", "value": True},
            {
                "path": "retrieval.trajectoryRevisionNos",
                "op": "subset",
                "value": [1, 2, 3],
            },
            {
                "path": "retrieval.trajectoryEvidenceIds",
                "op": "subset",
                "value": [1701, 1702, 1703],
            },
            {
                "path": "retrieval.understandingStatuses",
                "op": "contains",
                "value": "DEMONSTRATED",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "已纠正，历史",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "把 TLB Miss 当成缺页异常",
            },
            {"path": "context.falseNoHistoryClaim", "op": "eq", "value": False},
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "事实：用户过去把 TLB Miss 当成缺页，后来已纠正；TLB Miss 与缺页不是同一事件。",
                "score2": "明确区分 TLB Miss 与缺页，并说明旧误区已被纠正。",
                "score1": "基本事实正确，但没有把旧误区与当前理解分开。",
                "score0": "把 TLB Miss 说成就是缺页，或说旧误区仍是当前理解。",
            },
            task_completion={
                "description": "用户要求复盘“之前哪里搞错了”。",
                "score2": "指出具体误区（把 TLB Miss 当缺页）与纠正后的正确关系。",
                "score1": "只说“之前有误解”，没有指出具体分叉点。",
                "score0": "没有复盘旧误区，或只泛泛讲 TLB。",
            },
            historical_evidence_use={
                "description": "只能使用提供的 revision/evidence 记录，不得补写未记录的经历。",
                "score2": "复盘内容与检索到的 revision/evidence 一致，并明确这是记录中的历史。",
                "score1": "提到历史，但细节无法与记录对应。",
                "score0": "编造记录中不存在的错误过程或证据。",
            },
            mastery_calibration={
                "description": "当前状态是 DEMONSTRATED，但不能超出记录宣称完全掌握。",
                "score2": "说明当前已能区分，但把结论限定在已记录的范围。",
                "score1": "说“已经掌握”，没有限定证据范围。",
                "score0": "仍说用户当前不懂，或宣称已经完全掌握所有相关内容。",
            },
            uncertainty_citation_honesty={
                "description": "没有记录的部分应承认不确定；引用只能指向提供的资料。",
                "score2": "明确区分记录内事实与未记录内容，不虚构引用。",
                "score1": "有保留语气，但边界不清。",
                "score0": "把未记录内容说成事实，或引用不存在的资料。",
            },
            no_fabricated_personal_history={
                "description": "不得补写“自己意识到 / 推导出 / 别人教会”等未记录的心理或外部过程。",
                "score2": "只陈述记录事实，没有补写心理过程。",
                "score1": "有轻微推测词，但没有肯定断言。",
                "score0": "断言用户自己推导、突然想通或独立完成。",
            },
        ),
    },
    "A02": {
        "requirements": [1, 4],
        "hard": [
            {
                "path": "retrieval.understandingStatuses",
                "op": "contains",
                "value": "DEMONSTRATED",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "已能讲清",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "已纠正，历史",
            },
            {
                "path": "context.systemInstruction",
                "op": "not_contains",
                "value": "当前仍存在的误区：把 TLB Miss",
            },
            {"path": "context.falseNoHistoryClaim", "op": "eq", "value": False},
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "过去把 TLB Miss 当缺页是误区；当前理解已纠正。",
                "score2": "明确回答“过去是误区，现在已经纠正”。",
                "score1": "只回答过去或现在的一面。",
                "score0": "把过去误区说成当前状态，或否认过去的误区。",
            },
            task_completion={
                "description": "用户同时问“之前是不是”和“现在呢”。",
                "score2": "两个时间点都回答，并给出当前正确关系。",
                "score1": "只回答一个时间点。",
                "score0": "没有作答或答非所问。",
            },
            historical_evidence_use={
                "description": "历史误区与当前 Understanding 必须分开引用。",
                "score2": "明确标注旧误区是历史记录，当前状态来自新的 Understanding。",
                "score1": "提到旧记录，但没有说明它已被覆盖。",
                "score0": "拿旧记录当作当前状态，或忽略当前 Understanding。",
            },
            mastery_calibration={
                "description": "当前状态 DEMONSTRATED 不等于“所有相关内容都掌握”。",
                "score2": "当前能讲清，但没有越过记录扩大学会范围。",
                "score1": "语气基本合理，但有“完全掌握”式扩张。",
                "score0": "宣称完全掌握，或仍按误区状态判断。",
            },
            uncertainty_citation_honesty={
                "description": "只依据给定状态回答，不编造额外历史。",
                "score2": "结论均有状态来源，未记录处不臆测。",
                "score1": "大体诚实，但有一处无来源断言。",
                "score0": "编造历史或引用不存在的证据。",
            },
            no_fabricated_personal_history={
                "description": "不得补写用户如何意识到错误。",
                "score2": "只陈述误区与纠正结果。",
                "score1": "有未证实的心理过程描述，但未作为事实。",
                "score0": "断言用户自己发现、自己推导或他人教会。",
            },
        ),
    },
    "A03": {
        "requirements": [4, 6],
        "hard": [
            {
                "path": "retrieval.understandingStatuses",
                "op": "contains",
                "value": "DEMONSTRATED",
            },
            {"path": "retrieval.memoryIds", "op": "contains", "value": 1407},
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "学习状态（历史记录）",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "较早的学习状态记录",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "当前状态已不同",
            },
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "当前状态是 DEMONSTRATED；旧 Memory 1407 是较早的记录，不是当前结论。",
                "score2": "同时说清当前状态与旧记忆的历史属性。",
                "score1": "提到两者，但未说明谁覆盖谁。",
                "score0": "用旧记忆否定当前状态，或说没有旧记忆。",
            },
            task_completion={
                "description": "用户问“现在是什么状态”和“还有旧记忆吗”。",
                "score2": "两个问题都回答，并解释差异。",
                "score1": "只回答其中一个。",
                "score0": "没有回答状态或记忆问题。",
            },
            historical_evidence_use={
                "description": "必须区分 Memory 与 Understanding 两种来源。",
                "score2": "明确指出旧记忆是历史记录，当前 Understanding 已更新。",
                "score1": "说“有旧记录”，但来源区分含糊。",
                "score0": "把 Memory 与 Understanding 混为一谈。",
            },
            mastery_calibration={
                "description": "当前状态为 DEMONSTRATED，不能因旧记忆降级或升级。",
                "score2": "按当前 Understanding 判断，并保留范围限制。",
                "score1": "结论大体正确，但措辞过强或过弱。",
                "score0": "按旧记忆断言当前水平，或宣称完全掌握。",
            },
            uncertainty_citation_honesty={
                "description": "旧记录的时间/来源不确定时应说明。",
                "score2": "说明旧记录较早、与当前不同，不补造时间细节。",
                "score1": "提到历史属性，但补造了未提供的细节。",
                "score0": "虚构旧记忆的内容或时间。",
            },
            no_fabricated_personal_history={
                "description": "不得补写用户形成旧记忆的过程。",
                "score2": "只陈述记录中的旧状态。",
                "score1": "有轻微推测。",
                "score0": "断言用户过去如何学习或为何误解。",
            },
        ),
    },
    "A08": {
        "requirements": [3, 4],
        "hard": [
            {"path": "run.historyReviewRequested", "op": "eq", "value": True},
            {"path": "retrieval.trajectoryConcepts", "op": "length", "value": 0},
            {"path": "retrieval.understandingIds", "op": "length", "value": 0},
            {
                "path": "retrieval.knowledgeStatus",
                "op": "eq",
                "value": "NO_VALID_DOCUMENTS",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "本次没有检索到任何历史认知记录",
            },
            {"path": "answer.text", "op": "not_contains", "value": "你以前一直"},
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "当前没有任何历史记录，不能断言过去学过或犯过错。",
                "score2": "明确说没有可见历史，无法判断过去是否学过/出错。",
                "score1": "说“不知道”，但没有说明记录不足。",
                "score0": "断言用户过去一定学过、没学过或犯过某错。",
            },
            task_completion={
                "description": "用户问“之前学过吗/哪里错了”，需要正面回应两个问题。",
                "score2": "直接说明记录不足，两个问题都无法确认。",
                "score1": "只回应其中一个问题。",
                "score0": "回避问题或给出无依据结论。",
            },
            historical_evidence_use={
                "description": "没有轨迹、Understanding 或 Evidence，必须承认看不见历史。",
                "score2": "明确以“没有检索到记录”为依据拒绝推断。",
                "score1": "承认不知道，但没有指向记录缺失。",
                "score0": "编造历史记录或暗示系统里存在旧记录。",
            },
            mastery_calibration={
                "description": "在无记录时，不得推断当前掌握水平。",
                "score2": "明确无法评估掌握状态。",
                "score1": "语气含糊，但未下掌握结论。",
                "score0": "断言已掌握或完全没学过。",
            },
            uncertainty_citation_honesty={
                "description": "不确定时必须明说，不能用通用知识填补个人历史。",
                "score2": "清楚区分“没有记录”和“没有发生”。",
                "score1": "有保留，但边界不够清楚。",
                "score0": "把无记录说成确定事实。",
            },
            no_fabricated_personal_history={
                "description": "不得虚构学习经历、错误或帮助来源。",
                "score2": "完全没有补写个人历史。",
                "score1": "有轻微假设，但未当事实。",
                "score0": "编造过去的学习/出错/他人帮助经历。",
            },
        ),
    },
    "B02": {
        "requirements": [2, 4],
        "hard": [
            {
                "path": "retrieval.trajectoryEvidenceIds",
                "op": "subset",
                "value": [1701, 1702, 1703],
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "已纠正，历史",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "把 TLB Miss 当成缺页异常",
            },
            {"path": "answer.text", "op": "not_contains", "value": "自己推导"},
            {"path": "answer.text", "op": "not_contains", "value": "突然想通"},
            {"path": "answer.text", "op": "not_contains", "value": "独立完成"},
            {"path": "answer.text", "op": "matches", "value": "(?i)TLB"},
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "旧误区是把 TLB Miss 当缺页；正确关系是 TLB Miss 后查页表，页不在内存才缺页。",
                "score2": "事实完整且关系正确。",
                "score1": "事实基本正确但有细节遗漏。",
                "score0": "核心事实错误。",
            },
            task_completion={
                "description": "用户要求复盘“之前哪里搞错了 TLB”。",
                "score2": "指出具体误区并说明纠正后的关系。",
                "score1": "提到误区但不够具体。",
                "score0": "没有完成复盘。",
            },
            historical_evidence_use={
                "description": "只能引用给定 Evidence，不得补写心理过程。",
                "score2": "复盘内容与 Evidence 对应，未超出记录。",
                "score1": "大体对应，但夹杂未记录细节。",
                "score0": "编造记录外的错误或证据。",
            },
            mastery_calibration={
                "description": "当前状态是已纠正，不能夸大或贬低。",
                "score2": "按记录说明已纠正，范围适度。",
                "score1": "措辞略过度。",
                "score0": "仍说当前错误，或宣称完全掌握。",
            },
            uncertainty_citation_honesty={
                "description": "未记录的部分要承认。",
                "score2": "明确哪些是记录、哪些无法确认。",
                "score1": "有保留但不清晰。",
                "score0": "把推测写成事实。",
            },
            no_fabricated_personal_history={
                "description": "禁止“自己推导 / 突然想通 / 独立完成”等心理过程。",
                "score2": "完全没有未记录的心理或外部过程。",
                "score1": "有轻微推测词，但未肯定断言。",
                "score0": "出现任何被当作事实的心理过程补写。",
            },
        ),
    },
    "B05": {
        "requirements": [8],
        "hard": [
            {"path": "retrieval.knowledgeStatus", "op": "eq", "value": "NO_MATCH"},
            {"path": "retrieval.knowledgeChunkCount", "op": "eq", "value": 0},
            {"path": "answer.evidenceGuard.added", "op": "eq", "value": True},
            {
                "path": "answer.text",
                "op": "contains",
                "value": "本轮上传资料中没有检索到与本问题直接相关的依据",
            },
            {"path": "answer.text", "op": "not_contains", "value": "资料中不存在"},
            {"path": "answer.text", "op": "not_contains", "value": "没有这个资料"},
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "TLB 是页表项缓存；缺页是访问页不在内存。",
                "score2": "两者关系正确，且不把 TLB Miss 等同于缺页。",
                "score1": "大体正确但有细节错误。",
                "score0": "核心事实错误。",
            },
            task_completion={
                "description": "用户要求解释区别。",
                "score2": "给出清晰区别和必要因果。",
                "score1": "只给定义，没有讲清区别。",
                "score0": "没有解释区别。",
            },
            uncertainty_citation_honesty={
                "description": "本轮没有命中资料；应说“本轮未检索到”，而不是“资料中不存在”。",
                "score2": "明确限定本轮、没有命中，并区分通用知识。",
                "score1": "说资料不足，但未限定本轮。",
                "score0": "说资料中不存在该知识，或虚构引用。",
            },
        ),
    },
    "D01": {
        "requirements": [7],
        "hard": [
            {"path": "retrieval.knowledgeStatus", "op": "eq", "value": "SUCCESS"},
            {
                "path": "retrieval.knowledgeChunkIds",
                "op": "subset",
                "value": [1501, 1502],
            },
            {"path": "answer.citations.valid", "op": "gte", "value": 1},
            {"path": "answer.citations.invalid", "op": "eq", "value": 0},
            {
                "path": "answer.citations.citations[0].documentName",
                "op": "eq",
                "value": "tlb.md",
            },
            {
                "path": "answer.citations.citations[0].lexicalSupport",
                "op": "ne",
                "value": "none",
            },
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "TLB Miss 后仍需查页表；只有页表项表明页不在内存才缺页。",
                "score2": "两点都说对，且因果关系清楚。",
                "score1": "区别正确但因果不完整。",
                "score0": "把 TLB Miss 说成缺页，或方向相反。",
            },
            task_completion={
                "description": "用户要求解释区别并引用资料。",
                "score2": "解释完整且给出合法引用。",
                "score1": "解释或引用缺一项。",
                "score0": "没有解释或没有合法引用。",
            },
            uncertainty_citation_honesty={
                "description": "引用必须来自实际提供的 tlb.md 片段，不能编造文件名/序号。",
                "score2": "引用合法且句子有片段支持。",
                "score1": "引用合法但支持较弱。",
                "score0": "引用不存在的文件/片段，或给无来源断言。",
            },
        ),
    },
    "E01": {
        "requirements": [5],
        "hard": [
            {
                "path": "retrieval.understandingStatuses",
                "op": "contains",
                "value": "PARTIAL",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "部分理解",
            },
            {
                "path": "context.systemInstruction",
                "op": "contains",
                "value": "只有「已经理解」明确列出的内容才算已掌握",
            },
            {
                "path": "context.sectionTexts.understanding:9202",
                "op": "contains",
                "value": "部分理解",
            },
            {
                "path": "context.sectionTexts.understanding:9202",
                "op": "not_contains",
                "value": "已能讲清",
            },
            {"path": "answer.text", "op": "not_contains", "value": "已经完全掌握"},
            {"path": "answer.text", "op": "not_contains", "value": "完全理解"},
            {"path": "answer.text", "op": "not_contains", "value": "彻底掌握"},
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "缺页处理的基本过程，以及用户当前是 PARTIAL 而非完整掌握。",
                "score2": "知识解释正确，且明确当前只掌握部分。",
                "score1": "知识正确但状态判断含糊。",
                "score0": "知识错误，或把 PARTIAL 说成已掌握。",
            },
            task_completion={
                "description": "用户要求讲缺页处理并回答“是否完全掌握”。",
                "score2": "两部分都完成，指出还缺什么。",
                "score1": "只完成讲解或只回答状态。",
                "score0": "没有完成任一要求。",
            },
            historical_evidence_use={
                "description": "应使用 Understanding 中的 known/uncertain 部分，而不是泛泛判断。",
                "score2": "引用当前部分理解的具体缺口。",
                "score1": "说“还不完整”，但没有具体缺口。",
                "score0": "忽略 PARTIAL 状态或编造已掌握部分。",
            },
            mastery_calibration={
                "description": "PARTIAL 必须表述为部分理解，不能升级为完全掌握。",
                "score2": "明确说部分理解，并说明未掌握的部分。",
                "score1": "说“基本理解”，但没有断言完全掌握。",
                "score0": "说已经完全掌握/完全理解/彻底掌握。",
            },
            uncertainty_citation_honesty={
                "description": "对未掌握部分应诚实说明不确定。",
                "score2": "明确指出不确定或未覆盖的内容。",
                "score1": "有保留但不具体。",
                "score0": "把不确定内容说成已掌握。",
            },
            no_fabricated_personal_history={
                "description": "不得编造用户过去如何学习缺页处理。",
                "score2": "只依据当前 Understanding。",
                "score1": "有轻微未证实描述。",
                "score0": "编造学习经历或外部帮助。",
            },
        ),
    },
    "E07": {
        "requirements": [10],
        "hard": [
            {"path": "retrieval.knowledgeStatus", "op": "eq", "value": "SUCCESS"},
            {"path": "answer.length", "op": "gte", "value": 50},
            {"path": "answer.text", "op": "matches", "value": "(?i)TLB"},
            {"path": "answer.text", "op": "contains", "value": "缺页"},
            {"path": "answer.citations.invalid", "op": "eq", "value": 0},
        ],
        "rubric": dimensions(
            factual_correctness={
                "description": "TLB Miss 与缺页的区别：前者缓存未命中，后者页不在内存；TLB Miss 后查页表。",
                "score2": "核心事实全部正确。",
                "score1": "主要事实正确，但有一处不精确。",
                "score0": "核心事实错误。",
            },
            task_completion={
                "description": "用户已学过 TLB，要求直接讲区别，不要从零重复。",
                "score2": "直接进入区别与因果，并承接已有认知。",
                "score1": "讲了区别，但有大段从零铺垫。",
                "score0": "没有讲区别或完全跑题。",
            },
            historical_evidence_use={
                "description": "应承接已有 Understanding，而不是把用户当完全新手。",
                "score2": "明确承接已学内容，并只在必要处补基础。",
                "score1": "有承接迹象，但仍有重复。",
                "score0": "忽略已有认知，从头讲 TLB 是什么。",
            },
            mastery_calibration={
                "description": "用户说“学过”，但记录是已纠正状态；不应夸大为全部掌握。",
                "score2": "按已学基础讲解，不宣称全部掌握。",
                "score1": "语气略过度。",
                "score0": "宣称用户已完全掌握，或把用户当零基础。",
            },
            uncertainty_citation_honesty={
                "description": "引用只能来自提供的 tlb.md；没有引用时不得虚构来源。",
                "score2": "有引用则合法，无引用也不编造。",
                "score1": "引用合法但支持弱。",
                "score0": "引用不存在的资料或给无来源的精确断言。",
            },
            no_fabricated_personal_history={
                "description": "不得补写用户过去如何学会。",
                "score2": "没有补写个人历史。",
                "score1": "有轻微推测。",
                "score0": "编造学习过程或他人帮助。",
            },
        ),
    },
}

D09_RUBRIC = dimensions(
    factual_correctness={
        "description": "tlb.md 只讲 TLB 缓存与 TLB Miss/缺页关系，没有讲替换算法（LRU/FIFO）。",
        "score2": "明确说资料没有给出替换算法；若补充通用知识，明确标注为通用知识而非资料内容。",
        "score1": "说资料信息不足，但没有明确资料未覆盖替换算法。",
        "score0": "声称 tlb.md 写了 LRU/FIFO，或把通用知识冒充资料结论。",
    },
    task_completion={
        "description": "用户问“根据 tlb.md，替换算法是 LRU 还是 FIFO”。",
        "score2": "直接回答资料未覆盖，并说明无法据此二选一。",
        "score1": "给出算法讨论，但没有回答资料是否有依据。",
        "score0": "直接选一个算法并声称有资料依据。",
    },
    uncertainty_citation_honesty={
        "description": "资料已召回但问题超出资料范围；应承认边界，不能编造引用。",
        "score2": "明确区分“资料已召回”与“资料能回答的范围”，引用合法或明确不引用。",
        "score1": "承认不确定，但引用/来源边界含糊。",
        "score0": "为超出资料的结论编造引用，或说资料中不存在该主题。",
    },
)


CASE_META: dict[str, dict[str, Any]] = {
    "A01": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101；全部为合成数据",
            "TLB 概念 9201 有 revision 1/2/3，关联 evidence 1701/1702/1703",
            "历史误区“把 TLB Miss 当成缺页异常”已标记为纠正，当前 Understanding 为 DEMONSTRATED",
        ],
        "steps": [
            "单轮：用户问“我之前哪里搞错了 TLB 和缺页？帮我复盘一下。”",
            "AgentRuntime 以 alice_corrected fixture 构建上下文，触发 history review",
            "生成复盘回答；程序校验 revision/evidence/理解状态与内部 id 泄漏",
        ],
    },
    "A02": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101；与 A01 同一份历史",
            "当前 Understanding 为 DEMONSTRATED，旧误区已被覆盖",
        ],
        "steps": [
            "单轮：用户问“我之前是不是把 TLB Miss 当成缺页了？现在呢？”",
            "Context Builder 必须同时提供历史误区与当前快照",
            "程序校验上下文中不再出现“当前仍存在的误区”",
        ],
    },
    "A03": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101",
            "长期 Memory 1407 记录旧 TLB 状态，当前 Understanding 为 DEMONSTRATED",
        ],
        "steps": [
            "单轮：用户问“我现在对 TLB 的理解是什么状态？还有旧记忆吗？”",
            "检索必须同时返回 Memory 1407 与当前 Understanding",
            "上下文需标注“学习状态（历史记录）”“当前状态已不同”",
        ],
    },
    "A08": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101，但使用 alice_empty_history profile",
            "没有 trajectory、没有 Understanding、没有可用知识文档",
        ],
        "steps": [
            "单轮：用户问“我之前学过 TLB 吗？我哪里搞错了？”",
            "AgentRuntime 走 no-history 路径，不构造虚假历史",
            "程序校验上下文明确说明“本次没有检索到任何历史认知记录”",
        ],
    },
    "B02": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101；evidence 1701/1702/1703 与 revision 时间线可用",
            "记录中没有“用户自己推导/突然想通/外部帮助”的内容",
        ],
        "steps": [
            "单轮：用户问“我之前哪里搞错了 TLB？”",
            "真实模型生成复盘；History Guard 检查编造的心理/外部过程",
            "若命中受限问题则一次重生成；仍不通过时使用确定性回退",
        ],
    },
    "B05": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101，使用 alice_no_match profile",
            "知识检索返回 NO_MATCH、0 个 chunk；无可用文档",
        ],
        "steps": [
            "单轮：用户问“解释一下 TLB 和缺页的区别。”",
            "Evidence Guard 应注入“本轮未检索到相关依据”的边界说明",
            "程序校验回答不得说成“资料中不存在/没有这个资料”",
        ],
    },
    "D01": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101，使用 alice_citation profile",
            "tlb.md 已 READY，包含 chunk 1501（TLB 命中）与 1502（TLB Miss/缺页）",
        ],
        "steps": [
            "单轮：用户问“TLB Miss 和缺页有什么区别？请引用资料。”",
            "AgentRuntime 检索 tlb.md 并生成带引用的回答",
            "程序校验引用合法、文档名与词面支持",
        ],
    },
    "E01": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101，使用 alice_partial profile",
            "Understanding 9202 为 PARTIAL：知道缺页基本含义，处理流程未确认",
        ],
        "steps": [
            "单轮：用户问“缺页处理讲一下，我是不是已经完全掌握了？”",
            "Context Builder 必须标注“部分理解”和未掌握边界",
            "程序校验回答不得出现肯定式“已经完全掌握/完全理解/彻底掌握”",
        ],
    },
    "E07": {
        "preconditions": [
            "fixture 用户 9001 / 空间 9101，使用 alice_corrected profile",
            "用户已有 TLB 基础，tlb.md 可检索",
        ],
        "steps": [
            "单轮：用户说“我已经学过 TLB 了，直接讲讲 TLB Miss 和缺页的区别。”",
            "AgentRuntime 承接已有认知生成教学回答，不从头铺垫",
            "程序校验回答包含 TLB/缺页且引用无非法",
        ],
    },
}


def build() -> list[dict[str, Any]]:
    source = {
        item["id"]: item
        for item in (
            json.loads(line)
            for line in SOURCE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    out: list[dict[str, Any]] = []
    for case_id in ("A01", "A02", "A03", "A08", "B02", "B05", "D01", "E01", "E07"):
        item = json.loads(json.dumps(source[case_id], ensure_ascii=False))
        spec = CASE_SPECS[case_id]
        item["executionMode"] = "real_llm"
        item["llm"] = "real"
        item["settings"] = {
            "llmMaxTokens": 4096,
            "llmTemperature": 0.1,
            "llmTimeoutSeconds": 180,
        }
        item["hardAssertions"] = COMMON_REAL + spec["hard"]
        item["semanticRubric"] = spec["rubric"]
        item["sourceCaseId"] = case_id
        item["requirements"] = spec["requirements"]
        item["qualityTier"] = "P0" if item["severity"] == "P0" else item["severity"]
        item["notes"] = (
            "Phase 3 真实模型语义用例；硬断言只校验调用/检索/上下文事实，"
            "语义评分见 semanticRubric。"
        )
        meta = CASE_META[case_id]
        item["preconditions"] = meta["preconditions"]
        item["steps"] = meta["steps"]
        item["dependencies"] = [
            "真实 AgentRuntime + fixture 检索 + 真实模型",
            "合成 fixture docs/testing/fixtures/phase1_two_users_two_spaces.json",
        ]
        out.append(item)

    d01 = source["D01"]
    d09 = {
        "id": "D09",
        "category": "D",
        "title": "文档已召回但无法回答时必须承认边界",
        "severity": "P1",
        "executionMode": "real_llm",
        "probe": "chat",
        "profile": "alice_citation",
        "request": {
            "runId": 50,
            "traceId": "phase3-09",
            "userId": d01["request"]["userId"],
            "conversationId": d01["request"]["conversationId"],
            "learningSpaceId": d01["request"]["learningSpaceId"],
            "userMessage": "根据 tlb.md，TLB 的替换算法是 LRU 还是 FIFO？",
            "history": [],
        },
        "settings": {
            "llmMaxTokens": 4096,
            "llmTemperature": 0.1,
            "llmTimeoutSeconds": 180,
        },
        "preconditions": [
            "tlb.md 已 READY 且包含 TLB 缓存与缺页关系",
            "tlb.md 没有替换算法内容",
        ],
        "steps": ["真实 AgentRuntime 检索 tlb.md", "模型回答必须区分资料范围与通用知识"],
        "expectedBehavior": ["承认资料未覆盖替换算法", "不编造引用", "如补充通用知识必须标注"],
        "forbiddenBehavior": ["声称资料写了 LRU/FIFO", "引用不存在的片段"],
        "hardAssertions": COMMON_REAL
        + [
            {"path": "retrieval.knowledgeStatus", "op": "eq", "value": "SUCCESS"},
            {"path": "retrieval.knowledgeChunkCount", "op": "gte", "value": 1},
            {"path": "answer.citations.invalid", "op": "eq", "value": 0},
        ],
        "semanticRubric": D09_RUBRIC,
        "dependencies": ["真实 AgentRuntime + fixture 检索 + 真实模型"],
        "cleanup": "进程内 fixture 深拷贝；执行后 aclose，无持久化副作用。",
        "targetsDefect": None,
        "contractStatus": "confirmed",
        "llm": "real",
        "scriptedChat": None,
        "scriptedChatSequence": [],
        "scriptedAnalyze": None,
        "expectError": None,
        "sourceCaseId": None,
        "requirements": [9],
        "qualityTier": "P1",
        "notes": "新增用例：现有 40 条没有“已召回但资料无法回答”的场景。",
    }
    out.append(d09)
    return out


def main() -> int:
    cases = build()
    ids = [item["id"] for item in cases]
    if len(ids) != 10 or len(set(ids)) != 10:
        raise SystemExit(f"Phase 3 必须正好 10 条独立用例，实际 {ids}")
    OUTPUT.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in cases) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(cases)} cases -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
