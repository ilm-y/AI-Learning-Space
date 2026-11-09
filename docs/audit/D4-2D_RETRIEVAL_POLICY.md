# D4-2D Retrieval Policy 契约

> 范围：D4-2D（规则型 Retrieval Policy / Planner）。
> 依据：当前代码 `agent/app/context/policy.py`（`POLICY_VERSION = "d4-2-v1"`）、`agent/app/runtime.py` 的接线、`agent/tests/test_retrieval_policy.py`。
> 约束：本文只做文档化，不重新设计 Policy、不改生产代码、不改评测标准。
> 说明：仓库没有独立的 D4-2D 报告可对照；本文以代码为准。若与 D4-5 报告或历史描述冲突，以代码为准并在第 8 节标注。

---

## 1. 代码核实结论

| 项目 | 结论 |
|---|---|
| Policy 入口 | `plan_retrieval(...)`，纯函数，不读配置、不访问服务；返回冻结的 `RetrievalPlan` |
| 版本 | `POLICY_VERSION = "d4-2-v1"`，随 `RetrievalPlan.as_dict()["policyVersion"]` 输出，并进入 D4-4 Manifest |
| 接入点 | `runtime._retrieve`：先识别 Concept，再 `plan_retrieval`，随后按 `plan.*.triggered` 决定是否发起检索；`_apply_session_policy` 按 `SessionDecision` 清空旧 goal/focus |
| 与 D4-2C 的关系 | D4-2D 消费 D4-2C 的 Concept / Session 结果，并在 Agent 侧处理“Session 可能误挂旧话题”的缺口 |
| 与 D4-3 的关系 | `REQUIRED` / `OPTIONAL` 在本批都触发检索；差异由 D4-3 的按类型预算（REQUIRED 先分配、OPTIONAL 用剩余）和 D4-4 Manifest 使用 |
| 与 D4-5 报告的一致性 | D4-5 对 Plan 语义、Session 不匹配、多概念 Memory 的描述与当前代码一致；D4-5 称“D4-2D 无独立文档”，本文即为补全 |

---

## 2. 六类 Intent 定义与识别规则

`Intent` 是**最小意图集合**。规则版按固定优先级判定，第一条命中即停；不调用 LLM。

| 优先级 | Intent | 识别规则 | 规则名 |
|---:|---|---|---|
| 1 | `LEARNING_REVIEW` | 同时命中“时间 / 纠错类复盘词”和“第一人称或明确纠错问法” | `personal_history_review` |
| 2 | `META_QUESTION` | 命中自我评估 / 掌握程度 / 薄弱点词表 | `meta_marker` |
| 3 | `KNOWLEDGE_QUERY` | 命中用户上传资料 / 文档词表 | `document_reference` |
| 4 | `CASUAL` | **整句话**只由寒暄 / 确认词组成 | `casual_only_message` |
| 5 | `CONTINUE_TASK` | 存在 Session、Session 与本轮概念匹配、且命中续接词 | `session_concept_match+continuation` |
| 6 | `LEARNING_EXPLANATION` | 本轮有 `concept_ids` | `concept_hit_explanation` |
| 7（兜底） | `KNOWLEDGE_QUERY` | 没有概念，但也不是寒暄 / 文档显式引用 | `no_concept_substantive_question` |

### 2.1 词表（代码常量，语义与旧 Runtime 一致）

- `EVIDENCE_TRIGGERS`：为什么、凭什么、你怎么知道、依据、证据、你确定、确定吗、搞错、说错、不对吧、复盘、回顾。
- `REVIEW_TRIGGERS`：之前、以前、曾经、过去、后来、纠正、改过来、错在哪、哪里错、哪一步错、搞错、说错、误解、复盘、回顾、学习轨迹。
- `_PERSONAL_HISTORY_MARKERS`：我、自己。
- `_HISTORY_ERROR_MARKERS`：错在哪、哪里错、哪一步错、错在、错了、纠正、改过来、搞错、说错、复盘、回顾、学习轨迹。
- `_META_MARKERS`：掌握程度、掌握得、掌握怎么样、薄弱、弱点、盲区、学得怎么样、学到哪、学习进度、进度如何、我的水平、我理解对、理解得怎么样、我还差、哪里薄弱。
- `_DOCUMENT_MARKERS`：资料、文档、教材、课本、讲义、笔记、原文、章节、第几页、书里、文件中、上传的、pdf。
- `_CONTINUATION_MARKERS`：继续、接着、下一步、刚才、上面、前面、再讲、再说、还有呢、然后呢。
- `_CASUAL_ONLY_RE`：整句只含“你好 / 您好 / hi / hello / 嗨 / 在吗 / 谢谢 / 多谢 / 感谢 / 好的 / 好吧 / 好 / 嗯 / 哦 / 再见 / 拜拜 / 收到 / 明白 / 懂了 / 辛苦了 / 随便聊聊 / 讲个笑话 / 你是谁 / 你叫什么 / 你能做什么”等；可带标点与语气词。
  - 刻意**不包含**“继续 / 然后呢”，否则它们永远进不了 `CONTINUE_TASK`。
  - 宁可把一句话当知识问题，也不用寒暄把真正的追问静默掉。

### 2.2 复合判定

- `is_history_review(query)`：必须**同时**满足
  - 命中 `REVIEW_TRIGGERS`；
  - 且命中第一人称（我 / 自己）或明确纠错问法（错在哪 / 哪里错 / 纠正 / 改过来 / 搞错 / 说错 / 复盘 / 回顾 / 学习轨迹）。
  - 只看“以前”会把“过去十年 CPU 架构有什么变化”误判成个人复盘，因此需要第二组条件。
- `wants_evidence(query)`：只命中 `EVIDENCE_TRIGGERS`。
- `CONTINUE_TASK` 必须同时满足：`session is not None`、`concept_match is True`、命中 `_CONTINUATION_MARKERS`。单独说“继续”但没有可匹配的 Session / 概念时，仍按普通概念或知识问题处理。

---

## 3. RetrievalPlan 结构

```python
RetrievalPlan(
    policy_version: str,              # "d4-2-v1"
    intent: Intent,
    intent_reason: str,
    concepts: tuple[int, ...],        # 去重后的本轮 concept_ids
    session: SessionDecision,
    understanding: SourceDecision,
    memory: SourceDecision,
    memory_concept_ids: tuple[int, ...],  # memory.triggered 时为全部 concepts，否则空
    evidence: SourceDecision,
    evidence_related: SourceDecision,
    revision: SourceDecision,
    knowledge: SourceDecision,
    review_required: bool,
    memory_scope_priority: tuple[str, ...] = ("CONCEPT", "SPACE", "GLOBAL"),
    reasons: dict[str, str],
)
```

### 3.1 SourceDecision

```python
SourceDecision(
    need: RetrievalNeed,   # REQUIRED | OPTIONAL | SKIP
    reason: str,           # 规则名，供 trace / Manifest 复用
)
```

- `triggered = need is not SKIP`。
- `as_dict()` 输出 `{"need", "triggered", "reason"}`，不含正文。

### 3.2 SessionDecision

```python
SessionDecision(
    need: RetrievalNeed,
    inject_goal: bool,
    inject_current_focus: bool,
    inject_recent_turns: bool,
    reason: str,
    session_id: int | None,
    concept_match: bool | None,   # None = 无法判定
)
```

- `injects_frame = inject_goal or inject_current_focus`。
- `inject_recent_turns` 当前恒为 `True`，只表示“保留现有会话历史注入逻辑”；Runtime 不额外读取 Session 级 recent turns（当前请求只带 Conversation history）。
- 这里的 `REQUIRED / OPTIONAL / SKIP` 表示**是否注入 Session 框架**，不是“是否发起一次 Session 检索”。

### 3.3 `as_dict()` 输出

```jsonc
{
  "policyVersion": "d4-2-v1",
  "intent": "LEARNING_EXPLANATION",
  "intentReason": "concept_hit_explanation",
  "concepts": [1, 2],
  "reviewRequired": false,
  "session": {
    "need": "REQUIRED",
    "injectGoal": true,
    "injectCurrentFocus": true,
    "injectRecentTurns": true,
    "reason": "session_concept_match",
    "sessionId": 7,
    "conceptMatch": true
  },
  "needs": {
    "understanding": {"need": "REQUIRED", "triggered": true, "reason": "..."},
    "memory": {"need": "REQUIRED", "triggered": true, "reason": "..."},
    "evidence": {"need": "SKIP", "triggered": false, "reason": "..."},
    "evidenceRelated": {"need": "SKIP", "triggered": false, "reason": "..."},
    "revision": {"need": "SKIP", "triggered": false, "reason": "..."},
    "knowledge": {"need": "REQUIRED", "triggered": true, "reason": "..."}
  },
  "memoryConceptIds": [1, 2],
  "memoryScopePriority": ["CONCEPT", "SPACE", "GLOBAL"],
  "reasons": { "...": "..." }
}
```

---

## 4. 意图 × 数据源矩阵

下表是 `_decide_sources` 的规则，**在“无概念锚点统一覆盖”之前**。

| Intent | Understanding | Memory | Evidence | Related Evidence | Revision | Knowledge |
|---|---|---|---|---|---|---|
| `LEARNING_EXPLANATION` | REQUIRED | REQUIRED | REQUIRED（显式依据请求）/ SKIP | 跟随 Evidence | SKIP | REQUIRED |
| `LEARNING_REVIEW` | REQUIRED | REQUIRED | REQUIRED（显式依据请求）/ SKIP（默认走 Revision） | 跟随 Evidence | REQUIRED | OPTIONAL |
| `CONTINUE_TASK` | REQUIRED | REQUIRED | REQUIRED（显式依据请求）/ SKIP | 跟随 Evidence | SKIP | OPTIONAL |
| `KNOWLEDGE_QUERY` | OPTIONAL | OPTIONAL | REQUIRED（显式依据请求）/ SKIP | 跟随 Evidence | SKIP | REQUIRED |
| `META_QUESTION` | REQUIRED | REQUIRED | REQUIRED（无需触发词） | 跟随 Evidence | SKIP | OPTIONAL |
| `CASUAL` | SKIP | SKIP | SKIP | SKIP | SKIP | SKIP |

补充规则：

1. **Related Evidence 跟随原始 Evidence**：`evidence.triggered` → `OPTIONAL`，reason `follows_evidence:<evidence.reason>`；否则 `SKIP`，reason `no_evidence_query:<evidence.reason>`。
2. **无概念锚点统一覆盖**：`not has_concepts` 时，Understanding / Memory / Evidence / Revision 全部改为 `SKIP`，reason 前缀 `no_concepts:`；Knowledge 不变。
   - 这些数据在后端以 `user_id + concept_id` 为锚，不能在没有 `concept_id` 时发起“全量历史”查询。
3. **复盘默认不读最近 Evidence**：`LEARNING_REVIEW` 且没有显式依据请求时，Evidence 为 `SKIP`，reason `review_uses_revision_not_recent_evidence`；Revision 才是复盘第一手数据。否则“我之前错在哪”会在没有轨迹时把最近几条证据拼成一段并不存在的复盘叙事。
4. **META_QUESTION 必须看 Evidence**：即使用户没有说“为什么 / 依据”，掌握程度也不能只信当前快照。
5. **CASUAL 跳过一切检索**：寒暄不应触发个人数据或文档检索。

---

## 5. REQUIRED / OPTIONAL / SKIP 的当前语义

| 值 | 检索是否发起 | 当前用途 |
|---|---|---|
| `REQUIRED` | 是 | 意图对该数据源有强依赖；D4-3 按类型预算**先分配**；Manifest 记录需求强度 |
| `OPTIONAL` | 是 | 意图对该数据源有弱依赖；D4-3 在 REQUIRED 之后**使用剩余额度**；Manifest 记录需求强度 |
| `SKIP` | 否 | 不发起检索；Runtime 保持 `NOT_QUERIED / NOT_RETRIEVED`，不得渲染成“查过但没有” |

关键约定（代码注释与 D4-5 报告一致）：

- **本批 `REQUIRED` 与 `OPTIONAL` 都表示“发起检索”**，区别只在预算分配与可解释性，不在接线。
- Runtime 的接线是 `plan.<source>.triggered`；对 Understanding / Memory / Evidence / Revision 还额外要求 `concept_ids` 非空。
- `knowledge` 不受 `concept_ids` 限制：用户问一个还没建 Concept 的新知识点时，上传文档仍应参与回答。
- `evidence_related` 在 Plan 中是独立字段，但 Runtime 的实际触发条件仍是 `fetch_recent_evidence and evidence_related_concepts_limit > 0`，其中 `fetch_recent_evidence = evidence.triggered and not revision.triggered`。因此**复盘 + 显式依据请求**时，Plan 的 `evidenceRelated` 可能为 `OPTIONAL`，但 Runtime 不发起跨概念扩展。这一点是当前接线的细微差异，D4-3 只用它做预算/Manifest 判定。

---

## 6. D4-2C 缺口的 Policy 处理

### 6.1 Session 解耦

`_decide_session(session, intent, concept_match)` 的五种结果：

| 条件 | need | inject_goal | inject_current_focus | reason |
|---|---|---|---|---|
| 无 Session | SKIP | False | False | `no_session` |
| 寒暄 | SKIP | False | False | `casual_no_task_frame` |
| 两边都有概念且完全不相交（`concept_match=False`） | OPTIONAL | False | False | `session_concept_mismatch` |
| Session 没有 goal/focus | OPTIONAL | False | False | `session_without_goal` |
| 有目标且无法判定 / 匹配 | REQUIRED | 有 goal 则 True | 有 focus 则 True | `session_frame_undetermined` / `session_concept_match` |

- `concept_match` 的判定：`_session_concept_ids` 用 `detect_concepts` 从 Session 的 `title + goal + current_focus` 提概念（传空 history），再与本轮 `concept_ids` 求交集。
- **只有两边都有概念且完全不相交才是确定的 mismatch**。任一侧为空时返回 `None`，保持注入比误删真实目标更安全。
- Runtime 的 `_apply_session_policy` 在 Builder 之前把不该注入的字段置空；`id` 保留，摘要与溯源不受影响。
- 这是对 D4-2C “SessionResolver 规则 3 可能误挂旧 Session”的 Agent 侧缓解，不改变 Backend 的 Session 归属。

### 6.2 多概念 Memory

- `memory_concept_ids` 在 `memory.triggered` 时等于**全部去重后的本轮 concepts**，不是只取第一个。
- Runtime 把完整列表传给 Retriever；`HttpBackendRetriever.get_memories` 对 concept_ids 去重后**逐概念并发请求**，再按 memory.id 去重、轮转交错合并；任一概念 `DEGRADED` 则整体 `DEGRADED`。
- 没有跨概念统一 RRF；这是后端接口限制，Policy 不伪造排序（见 `D4-KNOWN_ISSUES.md` KI-03）。

### 6.3 Evidence 意图化

- Evidence 不再由 Runtime 的散落布尔条件决定，而由 Intent + `wants_evidence` 决定：
  - `LEARNING_REVIEW`：默认 `SKIP`，走 Revision；显式依据请求才 `REQUIRED`；
  - `META_QUESTION`：`REQUIRED`，无触发词；
  - `LEARNING_EXPLANATION` / `CONTINUE_TASK`：显式依据请求才 `REQUIRED`；
  - `KNOWLEDGE_QUERY`：显式依据请求才 `REQUIRED`；
  - `CASUAL`：`SKIP`。
- `review_required` 独立于 Evidence：它只表示“是否需要缓冲复盘回答”，比“是否读取轨迹”更窄，避免“过去十年 CPU 架构”被当成个人复盘。

---

## 7. 已知局限

1. **规则版，不是语义理解**：Intent 依赖关键词与正则，泛化措辞、跨语言、反讽、否定式追问可能判错。
2. **寒暄判定只认整句**：`_CASUAL_ONLY_RE` 要求整句话都是寒暄词；更长的寒暄或混合句会落到概念 / 知识分支。
3. **Session 概念匹配依赖文本与别名表**：Session 的 title/goal/focus 没有可识别概念时返回 `None`，旧目标仍会注入；这是有意的安全偏置，但无法识别真正的话题切换。
4. **`history` 参数当前不参与决策**：`plan_retrieval` 显式 `del history`，为未来“基于轮次的续接规则”保留接缝；当前续接只依赖 Session + 关键词。
5. **Knowledge 在非寒暄意图下几乎总是 `REQUIRED` 或 `OPTIONAL`**：没有概念时仍查知识；这是设计选择，不是遗漏。代价是每轮多一次知识检索。
6. **`evidence_related` 的 Plan 与实际接线存在细微差异**：复盘 + 显式依据请求时 Plan 为 `OPTIONAL`，Runtime 因 `fetch_recent_evidence=False` 不发起扩展。见第 5 节。
7. **没有 LLM 意图分类**：Policy 的结构已为替换留出空间（`Intent`、`RetrievalPlan` 不变），但当前不调用模型。
8. **没有概念时 U/M/E/Revision 一律 SKIP**：这是锚点约束，不是“用户没有历史”的结论；Runtime 必须保持 `NOT_QUERIED` 语义。

---

## 8. 文档与代码不一致处（以代码为准）

| 来源 | 表述 | 当前代码 | 处理 |
|---|---|---|---|
| D4-5 报告 | “C = 当前 D4-2D + D4-3” | `POLICY_VERSION = "d4-2-v1"`，D4-5 的 C 直接调用 `plan_retrieval` + `ContextBuilder` | 一致 |
| `policy.py` 模块注释 | “`REQUIRED` / `OPTIONAL` 在本批都表示发起检索，区别供 D4-3 预算与 D4-4 Manifest 使用” | 与 `builder._apply_type_budgets` 的 REQUIRED 先分配 / OPTIONAL 用剩余一致 | 一致 |
| `policy.py` 模块注释 | “没有任何概念锚点时 Understanding / Memory / Evidence / Revision 一律 SKIP；Knowledge 不受影响” | `_decide_sources` 的 `not has_concepts` 覆盖逻辑一致 | 一致 |
| D4-1 审计 | 触发判断散落在 `runtime._retrieve` 的硬编码词表 | 已收敛到 `policy.py`；Runtime 只执行 Plan | 历史描述已过时，以当前代码为准 |
| 历史描述 | “Memory 只传 `concept_ids[0]`” | D4-2D 已改为 `memory_concept_ids` 全量，逐概念请求后合并 | 历史描述已过时，以当前代码为准 |

---

## 9. 未完成项与边界

- 本文没有重新运行测试；结论来自当前代码与 `test_retrieval_policy.py`。
- 没有评估规则版 Intent 在真实用户分布上的准确率；需要端到端评测或人工抽检。
- 没有修复第 7 节局限；相关缺陷登记见 `docs/audit/D4-KNOWN_ISSUES.md`。
- 没有修改 Policy、Runtime、ContextBuilder 或评测数据。
