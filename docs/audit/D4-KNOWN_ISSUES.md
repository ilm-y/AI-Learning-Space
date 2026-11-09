# D4 Known Issues（Phase 4 收尾缺陷登记）

> 范围：D4-2 / D4-3 / D4-4 / D4-5 已知缺陷与限制的**登记**。
> 约束：本批只登记，不修复；不改生产代码、Policy、ContextBuilder、评测脚本、评测数据。
> 状态取值：`未修` / `已规避` / `待产品决策`。`已规避` 只表示当前有确定性防线或安全偏置，不等于缺陷消失。

---

## 1. 缺陷登记表

| 编号 | 描述 | 影响 | 状态 |
|---|---|---|---|
| KI-01 | 冗余阈值 0.6 对否定句误判，压缩后“不是”丢失 | 关键否定语义可能在上下文压缩中丢失 | 未修 |
| KI-02 | 模型自然语言误述“没有检索到”无法由确定性层阻止 | 回答可能与已知检索状态矛盾 | 已规避（确定性状态说明）；残留风险未修 |
| KI-03 | 多概念 Memory 无跨概念统一 RRF，逐概念请求增加延迟 | 最终 top_k 非跨概念统一排序；请求数随概念数增长 | 待产品决策 |
| KI-04 | Knowledge 缺版本 / 过期字段，Builder 无法按版本过滤 | 检索层已隔离版本，但 Builder 层无法二次过滤或展示版本 | 已规避（检索层 fail-closed）；Builder 层未修 |
| KI-05 | 失败 run 无 Manifest | 失败请求缺少上下文可观测性 | 待产品决策 |
| KI-06 | Session 概念匹配依赖文本识别与别名表，泛化措辞降级为“无法判定” | 旧 Session 目标可能继续注入；真正的话题切换无法识别 | 已规避（无法判定时保留注入的安全偏置）；能力缺口未修 |
| KI-07 | Understanding 接口可选 `spaceId` 未被 Agent 消费 | D4-2B 纵深防御未闭环 | 未修 |

---

## 2. 逐条说明

### KI-01 冗余阈值 0.6 对否定句误判

**描述**

`ContextBuilder._is_semantically_redundant` 用字符 bigram 重合率 ≥ 0.6 判定“历史材料与当前 Understanding 语义重复”。该判定只看字符重合，不考虑否定、极性、数字、实体差异。长文本中语义相反但字符高度重合的两段会被判为冗余，Memory 被降为 `structured`；`structured` 档的 120 字截断可能把关键否定（“不是”）截掉。

**复现方式**

```bash
cd agent
python -m tests.eval.context_quality.run_context_quality_eval
```

观察 `Known-risk probes` → `redundancy_threshold_0_6`：

```text
negation_overlap: 0.857
negation_detected_as_redundant: True
negation_tier: structured
negation_token_lost_in_compressed_text: True
```

也可运行：

```bash
cd agent
python -m pytest tests/test_context_quality_eval.py::test_known_risk_redundancy_threshold -q
```

该测试显式断言“已复现的误判，且 structured 截断丢掉否定词”。

**影响**

- 当前 Understanding 与历史 Memory / Evidence 语义相反时，历史材料被压缩，模型可能只看到肯定版本。
- 只影响被判定为“冗余”的 Memory / Evidence；Understanding 本身不会被删除。
- 改写方向（同义改写、重合低）是保守漏判，只影响预算节省，不丢内容；否定方向才是实质风险。

**建议修复范围**

- `agent/app/context/builder.py` 的 `_is_semantically_redundant`：在字符重合之外增加保护性规则，例如否定词 / 数字 / 实体差异不判冗余。
- 同步检查 `_render_memory` 的 `structured` 截断长度（当前 120 字），确保关键否定不会被截断。
- 必须复用 D4-5 的 paraphrase / negation 两个探针做回归。
- 不改检索层，不改 Manifest schema。

**当前状态**

未修。修复会改变 D4-3 的压缩语义，超出本批“只登记”的边界。

---

### KI-02 模型自然语言误述“没有检索到”

**描述**

知识检索成功、但片段因预算被全部裁掉时，ContextBuilder 会写入 `SUCCESS_BUT_DROPPED` 状态说明，并明确禁止改写成“没有检索到资料”。但模型仍可能在自然语言回答里自行说出“没有检索到任何相关资料”。该句是语义断言，确定性状态与引用 ID 无法验证；按契约不允许用关键词拦截或硬编码改写。

**复现方式**

```bash
cd agent
python -m pytest tests/test_runtime_budget_contract.py -q
```

结果包含：

```text
1 xfailed
```

对应用例：`test_runtime_must_not_deliver_no_retrieval_claim_when_chunks_dropped`（`strict=True`）。Prompt 已包含 `SUCCESS_BUT_DROPPED` 与“检索到了但未注入”，但模型输出仍可能含“没有检索到任何相关资料”。

D4-5 的 `budget_all_dropped` 探针验证了确定性层：

```text
knowledge_state: SUCCESS_BUT_DROPPED
false_no_retrieval_claim: False
```

**影响**

- 确定性层已能区分“检索成功但被裁掉”与“没有检索到”；但最终回答由模型生成，仍可能误述。
- 用户可能被告知“资料里没有”，而实际是“资料有但没放进上下文”。
- 不是新引入的回归；测试用 xfail 记录已知风险，避免伪装成已修复。

**建议修复范围**

- 回答后置校验或端到端评测：检查回答是否与 `knowledge_state` / `knowledge_dropped_chunk_ids` 矛盾。
- 不建议在 Runtime 做关键词拦截；那会与“确定性层不猜自然语言”的契约冲突。
- 需要产品决策：是否接受该残留风险，或是否引入回答后置校验（可能增加延迟与误判）。

**当前状态**

已规避（确定性状态说明 + prompt 禁令）；残留风险未修，xfail 保留。

---

### KI-03 多概念 Memory 无跨概念统一 RRF

**描述**

后端 hybrid Memory 接口只接受单个 `conceptId`。Agent `HttpBackendRetriever.get_memories` 对 `concept_ids` 去重后**逐概念并发请求**，再按 `memory.id` 去重、轮转交错合并；任一概念 `DEGRADED` 则整体 `DEGRADED`。没有跨概念统一的 RRF 或可比较分数，最终只按合并顺序截断到 `top_k`。

**复现方式**

```bash
cd agent
python -m pytest tests/test_retrieval_policy.py -q
```

相关用例：

- `test_multi_concept_memory_queries_each_concept_and_merges`
- `test_multi_concept_memory_partial_failure_stays_degraded`
- `test_single_concept_memory_keeps_one_request_contract`
- `test_empty_concept_memory_still_sends_one_request_without_concept`

也可阅读 `agent/app/retriever/http_backend.py` 的 `get_memories` 与 `_merge_memory_results`。

**影响**

- 每个概念都有候选，不会漏查某个概念；但“哪个概念的记忆更相关”没有被统一排序。
- 请求数随本轮概念数线性增长，增加尾延迟；并发缓解但不消除。
- 部分概念失败时整体标记 `DEGRADED`，状态可见，不会伪装成“没有记忆”。

**建议修复范围**

- 后端提供统一的多概念 hybrid 检索接口（服务端做 RRF / 统一排序）。
- Agent 侧不应伪造跨概念 RRF；当前合并顺序只是去重后的稳定交错。
- 需要产品决策：是否接受“逐概念候选 + 去重”的现状，或投入后端接口变更。

**当前状态**

待产品决策。

---

### KI-04 Knowledge 缺版本 / 过期字段，Builder 无法按版本过滤

**描述**

`KnowledgeChunk`（`agent/app/schemas.py`）字段为 `id / document_id / chunk_index / document_name / file_type / heading_path / concept_id / content / page_number / token_count / score`，**没有** `index_version`、`doc_version`、`updated_at`、`expires_at` 等版本 / 时效字段。

检索层 `agent/app/retriever/knowledge.py` 已做版本隔离：后端白名单 `document_id → index_version`，Qdrant payload 校验 `doc_version` / `index_version`，legacy point 不能混入。但版本信息在转换成 `KnowledgeChunk` 时被丢弃，Builder 无法二次过滤、排序或展示版本。

**复现方式**

- 阅读 `agent/app/schemas.py` 的 `KnowledgeChunk` 定义。
- 阅读 `agent/app/retriever/knowledge.py` 的 `_validate_payload` / `_parse_allowed_documents`，确认版本校验发生在检索层。
- 阅读 `agent/app/context/builder.py` 的 Knowledge 渲染，确认没有版本字段可用。

**影响**

- 当前正常 HTTP 检索路径已由检索层 fail-closed，Builder 不做二次过滤不会引入旧版本。
- 但 Fixture、legacy 数据、未来新增检索源或检索层校验失效时，Builder 没有兜底能力。
- Manifest 无法记录知识片段的版本，排查“为什么引用了旧内容”时缺少第一手字段。

**建议修复范围**

- `KnowledgeChunk` 增加可空 `index_version` / `doc_version` / `updated_at`（或统一 `version` 字段），由 Retrieval 层透传。
- Builder 只读取字段做展示 / 过滤，不改变检索层 fail-closed 语义。
- 需要同步更新 Knowledge 渲染与 Manifest；不影响现有评测 gold。

**当前状态**

已规避（检索层版本隔离 + fail-closed）；Builder 层未修。

---

### KI-05 失败 run 无 Manifest

**描述**

D4-4 把 Context Manifest 放在 SSE `done` 事件的 `contextManifest` 字段，由 Backend 写入 `agent_run.context_manifest`。失败 run（`RetrievalError` / `LLMError` / 未预期异常）走 `error` 事件，不携带 Manifest；`ChatService` 的 `updateById` 又默认忽略 `null` 字段，因此失败 run 的 `context_manifest` 保持为空。

**复现方式**

- 阅读 `agent/app/runtime.py` 的 `stream` 异常分支与 `done` 事件构造（失败路径只 `yield StreamEvent("error", ...)`）。
- 阅读 `backend/.../service/ChatService.java` 的 manifest 写入逻辑（`updateById` 忽略 null）。
- 阅读 D4-4 提交 `3f5c6db` 的说明：“失败 run 不落 manifest，是这一版已知的可观测边界”。

**影响**

- 失败请求无法回答“当时检索了什么、组装了什么”，只能看 `agent_run` 状态与错误摘要。
- 对排查“检索失败 / 模型失败”类问题不利；不影响成功请求的 Manifest。
- 不涉及正文泄露；只是可观测性缺口。

**建议修复范围**

- 在错误事件中携带“截至失败点的最小 Manifest”（至少 policyVersion / intent / 已触发的 source 状态），或由 Agent 在失败时单独写一条部分 Manifest。
- Backend 需要区分“无 Manifest”与“失败 run 的部分 Manifest”，并定义查询语义（当前无 Manifest 统一 404）。
- 需要产品决策：是否值得为失败 run 增加写入路径与存储成本。

**当前状态**

待产品决策。

---

### KI-06 Session 概念匹配依赖文本识别与别名表

**描述**

Agent 侧 `_session_concept_ids` 用规则版 `detect_concepts` 从 Session 的 `title + goal + current_focus` 提取概念，再与本轮 `concept_ids` 求交集。该识别依赖 canonical / display / 别名文本命中：

- 泛化措辞（“那个缓存”“它”“这一章”）无法识别；
- 中文别名只在 `ALIASES` 表中有配置时生效；
- 任一侧没有可识别概念时 `concept_match=None`，Policy 保持注入（`session_frame_undetermined`）。

Backend 的 `SessionResolver` 只按 `title / goal / current_focus` 的文本包含判定归属，且只匹配 canonical / display，不读 Agent 的 `ALIASES`。

**复现方式**

```bash
cd agent
python -m pytest tests/test_retrieval_policy.py -q
```

相关用例：`test_session_without_recognizable_concepts_stays_injected`、`test_session_goal_is_stripped_on_concept_mismatch`。

Backend：

```bash
cd backend/backend
./mvnw -B -Dtest=SessionResolverTest test
```

**影响**

- 大多数情况下“无法判定 → 保留注入”是安全偏置：不会误删真实学习目标。
- 但真正的话题切换如果措辞泛化，旧 goal/focus 仍可能进入 Prompt，改变回答落点。
- 无法识别时不会误判为 `mismatch`，因此不会错误剥离目标；这是有意的取舍。

**建议修复范围**

- 语义级概念匹配（LLM 或嵌入）或更完整的别名 / 同义词配置。
- 保持“无法判定时保留”的安全默认，不要把低置信匹配直接判为 mismatch。
- 需要与 Backend `SessionResolver` 的口径统一，避免两套识别结果不一致。

**当前状态**

已规避（无法判定时保留注入）；能力缺口未修。

---

### KI-07 Understanding 接口可选 `spaceId` 未被 Agent 消费

**描述**

D4-2B（`bd95fef`）给 Understanding / Revision 接口增加了可选 `spaceId`：传入时额外断言概念属于该 Space，跨 Space 返回 404。但 Agent `HttpBackendRetriever.get_understandings` 仍调用 `/api/understandings/{conceptId}`，**没有传 `spaceId`**；`get_revisions` 同样未传。因此后端新增的纵深防御在当前 Agent 路径上没有被触发。

**复现方式**

- 阅读 `agent/app/retriever/http_backend.py` 的 `get_understandings`：只拼 `/api/understandings/{concept_id}`，无 query 参数。
- 阅读 `backend/.../controller/UnderstandingController.java`：`@RequestParam(required = false) Long spaceId`。
- 运行 Backend 测试 `UnderstandingServiceSpaceScopeTest` 可验证接口本身支持 space 断言。

**影响**

- 当前运行路径仍然安全：`concept_ids` 来自 `list_concepts(space_id)`，且 Backend 的 `accessibleConcept` / `requireOwnedSpace` 会校验归属。
- 但 D4-2B 的“SQL / 接口层纵深防御”没有闭环；若未来新增调用方传入其他 Space 的 conceptId，缺少这一层显式断言。
- Understanding 表本身没有 `space_id`，空间归属只能由 concept 推导；可选参数是目前最直接的接口层防线。

**建议修复范围**

- Agent `get_understandings` / `get_revisions` 请求增加 `spaceId` query 参数。
- 保持向后兼容：后端参数是可选的，旧调用方不受影响。
- 补一条 Agent 侧测试，断言请求 URL 带 `spaceId`。
- 不改检索协议、不改表结构。

**当前状态**

未修。

---

## 3. 附：D4-5 评测结论的适用边界（非缺陷）

以下不是缺陷，但必须与上面的登记一起阅读，避免把评测结论外推：

1. **Grounded Answer Accuracy 是结构级**：它检查 required / forbidden / section 文本约束，不证明模型回答事实正确，也不是 LLM 语义评分。
2. **A/B 是构造基线**：
   - A = 只注入 User Input + Knowledge，不注入个性化上下文；
   - B = `plan=None` 的旧 Builder 路径 + 全部检索结果 + `DEGRADED → EMPTY` + Session 不剥离；
   - B 是“D4-2A 之前行为的近似”，**不是旧提交的逐行复现**；
   - A/B 的指标以 C 的预期作为 gold 计算，只能说明方向性改进，不是历史线上 A/B 测试。
3. **评测数据是人工 fixture**：不覆盖真实文档分布、长尾概念、真实 Embedding 质量、真实模型对上下文的利用。
4. **离线评测无外部依赖**：不连真实 MySQL / Qdrant / 外部 LLM；不能替代端到端评测。

---

## 4. 未完成项

- 本批没有修复任何一条缺陷。
- 没有新增表、中间件、外部 LLM 评分。
- 没有修改 Policy / ContextBuilder / 评测脚本 / 评测数据。
- 没有 commit、没有 push。
