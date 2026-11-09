# D4-2C Concept 连续性契约

> 范围：D4-2C（Concept 连续性 / 跨 Conversation 与 Session 的概念状态回读）。
> 依据：当前代码（HEAD `a91a589`）与 D4-2C 产出提交 `d2cc6ef` 及既有测试。
> 约束：本文只做文档化，不重新跑测试、不改生产代码、不改评测标准。
> 说明：D4-2C 没有独立的版本常量。本契约以代码与测试为准；若本文与代码冲突，以代码为准，并在第 8 节标注。

---

## 1. 代码核实结论

| 项目 | 结论 |
|---|---|
| D4-2C 的主要落点 | `backend/.../LearningAnalysisService.java`（概念匹配口径）、`agent/app/retriever/concept_detector.py`（规则识别）、`agent/app/runtime.py`（识别 → 检索 → 组装）、`agent/tests/test_concept_continuity.py`（四类连续性场景） |
| D4-2C 提交 | `d2cc6ef fix(backend): 对齐概念匹配口径并固化概念连续性契约` |
| Agent 连续性测试 | `agent/tests/test_concept_continuity.py`：跨 Conversation 回读、无关概念隔离、回到旧概念读当前快照、跨 Session 不串目标、History 是唯一 Conversation 级输入 |
| Backend 匹配测试 | `LearningAnalysisServiceConceptMatchTest`：小写 `tlb miss` 命中 `TLB`、`TLBSize` 不命中、多词 ASCII 名称、中文子串匹配 |
| Session 解析测试 | `SessionResolverTest`：空概念 NONE、文本命中 ATTACH、唯一活跃且最近 ATTACH、其余 CREATE、PAUSED 只能被文本命中 |
| D4-2B 的边界修复 | `bd95fef` 已让 Evidence SQL 通过 `JOIN concept` 带 `learning_space_id`；Understanding / Revision 接口支持可选 `spaceId`；Message history 增加 `user_id` join。D4-1 审计中对应的“SQL 层未证明”条目已被部分覆盖 |
| 与 D4-1 审计的差异 | D4-1 写于 D4-2B 之前：其“Evidence 无 space 条件”“Understanding 接口无 space 参数”已过时。本文以当前代码为准 |
| 与 D4-5 报告的差异 | D4-5 报告写于本文之前，称“D4-2C 无独立审计文档”。本文即为补全；D4-5 的评测结论本身未变 |

**一致性结论**：Agent 侧规则识别、Backend 侧分析匹配、SessionResolver 与 Policy 的 Session 解耦逻辑，与 D4-2C 提交说明和测试一致。未发现需要修改代码的契约冲突；发现若干“口径差异 / 上游依赖”，见第 6、7 节。

---

## 2. Concept 识别 / 解析链路

一次请求里，Concept 从“文本”变成“可检索的 `concept_id`”经过三层，任何一层都只做**提出候选**，最终落库/检索仍以服务端确定性数据为准。

### 2.1 Backend：分析写入侧的概念匹配

位置：`LearningAnalysisService.matchConcepts` / `containsConceptName`。

- 读取当前 `learning_space_id` 下的全部 Concept，按 id 升序。
- 只匹配 `canonical_name` 与 `display_name`；**没有别名表**。
- 纯 ASCII 字母数字名称（允许内部空格，如 `Page Table`）：
  - 按词边界、忽略大小写匹配；
  - `tlb miss` 能命中 `TLB`；`TLBSize` 不会命中；
  - 正则：`(?<![A-Za-z0-9])` + `Pattern.quote(term)` + `(?![A-Za-z0-9])`，`CASE_INSENSITIVE | UNICODE_CASE`。
- 非 ASCII 名称（中文等）按子串匹配，因为中文没有词边界。
- 该匹配决定两件事：本轮是否产生 Evidence / 更新 Understanding，以及 `SessionResolver` 的 `detectedConceptNames`。
- 匹配不到就不写 Evidence、不动 Understanding；这是“宁可漏召回，也不误召回”的既定原则。

### 2.2 Agent：规则版概念识别

位置：`agent/app/retriever/concept_detector.py`。

优先级与分数：

| 命中方式 | 依据 | 分数 |
|---|---|---:|
| `canonical_name` | 当前用户消息包含 canonical name | 1.0 |
| `display_name` | 当前用户消息包含 display name | 0.9（按别名档计） |
| `ALIASES` | 内置别名表（如 `tlb` → 快表 / 转换检测缓冲区 / translation lookaside buffer） | 0.9 |

- 先看**本轮用户消息**；只有本轮完全没命中时，才回看最近 `history_lookback=4` 条历史。
- 回看时**先用户轮次，再助手轮次**；每往回一条衰减 0.1，基础分 0.6。这样避免 AI 自己的措辞把“用户在学什么”带偏。
- ASCII 短名称同样要求词边界（`TLBSize` 不算 `TLB`）；中文直接子串。
- 识别结果写入 `state.detected_concepts`，其 `concept_ids` 是本轮检索的唯一概念锚点。

### 2.3 Agent：E2 结构化 Concept Resolver（按需）

位置：`agent/app/retriever/concept_resolver.py`，由 `runtime._retrieve` 按需调用。

- **LLM 只提议，不裁决**。最终进入 `concept_ids` 的每个 id 必须通过服务端确定性校验：
  1. 属于本次实际呈现给模型的当前 Space 候选列表（跨 Space / 编造一律丢弃）；
  2. `confidence >= 0.6`（默认阈值）；
  3. `current_concepts` / `related_concepts` 每字段最多 3 个；
  4. `related_concepts` 不进入 `concept_ids`，最多作为 E1 关系扩展的排序提示；
  5. 与规则识别合并时规则优先，Resolver 只补充规则未命中的概念。
- 按需调用策略：规则命中且非前置 / 复盘问题时不调用；规则未命中但意图为知识 / 前置 / 复盘时调用；闲聊、候选为空、显式关闭时不调用。
- 失败语义：超时 / 5xx / 非法 JSON → `DEGRADED`；未预期异常 → `FAILED`；两者都不阻塞主链路，均只保留规则结果。
- `related_concepts` 永远不能绕过 `concept_relation` 表；它只影响真实关系候选的排序，不扩大 Evidence 查询范围（由 `_select_expansion_concepts` 与测试钉住）。

### 2.4 合并结果

`runtime._apply_resolver_decision`：

- 只有 `SUCCESS` 才可能补充 `current_concepts`；`DEGRADED / FAILED` 不改变规则结果。
- 规则已命中的 id 不重复追加。
- 即使 Resolver 内部校验失效，Runtime 仍按候选表再核对一次 id。
- `state.concept_ids` = 规则识别 + 通过校验的 Resolver `current_concepts`；`related_concepts` 不在其中。

---

## 3. Evidence / Understanding / Memory 的锚点

| 数据 | 存储锚点 | 检索参数 | 与 Conversation / Session 的关系 |
|---|---|---|---|
| Evidence | `user_id + concept_id`（append-only，无删改）；空间归属通过 `JOIN concept` 的 `learning_space_id` 证明 | `user_id`、`space_id`、`concept_ids`、`limit` | 不接收 Conversation / Session；两者都不进入键 |
| Understanding | `user_id + concept_id`；Revision 挂 `understanding_id` | `user_id`、`concept_id`；接口可选 `spaceId` | 不接收 Conversation / Session；Session 切换不改变快照 |
| Memory | `user_id + status='ACTIVE'` + scope：`GLOBAL` 全局 / `SPACE + learning_space_id` / `CONCEPT + learning_space_id + concept_id` | `user_id`、`space_id`、`concept_ids`、`query` | 不接收 Conversation / Session；SPACE / GLOBAL 记忆本来就跨概念可见 |
| Knowledge | 文档 / 片段：`learning_space_id + user_id + status=READY + deleted_at IS NULL + index_version + embedding fingerprint` | `user_id`、`space_id`、`question`、`concept_ids`、`top_k`、`min_score` | 不受 Conversation / Session / Concept 锚点限制；无概念时仍可检索 |

关键点：

- **概念层状态不以 Conversation 或 Session 为键**。同一 `user_id + space_id + concept_id` 在任意 Conversation / Session 中读到的是同一份当前快照与同一批 Evidence。
- Conversation 只决定“哪条消息被送到 Agent”；Session 只决定“当前活动目标是什么”。
- Memory 的 SPACE / GLOBAL 作用域本来就是跨概念可见，这不是概念状态污染。

---

## 4. Conversation / Session 的角色

### 4.1 Conversation

- 只承载 **History**。`MessageService.history` 取当前 Conversation 最近 20 条 TEXT；Agent 再按 `recent_history_limit` 截取。
- History 是**唯一** Conversation 级输入：同一句“它没命中的时候呢？”，在当前 Conversation 带 TLB 历史时可以回看并接上 TLB；换一条没有历史的 Conversation 就不会凭空知道 TLB。
- Conversation 不拥有 Understanding / Evidence / Memory，也不影响它们的键。

### 4.2 Session

- Session 是“当前这段学习活动”的框架，字段为 `title / goal / current_focus / status / started_at / last_active_at`。
- `SessionResolver` 在 Backend 侧决定一条 Conversation 归属哪个 Session：
  1. 本轮没有命中 Concept → `NONE`，不创建 Session；
  2. 某个候选 Session 的 `title / goal / current_focus` 文本命中本轮 Concept → `ATTACH`；
  3. 恰好一个 `ACTIVE` 候选且 `last_active_at` 在 7 天内 → `ATTACH`；
  4. 其余 → `CREATE`。
- 已绑定 Conversation 的读取路径 `boundSession` 会校验 `user_id / learning_space_id / status ∈ (ACTIVE, PAUSED)`；不一致时拒绝注入并返回 `null`。
- Session **不拥有、不覆盖**概念层状态；它只贡献自己的 `goal / current_focus`。

### 4.3 Policy 对 Session 的二次约束

`agent/app/context/policy.py` 的 `SessionDecision` 在 Agent 侧再判断一次：

- 无 Session → 不注入；
- 寒暄 → 不注入旧目标，但仍保留历史轮次；
- Session 文本与本轮概念都非空且完全不相交（`concept_match=False`）→ 不注入 `goal / focus`，原因 `session_concept_mismatch`；
- Session 没有 goal/focus → 不注入；
- 无法判定（任一侧没有可识别概念）→ 保持注入，原因 `session_frame_undetermined`。

Runtime 的 `_apply_session_policy` 在 ContextBuilder 之前把不该注入的字段置空，因此 Builder 不会渲染空 Session 区块。

---

## 5. 跨 Conversation / Session 检索条件

概念层数据可被回读，当且仅当同时满足：

1. 本轮从当前用户消息（或当前 Conversation 的历史回看）识别出 Concept；
2. 该 Concept 属于当前 `learning_space_id`（Agent 用 `list_concepts(space_id)` 取候选）；
3. Runtime 的 Policy 决定发起对应检索，且没有因“无概念锚点”被统一置为 `SKIP`；
4. 后端按 `user_id + concept_id`（Memory 另加 scope）返回该用户自己的数据。

Conversation 与 Session **不是**检索条件。它们只影响：

- 能否从历史回看概念（Conversation History）；
- 是否注入 `goal / current_focus`，以及是否因概念不匹配而剥离旧目标（Session + Policy）。

---

## 6. 已保证 / 依赖上游 / 缺口

### 6.1 已由测试与代码保证

| 契约 | 证据 |
|---|---|
| 同一 Concept 在另一条 Conversation 仍能读到 A 写入的 Understanding / Evidence | `test_conversation_b_reads_evidence_and_understanding_written_by_a` |
| 切到无关 Concept 时，旧 Concept 的 Understanding / Evidence / CONCEPT Memory 不注入；SPACE / GLOBAL Memory 仍可见 | `test_redis_conversation_does_not_receive_tlb_state` |
| 回到旧 Concept 时读到的是**当前** Understanding 与最近 Evidence，不是历史副本 | `test_returning_to_tlb_reads_current_understanding_and_recent_evidence` |
| 普通概念问题默认只读 Understanding / Memory，原始 Evidence 按“为什么 / 依据”等词按需召回 | `test_plain_tlb_question_reads_understanding_but_not_raw_evidence` |
| Session 切换不改变概念层状态，上一 Session 的 goal/focus 不串入下一 Session | `test_concept_state_survives_session_switch_without_cross_leak` |
| History 是唯一 Conversation 级输入；没有历史时不会凭空识别概念 | `test_conversation_history_is_the_only_conversation_scoped_input` |
| 小写 `tlb miss` 与 `TLB` 是同一概念；`TLBSize` 不是 TLB | Agent `test_tlb_miss_is_the_same_concept_when_space_defines_only_tlb`、`test_ascii_concept_name_requires_word_boundary`；Backend `LearningAnalysisServiceConceptMatchTest` |
| Session 概念不匹配时旧 goal/focus 被 Runtime 剥离 | `test_session_goal_is_stripped_on_concept_mismatch`、`test_runtime_strips_stale_session_frame_on_concept_mismatch`、D4-5 `session_mismatch` 探针 |

### 6.2 依赖上游（不由 Agent 单方面保证）

1. **Concept 目录必须来自当前 Space**。Agent 的 `list_concepts(space_id)` 是 `concept_ids` 合法性的来源；Understanding / Evidence 的 SQL 本身没有 `space_id`（Understanding）或依赖 `JOIN concept`（Evidence），因此“跨 Space 不返回”最终依赖概念来源与后端归属校验。
2. **分析写入是异步的**。Evidence / Understanding 由 Backend 分析链路写入；本轮消息之后的新状态不一定在下一轮前已落库。Agent 只能读到“当前已持久化”的状态。
3. **Backend 匹配口径必须与 Agent 对齐**。D4-2C 已对齐 ASCII 词边界与大小写；但 Backend 只匹配 canonical / display，Agent 还有别名表（见 7.2）。
4. **SessionResolver 的文本匹配只覆盖 canonical / display 名称**，不覆盖 Agent 别名；它只决定 Session 归属，不决定概念层检索范围。

### 6.3 缺口

| 编号 | 缺口 | 影响 |
|---|---|---|
| C-G1 | Session 概念匹配依赖文本识别与别名表；泛化措辞（“那个缓存”“它”）无法识别时返回“无法判定” | `concept_match=None`，旧 Session 目标仍被注入；宁可保留也不误删，但无法识别真正的话题切换 |
| C-G2 | `SessionResolver` 规则 3 可能把新话题挂到“唯一活跃且最近”的旧 Session | 已由 D4-2D Policy 的 `session_concept_mismatch` 在 Agent 侧剥离 goal/focus；但不能改写 Backend 的 Session 归属 |
| C-G3 | 多概念 Memory 没有跨概念统一 RRF；Agent 逐概念请求后按轮转交错合并 | 每个概念都有候选，但最终 top_k 不是跨概念统一排序；请求数随概念数增加 |
| C-G4 | Understanding 接口支持可选 `spaceId`，但 Agent 的 `get_understandings` / `get_revisions` 未传 | D4-2B 的纵深防御未闭环；当前路径依赖概念来源保证 |
| C-G5 | Backend 分析匹配没有别名表；Agent 识别有 | “快表”等别名可能被 Agent 识别并用于检索，但 Backend 不据此写 Evidence / 更新 Understanding（除非它同时命中 display name） |
| C-G6 | Knowledge 的版本 / 时效字段不进入 `KnowledgeChunk`，Builder 无法二次过滤 | 检索层已做版本隔离与 fail-closed；Builder 层没有可用的版本字段做展示或再过滤 |

---

## 7. TLB / TLB Miss 建模边界

1. **TLB 是一个 Concept，TLB Miss 不是默认的独立 Concept。**
   - `TLB Miss` 里的 `TLB` 是独立 ASCII 词，按词边界命中 canonical name；
   - 大小写不敏感，`tlb miss` 同样命中；
   - `TLBSize` / `TLBMissRate` 这类粘连词不会命中。
2. **“TLB Miss”的整体语义没有被建模。** Agent 只把它识别为 Concept `TLB` 的一次提及；不会因为出现 “Miss” 就推断用户在问缺页、页表遍历或硬件异常。具体语义仍由 Knowledge / Understanding / Memory 与模型回答承担。
3. **如果 Space 真的把 “TLB Miss” 定义为独立 Concept**：
   - canonical / display 名称按文本命中，可能同时命中 `TLB` 与 `TLB Miss`；
   - `ALIASES` 只为 `tlb` 配置了中文别名；不会自动把 `TLB Miss` 归并到 `TLB`；
   - 是否重复召回取决于两个 Concept 是否都在当前 Space 目录中。
4. **别名边界**：
   - Agent 的 `ALIASES` 是人工表，只收录“同一概念的不同说法”，不做同义词推理；
   - 中文别名（“快表”→“tlb”）只在 canonical_name 为 `tlb` 时生效；
   - Backend 分析匹配不读 `ALIASES`，见 C-G5。
5. **未建模部分**：TLB Miss 与 Page Fault 的因果 / 时序关系、TLB 命中率、多级页表遍历等，均属于 Knowledge 内容或模型解释，不属于 Concept 识别或连续性契约。

---

## 8. 文档与代码不一致处（以代码为准）

| 来源 | 旧表述 | 当前代码 | 处理 |
|---|---|---|---|
| `docs/audit/D4-1_CONTEXT_ASSEMBLY_AUDIT.md`（H/M 条目） | Evidence SQL 无 `space_id`，跨 Space 仅 Service 层证明 | `LearningEvidenceMapper.selectRecentByConcepts` 已 `JOIN concept` 并带 `c.learning_space_id = ?` | 以当前代码为准；D4-1 条目已过时 |
| 同上 | Understanding 接口不接收 `spaceId` | `UnderstandingController` 的 `get` / `revisions` 支持可选 `spaceId`，Service 校验概念归属 | 后端已支持；Agent 尚未消费（C-G4） |
| 同上 | Message history 无 `user_id` 条件 | `MessageMapper` 已 `JOIN conversation` 并校验 `c.user_id` | 以当前代码为准 |
| `docs/eval/D4-5_CONTEXT_QUALITY_EVALUATION.md` | 没有 D4-2C 独立审计文档 | 本文已补 | 缺口已关闭；D4-5 指标不变 |
| `concept_resolver.py` 模块注释 | 表格写“规则命中且 intent=prerequisite/review → 调用” | `decide_resolver_call` 与注释一致 | 无冲突 |

---

## 9. 未完成项与边界

- 本文没有重新运行测试；结论来自当前代码、D4-2C 提交与既有测试文件。
- 没有验证真实 MySQL / Qdrant / 外部 LLM 下的时序与并发行为。
- 没有评估模型是否真的利用了“当前快照优先于旧 Evidence”等 prompt 规则；这属于 D4-5 / 端到端评测范围。
- 没有修复 C-G1～C-G6；相关缺陷登记见 `docs/audit/D4-KNOWN_ISSUES.md`。
