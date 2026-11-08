# D4-1 Agent 上下文组装链路只读审计与 Manifest Schema 方案

> 审计范围：仅 D4-1。只读审计，不改代码、不改 ContextBuilder、不新增 Selector、不做 D4-2～D4-5、不引入新中间件/数据库、不 commit。
>
> 审计方式：静态阅读代码与 SQL。**本批没有运行任何测试，也没有连接数据库执行查询**；文中所有“会/不会”“证明/未证明”均来自源码阅读，不代表运行时复现结果。凡涉及“LLM 是否真的利用了某段上下文”的判断，均需 D4-5 动态评测验证。

---

## 1. 基线

| 项目 | 值 |
|---|---|
| 仓库 | `D:\learningMatiarials\AI-Learning-Space` |
| 分支 | `main` |
| 审计开始时 HEAD | `96d82d23ca8451b5b62aab38915647225a1534bc` |
| 审计开始时工作区 | clean（`git status --short` 为空） |
| 本批改动 | 仅新增本文档，不改业务代码 |

主要阅读范围：

- Agent：`agent/app/runtime.py`、`agent/app/context/builder.py`（逐行）、`agent/app/state/agent_state.py`、`agent/app/schemas.py`、`agent/app/trace.py`、`agent/app/retriever/{base,http_backend,fixture,routing,knowledge,concept_detector,concept_resolver}.py`。
- Backend：`SessionService`、`UnderstandingService`、`EvidenceService`、`MemoryService`、`HybridSearchService`、`DocumentService`、`ConceptService`、`ConceptRelationService`、`LearningSpaceService`、`ChatService`、`MessageService`、`ChatTurnService`；相关 Controller 与 Mapper 的注解 SQL。
- 数据模型：`schema.sql` 中 `learning_session`、`learning_understanding`、`learning_evidence`、`memory`、`document`、`document_chunk`、`concept`、`concept_relation`、`agent_run`。
- 测试：`agent/tests/test_context_builder.py` 等 ContextBuilder 相关测试只作为“现有覆盖范围”的证据，**未运行**。

---

## 2. T1 七层调用链审计

### 2.1 总表

> 说明：`Runtime 决策` 指 Python Agent 内部“是否发起这次检索”的判断；Session 与 History 由 Backend 组装后随 `ChatAgentRequest` 传入，Agent 不再发起 HTTP 检索。
>
> “SQL 条件”一列只写 Mapper 注解/XML 中真实出现的条件；“Service 过滤”只写 Java 层真实执行的判断。**上层参数不算证明。**

| 数据类别 | Runtime 决策 | Python 参数 | HTTP 路径 | Controller 权限 | Service 过滤 | SQL 条件 | 模型字段 |
|---|---|---|---|---|---|---|---|
| Session | 不检索。Backend 在 `ChatTurnService.prepare → LearningAnalysisService.beginTurn → SessionService.resolveForConversation` 中解析后，经 `ChatService.sessionContext(opening)` 放入 `ChatAgentRequest.session`；Agent 在 `AgentState.from_request` 直接接收。`ContextBuilder._collect_sections` 仅在 `state.session is not None` 且渲染非空时注入 | 无（Agent 不发 HTTP） | 无。Backend 内部 `SessionService.resolveForConversation` | `ChatService.stream` 先 `conversationService.ownedConversation(userId, conversationId)`；Session 本身不再单独鉴权 | 新归属：`SessionResolver` 基于 `user_id + learning_space_id + status ACTIVE/PAUSED` 候选；已归属：`sessionMapper.selectById(conversation.getSessionId())`，**不再校验 user/space/status** | 已归属路径：`WHERE id = ?`（MyBatis-Plus `selectById`）；候选路径：`user_id = ? AND learning_space_id = ? AND status IN ('ACTIVE','PAUSED')`。`learning_session` 无 `deleted_at` | `user_id`、`learning_space_id`、`title`、`goal`、`current_focus`、`status`、`started_at`、`last_active_at` |
| History | 不检索。Backend `MessageService.history` 取最近 20 条 TEXT，Agent `ContextBuilder._usable_history` 再按 `recent_history_limit`（默认 12）截取 | 无（Agent 不发 HTTP） | 无。Backend 内部 `MessageService.history` | `ChatService.stream` 已校验 conversation 归属；`history` 方法自身只按 `conversationId` 查询 | `message_type = 'TEXT'`，排除本轮 user message；MyBatis-Plus `@TableLogic` 追加 `deleted_at IS NULL` | `conversation_id = ? AND message_type = 'TEXT' AND id NOT IN (...) ORDER BY sequence_no DESC, id DESC LIMIT ?`；**无 `user_id` 条件** | `conversation_id`、`role`、`message_type`、`content`、`sequence_no`、`deleted_at`、`created_at` |
| Understanding（当前快照） | `concept_ids` 非空时**必查**，与 Memory 并行；每个 concept 一次 GET。无独立三态状态 | `get_understandings(user_id, space_id, concept_ids)`，但 `space_id` **未用于 HTTP 参数** | `GET /api/understandings/{conceptId}` | `SecurityUtil.currentUserId()`；Controller 只传 `userId + conceptId`，**无 space 参数** | `UnderstandingService.get`：`conceptService.accessibleConcept(userId, conceptId)` → `conceptMapper.selectById` + `requireOwnedSpace`（Java 比较 `space.getUserId()`）；再 `find(userId, conceptId)` | Understanding：`WHERE user_id = ? AND concept_id = ?`。Concept：`WHERE id = ? AND deleted_at IS NULL`。Space：`selectById` 后 Java 校验 owner。**Understanding SQL 无 `space_id`；跨 Space 仅由“概念→空间”服务链证明** | `user_id`、`concept_id`、`status`、`summary`、`known_parts`、`misconceptions`、`uncertain_parts`、`latest_user_explanation`、`current_revision_id`、`updated_at`；**无 `space_id`** |
| Understanding Revision（复盘） | 仅在 `concept_ids` 非空且 `history_review_requested` 时逐 concept GET；失败静默跳过 | `get_revisions(user_id, concept_ids)` | `GET /api/understandings/{conceptId}/revisions` | 同 Understanding；`SecurityUtil.currentUserId()`，无 space 参数 | `UnderstandingService.revisions`：`accessibleConcept` + `find(userId, conceptId)` | `WHERE understanding_id = ?`（先由 user+concept 找到 understanding）。跨用户由 understanding 的 `user_id` 证明；跨 Space 同 Understanding，仅服务链证明 | `understanding_id`、`revision_no`、`summary`、`known_parts`、`misconceptions`、`uncertain_parts`、`user_explanation`、`status`、`trigger_evidence_id`、`created_at` |
| Memory | `concept_ids` 非空时**必查**，与 Understanding 并行；只传 `concept_ids[0]` | `get_memories(user_id, space_id, concept_ids, query)`；HTTP 参数 `spaceId`、`keyword=query[:60]`、`limit`、`conceptId=concept_ids[0]` | `GET /api/memories/search/hybrid` | `MemoryController` 使用 `SecurityUtil.currentUserId()`；Service 内再做 scope 归属校验 | `HybridSearchService.search`：向量/关键词并行、超时、RRF、`selectVisibleByIds` 强制回查；`MemoryService` 校验 space/concept 归属 | 全文 SQL：`m.user_id = ? AND m.status = 'ACTIVE' AND MATCH(...)`；可见性：`GLOBAL` 或 `SPACE + learning_space_id = ?` 或 `CONCEPT + learning_space_id = ? AND concept_id = ?`；回查 `id IN (...) + user_id + status='ACTIVE' + 同样可见性`。**SQL 层已证明 user/space/concept/status** | `user_id`、`scope_type`、`scope_key`、`learning_space_id`、`concept_id`、`memory_type`、`memory_key`、`content`、`status`、`importance`、`version`、`updated_at`；无 `deleted_at`（用 `status` 管生命周期） |
| Knowledge（文档） | **每轮必查**，与 concepts 无关；question 为空时由 KnowledgeSearchService 返回 `NO_MATCH` | `KnowledgeQuery(user_id, space_id, question, concept_ids, top_k=5, min_score)`；再 `get_knowledge` 调 ready-ids | `GET /api/spaces/{spaceId}/documents/ready-ids?embeddingModel&embeddingProvider&embeddingDimension&embeddingFingerprint`，再 Qdrant `search_knowledge` | `DocumentController`：`SecurityUtil.currentUserId()` + path `spaceId`；Service `ownedSpace(userId, spaceId)` | `DocumentService.listReadyDocuments`：`ownedSpace`、model/provider/dimension/fingerprint 校验、严格模式 fail closed；`KnowledgeSearchService`：白名单 fail closed、fingerprint、阈值、payload 校验、版本隔离 | Document SQL：`d.learning_space_id = ? AND d.user_id = ? AND d.status='READY' AND d.deleted_at IS NULL AND d.indexed_at IS NOT NULL AND d.embedding_model ... AND d.chunk_count > 0 AND d.chunk_count = (活跃版本带 vector_ref 的 chunk 数)`；严格模式加 fingerprint 精确匹配。Qdrant filter：`user_id`、`space_id`、`document_id + index_version`。**SQL + 向量 filter 双重证明** | `document.user_id`、`learning_space_id`、`status`、`index_version`、`deleted_at`、`embedding_model/provider/dimension/fingerprint`、`vector_collection`；`document_chunk.document_id/index_version/content/vector_ref` |
| Evidence（当前概念，按需） | `concept_ids` 非空 **且** `_wants_evidence(query)` 命中词表，且不是复盘（复盘走 Revision 路径） | `get_evidences(user_id, space_id, concept_ids, limit=3)`；HTTP 参数 `spaceId`、`conceptIds` CSV、`limit` | `GET /api/evidences?spaceId&conceptIds&limit` | `EvidenceController`：`SecurityUtil.currentUserId()`；batch 端点要求 `spaceId` | `EvidenceService.listRecentByConcepts`：`spaceService.ownedSpace`；normalize conceptIds（非空、正数、≤50）；`selectBatchIds` 后 Java 过滤 `learningSpaceId == spaceId`；再按 `user_id` 查 Evidence | `SELECT * FROM learning_evidence WHERE user_id = ? AND concept_id IN (...) ORDER BY created_at DESC, id DESC LIMIT ?`。**无 `space_id`、无 `deleted_at`**；Space 由 Service 证明，跨 Space 未在 SQL 证明；Evidence 表 append-only，无软删 | `user_id`、`concept_id`、`understanding_id`、`evidence_type`、`observation`、`correctness`、`dedupe_key`、`created_at`；**无 `space_id`、无 `deleted_at`** |
| Related-concept Evidence（受控一跳） | 与 Evidence 同触发，且 `evidence_related_concepts_limit > 0`；先查关系，再选最多 N 个扩展概念，再查这些概念的 Evidence | `get_related_concepts` 逐 concept GET；`get_evidences` 一批扩展概念；`limit=evidence_related_evidence_limit` | `GET /api/spaces/{spaceId}/concepts/{conceptId}/relations?types=PREREQUISITE,RELATED&limit`；再 `GET /api/evidences` | `SpaceController` 使用 `SecurityUtil.currentUserId()`；`ConceptRelationService` 校验 Space owner 与 concept 归属 | `ConceptRelationService`：`ownedSpace`、concept `selectById` + space 匹配、类型白名单、limit 默认 5/最大 20；Evidence 同 EvidenceService | 关系 SQL：`concept src/tgt` 均 `deleted_at IS NULL`，均 `learning_space_id = ?`，`relation_type IN (...)`，`ORDER BY weight DESC, id LIMIT ?`。Evidence SQL 同 Evidence（无 space） | `concept_relation.source_concept_id/target_concept_id/relation_type/weight`；`concept.learning_space_id/deleted_at` |
| Learning Trajectory | `concept_ids` 非空且 `_is_history_review(query)` 为真；用 Revision + 精确 Evidence 回查；失败静默 | `get_revisions`、`get_evidence_details(user_id, concept_ids, evidence_ids)` | 见 Revision；Evidence 详情 `GET /api/evidence/{evidenceId}` | 同 Understanding / Evidence | `EvidenceService.get` 校验 `userId`；`get_evidence_details` 再按 `item.concept_id in wanted_concepts` 客户端过滤 | Evidence 详情：`selectById` → `WHERE id = ?`；Service 再用 `Objects.equals(evidence.getUserId(), userId)` 校验。Revision 见上。跨 Space 仍依赖概念链 | `understanding_revision.*`；`learning_evidence.*`；`evidence_source.source_type/source_id` |

### 2.2 “尚未证明”清单（必须保留）

以下项目**不能**因为 Python 传了正确参数就判定为安全。按“跨用户 / 跨 Space / 软删 / 版本”逐项列出：

1. **Understanding 跨 Space：尚未证明（SQL 层）。**
   `GET /api/understandings/{conceptId}` 不接收 `spaceId`；`LearningUnderstanding` 表没有 `space_id` 字段；SQL 只有 `user_id + concept_id`。跨 Space 仅由 `accessibleConcept → requireOwnedSpace` 的 Java 链证明，且依赖 `concept_ids` 来源于本轮 `list_concepts(space_id)`。若调用方传入其他 Space 的 conceptId，接口本身不会拒绝（只要该概念属于当前用户）。
2. **Understanding Revision 跨 Space：尚未证明（SQL 层）。**
   同 1；`revisionMapper` 只按 `understanding_id` 查，空间归属依赖 Understanding → Concept → Space 的服务链。
3. **Evidence 跨 Space：尚未证明（SQL 层）。**
   `learning_evidence` 无 `space_id`；`selectRecentByConcepts` 只有 `user_id + concept_id IN`。`EvidenceService` 在 Java 中过滤 `learningSpaceId == spaceId`，但这属于 Service 层证明，不是 SQL WHERE。运行路径安全依赖 conceptIds 来自本轮 Space。
4. **Evidence 软删：不适用但需明确。**
   表设计为 append-only，没有 `deleted_at`，因此不存在“软删过滤缺失”；不能把它标成“已做软删过滤”。
5. **Session 已归属路径的范围/状态：尚未证明（SQL 层）。**
   `resolveForConversation` 发现 `conversation.session_id != null` 时直接 `selectById`，不再校验 `user_id`、`learning_space_id`、`status`。数据库外键只保证 session 存在，不保证与本 conversation 同用户同 Space。正常运行路径由创建逻辑保证，但不是 SQL 层证明。
6. **History 跨用户：尚未证明（SQL 层）。**
   `MessageService.history` 只按 `conversation_id` 查；`message` 表无 `user_id`。安全依赖 `ChatService.stream` 先做的 `ownedConversation`。若该方法被其他入口复用，需重新证明。
7. **Concept 列表跨用户：尚未证明（SQL 层）。**
   `conceptMapper` 按 `learning_space_id + deleted_at IS NULL` 查；Space owner 校验在 `ConceptService.listBySpace → ownedSpace` 的 Java 层。运行路径安全，但 SQL 没有 `user_id`。
8. **软删过滤汇总：**
   - `document`、`document_chunk`（随 document 级联）、`concept`、`concept_relation`：SQL 明确带 `deleted_at IS NULL`，**已证明**。
   - `message`：MyBatis-Plus `@TableLogic` 追加，**已证明（依赖 ORM 生效）**。
   - `learning_understanding`、`learning_evidence`、`learning_session`、`memory`：表本身无 `deleted_at`。Understanding/Evidence 是快照/追加模型；Memory 用 `status`；Session 无软删。**不能声称这些表做了软删过滤。**
9. **版本过滤汇总：**
   - Knowledge：`document.index_version` + `document_chunk.index_version` + Qdrant `doc_version` + `embedding_fingerprint`，**已证明**。
   - Memory：`selectVisibleByIds` 回查 MySQL，`contentHash` 变化会丢弃向量结果，**已证明（回查层）**。
   - Understanding：只取当前快照；Revision 只取该 understanding 下的版本；**无独立版本号证明，但语义上无“旧版本混入”路径**。
   - Evidence：append-only，`created_at` 排序；**无版本概念**。
10. **Controller 权限：**
    各 Controller 均使用服务端 `SecurityUtil.currentUserId()`，未发现从请求体接收 `userId` 的越权入口。但 Controller 普遍不直接校验 Space 归属，归属校验在 Service；这与上表“SQL 未证明”是两件事，不能互相替代。

---

## 3. T2.1 八状态可区分性

状态定义：`NOT_QUERIED`（未发起）、`EMPTY`（查了但没有）、`SUCCESS`（查到了）、`DEGRADED`（失败/不完整）、`SELECTED`（进入最终 prompt）、`EXCLUDED`（检索到但被策略/去重排除）、`TRUNCATED`（进入但被截断）、`OMITTED`（因预算整段丢弃）。

符号：✅ 可区分；⚠️ 部分可区分/只能间接推断；❌ 不可区分。

| 数据类别 | 当前能否区分该状态 | 代码位置 | 缺口说明 |
|---|---|---|---|
| Session | NOT_QUERIED ⚠️；EMPTY ❌；SUCCESS ✅；DEGRADED ❌；SELECTED ✅；EXCLUDED ✅（无排除路径）；TRUNCATED ❌（不允许截断）；OMITTED ✅（`dropped` + warning） | `agent_state.py:129-145`；`builder.py:420-432`、`648-672`、`777-782`；`ChatService.java:463-471` | `session == null` 同时表示“未归属”“后端未传”“解析失败”，无法区分；`SessionContext` 没有 status/时间；Session 不进 `_reduce_one`，但 `_fit_budget` 可在预算耗尽时整段丢弃，丢弃有 warning 但无“检索状态”。 |
| History | NOT_QUERIED ⚠️；EMPTY ⚠️；SUCCESS ✅；DEGRADED ❌（Backend 不返回状态）；SELECTED ✅；EXCLUDED ⚠️（仅文本去重）；TRUNCATED ❌（History section 不允许截断，只能整段丢弃）；OMITTED ✅（有 warning） | `MessageService.java:175-204`；`builder.py:404-415`、`584-601`、`777-782` | 历史为空无法区分“本来就没有”与“查询失败”；`_usable_history` 的文本去重会静默丢掉与当前输入同文的上一轮，未记录 `EXCLUDED` 原因。 |
| Understanding（当前快照） | NOT_QUERIED ⚠️（`concept_ids` 为空可推断，但 prompt/warning 仍可能说“没有检索到”）；EMPTY ❌；SUCCESS ✅；DEGRADED ❌；SELECTED ✅；EXCLUDED ⚠️（仅 `dedupe_key` 冲突）；TRUNCATED ✅（`allow_truncate`）；OMITTED ✅（warning） | `runtime.py:782-786`、`812-814`；`http_backend.py:117-127`、`422-458`；`builder.py:440-460`、`605-618`、`777-782` | **核心缺口**：`get_understandings` 对每个 concept 用 `fatal=False`，5xx/超时/业务错误/解析失败/`null` 全部变成“没有这个元素”（401 除外，会抛 `RetrievalError` 中断整轮），`AgentState` 没有 understanding 状态字段。于是 `EMPTY` 与 `DEGRADED` 完全不可区分；`status=EXPOSED` 但无其他内容的快照也被 `has_content` 跳过（`builder.py:441-443`），看起来像没查到。 |
| Memory | NOT_QUERIED ✅（无 concepts）；EMPTY ❌；SUCCESS ✅；DEGRADED ❌；SELECTED ✅；EXCLUDED ⚠️（去重）；TRUNCATED ❌（Memory section 不允许截断，只能整段丢弃）；OMITTED ✅ | `http_backend.py:129-167`、`461-504`；`runtime.py:787-790`、`812-814`；`builder.py:485-495` | **核心缺口**：`_parse_memory_search` 已解析出 `OK / DEGRADED_VECTOR / DEGRADED_KEYWORD / UNAVAILABLE / BLANK_QUERY`，但 `get_memories` 对 `UNAVAILABLE` 直接 `return []`，对 `DEGRADED_*` 只 `log.warning` 后返回 items；状态没有进入 `AgentState`，ContextBuilder 也没有 memory 状态区块。**检索失败 = 没有记忆**。另外只传 `concept_ids[0]`，其他命中概念的 CONCEPT 级记忆不会进入候选，且不记录“因范围未查”。 |
| Knowledge | NOT_QUERIED ⚠️（Runtime 总是查；空 question 走 `NO_MATCH`）；EMPTY ✅（`NO_VALID_DOCUMENTS` / `NO_MATCH` / `THRESHOLD_FILTERED`）；SUCCESS ✅；DEGRADED ✅；SELECTED ✅（injected ids）；EXCLUDED ⚠️（阈值/非法 payload 只有计数或 warning）；TRUNCATED ❌（knowledge section 不允许截断，只能整段丢弃）；OMITTED ✅（`SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`） | `runtime.py:750-757`、`781`、`807-810`；`knowledge.py:92-155`、`159-266`；`builder.py:314-355`、`1026-1124` | 当前**最完整**的一类：检索层 6 状态 + 预算后 3 状态 + injected/dropped chunk ids。仍未记录逐条 `EXCLUDED` 原因（低于阈值、payload 非法、重复 chunk 只有计数）；知识片段不允许截断，所以 `TRUNCATED` 不适用，但这一点没有在状态里显式表达。 |
| Evidence（当前概念） | NOT_QUERIED ✅（默认 `NOT_RETRIEVED`）；EMPTY ✅（`EMPTY`）；SUCCESS ✅；DEGRADED ✅（`DEGRADED` + warning）；SELECTED ✅；EXCLUDED ⚠️（复盘时被 trajectory 覆盖、去重）；TRUNCATED ❌（Evidence section 不允许截断，只能整段丢弃）；OMITTED ✅ | `runtime.py:764-798`、`822-826`、`858-877`；`http_backend.py:229-286`；`builder.py:537-580`、`1160-1191` | 三态语义已进入 ContextBuilder；但“因复盘改走 Revision 而不查 recent evidence”只体现在 `fetch_recent_evidence` 布尔，没有独立 `NOT_QUERIED` 原因；被 trajectory 覆盖的 evidence（`builder.py:553-556`）被静默跳过，未记为 `EXCLUDED`。 |
| Related-concept Evidence | NOT_QUERIED ✅（默认）；EMPTY ✅（`EMPTY`）；SUCCESS ✅（`OK`）；DEGRADED ✅（`PARTIAL` / `FAILED`）；SELECTED ✅；EXCLUDED ❌；TRUNCATED ❌（Related Evidence section 不允许截断，只能整段丢弃）；OMITTED ✅ | `runtime.py:799-820`、`828-856`；`http_backend.py:288-373`；`builder.py:557-569`、`1160-1191` | `related_concepts_status` 已记录 `OK/EMPTY/PARTIAL/FAILED`，但 ContextBuilder **只为跨概念 Evidence 的 `DEGRADED` 输出提示**；关系查询 `FAILED/PARTIAL` 只进 `warnings`，不进 system instruction。扩展概念选择（`_select_expansion_concepts`）未选中的候选、因 limit 截掉的证据都没有 `EXCLUDED` 记录。 |
| Learning Trajectory / Revision | NOT_QUERIED ✅（`history_review_requested` 为假）；EMPTY ❌；SUCCESS ✅；DEGRADED ❌；SELECTED ✅；EXCLUDED ⚠️；TRUNCATED ✅；OMITTED ✅ | `runtime.py:858-877`；`http_backend.py:375-391`；`builder.py:462-477`、`777-782` | `get_revisions` 用 `fatal=False`，失败返回 `{}`；`build_trajectories` 得到空列表。**“没有历史版本”与“Revision 查询失败”不可区分**。`_render_understanding` 虽然提醒“没有轨迹时不要反推过去”，但无法阻止模型把失败当成“用户过去没有错误”。 |
| Concept Resolver 状态 | NOT_QUERIED ✅（`resolver_intent`）；SUCCESS ✅；DEGRADED ✅；FAILED ✅ | `runtime.py:693-745`；`builder.py:497-508`、`1140-1157` | 只有 degraded/failed 才渲染状态区块；正常“未调用”不渲染，这是设计选择，不算缺陷。状态区块只在无认知记录时与“没有历史”合并说明。 |

**结论：** 八状态中，Knowledge 最接近完整；Evidence 次之；Understanding、Memory、Trajectory 存在“失败 = 空”的语义合并；所有类别都缺少逐条 `EXCLUDED` 原因；`TRUNCATED` 只在 section 层有 flag，没有类别级最终状态。

---

## 4. T2.2 裁剪语义依赖检查

> 重点检查“高优先级/摘要类内容留下，但支撑它的语义前提被裁掉”的情况。ContextBuilder 的全局优先级是：Understanding 70 > Trajectory 68 > Memory 50 > Knowledge 40 > Evidence 20 > Related Evidence 18；`_fit_budget` 从高到低装，`_reduce_one` 从低到高裁。

### 4.1 旧 Evidence 留下，但当前 Understanding 丢失

| 项目 | 内容 |
|---|---|
| 触发条件 | 不是预算裁剪：Understanding 优先级 70 高于 Evidence 20，正常裁剪不会“留下 Evidence、丢掉 Understanding”。真实触发是 **Understanding 检索失败/无快照/`has_content` 为假**，而 Evidence 查询成功。 |
| 实际后果 | 模型看到“判定依据”，却没有“当前认知”作为解释框架；可能把某条历史 Evidence 当成当前掌握程度，或不知道这些证据对应哪个学习阶段。Evidence 区块本身没有“当前 Understanding 缺失”的提示。 |
| 严重度 | 中高。检索失败路径会把“不知道当前认知”伪装成“没有当前认知”，与 Phase 4 目标直接冲突。 |

### 4.2 摘要留下，但时间/状态丢失

| 项目 | 内容 |
|---|---|
| 触发条件 | 任意非空 Session / Understanding / Memory 被注入。 |
| 实际后果 | Session 只渲染 `goal`、`current_focus`，不渲染 `status`、`started_at`、`last_active_at`；Understanding 只渲染状态标签与内容，不渲染 `updated_at`；Memory 渲染了 `created_at/updated_at` 与 scope，但没有 `status`/importance/version。模型无法判断“这是本周的还是三个月前的”“这个 Session 是否已暂停/完成”“这条 Understanding 是否已过期”。 |
| 严重度 | 中。不会直接注入错误内容，但会让模型把陈旧信息当成当前事实；属于“看似有上下文、实际缺少时效语义”。 |

### 4.3 内容留下，但原始来源引用丢失

| 项目 | 内容 |
|---|---|
| 触发条件 | Evidence / Related Evidence 被注入。 |
| 实际后果 | `_render_evidence` 只输出 `[correctness/type] 时间 + observation`，没有 `EvidenceItem.sources`（MESSAGE / HIGHLIGHT / TEST_ANSWER 及其 source_id）；`ContextSection.source_ids` 只保存 `evidence:{id}`，不进入 prompt。模型无法回答“这条判断依据具体来自哪条消息/哪次测试/哪个批注”，也无法让用户核对原始出处。Knowledge 片段有文档名/片段序号/章节/页码，但同样不渲染 `document_id/chunk_id`（这是刻意的引用格式约束）；Related Evidence 只渲染概念 id，不渲染 evidence id。 |
| 严重度 | 中。对“为什么你认为我理解了”这类追问，模型只能复述 observation，不能指向原始来源；与 Citation 三态不是同一层问题。 |

### 4.4 Memory 留下，但 Scope / 有效性丢失

| 项目 | 内容 |
|---|---|
| 触发条件 | 任意 Memory 被注入。 |
| 实际后果 | `_render_memory` 渲染了 `GLOBAL/SPACE/CONCEPT` 的中文标签和内容、时间，但没有 `learning_space_id` / `concept_id`（CONCEPT 级记忆只写“本知识点”，不写是哪个知识点）；没有 `status`（后端只返回 ACTIVE，但 prompt 不知道“这是仍有效的”）；没有 `importance`。当同一用户有多个 Space/Concept 的相似记忆时，模型无法判断适用范围。更关键的是：Runtime 只把 `concept_ids[0]` 传给 hybrid 检索，其他命中概念的 CONCEPT 级记忆**根本没有成为候选**，而这一点在上下文中不可见。 |
| 严重度 | 中。Scope 标签缓解了一部分；但“只查第一个概念”属于系统性漏检，且没有状态说明。 |

### 4.5 Session Summary 留下，但原始对话引用丢失

| 项目 | 内容 |
|---|---|
| 触发条件 | `conversation.session_id` 非空，且 Session 有 `goal`/`current_focus`。 |
| 实际后果 | `ChatAgentRequest.SessionContext` 只有 `id/title/goal/currentFocus`；Agent prompt 只有“这一阶段要解决/当前焦点”，没有 `session_id`、`status`、`started_at`、`last_active_at`，也没有“这个目标最初由哪条对话/哪条消息提出”的引用。`resolveForConversation` 对已归属 Session 直接 `selectById`，不校验状态；如果 Session 已 PAUSED/COMPLETED 或属于旧对话，Agent 仍会把它当成当前学习框架。 |
| 严重度 | 中高。Session 是“当前这段学习活动”的框架；把陈旧/已暂停的 Session 当当前目标，会直接改变回答落点。静态代码无法证明模型会如何处理，**需 D4-5 动态评测验证**。 |

### 4.6 补充：Trajectory 被裁掉但 Understanding 留下

| 项目 | 内容 |
|---|---|
| 触发条件 | 显式复盘请求，预算不足；Trajectory 优先级 68，Understanding 70，Trajectory 先被截断/丢弃。 |
| 实际后果 | 模型看到“当前认知”，但看不到“过去版本/已纠正误区”；`_render_understanding` 尾部有“没有提供历史轨迹时不要反推过去”的规则，能降低风险；但 `_render_trajectory` 被截断时，尾部“以上是全部可核对记录”的免责声明可能被截掉，留下半段历史。 |
| 严重度 | 中。截断方向本身合理（当前认知优先），但截断后的免责声明完整性没有被检查。 |

---

## 5. T2.3 缺陷清单

> 严重度定义：**高** = 会把“检索失败/范围不足”伪装成“没有数据”，可能导致模型对用户作出错误断言；**中** = 会丢失范围/时效/来源语义，或造成重复/不完整注入；**低** = 可观测性/文案/边界情况。
>
> 每条均给出前端/后端位置；“前端”指 Agent 侧（Python），“后端”指 Spring Boot/MySQL。本批未运行测试，缺陷均为静态推断。

### 高

| 编号 | 类型 | 位置 | 触发 | 后果 | 严重度 |
|---|---|---|---|---|---|
| D4-1-H01 | 无效注入 / 三态误消费 | Agent：`http_backend.py:117-127`、`422-458`；`runtime.py:812-814`；`agent_state.py:88`；`builder.py:440-460` | Understanding 的任一 concept GET 返回 5xx/超时/非统一响应/业务错误/`null`（注意：401 例外，`_get` 无论 `fatal` 都会抛 `RetrievalError`，会中断整轮而不是静默跳过） | 失败被静默跳过，`state.understandings` 少一项；`_collect_warnings` 可能输出“没有检索到任何 Understanding”；模型把“查询失败”当成“用户没学过”。**DEGRADED 被当成 EMPTY** | 高 |
| D4-1-H02 | 无效注入 / 三态误消费 | Agent：`http_backend.py:129-167`、`461-504`；`runtime.py:787-790`；`agent_state.py:88-89`；`builder.py:485-495` | Memory hybrid 返回 `UNAVAILABLE` 或 `DEGRADED_VECTOR/DEGRADED_KEYWORD` | `UNAVAILABLE → []`；`DEGRADED_* → items`（无状态）；`AgentState` 无 memory 状态/警告；ContextBuilder 无 Memory 状态区块。模型把“记忆检索不可用/降级”当成“没有相关记忆” | 高 |
| D4-1-H03 | 缺失注入 / 三态误消费 | Agent：`http_backend.py:375-391`；`runtime.py:858-877`；`builder.py:462-477` | 显式复盘请求，`get_revisions` 失败（`fatal=False`） | `revisions = {}`，`trajectories` 为空；模型可能断言“你过去没有这个误区/没有相关记录”，而实际是 Revision 查询失败。`_render_understanding` 的“没有轨迹不要反推”只能约束一部分表述 | 高 |
| D4-1-H04 | 缺失注入 / 状态语义丢失 | Agent：`builder.py:441-443`；Backend：`UnderstandingResponse.java:34-46` | 快照 `status=EXPOSED`，但 `summary/known_parts/misconceptions/uncertain_parts/latest_user_explanation` 都为空 | `has_content` 为假，整条 Understanding 被跳过；模型看不到“用户接触过这个知识点”这一事实。状态本身有信息量，却被当成空快照 | 高（对“从哪讲起”的判断影响大） |

### 中

| 编号 | 类型 | 位置 | 触发 | 后果 | 严重度 |
|---|---|---|---|---|---|
| D4-1-M01 | 范围缺失（漏检） | Agent：`http_backend.py:143-153` | 本轮命中多个 concepts | 只把 `concept_ids[0]` 作为 `conceptId` 传给 hybrid；其他概念的 CONCEPT 级记忆不会被检索；返回空列表时与“确实没有”不可区分。Space 级/GLOBAL 级不受影响 | 中 |
| D4-1-M02 | 错误注入（范围未在 SQL 证明） | Agent：`http_backend.py:117-127`；Backend：`UnderstandingController.java:31-44`、`UnderstandingService.java:412-416` | 调用方传入当前用户其他 Space 的 conceptId | Understanding 端点不接收/不校验 `spaceId`，Understanding 表无 `space_id`；只要概念属于当前用户就会返回。当前运行路径靠 `list_concepts(space_id)` 保证，但接口自身不绑定 Space | 中（纵深防御缺口；当前路径未证明可被利用） |
| D4-1-M03 | 错误注入（范围未在 SQL 证明） | Agent：`http_backend.py:229-286`；Backend：`EvidenceService.java:201-251`、`LearningEvidenceMapper.java:22-38` | conceptIds 混入其他 Space 的同用户概念 | SQL 只有 `user_id + concept_id IN`；Space 过滤在 Java 内存中做（`learningSpaceId == spaceId`）；若 Service 过滤被绕过/改动，SQL 不会兜底。当前路径依赖 conceptIds 来源 | 中 |
| D4-1-M04 | 缺失注入 / 状态不可见 | Agent：`runtime.py:816-820`；`builder.py:537-551`、`1160-1191` | 关系查询 `FAILED` 或 `PARTIAL`（部分 concept 失败） | `related_concepts_status/warning` 只进入 `warnings`；ContextBuilder 只渲染跨概念 Evidence 的 `DEGRADED`，不渲染关系查询 `FAILED/PARTIAL`。模型不知道“相关概念可能漏了” | 中 |
| D4-1-M05 | 错误注入（陈旧 Session） | Backend：`SessionService.java:70-74`；`ChatService.java:463-471`；Agent：`builder.py:870-877` | `conversation.session_id` 指向已 PAUSED/COMPLETED 或长期未活跃的 Session | 直接 `selectById` 返回；`SessionContext` 不含 status/时间；prompt 把旧目标当当前框架。是否真的误导模型 **需 D4-5 动态评测验证** | 中高 |
| D4-1-M06 | 缺失语义（时效） | Agent：`builder.py:880-915`（Understanding 无 `updated_at`）、`870-877`（Session 无时间） | 任意 Understanding/Session 注入 | 模型无法判断认知快照/学习目标的陈旧程度；无法表达“这是很久以前的判断” | 中 |
| D4-1-M07 | 缺失语义（来源） | Agent：`builder.py:1224-1231`、`1234-1255`；`schemas.py` EvidenceItem.sources | Evidence/Related Evidence 注入 | 不渲染 `sources`（消息/批注/测试回答）与 evidence id；模型无法指向原始出处 | 中 |
| D4-1-M08 | 重复注入 | Agent：`builder.py:440-460`（Understanding）与 `462-477`（Trajectory） | 显式复盘，同一概念同时有 Understanding 与 Trajectory | 两段都包含“当前状态/当前误区”；Trajectory 还包含历史误区。语义有区分（当前 vs 历史），但同一事实出现两次；`_render_understanding` 未检测 Trajectory 已注入 | 低中 |
| D4-1-M09 | 重复注入/冲突 | Agent：`builder.py:444-447`、`904-908`、`485-495` | 同一概念同时有 LEARNING Memory 与 Understanding | Memory 与 Understanding 都描述学习状态；Understanding 尾部有“另有一条较早记录”提示，但 Memory 区块本身没有反向提示；两条内容可能矛盾 | 低中 |
| D4-1-M10 | 丢弃披露不完整 | Agent：`builder.py:694-730` | 预算同时留下部分认知记录、丢掉另一部分 | 只有当 `dropped_records` 非空且 `kept_records` 为空时才披露“检索到了但被预算丢弃”。若同时有 Memory 留下、Understanding 被丢，prompt 不会说明 Understanding 被丢；模型可能把“没有 Understanding”当成“没有认知记录” | 中 |
| D4-1-M11 | 无类别级最终状态 | Agent：`builder.py:1026-1052` | 任意预算裁剪 | 只有 Knowledge 有 `SUCCESS_PARTIAL/SUCCESS_BUT_DROPPED` 最终状态；Understanding/Memory/Evidence/Related/Trajectory 只有 section 级 `dropped/truncated`，没有“检索成功但全部/部分未注入”的类别状态 | 中（D4-3/D4-4 的核心缺口） |
| D4-1-M12 | 无逐条排除原因 | Agent：`builder.py:605-618`（`_dedupe` 静默 `continue`） | 两个 section 有相同 `dedupe_key` | 被去重的那条没有任何记录；`EXCLUDED` 不可解释。当前正常路径下 dedupe 冲突很少，但 Manifest 需要这个字段 | 中（可观测性） |
| D4-1-M13 | 无按类别预算 | Agent：`builder.py:622-672` | 任意预算紧张 | 只有一个全局优先级池；`recent_history_limit`、`knowledge_top_k` 是检索前限制，不是最终分配。无法表达“给 Understanding 预留多少、给 Knowledge 预留多少”；高优先级类别可能挤掉所有低优先级类别 | 中（D4-3 缺口） |
| D4-1-M14 | 无保留预算（reserved） | Agent：`builder.py:622-672` | 预算紧张 | 没有为“状态说明/免责声明”预留空间；`_upsert_budget_knowledge_status` 是预算后强行补入，可能造成 `context_tokens` 超预算（虽有 `budgetExceeded` 记录） | 低中 |
| D4-1-M15 | 截断后免责声明丢失 | Agent：`builder.py:663-672`、`930-997` | Trajectory 被 `_reduce_one` 截断 | 尾部“以上是全部可核对记录……”可能被截掉；模型看到半段历史却没有“这是不完整记录”的说明 | 中 |
| D4-1-M16 | NOT_QUERIED 被渲染成 EMPTY | Agent：`builder.py:705-722`、`749-755` | 本轮没有识别到任何 Concept（`concept_ids` 为空，U/M/E/Trajectory 均未检索） | `_collect_warnings` 仍输出“没有检索到任何 Understanding”；`_render_system_instruction` 在无任何认知 section 时输出“本次没有检索到任何历史认知记录”。实际上本轮**没有发起**这些检索。模型可能对用户说“你没有相关记录”，而正确语义是“本轮没有查”（新话题/闲聊）。 | 中高 |

### 低

| 编号 | 类型 | 位置 | 触发 | 后果 | 严重度 |
|---|---|---|---|---|---|
| D4-1-L01 | 重复注入（防御性） | Agent：`builder.py:584-601` | 后端历史末尾与本轮输入文本完全相同 | 上一轮同文消息被当成副本丢掉；用户连续问同一句话时少一轮历史。代码注释已承认此误判方向 | 低 |
| D4-1-L02 | 重复注入 | Agent：`runtime.py:828-856`、`_mark_cross_concept_evidences` | 扩展概念的 Evidence 与当前概念 Evidence 相同 id | 代码会去重（`seen`/`set(concept_ids)`），正常不重复；但被丢弃的重复项没有 `EXCLUDED` 记录 | 低 |
| D4-1-L03 | 无效注入（不完整） | Agent：`http_backend.py:165-167`；`runtime.py:822-826` | Evidence `DEGRADED` 但仍有部分 items | items 会进入上下文；`_render_evidence_status` 有“部分失败”提示，但没有逐条标注哪些结果可能不完整 | 低中 |
| D4-1-L04 | 状态文案 | Agent：`builder.py:705-722` | 无 Understanding、无 Memory、无 Session、无 Trajectory | 输出“本次没有检索到任何历史认知记录”；若 Understanding 实际是查询失败（H01），该文案会误导。已有 degraded_notes 缓解 Evidence/Resolver 降级，但没有 Understanding/Memory 降级 | 中（与 H01/H02 同源） |
| D4-1-L05 | 隐私/可观测性 | Agent：`builder.py:749-783`、`BuiltContext.as_dict` | 把 warnings/systemInstruction/messages 原样暴露给 preview | Preview 端点返回完整 system instruction 与 messages（含用户内容、记忆内容、文档片段）。这不是生产 chat 路径，但如果 preview 被用于日志/Manifest，会违反“不落全文”边界 | 中（设计边界，不是当前泄露） |

### 正向确认（不是缺陷，但应在 D4-2～D4-5 保留）

1. **Knowledge 版本隔离最完整**：`doc_version` + `index_version` + fingerprint + 白名单 fail closed；预算后还有 `SUCCESS_PARTIAL/SUCCESS_BUT_DROPPED`，明确区分“检索到但没放下”与“没检索到”。
2. **Evidence 三态已进入 prompt**：`EvidenceStatus.SUCCESS/EMPTY/DEGRADED`，DEGRADED 有专门 system instruction，明确禁止说成“没有证据”。
3. **Resolver 降级有专门提示**：`resolver_status` 只在 degraded/failed 时渲染，且明确“不能据此判断用户没有相关历史”。
4. **Memory SQL 可见性规则完整**：GLOBAL/SPACE/CONCEPT + `status='ACTIVE'` + 强制 MySQL 回查，跨 Space 在 SQL 层有证明。
5. **ConceptRelation SQL 范围证明最强**：双端 `learning_space_id` + `deleted_at IS NULL` + 类型白名单。
6. **冲突处理有 prompt 级规则**：Trajectory 区块明确“当前状态以当前认知为准”；Memory 区块明确“与当前认知冲突时说明这是较早记录”。
7. **ContextBuilder 已有 section 级可观测性**：`priority/source_ids/estimated_tokens/truncated/dropped/preview`，以及 `droppedSections/truncated` warnings；这是 D4-4 可以直接复用的基础。

---

## 6. T3 ContextBuilder 当前能力 vs Context Selector 应具备能力

### 6.1 能力对照

| 能力 | ContextBuilder 当前 | 是否满足 Phase 4 | 说明 |
|---|---|---|---|
| 是否检索 / 检索什么 | ❌ | ❌ | 决策在 `runtime._retrieve`：`_wants_evidence`、`_is_history_review`、`_EVIDENCE_TRIGGERS`、`_REVIEW_TRIGGERS` 硬编码词表；Knowledge 每轮必查；Memory/Understanding 有 concepts 就查。没有统一 `ContextPolicy`/`ContextPlan`，没有 policy version，没有“为什么查/为什么不查”的结构化输出 |
| 候选收集 | ✅ | ✅ | `_collect_sections` 覆盖 user_input/history/session/understanding/trajectory/memory/concept_status/knowledge_status/knowledge/evidence_status/evidence/related_evidence |
| 优先级 | ✅（固定常量） | ⚠️ | 优先级是代码常量，不可按 intent/场景配置；D4-2 若引入 policy，应让优先级/上限由 policy 提供 |
| 去重 | ⚠️（精确 key） | ⚠️ | `_dedupe` 只按 `dedupe_key` 精确匹配；无内容级/语义级去重；被去重项无记录 |
| 全局 Token 预算 | ✅ | ✅ | `_fit_budget` + `_reduce_one` + `budgetExceeded/overrunReason` |
| 按类别预算/配额 | ❌ | ❌ | 只有一个全局池；没有 per-category cap/reserved |
| 截断 | ✅ | ⚠️ | `allow_truncate` section 可截断；截断后不保证免责声明完整；没有类别级 `TRUNCATED` 状态 |
| 整段丢弃 | ✅ | ✅ | `dropped` + warnings |
| 排除原因 | ❌ | ❌ | dedupe/limit/scope/version 排除都没有结构化 reason |
| 类别最终状态 | ⚠️（仅 Knowledge） | ❌ | Knowledge 有 6+3 状态；Understanding/Memory/Evidence/Related/Trajectory 没有 |
| 三态消费 | ⚠️ | ❌ | Evidence/Knowledge/Resolver 已消费；Memory/Understanding/Revision/Related 未消费 |
| 冲突处理 | ✅（prompt 规则） | ⚠️ | 有 system instruction 规则；实际遵循度 **需 D4-5 动态评测验证** |
| Prompt 渲染 | ✅ | ✅ | section 渲染 + RENDER_ORDER + 固定教学契约 |
| Manifest/可观测 | ⚠️（section 级） | ❌ | `BuiltContext.as_dict` 只服务 preview；没有 run 级 Manifest，没有持久化，没有逐条 selected/excluded/token 占比 |
| 评测接口 | ❌ | ❌ | 没有按 policy/状态/预算维度组织的最小评测场景集 |

### 6.2 明确回答：Phase 4 是否需要新增 Selector？

**结论：目前没有证据表明需要“新增一个独立 Context Selector”。** 理由：

1. ContextBuilder 已经承担了 Selector 的核心职责：候选收集、优先级、全局预算、截断/丢弃、prompt 渲染。D4-1 没有发现“选不出来”或“预算无法执行”的能力缺口。
2. 真正的缺口不在“选择”本身，而在三处：
   - **选择之前的决策**（什么时候查什么、查多少）散落在 `runtime._retrieve` 的硬编码触发词/布尔条件里 → 这是 **Policy/Planner（D4-2）** 的缺口。
   - **选择过程的可解释性**（选中/排除/截断/丢弃的原因、类别级最终状态、token 占比）没有结构化输出 → 这是 **Manifest（D4-4）** 与 ContextBuilder 的**小幅扩展**。
   - **失败与空结果的区分**（Understanding/Memory/Revision/Related 的 DEGRADED 没有进入状态）→ 这是**检索层与状态字段**的缺口，不是 Selector 的缺口。
3. 如果 D4-2 的 Policy 需要“多个候选来源打分/重排”，才可能出现独立的 Selector；当前代码没有这种候选集，也没有多路召回同一类数据的竞争。**先做 Policy + 状态补齐 + Manifest，再根据 D4-5 评测结果决定是否引入 Selector 打分层。**

因此，Phase 4 的建议架构是：

```
Context Policy/Planner (D4-2)  →  现有 ContextBuilder（扩展 per-category budget/exclusion reasons）  →  Manifest (D4-4)  →  Evaluation (D4-5)
```

而不是：

```
Context Policy → 新 Selector（替换/并列 ContextBuilder）→ ...
```

---

## 7. T4 Manifest 可观测性方案（只设计，不实现）

### 7.1 Manifest 要回答的 7 个问题

1. 每个 Retriever 是否被触发（`triggered` + `triggerReason`）。
2. 执行状态（8 状态：`NOT_QUERIED/EMPTY/SUCCESS/DEGRADED/SELECTED/EXCLUDED/TRUNCATED/OMITTED`）。
3. 候选数 / 选中数 / 排除数 / 截断数 / 丢弃数。
4. 失败、超时、部分失败（`DEGRADED` 细分）。
5. 排除原因（枚举，不存原文）。
6. 预算裁剪前后的 token 估算（`budget.before/after`，按类别）。
7. 最终上下文中各类别的 token 占比。

**关键设计约束：八状态不是单一互斥枚举。**

- `NOT_QUERIED / EMPTY / SUCCESS / DEGRADED` 描述**检索阶段**；
- `SELECTED / EXCLUDED / TRUNCATED / OMITTED` 描述**注入阶段**；
- 同一类别可以同时是 `SUCCESS + OMITTED`（查到了但没放进 prompt），也可以同时是 `DEGRADED + SELECTED`（部分失败但仍有可用结果）。用一个 `status` 字段压扁会丢失信息。

因此 Manifest 对每个类别至少记录两个轴：`retrievalStatus` 与 `injectionStatus`；逐条 item 再记录 `SELECTED/EXCLUDED`。下方示例按这个结构给出。

### 7.2 建议的 Manifest Schema（v1）

```jsonc
{
  "manifestVersion": 1,
  "runId": 12345,
  "traceId": "b7c2...",
  "runType": "CHAT",
  "contextPolicyVersion": "d4-2-v1",       // D4-2 前可为 null
  "retriever": {
    "requestedMode": "http",
    "resolvedSource": "http",
    "degraded": false,
    "fallbackReason": null
  },
  "intent": {
    "resolverStatus": "OK",
    "resolverIntent": "NEW_TOPIC",
    "detectedConceptIds": [10, 11],
    "historyReviewRequested": false,
    "evidenceRequested": true,
    "triggerReasons": ["concept_hit", "evidence_lexical:为什么"]
  },
  "sessionId": 20,
  "budget": {
    "maxContextTokens": 3000,
    "reservedTokens": 0,
    "contextTokensBefore": 4120,
    "contextTokensAfter": 2870,
    "promptTokens": 3400,
    "overrun": false,
    "overrunReason": null
  },
  "categories": {
    "session": {
      "triggered": true,
      "retrievalStatus": "SUCCESS",
      "injectionStatus": "SELECTED",
      "states": ["SUCCESS", "SELECTED"],
      "candidates": 1,
      "selected": 1,
      "excluded": 0,
      "truncated": 0,
      "omitted": 0,
      "tokens": 42,
      "selectedItems": [
        {"sourceId": "session:20", "reason": "PRIORITY_SESSION", "tokens": 42}
      ],
      "excludedItems": [],
      "warnings": []
    },
    "history": { "...": "同上，sourceId=message:<id>" },
    "understanding": {
      "triggered": true,
      "retrievalStatus": "SUCCESS",       // NOT_QUERIED / EMPTY / SUCCESS / DEGRADED
      "injectionStatus": "SELECTED",      // SELECTED / EXCLUDED / TRUNCATED / OMITTED / PARTIAL
      "states": ["SUCCESS", "SELECTED"],
      "candidates": 2,
      "selected": 1,
      "excluded": 1,
      "truncated": 0,
      "omitted": 0,
      "tokens": 180,
      "selectedItems": [
        {"sourceId": "understanding:500", "conceptId": 10, "reason": "HAS_CONTENT", "tokens": 180}
      ],
      "excludedItems": [
        {"sourceId": "understanding:501", "conceptId": 11, "reason": "NO_CONTENT"}
      ],
      "warnings": []
    },
    "memory": {
      "triggered": true,
      "retrievalStatus": "DEGRADED",      // 8 状态里的检索轴
      "retrievalDetail": "DEGRADED_VECTOR", // 后端原始细分
      "injectionStatus": "PARTIAL",
      "states": ["DEGRADED", "SELECTED"],
      "candidates": 3,
      "selected": 2,
      "excluded": 1,
      "truncated": 0,
      "omitted": 0,
      "tokens": 96,
      "selectedItems": [
        {"sourceId": "memory:100", "scope": "SPACE", "reason": "RRF_HIT", "tokens": 48}
      ],
      "excludedItems": [
        {"sourceId": "memory:101", "reason": "SCOPE_NOT_APPLICABLE"}
      ],
      "warnings": ["VECTOR_DEGRADED"]
    },
    "knowledge": {
      "triggered": true,
      "retrievalStatus": "SUCCESS",
      "injectionStatus": "PARTIAL",
      "states": ["SUCCESS", "SELECTED", "OMITTED"],
      "candidates": 8,
      "aboveThreshold": 5,
      "selected": 3,
      "excluded": 2,
      "truncated": 0,
      "omitted": 2,
      "tokens": 620,
      "injectedChunkIds": [9001, 9002, 9003],
      "droppedChunkIds": [9004, 9005],
      "excludedItems": [
        {"sourceId": "chunk:9006", "reason": "THRESHOLD"},
        {"sourceId": "chunk:9007", "reason": "INVALID_PAYLOAD"}
      ],
      "warnings": []
    },
    "evidence": {
      "triggered": true,
      "retrievalStatus": "SUCCESS",
      "injectionStatus": "SELECTED",
      "states": ["SUCCESS", "SELECTED"],
      "candidates": 3,
      "selected": 3,
      "excluded": 0,
      "truncated": 0,
      "omitted": 0,
      "tokens": 210,
      "selectedItems": [
        {"sourceId": "evidence:700", "conceptId": 10, "reason": "RECENT_EVIDENCE", "tokens": 70}
      ],
      "excludedItems": [],
      "warnings": []
    },
    "relatedEvidence": {
      "triggered": true,
      "retrievalStatus": "SUCCESS",
      "retrievalDetail": "OK",
      "injectionStatus": "SELECTED",
      "states": ["SUCCESS", "SELECTED"],
      "relationStatus": "OK",
      "candidates": 3,
      "selected": 2,
      "excluded": 1,
      "truncated": 0,
      "omitted": 0,
      "tokens": 140,
      "selectedItems": [
        {"sourceId": "evidence:800", "conceptId": 11, "reason": "RELATED_VIA:PREREQUISITE", "tokens": 70}
      ],
      "excludedItems": [
        {"sourceId": "concept:11", "reason": "EXPANSION_LIMIT"}
      ],
      "warnings": []
    },
    "trajectory": {
      "triggered": true,
      "retrievalStatus": "SUCCESS",
      "injectionStatus": "TRUNCATED",
      "states": ["SUCCESS", "SELECTED", "TRUNCATED"],
      "candidates": 3,
      "selected": 1,
      "excluded": 0,
      "truncated": 1,
      "omitted": 0,
      "tokens": 300,
      "selectedItems": [
        {"sourceId": "trajectory:10", "reason": "REVIEW_REQUESTED", "tokens": 300}
      ],
      "excludedItems": [],
      "warnings": ["TRAJECTORY_TRUNCATED"]
    }
  },
  "retrieverStatus": {
    "knowledge": "SUCCESS",
    "understanding": "SUCCESS",
    "memory": "DEGRADED",
    "memoryDetail": "DEGRADED_VECTOR",
    "evidence": "SUCCESS",
    "relatedConcepts": "SUCCESS",
    "revisions": "SUCCESS"
  },
  "trim": {
    "dropped": [
      {"sourceId": "memory:102", "category": "memory", "priority": 50, "reason": "BUDGET"}
    ],
    "truncated": [
      {"sourceId": "trajectory:10", "category": "trajectory", "beforeTokens": 600, "afterTokens": 300, "reason": "BUDGET"}
    ]
  },
  "final": {
    "byCategoryTokens": {
      "history": 480,
      "session": 42,
      "understanding": 180,
      "memory": 96,
      "knowledge": 620,
      "evidence": 210,
      "relatedEvidence": 140,
      "trajectory": 300,
      "statusNotes": 80
    },
    "contextTokens": 2870,
    "promptTokens": 3400,
    "messageCount": 9
  }
}
```

### 7.3 存储边界

**允许记录：**

- `run_id`、`trace_id`、`context_policy_version`、`intent`、`session_id`。
- 逐类别的 `selected`：`source_id` + `reason` + `token`（`source_id` 用内部 id，如 `chunk:9001`、`evidence:700`、`understanding:500`）。
- 逐类别的 `excluded`：`source_id` + `reason`（枚举）。
- `budget`：`total / used / reserved`、裁剪前后估算、是否超预算。
- `retriever_status` 汇总。
- `trim`：被丢弃/截断的 source_id、类别、优先级、原因、前后 token。
- 类别级 `status` 与 `retrievalStatus`。

**禁止记录：**

- 任何正文：`KnowledgeChunk.content`、`EvidenceItem.observation`、`MemoryItem.content`、`Understanding.summary`、`Session.goal/current_focus`、`HistoryMessage.content`。
- 完整 prompt / system instruction / messages。
- 用户原始 query、`keyword`、文档原文、文件名（文件名可能含敏感信息；如需要，只存 hash 或 id）。
- 原始异常堆栈/底层错误文本；只存 `warningCode` 或脱敏后的枚举。
- `BuiltContext.as_dict` 里的 `preview`（当前是 200 字内容）、`systemInstruction`、`messages`。

### 7.4 复用现有 AgentRun / Trace 能力

**可直接复用：**

| 现有字段/事件 | 位置 | 可复用内容 |
|---|---|---|
| `agent_run.id` | `AgentRun.java:27-28` / `schema.sql:563` | Manifest 的 `runId`，1:1 |
| `agent_run.trace_id` | `AgentRun.java:70` / `schema.sql:570` | 跨进程关联，Manifest 的 `traceId` |
| `agent_run.user_id / conversation_id / session_id / run_type / status` | `AgentRun.java:30-39` | 权限/场景过滤；Manifest 不必重复存储，可 join |
| `agent_run.duration_ms / created_at / finished_at` | `AgentRun.java:60-78` | 延迟与保留期 |
| `agent_run.input_summary / output_summary` | `AgentRun.java:57-59` | 当前只存 `content[:255]` 与 `chars=...`；**不适合**承载 Manifest（语义冲突、可能被覆盖） |
| Python `RunTrace` 事件 | `agent/app/trace.py:22-45` | `RUN_START/CONCEPT_DETECT/CONCEPT_RESOLVE/RETRIEVE/CONTEXT_BUILT/LLM_CALL/CITATION_CHECK/CITATION_FILTER/EVIDENCE_GUARD/HISTORY_GUARD/RUN_END/RUN_ERROR` 已覆盖决策与检索的主要节点 |
| `RETRIEVE` trace 字段 | `runtime.py:879-911` | `conceptIds`、`understandings`、`memories`、`knowledge`、`knowledgeStatus`、`knowledgeCandidates`、`evidences`、`evidenceStatus`、`crossConceptEvidenceStatus`、`evidenceRequested`、`relatedConceptStatus`、`relatedConceptCandidates`、`expandedConceptIds`、`historyReviewRequested`、`trajectoryConcepts`、`trajectoryRevisions` |
| `CONTEXT_BUILT` trace 字段 | `runtime.py:648-659` | `estimatedTokens`、`droppedSections`、`knowledgeStatus`、`knowledgeState`、`knowledgeInjected`、`knowledgeDropped`、`warnings` |
| `BuiltContext.as_dict` 结构字段 | `builder.py:257-281` | `estimatedTokens/contextTokens/budgetTokens/budgetExceeded/budgetOverrunReason/knowledgeStatus/knowledgeState/knowledgeInjectedChunkIds/knowledgeDroppedChunkIds/droppedSections/warnings/sections(as_dict)`；**只取结构字段，不取 preview/systemInstruction/messages** |
| `ContextSection.as_dict` | `builder.py:199-211` | `name/priority/sourceIds/estimatedTokens/truncated/dropped/preview`；Manifest 只取前 6 项，`preview` 禁用 |
| SSE `meta` | `main.py`/`runtime.py:257-268` | `detectedConcepts/estimatedTokens/warnings/retrieverSource/degraded/fallbackReason` |
| SSE `done` | `runtime.py:290-304` | `detectedConcepts/estimatedTokens/elapsedMs/finishReason/truncated/citations`；Manifest 可在此扩展 |
| `scheduled_task_lease` | `schema.sql:515-521` | 保留期清理任务可复用现有租约机制，不引入新中间件 |

**当前不可复用/缺失：**

1. **Agent trace 不跨 HTTP 持久化。** `stream` 的 `done` 事件不包含 trace；`HttpPythonAgentGateway.dispatch` 只解析 `finishReason/truncated/citations`（`HttpPythonAgentGateway.java:290-300`）。Trace 只在 Agent 进程日志与 `/agent/context/preview` 响应里出现（`main.py:211-225`）。
2. **`agent_run` 没有 JSON 列。** 只有 `input_summary/output_summary` 两个 TEXT；Manifest 没有正式落点。
3. **Backend 不读取 Agent 的 `meta`。** `HttpPythonAgentGateway` 对 `meta` 只 `log.debug`（第 280-283 行），不会进入 run。
4. **没有逐条 selected/excluded 的事实。** 现有 trace 只有计数和 section 名称；`sourceIds` 在 `ContextSection` 里，但没有“为什么选中/排除”的 reason。

### 7.5 存储建议

**首选：在 `agent_run` 增加一个可空 JSON 列 `context_manifest`，不新增表。**

理由：

- Manifest 与 run 严格 1:1，放同一行最自然，查询/保留期/权限都复用 `agent_run`。
- 不引入新数据库/中间件；只加列，不改现有读写语义（所有旧代码忽略该列）。
- 体积可控：只存 id/reason/token，不存正文；典型 CHAT run 约 4–16 KB，远小于 MySQL JSON 上限。
- 可用 MySQL JSON 函数做聚合（如按 `contextPolicyVersion`、`retrieverStatus` 统计），不需要额外索引；如需要，可加生成列或普通索引，但这属于 D4-4 实现细节。
- 保留期沿用 `agent_run.created_at`；清理任务可复用 `scheduled_task_lease`。

**备选（如果本阶段不允许改表结构）：**

- 在 `done` 事件中增加 `contextManifest`，由 `HttpPythonAgentGateway` 解析后写入 `output_summary`。但这会把人类可读摘要与结构化 JSON 混在同一列，且 `ChatService.finishRun` 目前会用 `chars=...` 覆盖 `outputSummary`；需要额外约定与迁移，**不如加列干净**。
- 只写 Agent 日志：可用于排查，但不可查询、不可按 run 关联、不可做评测统计，不能作为 D4-4 的正式存储。

**不推荐：新增 Manifest 表。** 1:1 关系、低频写入、按 run 查询，单独建表只会增加 join 与一致性负担；除非未来需要跨 run 的大规模分析仓库，否则没有必要。

### 7.6 保留期与清理建议

- CHAT Manifest：保留 **90 天**（可按配置调整）；超过后 `context_manifest = NULL` 或删除整行（遵循现有 `agent_run` 保留策略）。
- ANALYSIS Manifest：默认不生成；如生成，保留 30 天。
- 清理方式：按 `created_at` 分批（例如每批 1000 行）置空/删除，复用 `scheduled_task_lease` 防多实例并发；不引入新中间件。
- 长期统计：如需保留超过 90 天，只保留聚合计数（按 policyVersion/status/category），不保留逐条 source_id。
- 隐私：清理前确认 Manifest 不含正文；`source_id` 本身是内部 id，不构成内容泄露，但仍应按内部数据管理。

---

## 8. T5.1 D4-2～D4-5 范围建议

### D4-2 Context Policy / Planner

**是否必要：必要。** 当前“什么时候查什么”完全由 `runtime._retrieve` 的硬编码词表和布尔条件决定：

- `_EVIDENCE_TRIGGERS`（`runtime.py:100-113`）
- `_REVIEW_TRIGGERS` / `_is_history_review`（`runtime.py:117-134`、`1199-1204`）
- `wants_evidence = bool(concept_ids) and _wants_evidence(query)`（`runtime.py:764`）
- `retrieve_trajectory = bool(concept_ids) and state.history_review_requested`（`runtime.py:767`）
- Memory/Understanding 只受 `concept_ids` 控制；Knowledge 每轮必查（`runtime.py:781`）。

建议新增一个**纯决策层**（可以是 `agent/app/context/policy.py`，不引入中间件），输出：

```python
ContextPlan(
    policy_version="d4-2-v1",
    intent=...,
    needs={"knowledge": True, "understanding": True, "memory": True,
           "evidence": False, "related": False, "trajectory": False},
    reasons={...},
    limits={"knowledge_top_k": 5, "evidence_top_k": 3, "history_limit": 12, ...},
)
```

`runtime._retrieve` 只负责执行 plan，不再内嵌触发词。**这是重构/提取，不是新增一个并行检索系统。** 规模：中（主要是把现有布尔判断集中、加 policy version 与 reasons；不改变检索实现）。

### D4-3 Selector / Budget Allocator

**差集大小：小到中。** ContextBuilder 已经有选择与全局预算；D4-3 应优先做“扩展”而不是“新 Selector”：

1. 类别级预算/配额（per-category cap / reserved），避免高优先级类别挤掉全部低优先级类别。
2. 逐条 `EXCLUDED` reason（dedupe/limit/scope/version/threshold/invalid）。
3. 类别级最终状态（不只是 Knowledge）：`SUCCESS/SELECTED/TRUNCATED/OMITTED/DEGRADED`。
4. 截断后的免责声明完整性检查（尤其 Trajectory）。

只有当 D4-2 引入“同一类别多路候选需要打分/重排”时，才需要独立 Selector；当前代码没有这种候选竞争。规模：小到中（主要在 `builder.py` 内加元数据与预算分配，不改整体结构）。

### D4-4 Manifest

**能否独立于 D4-2/D4-3 先做：可以，但建议分两步。**

- **v1（可独立）**：基于当前 `state` + `ContextBuilder` section 元数据 + `RETRIEVE/CONTEXT_BUILT` trace，生成“结构性 Manifest”（触发/计数/状态/预算/选中与丢弃的 source_id）。此时 exclusion reasons 只能到 `BUDGET/DEDUPE/NO_CONTENT` 这一级。
- **v2（依赖 D4-2/D4-3）**：补 `contextPolicyVersion`、`intent/reasons`、逐条 `EXCLUDED` 原因、类别级最终状态。
- 持久化需要 Backend 配合：`done` 事件增加 `contextManifest`（或单独的 manifest 通道）+ `agent_run.context_manifest` 列。**这一步不依赖 D4-2/D4-3 的算法，只依赖 Manifest schema 定稿。** 规模：中。

### D4-5 Evaluation

**最小场景集（建议至少覆盖）：**

1. **NOT_QUERIED vs EMPTY vs DEGRADED**：无 concepts（不查 U/M/E）；有 concepts 但无 Understanding（EMPTY）；Understanding 接口 500（DEGRADED）。检查回答是否把后两者说成“没学过”。
2. **Memory UNAVAILABLE / DEGRADED_VECTOR / DEGRADED_KEYWORD**：检查回答是否把“记忆检索失败”说成“没有记忆”。
3. **Revision 查询失败**：显式复盘问题 + `/revisions` 500；检查回答是否断言“你过去没有这个误区”。
4. **EXCLUDED vs TRUNCATED vs OMITTED**：同一概念有 2 条 Understanding/多条 Knowledge；预算只够 1 条；检查模型是否知道“还有记录但没放进来”。
5. **Knowledge `THRESHOLD_FILTERED` / `NO_VALID_DOCUMENTS` / `NO_MATCH`**：检查回答是否把三者混为“资料里没有”。
6. **Knowledge `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`**：检查模型是否只引用实际出现的片段，不编造被丢弃片段。
7. **跨用户/跨 Space**：用其他用户/其他 Space 的 conceptId 调 Understanding/Evidence 接口，确认 404/空结果；再检查 Agent 是否会把空结果说成“没有历史”。
8. **软删文档**：`document.deleted_at` 非空但 Qdrant point 仍在；确认 ready-ids 排除、检索不返回。
9. **版本/指纹**：旧 `index_version` point + 新白名单；严格指纹开关开/关；确认不混用。
10. **旧 Evidence vs 当前 Understanding**：先形成误区 Evidence，再产生纠正后的 Understanding；检查回答是否把旧 Evidence 当当前状态。
11. **Memory Scope**：GLOBAL/SPACE/CONCEPT 三条相似记忆 + 多概念请求；检查是否只用了 `concept_ids[0]` 的 CONCEPT 记忆、是否把其他概念的记忆误用。
12. **Session 陈旧**：PAUSED/COMPLETED/长期未活跃 Session；检查回答是否仍把它当当前目标。
13. **Duplicate injection**：复盘时 Understanding + Trajectory 同时存在；LEARNING Memory + Understanding 同时存在；检查是否重复/冲突。
14. **预算超限**：`max_context_tokens` 很小；检查 `budgetExceeded`、user_input 是否保留、状态说明是否仍在。
15. **Resolver DEGRADED**：Resolver 超时；检查是否仍使用规则结果、是否声称“没有相关历史”。

每个场景至少断言：**检索状态、最终注入的 source_id 集合、prompt 中是否出现禁区断言**。LLM 质量本身用人工/评分器，但“是否声称没有历史”这类事实断言应可自动检查。

---

## 9. T5.2 静态审计与动态评测的边界

### 静态审计（本批）可以判断

- 某类数据**是否被检索**、**是否被选中**、**是否进入 prompt**、**是否被截断/丢弃**（通过代码路径与 `ContextSection` 元数据）。
- **数据流完整性**：Backend → Python → ContextBuilder → prompt 的字段是否齐全、状态是否传递。
- **明显的相关性/范围/版本问题**：例如 `space_id` 未进 SQL、`concept_ids[0]` 漏检、`deleted_at` 未过滤、`index_version` 未校验。
- **权限是否在 SQL 层被证明**：本批已逐条标注“已证明/尚未证明”。
- **三态语义是否被消费**：例如 `DEGRADED` 是否被当成 `EMPTY`。
- **Manifest 能否从现有字段构造**：哪些字段可复用、哪些缺失。

### 静态审计不能判断（必须放入 D4-5）

- LLM **是否真的使用了**某段上下文。
- 上下文是否**改善了回答质量**。
- 上下文是否**帮助模型识别用户薄弱点**。
- 模型是否**遵守**“当前认知优先于历史记忆/轨迹”的 prompt 规则。
- 模型是否会把 `DEGRADED` 说成 `EMPTY`（代码给了提示，但模型可能不遵守）。
- 模型是否会在 `SUCCESS_PARTIAL` 下编造未注入片段的引用。
- 截断/丢弃后的免责声明是否**实际**阻止了错误断言。

**因此，本文所有“模型会/不会……”的表述都只是风险假设，不是结论。凡涉及“LLM 是否利用了上下文”的判断，均标注：需 D4-5 动态评测验证。**

---

## 10. 未完成项与风险

1. **未运行测试。** 本批没有执行 `pytest`、`mvn test`、E2E 或数据库查询；所有结论来自静态阅读。ContextBuilder 测试文件只用于确认“现有覆盖范围”，不代表本批验证。
2. **未逐行审计所有 Controller/Service。** 重点覆盖 Session/Understanding/Evidence/Memory/Knowledge/Concept/Relation 与 Chat 链路；ConversationController、Highlight、Test 等未纳入。
3. **未审计 Analysis 链路。** `/agent/analyze`、`TurnAnalysisService`、`AnalysisDispatchService` 的上下文组装不在 D4-1 范围；Evidence 的“产生”链路没有逐行检查。
4. **SQL 动态行为未在数据库验证。** MyBatis-Plus `@TableLogic`、`selectById`、`selectBatchIds` 的实际 SQL 由框架生成；文中按实体注解与 Mapper 注解推断。`@Select` 注解 SQL 是确定的，但参数绑定/动态 `<if>` 的运行时行为未执行。
5. **Fixture 模式未审计。** 本文以 HTTP Retriever 为主；`fixture.py` 的可见性/状态语义不同，若评测使用 fixture，需要单独对照。
6. **Preview 端点不是生产路径。** `/agent/context/preview` 返回完整 `systemInstruction/messages`；不要把它的输出直接当 Manifest 落库。
7. **“跨 Space 未证明”不等于“已发现越权”。** 当前运行路径的 conceptIds 来自 `list_concepts(space_id)`，且 `EvidenceService`/`UnderstandingService` 有服务层校验；本批只指出“SQL 层没有独立证明”，不声称存在可利用漏洞。
8. **Manifest 列新增属于 D4-4 实现。** 本文只给设计；加列需要迁移脚本、Backend DTO/实体/Mapper 改动、`done` 事件扩展，均不在本批。
9. **模型遵循度未知。** 冲突处理、降级提示、引用约束都是 prompt 规则；实际效果 **需 D4-5 动态评测验证**。
10. **风险最高的四个点**：
    - Understanding/Memory/Revision 的失败被当成空（H01/H02/H03）；
    - `concept_ids[0]` 导致 CONCEPT 级 Memory 漏检（M01）；
    - Session 已归属路径不校验状态/空间（M05）；
    - 未检索（NOT_QUERIED）仍被 prompt 渲染成“没有历史记录”（M16）。

---

## 11. 建议的提交信息（本批未 commit）

本批只新增本文档，未改业务代码、未 commit。若确认要提交文档，建议：

```text
docs(audit): 增加 D4-1 上下文组装链路审计与 Manifest 方案

- 逐层审计 Session/Understanding/Evidence/Memory/Knowledge 的 Python、HTTP、Controller、Service、SQL 过滤，标注 SQL 层尚未证明项
- 记录 Understanding/Memory/Revision 失败与空结果不可区分、concept_ids[0] 漏检、Session 陈旧等缺陷
- 给出八状态可区分性、裁剪语义依赖、ContextBuilder 与 Selector 差集
- 设计 agent_run 可复用字段与 context_manifest JSON 列的 Manifest v1 方案
- 明确静态审计边界，D4-5 动态评测场景集

Co-Authored-By: Claude Code <noreply@anthropic.com>
```
