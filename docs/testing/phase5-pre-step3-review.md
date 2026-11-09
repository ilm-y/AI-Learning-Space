# Phase 5 Step 3 前置复核报告（pre-Step3）

- 日期：2026-11-09
- 分支：`main`
- 基线提交：`3c66b58 chore(phase5): Memory 隔离补丁、Usage UNKNOWN 映射与策略开关设计审核`
- 复核范围：Manifest 修复验证、A/B/C 公平性、Memory Collection 隔离、Usage 诊断可信边界、Tavily 最小集成设计、修复后 3 次真实验证计划
- 批次 1：Memory 隔离补丁实施、Usage UNKNOWN 映射（脚本层）、A/B/C 策略开关设计审核、Tavily 溯源方案确认
- 批次 2：三个设计细节确认、A/B/C 策略开关实现 + C 回归、统一数据快照脚本（§11）
- 本轮约束：**未启动 90 次全量调用；未调用 LLM / Embedding / Tavily；未写入/删除 MySQL 或 Qdrant 业务数据；未执行快照脚本的 `--apply` / `--index-knowledge`；未进入 Step 4；未 commit / push。**
- 唯一资源动作：`scripts/phase5/pre-step3/verify_memory_isolation.py` 在临时 Qdrant collection 内写入 1 个点、查询 1 次、删除 1 个临时 collection；默认 collection 前后只读复查，未变化。

---

## 0. 结论摘要

| 项目 | 结论 |
|---|---|
| Manifest 多 source 修复 | **PASS**（代码 + 单测） |
| Manifest 六字段 / 五顶层字段兼容 | **PASS** |
| 多 source token 统计是否重复 | **未发现重复**；但 `per_type.trajectory.used` 只包含首个 source 的份额，属语义限制 |
| A/B/C 公平性（当前 Pilot 配置） | **FAIL（整改已落地，未执行验证）**：策略开关与统一快照脚本已实现/交付（§11）；3 次真实验证尚未运行 |
| B 是否为旧策略 | **否，但已按三层近似实现**：B = `plan=None` + 强制检索 + 无预算/Session policy + 模拟 `DEGRADED→EMPTY`；禁止表述为「B = 旧版」（§2.7.3 / §11.4） |
| 真实链路内 A/B/C 策略切换 | **PASS（已实现）**：`AILS_CONTEXT_STRATEGY` 仅启动读取；C golden 回归通过（§11.3/§11.5） |
| Memory 默认 Collection 隔离（当前 Pilot 配置） | **PASS（本批已修）**：`start_services.py` 注入带 stamp 的 `AILS_MEMORY_COLLECTION_PREFIX` + fail-closed 断言 |
| Memory 隔离机制证明 | **PASS（本批已增强）**：写入/查询/删除三点隔离，默认 collection 三次快照未变，无残留 |
| Memory 清理 | **PASS（本批已修）**：`cleanup.py` 校验后删除 memory collection、复查 404、检查残留 |
| Usage 来源 | **PASS（来源可信）**：来自 provider 响应，不是本地估算 |
| Usage 缺失表示 | **PASS（本批已修，脚本层）**：诊断缺失映射为 `UNKNOWN` + `usageStatus`，不伪造 |
| Usage 与 run_id 关联 / 并发安全 | **FAIL / PARTIAL**：仅暴露 last-call 诊断，无 Spring `run_id` 关联；`runIdAssociable=false` |
| Tavily | **BLOCKED（溯源方案已确认）**：当前无任何集成；§5.8 六项确认，未实现 |
| 修复后 3 次真实验证 | **未执行**（仅计划） |
| 全量 90 次 | **BLOCKED**，不具备启动条件 |

**结论：当前不得进入 Step 3 全量。** 最低限度需先完成 Memory Collection 隔离补丁，并明确 A/B/C 公平性与策略切换方案。

---

## 1. 代码验证结论

### 1.1 Manifest 修复是否生效

已确认提交 `5afb20e` 的修复在两条路径上都展开 section 的全部 `source_ids`：

| 路径 | 入口 | 行为 |
|---|---|---|
| 已注入（SELECTED / TRUNCATED / 预算内 OMITTED） | `ContextBuilder._manifest_entries()` `builder.py:1287-1310` | 对 `section.source_ids` 逐项生成 entry |
| 被 Plan / 去重排除 | `ContextBuilder._record_exclusion()` `builder.py:1161-1186` | 同样对 `section.source_ids` 逐项生成 entry |
| 预算丢弃（OMITTED） | section 仍保留在 `sections`，由 `_manifest_entries()` 记录 `builder.py:1187-1199` | 同样展开全部 source_ids |

关键实现点：

- `_collect_sections()` 为 trajectory 构造的来源顺序是 revision 在前、evidence 在后：
  `builder.py:651-653`
  ```python
  source_ids=[
      *(f"revision:{item.id}" for item in trajectory.revisions),
      *(f"evidence:{item.id}" for item in trajectory.evidences),
  ]
  ```
- `_manifest_entries()` / `_record_exclusion()` 使用同一拆分函数：
  `builder.py:1141-1158`、`1287-1310`、`1161-1186`
- 第一个 source 保留基础 `type`，后续 source 使用 `${type}_source`：
  `builder.py:1177-1181`、`1298-1302`
- 同一个 section 的所有 entry 共享相同的 `retrieval_status`、`injection_status`、`reason`，token 按 source 数拆分。

**结论：selected、Plan/去重 excluded、预算 dropped 三条路径都保留全部 source_ids；不存在只记录第一个 source 的旧问题。**

### 1.2 六字段与五顶层字段兼容

- Manifest 顶层仍为五项：`schema_version`、`policy_version`、`intent`、`sources`、`budget`（`builder.py:1262-1277`）。
- 每个 source 仍严格为六字段：`type`、`source_id`、`retrieval_status`、`injection_status`、`reason`、`estimated_tokens`（`builder.py:1294-1307`）。
- 回归测试 `agent/tests/test_pipeline.py:53-87` 断言顶层五字段与六字段，并禁止正文/prompt/query 进入 Manifest。
- 多 source 回归测试 `agent/tests/test_context_d4_3.py` 中：
  - `test_manifest_records_all_source_ids_for_multi_source_section`
  - `test_manifest_records_all_source_ids_for_excluded_section`
  断言 `trajectory:revision:700` 与 `trajectory_source:evidence:701` 两条 entry、状态/原因一致、token 之和等于 section 估算值。
- 本轮执行：
  `python -m pytest tests/test_context_d4_3.py tests/test_pipeline.py tests/test_context_quality_eval.py`
  → **41 passed in 3.82s**。

**结论：Manifest Schema v1 未被破坏，兼容。**

### 1.3 多 source 是否导致 token 重复/错误统计

代码事实：

- `_split_section_tokens()` 明确是**预算估算拆分**，不是 provider 真实 token：
  `builder.py:1141-1158`
  ```python
  base, remainder = divmod(total, len(source_ids))
  return [base + (1 if index < remainder else 0) for index in range(len(source_ids))]
  ```
  拆分和恒等于 section 的 `estimated_tokens`。
- `sources[].estimated_tokens` 是拆分后的份额；同一 section 的多个 entry 不会各自携带完整 token。
- `budget.used` 是整次动态上下文的 `context_tokens`，不是 `sum(sources[].estimated_tokens)`：
  `builder.py:1270`
- `budget.per_type[kind].used` 只累加该 type 下 `SELECTED/TRUNCATED` 的 entry：
  `builder.py:1244-1249`
- 因此不会因为多 source 产生「同一 section token 被重复计入 per_type」的问题。

已发现的**语义限制**（不是本轮修复引入的缺陷）：

1. `per_type.trajectory.used` 只包含 `trajectory` 这一条（即 revision 的份额），不包含 `trajectory_source`（evidence）的份额。两者在 `per_type` 中是两个独立键。
2. `trajectory_source` 不在 `DEFAULT_TYPE_CAP_RATIOS` / `ELASTIC_KIND_ORDER` 中，因此其 `cap=None`、`required=False`；它没有独立的类型预算，只是同一 section 的附属来源。
3. `budget.used` 与 `sum(sources[].estimated_tokens)` 不相等是设计使然：前者覆盖完整动态上下文（含固定/其他部分），后者只覆盖被记录 source 的拆分估算。不能把两者当作同一口径互相校验。
4. 被排除 section 的拆分 token 仍会写入 entry，但 `per_type.used` 不计入；这是正确的，避免排除项占用「已用预算」。

**结论：未发现 token 重复计入；但 `per_type` 是 type 口径，不是 section 口径，后续评估器必须按 section/来源关系聚合，不能直接把 `per_type.trajectory` 当作 trajectory 全量。**

### 1.4 Manifest `type` 的全部消费方

| 消费方 | 位置 | 对 `${type}_source` 的行为 |
|---|---|---|
| D4-5 离线评估 harness | `agent/tests/eval/context_quality/harness.py:49-53, 671-675` | `CONTENT_TYPES` 含 `trajectory`、不含 `trajectory_source`；`_injected_content_ids()` 按 type 过滤，因此 evidence 份额不会被重复算作独立内容单元 |
| D4-5 per_type 检查 | `harness.py:623-638` | 从实际 entry 的 `type` 推导 selected_types，`trajectory_source` 也能查到对应 `per_type` 并检查 `injection_status` |
| D4-5 probe | `agent/tests/eval/context_quality/probes.py:154-160` | 按 `source_id` 过滤（如 `session:30`），不依赖 type |
| 后端 Manifest 过滤 | `backend/.../service/AgentRunManifestService.java:57, 148-170` | `source.type` **精确匹配**；查询 `type=trajectory` 不会命中 `trajectory_source`，也不会自动聚合 |
| 后端 SSE 解析/落库 | `backend/.../agent/HttpPythonAgentGateway.java:317-330` | 原样解析 `done.contextManifest`，不重命名 type |
| Agent 回归测试 | `agent/tests/test_context_d4_3.py`、`test_pipeline.py` | 断言六字段、多 source 展开、token 拆分 |
| Manifest 构造自身 | `agent/app/context/builder.py` | 唯一写入方 |

**结论：没有发现其他会按 type 做业务判断的消费方。当前唯一需要留意的是后端精确 type 过滤：`trajectory_source` 需要单独查询或后续引入 type-family。**

### 1.5 `_split_section_tokens` 是否被当作真实单 source token

- 函数 docstring 明确写「没有 per-source 的精确 token；这里按 source 数均分」（`builder.py:1145-1150`）。
- 调用点只有两处：`_record_exclusion()` 与 `_manifest_entries()`，都只用于 Manifest 估算。
- 运行时真实 Usage 来自 provider 响应：`agent/app/llm/openai_compatible.py:305-307, 388-390`，与 `_split_section_tokens` 无调用关系。
- 未发现任何评估、计费、预算判断把拆分值当作真实 token 消费。

**结论：该方法仅用于预算估算与 Manifest 记账，未冒充真实 token。**

### 1.6 `${type}_source` 的语义限制与 D4-5 评估器计划（本批不实现）

已知限制：

- `type` 从「内容类型」变成了「内容类型 + 同 section 第几个来源」的混合语义。
- `per_type.trajectory.used` 不等于整个 trajectory section 的 used。
- 后端精确 type 过滤会漏掉附属来源。
- 同一 section 的多个来源被拆成不同 type 后，按 type 统计的召回/精确率会低估该 section 的整体注入。

**未来 D4-5 评估器计划（只记录，不实现）：**

1. 在评估器内部派生 `section_key`，不修改 Manifest Schema：
   - `trajectory` 与 `trajectory_source` 归入同一 `section_key=trajectory`；
   - 其他 `<type>_source` 同理归入基础 type。
2. 按 `source_id` 前缀识别来源关系（`revision:`、`evidence:`、`chunk:`、`memory:` 等），把同一 section 的 entry 作为一个内容单元评估。
3. per-type 指标改为「section-family 聚合」：`used = sum(该 family 下 SELECTED/TRUNCATED entry tokens)`；同时保留原始 entry 级明细。
4. 后端若需要按 type 查询完整 section，后续可增加 type-family 过滤参数；本批不扩大生产代码范围。
5. 评估 Gold 标准保持不变，不因为 subtype 调整而改 Gold。

---

## 2. A/B/C 公平性判断与最小整改

### 2.1 当前 Pilot 的实际执行顺序（证据）

`scripts/phase5/pilot/pilot.py:530-563`：

1. 建 doc collection、注册用户、建 space；
2. 建 A conversation；
3. **先跑 A**（此时 space 内无 concept、无 session、无 Understanding/Memory/Evidence、无文档）；
4. 之后才建 concept、session、B/C conversation；
5. `seed_personal_context()` 直接写 MySQL：B/C 历史消息、Understanding、Evidence、revision、Memory；
6. 跑 B（有个人上下文，无文档）；
7. 上传并索引文档；
8. 跑 C（有个人上下文，有文档）。

问题文本也不同：

- A：`TCP 三次握手中，为什么第三次握手是必要的？`（`pilot.py:33`）
- B：要求回顾并引用「我之前说过的原话」（`pilot.py:34-37`）
- C：要求结合「我刚上传的资料」并引用证据（`pilot.py:38-42`）

### 2.2 公平性逐项对照

| 维度 | A | B | C | 是否相同 |
|---|---|---|---|---|
| 用户 / Space | 同一 | 同一 | 同一 | 是 |
| MySQL 数据快照 | 无 concept/session/个人数据 | 有个人数据 | 有个人数据 + 文档 | **否** |
| 问题 | 三次握手原理 | 回顾并引用原话 | 结合刚上传资料 | **否** |
| 历史消息 | 空 | 2 条种子消息 | 2 条种子消息 | **否** |
| Understanding | 无 | PARTIAL | PARTIAL | **否** |
| Evidence | 无 | USER_EXPLANATION | USER_EXPLANATION | **否** |
| Memory | 无 | 1 条 CONCEPT | 1 条 CONCEPT | **否** |
| Concept | 无 | 1 个 | 1 个 | **否** |
| Knowledge 文档 | 无 | 无 | 1 篇 + chunk | **否** |
| Qdrant Knowledge 候选 | 无 | 无 | 有 | **否** |
| 上下文策略 | 当前 Policy + Builder（因数据缺失而“像 A”） | 当前 Policy + Builder（无文档） | 当前 Policy + Builder | 名义相同，实质不同 |
| 模型 / 生成参数 | 同一配置 | 同一配置 | 同一配置 | 是 |
| Prompt | 因问题/数据不同而不同 | 不同 | 不同 | **否** |

**结论：当前 A/B/C 不是公平对照。** 只有模型、生成参数、MySQL 实例、Space 相同；数据快照、问题、Knowledge 候选均不同。A 的「无个人上下文」是数据缺失造成的，不是策略差异；B 也不是旧策略。

### 2.3 B 与「旧策略」的接近程度

D4-5 的 B 基线语义是：

- `retrieval_plan = None`；
- 所有已检索的 U/M/E/Knowledge/History/Session 都注入；
- 不应用 Plan scope、类型预算、Session policy；
- `DEGRADED` 状态折叠为 `EMPTY`；
- 使用同一个 ContextBuilder。

当前 Pilot 的 B：

- 仍然走当前 `plan_retrieval()`；
- 仍然有 intent、Plan scope、类型预算、Session policy；
- 仍然会发起 Knowledge 检索（只是没有文档，结果 `NO_VALID_DOCUMENTS`）；
- 只是缺少 Knowledge 数据。

**因此当前 B 不是旧策略，而是「当前 C 策略去掉 Knowledge 数据」。**

无法原样恢复的旧策略部分：

1. 旧代码版本的检索触发顺序与合并细节；
2. 旧 Prompt 的 section 顺序/标题/裁剪行为；
3. 旧策略对 DEGRADED 的折叠；
4. 旧策略无类型预算、无 Session policy 的行为。

可以近似、但必须标注的部分：

- 用当前 ContextBuilder 在 `plan=None` 下构建；
- 强制检索全部来源；
- 把 DEGRADED 折叠为 EMPTY；
- 不应用类型预算与 Session policy。

### 2.4 当前架构能否在真实链路内切换策略

**不能，存在明确缺口。**

- `agent/app/runtime.py:747-759` 每次请求都计算 `plan = plan_retrieval(...)`，写入 `state.retrieval_plan`、`history_review_requested`，并执行 `_apply_session_policy(state, plan)`。
- 没有请求字段、Header、环境变量或依赖注入点选择「A / B / C 策略」。
- Spring 的聊天请求也没有策略字段；Pilot 只能通过同一个 Spring 端点发起请求。
- 因此：
  - 无法用当前生产链路直接跑「旧策略 B」；
  - 无法用当前生产链路直接跑「无个人上下文但同 Knowledge 的 A」；
  - 不能用「在 Pilot 脚本里拼 Prompt」绕过，因为这不再是 Spring → Agent 的真实上下文构建。

### 2.5 最小整改方案（设计；批次 2 已实现，见 §11）

#### A. 统一数据快照

所有组共享同一份隔离 MySQL 快照和同一个隔离 Qdrant 文档 collection：

- 1 个用户、1 个 Space、1 个 Concept；
- 3 个 conversation（A/B/C），每个都预置**内容完全相同**的 2 条历史消息（user + assistant），或三组各自绑定内容相同的 session；
- 1 条 Understanding（PARTIAL）；
- 1 条 Evidence（USER_EXPLANATION）+ evidence_source；
- 1 条 revision，并设置 `current_revision_id`；
- 1 条 Memory（ACTIVE，CONCEPT scope）；
- 1 篇文档 + 固定 chunk，在任何组运行前完成索引；
- 三个组都**可以使用**同一批 Knowledge 候选；A 只是策略上不注入个人上下文，不是没有文档。

关键点：先建齐所有数据，再跑任何组；不能像当前 Pilot 那样 A 先跑、后建数据。

#### B. 统一问题

三组使用同一个中性问题，避免问题本身要求某组不具备的上下文。例如：

> `TCP 三次握手中，为什么第三次握手是必要的？请结合你可用的资料回答，并说明你的依据。`

该问题：

- 不要求引用个人历史，A 不会因缺个人上下文而被问题惩罚；
- B/C 仍可使用个人上下文；
- 三组使用同一 query，Knowledge 检索候选完全一致。

#### C. 统一生成条件

- 同一 provider、model、temperature、max_tokens；
- 同一 `knowledge_top_k`、`min_score`、预算、`recent_history_limit`；
- 同一生产 `BASE_SYSTEM_INSTRUCTION`；
- 同一 retriever mode（`http`）；
- 只有上下文策略不同。

#### D. 冻结快照

- `AILS_ANALYSIS_MODE=mq` 且不启动分析消费者，`AILS_ANALYSIS_COMPENSATION_ENABLED=false`，避免每次运行后写 Understanding/Memory/Evidence。
- 运行前、每组运行后计算冻结表的内容哈希；要求不变。
- 冻结表：user、space、concept、conversation（除可变游标）、预置 message、session、understanding、evidence、evidence_source、revision、memory、memory_source、document、document_chunk。
- 排除可变运行产物：`agent_run`、assistant message、outbox、分析队列、conversation 的 `last_message_seq`。
- Qdrant 侧记录文档 collection 的 point id + payload hash，要求三组前一致。

#### E. 策略切换（真实链路内）

**缺口：当前没有策略开关。**

推荐 **方案 1（最小可行、仍在真实 Spring → Agent 链路内）**：

1. Agent `Settings` 增加仅测试用的 `context_strategy: str = "C"`，环境变量 `AILS_CONTEXT_STRATEGY`，默认 `C`。
2. 增加硬保护：仅当 `AILS_TEST_ONLY=true` 且 `AILS_HOST=127.0.0.1` 时允许非 `C`；否则启动失败。该配置不暴露给 Spring 请求，不接受用户输入。
3. `AgentRuntime` 在 retrieval/build 之间按策略分支：
   - **C**：完全保持当前行为。
   - **A**：跳过 U/M/E/trajectory 检索；清空 session/history；保留 Knowledge 检索；构建时 `plan=None`，只注入 user_input + knowledge。语义是「无个人上下文，但同 Knowledge」。
   - **B**：按旧策略近似：强制检索 U/M/E/Knowledge/Revision；构建时 `plan=None`；不应用类型预算与 Session policy；`DEGRADED` 折叠为 `EMPTY`。明确标注这是近似，不是旧代码。
4. Pilot 只启动一个 Spring（固定指向 `127.0.0.1:8001`），在 A/B/C 之间**只重启 Agent 进程**并切换 `AILS_CONTEXT_STRATEGY`；MySQL/Qdrant 快照不变。
5. 这样仍然是 Spring 公开 API → Spring ChatService → Agent HTTP → 生产 ContextBuilder/LLM，不存在 Pilot 自行拼 Prompt。
6. Manifest Schema 不变；策略只记录在 Pilot 结果元数据或 trace（测试字段），不进入生产 Schema。

**方案 2（不改生产代码，但较弱）**：

- 在 `scripts/phase5/pre-step3/` 放一个测试启动器，import 生产 app 后替换 `AgentRuntime` 的 retrieval/build 策略；
- 仍通过 Spring → Agent HTTP 调用；
- 必须标注「测试进程内策略包装，不是生产行为」，并用 C 组与生产 C 做一致性对照，防止漂移；
- 仅在「禁止改生产代码」时采用。

**方案 3（最忠实但最重）**：

- B 组直接部署旧 commit 的 Agent；
- 需要处理旧 Schema/旧依赖/旧配置兼容，成本高，不作为最小方案。

**本轮建议：方案 1。批次 2 已按方案 1 实现，见 §11。**

### 2.6 公平性验收标准

- 三组快照哈希一致（冻结表）；运行后仍一致。
- 三组问题字符串完全一致。
- 三组 Knowledge 候选 chunk id/score 一致。
- 三组 model/temperature/max_tokens/top_k/min_score/budget 一致。
- 三组 Prompt 仅在上下文 section 上不同，不存在 Pilot 拼接最终 Prompt。
- A 的 Manifest 不含 U/M/E/trajectory/session/history；含 Knowledge。
- B 的 Manifest 含全部来源但无类型预算裁剪（按近似定义）。
- C 的 Manifest 符合当前 Policy。
- 任何一组运行后不得改变冻结表或共享 memory collection。

### 2.7 A/B/C 策略开关最小设计审核（本批已实现，见 §11）

本节最初只输出设计；**前置整改批次 2 已按此实现**，三处设计细节的最终确认与实现证据见 §11。方案采用 §2.5 方案 1：仍走真实 Spring → Agent HTTP → 生产 Runtime/ContextBuilder，不新增 Spring 字段，不拼 Prompt。

#### 2.7.1 配置项

| 项 | 设计 |
|---|---|
| `Settings.context_strategy` | `Literal["A", "B", "C"] = "C"`；环境变量 `AILS_CONTEXT_STRATEGY` |
| `Settings.test_only` | `bool = False`；环境变量 `AILS_TEST_ONLY` |
| `Settings.test_env_id` | `str = ""`；环境变量 `AILS_TEST_ENV_ID`，必须是本次 Pilot stamp |
| 读取时机 | 只在 Agent 进程启动、`Settings` 构造时读取；Runtime 只读 `self._settings.context_strategy` |
| 请求边界 | `ChatRequest` / `AnalyzeRequest` / HTTP Header 均不增加策略字段；用户无法按请求切换 |
| 默认 | `C`；未设置即生产当前行为 |

#### 2.7.2 安全约束与逐条实现方式

| 约束 | 实现方式 |
|---|---|
| 仅启动时从环境变量读取，不接受 HTTP 传入 | `Settings` 字段 + 启动校验；请求模型零改动；Runtime 不使用任何请求级策略输入 |
| 仅 `AILS_TEST_ONLY=true` 且隔离测试环境才允许 A/B | 在 `Settings` 的 `model_validator(mode="after")` 中做组合校验，**不以 loopback 作为唯一条件**：① `test_only is True`；② `test_env_id` 匹配 `^phase5-pilot-\d{14}-[0-9a-f]{6}$`；③ `qdrant_collection_prefix` 与 `memory_collection_prefix` 都以 `test_env_id` 开头；④ 两者都不等于生产默认前缀 `ails_documents` / `ails_memories`；⑤ `host` 为 loopback（辅助条件，不是唯一条件）；⑥ 既有 `internal_delegation_secret` 必须为空。任一不满足 → `ValueError`，进程启动失败 |
| 生产环境检测到 `A != C` 时拒绝启动 | 生产判定：`AILS_TEST_ONLY` 缺失/false，或 `AILS_TEST_ENV_ID` 为空/格式不符。此时 `context_strategy != "C"` 直接 `ValueError`；错误信息只说明策略与缺失条件，不回显密钥/连接串。可选再加显式 `AILS_ENV=production` 作为加分拒绝条件 |
| 每次 AgentRun 记录实际使用的 `contextStrategy` | Runtime 在每次运行开始时把 `context_strategy` 写入 `RunTrace` 事件（含 `runId`/`traceId`），并输出一行结构化日志；Pilot 从 Agent 日志与结果元数据采集。**生产 Schema 不改**。若要写入 Spring `agent_run.context_strategy` 列，需独立迁移评审（本批不做） |

#### 2.7.3 B 的近似定义（必须区分三层）

1. **旧版代码能够确认、可复现的行为**：
   - `retrieval_plan = None`；
   - 不应用 Plan scope、类型预算、Session policy；
   - 所有已检索的 U/M/E/Knowledge/History/Revision 都注入；
   - 仍使用同一个当前 `ContextBuilder`。
2. **无法精确复现、只能近似的行为**：
   - 旧代码的检索触发顺序、并行/合并细节、失败降级顺序；
   - 旧版 Prompt 的 section 顺序、标题、裁剪与预算行为；
   - 旧版对个别状态（尤其 DEGRADED）的自然处理。
   这些只能说明「近似」，不得写成「等同旧版」。
3. **为实验而模拟的旧版缺陷**：
   - `DEGRADED → EMPTY` 是**为实验构造的行为近似**，不是旧版自然表现，也不是当前代码行为；
   - 必须在结果元数据标记 `bApproximation: "simulated-degraded-as-empty"`，报告中单列，不得混入「旧版真实表现」结论。

#### 2.7.4 最小改动范围（文件 / 函数 / 行数级）

| 文件 | 位置 | 改动 | 估计 |
|---|---|---|---|
| `agent/app/config.py` | `Settings` 字段区（Memory 配置之后，约 L164 附近） | 新增 `context_strategy` / `test_only` / `test_env_id` 三个字段 | 3 行 |
| `agent/app/config.py` | `_validate_document_pipeline()` 或新增 `_validate_context_strategy()`（约 L187-230 区域） | 新增组合校验：取值合法、A/B 的测试环境证据、生产拒绝 A/B | 20-30 行 |
| `agent/app/runtime.py` | 检索计划入口 `L747-759` | 读取策略；A/B 不执行 `plan_retrieval()`，设置 `state.retrieval_plan=None`；C 保持原逻辑 | 8-12 行 |
| `agent/app/runtime.py` | 检索触发标志 `L774-783` | A：U/M/E/Revision/Session/History 全部不触发，仅保留 Knowledge；B：强制触发 U/M/E/Knowledge/Revision | 8-12 行 |
| `agent/app/runtime.py` | 检索 gather `L794-825` | A 只加入 knowledge task；B 加入全部 task；C 不变 | 6-10 行 |
| `agent/app/runtime.py` | `L640` 构建前 | A/B 以 `plan=None` 构建；B 跳过 `_apply_session_policy`；B 的 DEGRADED 在注入前折叠为 EMPTY | 10-18 行 |
| `agent/app/runtime.py` | 运行开始 | 将 `contextStrategy` 写入 trace/结构化日志 | 2-3 行 |
| `agent/tests/` | 新增测试 | C 行为不变；A/B 触发与注入差异；fail-closed 组合校验 | 独立测试文件 |

不修改：`ContextBuilder`、Manifest Schema、SSE `done`、Spring DTO/Service、Provider 适配层。

#### 2.7.5 实验设计确认

- **同一中性问题**：三组使用同一字符串（例如「TCP 三次握手中，为什么第三次握手是必要的？请结合你可用的资料回答，并说明你的依据。」），A 不会因缺个人上下文被问题惩罚。
- **同一 Knowledge 文档**：三组运行前都完成同一篇文档 + 同一批 chunk 的索引；A/B/C 都可检索到同一候选（A 只是在策略上不注入个人上下文，不是没有文档）。
- **同一数据快照**：1 用户 / 1 Space / 1 Concept / 1 条 Understanding / 1 条 Evidence + source / 1 条 revision / 1 条 Memory / 3 个同内容历史的 conversation；先建齐，再跑任何组。
- **同一生成条件**：同 provider/model/temperature/max_tokens、同 `knowledge_top_k`/`min_score`/预算/`recent_history_limit`、同 `BASE_SYSTEM_INSTRUCTION`、同 retriever mode。
- **唯一变量**：`AILS_CONTEXT_STRATEGY`（A/B/C）；切换方式为只重启 Agent 进程，Spring/MySQL/Qdrant 快照不变。
- **冻结**：分析消费者关闭、补偿关闭；运行前后冻结表哈希一致；Qdrant 文档 collection point + payload hash 一致。

#### 2.7.6 风险与缓解

| 风险 | 缓解 |
|---|---|
| 生产误开 A/B | 启动期组合校验 fail closed；默认 C；`test_env_id` 必须与两个隔离 collection 前缀一致；生产判定拒绝非 C |
| 策略记录缺失 | Runtime 每次运行写 trace + 结构化日志（含 runId/traceId）；Pilot 结果必须包含 `contextStrategy`；缺失即判该次运行无效 |
| B 近似被误读为旧版真实表现 | 三层定义写入报告与结果元数据；结论中禁止写「B = 旧版」；模拟的 DEGRADED→EMPTY 单独标记 |
| A 的 `plan=None` 与「无个人上下文」双重差异 | 在报告中明确 A 也是实验定义，不是「当前策略去掉数据」；三组只差策略 |
| 策略分支侵入生产 Runtime | 分支集中在检索/构建边界，C 路径保持原代码路径；新增 C 回归测试；上线前由人工审核 diff |

**批次 1 结论：设计已输出。批次 2 已实现，见 §11。**

---

## 3. Memory Collection 隔离

### 3.1 所有 Memory 写入 / 更新 / 查询 / 删除路径

| 动作 | 路径 | Collection 决定点 | 过滤/身份 |
|---|---|---|---|
| 创建/更新/归档（业务写） | `MemoryService` → Outbox → `OutboxRelayWorker` → `AgentMemoryClient.sync()` → `POST /agent/memories/sync` → `MemoryIndexer.sync()` → `QdrantMemoryStore.upsert_memory()` / tombstone | Agent `settings.memory_collection_prefix`（`agent/app/main.py:90-95`） | `routes.py:29-41` 要求内部身份，且 `payload.user_id == internal_user_id`，否则 403 |
| 删除（归档） | 同上，DELETE 走 `MemoryIndexer` → `QdrantMemoryStore` tombstone | 同上 | 同上 |
| 混合检索（业务读） | `MemoryService.search()` → `HybridSearchService.search()` → MySQL 词法 + `AgentMemoryClient.search()` → `POST /agent/memories/search` → `MemoryVectorSearch.search()` → `QdrantMemoryStore.search()` | 同上 | `routes.py:54-66` 身份校验；Qdrant `memory_store.py:371-378` 强制 `user_id`、`status=ACTIVE`、`embedding_fingerprint`；`380-413` 施加 GLOBAL/SPACE/CONCEPT 可见性 |
| Agent 上下文记忆检索 | `HttpBackendRetriever.get_memories()` → `GET /api/memories/search/hybrid` | 不直接访问 Qdrant；由 Spring 的 Agent 调用决定 | Spring `MemoryController.java:66-73` 使用 `SecurityUtil.currentUserId()`；`MemoryMapper.java:79-132` 再按 `user_id`、`status`、space/concept 过滤；`HybridSearchService.java:150-160` 回源复核 |
| 清理 | `scripts/phase5/pilot/cleanup.py:135-152` | 当前只删 doc collection | **不删 memory collection** |

### 3.2 是否存在默认 Collection fallback

- 运行时没有「找不到指定 collection 就退回另一个 collection」的逻辑。
- 但 `Settings.memory_collection_prefix` 默认值是 `ails_memories`（`agent/app/config.py:164`），且只校验非空（`config.py:203-204`）；`QdrantMemoryStore` 也要求前缀非空（`memory_store.py:133-136`）。
- **因此：不是代码 fallback，而是“未配置时落到共享默认名”的配置风险。** 当前 Pilot 恰好没有设置该环境变量，所以实际使用 `ails_memories_text-embedding-v4_1024`，与任何其他使用默认名的环境共享。

### 3.3 当前 Pilot 配置的隔离状态

- `scripts/phase5/pilot/start_services.py:131` 只设置了 `AILS_QDRANT_COLLECTION_PREFIX`（文档 collection）。
- 没有设置 `AILS_MEMORY_COLLECTION_PREFIX`。
- `state` 只记录 `collectionPrefix` / `collection`（`start_services.py:198-201`），没有 memory collection。
- `cleanup.py:135-152` 只删除 `state["collection"]`，不删除 memory collection。
- 前次 Pilot 的 memory 检索 warning「memory collection 未建立」说明默认 collection 当时不存在；**一旦共享默认 collection 被任何环境创建，Pilot 就会读写共享数据。**

**结论：当前 Pilot 的 Memory 隔离为 FAIL。**

### 3.4 测试清理是否会误删共享 Collection

- 现状：清理不碰 memory collection，所以不会误删；但也不会清理自己写入的数据——因为它写进了共享默认 collection。
- 修复后：清理必须只删除 `state.memoryCollection`，并在删除前校验名字包含本次 `stamp` 且不等于默认名；同时检查没有残留的 `phase5-pilot-*` memory collection。
- 绝不允许清理脚本按前缀模糊删除 `ails_memories*`。

### 3.5 建议的隔离方案（最小改动，待批准）

1. `start_services.py` 增加：
   - `memory_prefix = collection_prefix + "-memory"`；
   - Agent env：`AILS_MEMORY_COLLECTION_PREFIX=memory_prefix`；
   - state：`memoryCollectionPrefix`、`memoryCollection = f"{memory_prefix}_text-embedding-v4_1024"`。
2. 启动断言：
   - `memory_prefix` 非空；
   - `memory_prefix != settings.memory_collection_prefix`（不能等于默认 `ails_memories`）；
   - 包含本次 `stamp`；
   - 失败即启动失败（fail closed）。
3. `cleanup.py`：
   - 删除 `state.memoryCollection`；
   - 复查 404；
   - 检查残留的 `phase5-pilot-*` memory collection；
   - 结果写入 `cleanup-result.json`。
4. 文档：`docs/testing/phase5-pilot-results.md` 记录实际 memory collection 名。
5. 不改生产默认值，不改生产 Schema。

### 3.6 隔离证明（已完成的最小验证）

脚本：`scripts/phase5/pre-step3/verify_memory_isolation.py`

验证内容：

- 读取 `Settings`，计算默认 collection 名与临时隔离 collection 名；
- 只读检查默认 collection 前后状态；
- 在临时 collection 创建、写入 1 个点、按 `user_id/space_id/concept_id` 检索 1 次；
- 删除临时 collection 并复查 404；
- 再读默认 collection，比较是否存在及点数。

实际结果：

```json
{
  "defaultCollection": "ails_memories_text-embedding-v4_1024",
  "defaultBefore": { "exists": false, "httpStatus": 404 },
  "isolatedCollection": "phase5-prestep3-20261109192218-586174-memory_text-embedding-v4_1024",
  "namesDiffer": true,
  "createHttp": 200,
  "upserts": 1,
  "queries": 1,
  "deletes": 1,
  "hitCount": 1,
  "hitMemoryIds": [990001],
  "cleanup": { "deleteHttp": 200, "goneAfterDelete": true },
  "defaultAfter": { "exists": false, "httpStatus": 404 },
  "defaultUnchanged": true,
  "passed": true
}
```

Qdrant collection 列表复查：无残留临时 collection。

**证明边界：**

- 已证明「前缀可配置 + 指定 collection 内读写 + 默认 collection 不受影响」的机制。
- ~~**未证明**当前 Pilot 配置已经隔离~~ → **已由 §3.8 补丁覆盖**（`start_services.py` 注入前缀 + fail-closed 断言）。
- **仍未证明**真实 Spring → Agent → Memory sync 路径在该前缀下完整工作；这需要 3 次真实验证时顺带写入/查询一次 memory（会触发 Embedding，本批禁止）。
- 在真实链路验证完成前，**仍不得进入全量**。

### 3.7 残余风险

- `point_id_for_memory()` 使用全局 `UUIDv5("ails-memory:{memory_id}")`；如果两个环境共享同一 collection 且 memory_id 相同，会命中同一点。隔离 collection 后风险消失；同前缀多实例仍会冲突。
- `should_discard_upsert()` 允许同版本覆盖；跨环境共享 collection 时可能互相覆盖。
- tombstone 需要严格更高版本；跨环境共享 collection 时，删除可能影响另一环境。
- `HybridSearchService` 的 MySQL 回源复核能挡住跨用户/跨 space 的向量结果，但挡不住「同用户同 space 但来自另一测试环境」的数据；collection 隔离是必要的。

### 3.8 补丁已实施（2026-11-09）

§3.5 的最小方案已落地，只改 Pilot 脚本，未改生产 Memory 检索逻辑、`QdrantMemoryStore`、Spring `MemoryService`。

**改动文件**

| 文件 | 改动 |
|---|---|
| `scripts/phase5/pilot/start_services.py` | 计算 `memory_prefix = <collection_prefix>-memory`；注入 `AILS_MEMORY_COLLECTION_PREFIX`；启动前 fail-closed 断言；state 记录 memory collection 实际名 |
| `scripts/phase5/pilot/run_agent.py` | 进程启动时再次断言 `AILS_MEMORY_COLLECTION_PREFIX` 匹配 `phase5-pilot-<stamp>-memory`（防止绕过启动器）；其余改动仅 Usage 诊断（见 §4.7） |
| `scripts/phase5/pilot/cleanup.py` | 新增 `delete_memory_collection()`：先校验再删除、删除后复查 404、检查 `phase5-pilot-*` 残留；`ok` 纳入 memory 清理与全局残留 |
| `scripts/phase5/pre-step3/verify_memory_isolation.py` | 增强为写入/查询/删除三点隔离证明，并记录默认 collection 在写入后、查询后、删除后三次只读快照 |

**启动前 fail-closed 断言**（`start_services.py`，任一条失败即 `SystemExit`，进程不启动）：

1. `AILS_MEMORY_COLLECTION_PREFIX` 非空；
2. 计算出的 memory collection 非空；
3. memory 前缀不等于共享默认 `ails_memories`；
4. memory collection 不等于共享默认名 `ails_memories_<model>_<dimension>`；
5. memory 前缀与 collection 都包含本次 Pilot `stamp`；
6. memory collection 以 memory 前缀开头。

collection 名后缀按 `collection_name_for` 同一规则从生效配置推导（`<model>_<dimension>`），不再硬编码；本次为 `text-embedding-v4_1024`。state 新增：

```text
collectionSuffix / memoryCollectionPrefix / memoryCollection /
defaultMemoryCollection / memoryIsolationChecked
```

**清理安全边界**：`delete_memory_collection()` 在删除前校验 `nonEmpty`、`phase5-pilot-` 前缀、包含 `stamp`、不等于默认名、不以 `ails_memories` 开头；任一不满足则 `refused=true`，不发出删除请求。删除后复查 404，并列出全部 `phase5-pilot-*` 残留。

**验证证据**（2026-11-09 实际执行）：

```json
{
  "defaultCollection": "ails_memories_text-embedding-v4_1024",
  "isolatedCollection": "phase5-prestep3-20261109194124-fec099-memory_text-embedding-v4_1024",
  "namesDiffer": true,
  "notSharedDefaultName": true,
  "defaultBefore": { "exists": false, "httpStatus": 404 },
  "createHttp": 200,
  "upserts": 1,
  "isolatedPointsAfterWrite": 1,
  "defaultAfterWrite": { "exists": false, "httpStatus": 404 },
  "queries": 1,
  "hitCount": 1,
  "hitMemoryIds": [990001],
  "mismatchedUserHitCount": 0,
  "defaultAfterQuery": { "exists": false, "httpStatus": 404 },
  "deletes": 1,
  "cleanup": { "deleteHttp": 200, "goneAfterDelete": true },
  "isolatedGoneAfterDelete": true,
  "defaultAfterDelete": { "exists": false, "httpStatus": 404 },
  "defaultUnchanged": true,
  "residualBefore": [],
  "residualAfter": [],
  "phase5PilotResidual": [],
  "noResidual": true,
  "passed": true
}
```

- 结论：**PASS**。写入只进隔离 collection（隔离 collection 恰 1 点，默认 collection 未创建）；查询只读隔离 collection（同 user 命中 1、换 user 命中 0）；删除只删隔离 collection；默认 collection 前后均不存在（404）且三次只读快照一致；无 `phase5-prestep3-*` / `phase5-pilot-*` 残留。
- 同时用独立单元校验确认：合法配置通过；默认名前缀、无 stamp、空前缀均被拒绝。

**仍未覆盖的边界**：

- 本次验证直接使用 `QdrantMemoryStore`，未触发 Embedding，也未走真实 Spring → Agent `POST /agent/memories/sync` / `search`。该路径需在 3 次真实验证中顺带确认（会调用 Embedding，本批禁止）。
- fail-closed 断言有两层：`start_services.py` 启动前校验 + `run_agent.py` 进程启动时校验 `AILS_MEMORY_COLLECTION_PREFIX` 格式。若用生产入口（`agent/app/main.py`）直接启动，则不走这两层，生产侧默认值风险未改变（本批不允许改生产配置）。
- 默认 memory collection 当前不存在；若其他环境先创建了同名 collection，Pilot 也不会读写它，但仍应在全量前复查一次。

---

## 4. Usage 诊断链路可信边界

### 4.1 是否真实 Spring → Agent 业务链路

- 是。Pilot 调 Spring 公开聊天 API；Spring `ChatService` → `HttpPythonAgentGateway` → Agent `:8001` SSE；Agent 走生产 `AgentRuntime` → 生产 `ContextBuilder` → LLM adapter。
- `scripts/phase5/pilot/run_agent.py` 只 import 生产 `app.main:app`，额外挂一个只读诊断路由，不改生产模块（文件头注释与 `run_agent.py:18-46`）。

### 4.2 Usage 是否来自 provider 真实响应

- 是。`OpenAICompatibleAdapter` 从流式 chunk 的 `usage` 字段、非流式 body 的 `usage` 字段读取：
  `agent/app/llm/openai_compatible.py:305-307, 388-390`。
- `StreamDiagnostics.usage` / `CompletionDiagnostics.usage` 保存该字典；`as_dict()` 原样返回（`openai_compatible.py:48, 71, 92, 107`）。
- 不是本地 token 估算，也不使用 `_split_section_tokens`。

### 4.3 能否关联真实 request_id / run_id

- 诊断里有 adapter 自己的 `request_id`（`uuid.uuid4().hex`，`openai_compatible.py:176`），但**没有 Spring `agent_run.id` / `request_id`**。
- 诊断路由读取的是 `adapter.last_diagnostics` / `last_completion_diagnostics`（`run_agent.py:27-28`），这是「最近一次调用」的共享字段。
- 每次流式调用内部有独立 `StreamDiagnostics` 闭包，不会在流内串号（`openai_compatible.py:171-180`）；但外部诊断路由只能看到最后一次，无法在并发下可靠对应。
- Pilot 若串行运行，可按时间顺序人工对应；但这不是可靠的 run_id 关联。

### 4.4 诊断是否只读 / 是否有鉴权与泄漏风险

- 路由只读取内存对象并返回 JSON，不写库、不改状态（`run_agent.py:21-39`）。
- 绑定 `127.0.0.1`（`run_agent.py:9-10, 46`），进程随 Pilot 结束清理。
- **无鉴权**：本机任意进程都可访问；返回 provider/model/maxTokens/temperature/retrieverMode/qdrantCollectionPrefix/conceptResolverEnabled/stream/completion 诊断。
- 不返回 API key、Bearer、prompt、正文；`as_dict()` 只含状态/usage/模型等（`openai_compatible.py` 诊断字段）。
- 风险边界：仅限本机测试；不得把该端口暴露到非 loopback，不得用于生产。

### 4.5 Usage 缺失时是否显式 UNKNOWN

- **否。** `usage` 缺失时是 `None`，`as_dict()` 返回 `null`；没有 `status: "UNKNOWN"` 或 `usageSource` 字段。
- 因此记录时必须人工映射：`null → UNKNOWN`，不得写成 0，也不得用本地估算补齐。

### 4.6 可靠性边界

可用于：

- 证明本次调用 provider 返回了什么 usage；
- 3 次验证中记录真实 usage 证据（串行、每次立即读取）。

不可用于：

- 生产可观测性、计费、审计；
- 并发请求的逐请求 usage 归属；
- 跨进程/重启后的历史查询；
- 与 Spring `agent_run` 的自动关联。

结论：**Usage 来源可信（provider），但关联与持久化不可信；只能作为测试证据，且缺失时必须标 UNKNOWN。**

### 4.7 UNKNOWN 映射已实施（脚本层）

只改 `scripts/phase5/pilot/run_agent.py` 的诊断路由；未改 Provider 适配层、生产 Schema、SSE `done` 事件。

**新增/变化字段**

| 字段 | 位置 | 取值与含义 |
|---|---|---|
| `usage` | `stream` / `completion` | provider 返回则为原 usage 字典；缺失时由 `null` 改为字符串 `"UNKNOWN"` |
| `usageStatus` | `stream` / `completion` | `REAL`：provider 返回了 usage；`UNKNOWN`：未返回，未估算、未补 0 |
| `runIdAssociable` | 顶层 | `false`：诊断只保留 last-call 且未记录 Spring `run_id`/`trace_id`；`unknown`：进程尚未观察到 LLM 调用 |
| `runIdAssociableReason` | 顶层 | 上述判断的中文说明 |
| `diagnosticScope` | 顶层 | 固定 `last-call`，提醒消费方这不是逐请求历史 |

实现方式：`_diagnostic_view()` 复制 `as_dict()` 结果后覆盖 `usage` / `usageStatus`，不修改 adapter 内部状态。

**边界**

- 这是**诊断展示层映射**，不是数据修复：provider 没给的 token 仍然不存在，不得用于计费、审计或补全预算。
- `usage` 字段类型在缺失时会从 `null` 变为字符串 `"UNKNOWN"`；消费方必须同时检查 `usageStatus`，不能把字符串当字典读取。
- `runIdAssociable=false` 是诚实结论：Agent 请求体虽然带 `runId`/`traceId`，但 adapter 诊断没有保存它们，诊断 GET 请求也不带 run_id。串行单请求时仍只能人工按时间顺序对应，不构成程序化关联。
- 若后续要求机器可关联，需要单独评审：在 AgentRun 范围内保存 `run_id` 与 adapter `request_id` 的映射（或让 Spring 落库 provider usage），这属于生产改动，不在本批。

---

## 5. Tavily 最小集成设计（只设计，不实现）

### 5.1 现状确认

- Agent 代码、依赖、配置、工具抽象中均无 Tavily / Web Search：
  - `agent/requirements.txt` 无 `tavily`；
  - `agent/app` 无 Tavily 客户端 / 工具抽象 / function-calling 工具；
  - `agent/.env` 无 `AILS_TAVILY_*`；
  - 仅 `scripts/phase5/connectivity_check.py:310-318` 将其标记为 BLOCKED。
- 当前没有可复用的「工具抽象」。最小的新增抽象是必要的，但应保持很小。

### 5.2 最小文件/模块

| 文件 | 职责 |
|---|---|
| `agent/app/tools/__init__.py` | 导出 `WebSearchTool`、`WebSearchResult` |
| `agent/app/tools/base.py` | `WebSearchTool` Protocol + `WebSearchResult` 数据类 + `WebSearchStatus` |
| `agent/app/tools/tavily.py` | `TavilySearchTool`，使用现有 `httpx`（`agent/requirements.txt:4`） |
| `agent/app/config.py` | `web_search_enabled`、`tavily_api_key`、`tavily_base_url`、`tavily_timeout_seconds`、`tavily_max_results` |
| `agent/app/runtime.py` | 在明确的触发条件下调用工具；不在 ContextBuilder 内隐式调用 |
| `agent/tests/test_tavily_tool.py` | 单元测试（本地假 HTTP 服务） |
| `agent/tests/integration/test_tavily_live.py` | 真实集成测试，默认 skip，需 `TAVILY_LIVE_TEST=1` + key |
| `agent/.env.example` | 只放变量名与空值，不放密钥 |

### 5.3 API key 配置

- 只从环境变量读取：`TAVILY_API_KEY`（或 `AILS_TAVILY_API_KEY`），不写入仓库、不打印、不进入异常消息。
- 默认 `web_search_enabled=false`；未启用时工具不注册/不调用。
- 启动日志只打印 `webSearchEnabled`，不打印 key 前缀。

### 5.4 结果如何进入 Agent 上下文

**建议 v1：作为 tool result，不作为 ContextBuilder source。**

理由：

- ContextBuilder 面向「请求前已检索好的上下文」，其 Manifest Schema v1 与 D4-5 评估已稳定；把网页结果塞进 ContextBuilder 会扩大 type 词汇和评估口径。
- tool result 是模型主动调用后的结果，符合 function-calling 语义；Citation 可直接从 tool result 的 title/url 生成。
- 后续若需要 Manifest 可追溯，再单独评审是否增加 `web_search` 来源类型；本批不改 Schema。

数据形状：

```python
@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    score: float | None = None
    published_at: str | None = None
```

- 保留 `url`、`title`、`source`（Tavily 返回的域名或 `source` 字段）；
- tool result 内容只放 title/url/snippet，不放整页正文；
- system instruction 要求引用格式 `[title](url)`；
- 结果条数上限 `tavily_max_results`（建议 3），snippet 截断（建议 500 字符）。

### 5.5 如何测试「真的调用了」而不是 Mock

两层：

1. **单元/契约测试（默认跑）**：本地 `http.server` 假服务，断言：
   - 收到了 `POST /search`；
   - 请求体含 query/max_results；
   - `Authorization` 头存在（只断言存在，不打印值）；
   - 返回固定 JSON 后，工具解析出 title/url/snippet；
   - 这证明工具真的发出了 HTTP 请求，而不是返回硬编码。
2. **真实集成测试（默认 skip）**：`TAVILY_LIVE_TEST=1` 且 key 存在时执行：
   - 发起一次真实查询；
   - 断言 HTTP 2xx、结果非空、至少一条含 `https://` URL；
   - 记录耗时/状态，不记录 key；
   - 与 A/B/C 实验完全分开，不混入 90 次调用。

### 5.6 超时 / 错误 / 空结果 / 重试

- 超时：`tavily_timeout_seconds`（建议 5s），连接/读取分离可选。
- 重试：仅对 429 / 5xx / 网络超时重试 1 次，指数退避（0.5s）；4xx（401/403/400）不重试。
- 空结果：返回 `NO_RESULTS` 状态 + 空列表，不抛异常；模型可继续回答。
- 错误：返回 `DEGRADED` 状态 + 简短 warning；聊天不中断；日志脱敏。
- 重试耗尽：`UNAVAILABLE`，不伪造结果。
- 查询内容：用户问题截断（如 200 字符），不发送内部上下文/密钥。

### 5.7 与 A/B/C 实验隔离

- Tavily 集成测试独立目录、独立开关；
- A/B/C Pilot 不启用 `web_search_enabled`；
- 不在 ContextBuilder 中隐式调用；
- 不在 90 次调用中混入外部搜索，避免变量污染。

### 5.8 溯源方案已确认（只确认，不实现）

逐条确认 §5.4 的 v1 方向：

1. **v1 作为 tool result，不进 ContextBuilder、不改 Manifest Schema**：确认。网页结果只在模型主动调用工具后以 tool message 返回；`ContextBuilder` 不感知 Tavily；Manifest 顶层五字段/source 六字段不变；不新增 `web_search` source type。
2. **必须有与 AgentRun 关联的工具调用记录**：确认，且记录至少包含 Tool Name、请求标识、搜索结果标题、URL、执行状态。v1 实现约束：
   - Agent `ChatRequest`/`AnalyzeRequest` 已带 `run_id` 与 `trace_id`；工具执行时写一条结构化日志（`toolName`、`runId`、`traceId`、`toolRequestId`、`query`、`resultTitles`、`resultUrls`、`status`、`latencyMs`），不写正文、不写密钥；
   - Pilot 从 Agent 日志采集该记录，按 `runId`/`traceId` 与 Spring `agent_run` 对应；
   - **边界**：v1 是日志级关联，不是数据库表。若要求可查询的持久化工具调用表，需要独立评审 Schema，不在本批。
3. **模型引用网页必须能追溯到实际 Tavily 返回的 URL**：确认。实现约束：tool result 只含 Tavily 返回的 `title/url/snippet`；system instruction 要求引用格式 `[title](url)`；回答生成后解析 markdown/裸 URL，逐一与本次工具调用记录的 URL 集合比对，未命中的 URL 标记为「不可溯源引用」，不得注入虚构 URL。
4. **网页搜索结果不得混入用户个人学习历史**：确认。工具结果是请求内临时消息，不写 Memory / Evidence / Understanding / Outbox，不进入分析消费者；不落库为 conversation message；不与 U/M/E/revision 合并；Tavily 测试数据与 Pilot 学习数据使用不同 collection/DB 命名空间。
5. **独立真实集成测试用 `TAVILY_LIVE_TEST=1` 默认 skip，与 A/B/C 完全分开**：确认。单测使用本地假 HTTP 服务默认运行；真实测试仅在 `TAVILY_LIVE_TEST=1` 且 key 存在时执行；A/B/C Pilot 保持 `web_search_enabled=false`；外部搜索不进入 90 次全量。
6. **本批不实现**：确认。未新增 `agent/app/tools/`、未改 `config.py`/`runtime.py`、未加依赖、未读取或写入 Tavily key。

**待人工确认**：第 2 条的「日志级关联」是否足以满足 Phase 5 的溯源要求；若必须数据库持久化，应作为独立批次先评审 Schema 与写入责任方。

---

## 6. 修复后 3 次真实验证计划（不执行）

### 6.1 目标

用修复后的 Manifest，在真实 Spring → Agent → LLM、真实 MySQL 测试数据上跑 3 次，重点验证：

- 一个同时含 Revision 与 Evidence 的 trajectory section；
- Manifest 生成 `trajectory` + `trajectory_source` 两条 source；
- 两条来源都可追溯；
- 回答确实来自对应历史；
- Citation 正确；
- token 总数守恒；
- 记录 run_id、Manifest、模型输出快照、真实 Usage（不可得则 UNKNOWN）。

### 6.2 前置条件

- Manifest 修复已在基线（是）。
- Memory Collection 隔离补丁已落地（**当前未落地，必须先做**）。
- 专用验证脚本 `scripts/phase5/pre-step3/run_manifest_verification.py` 已编写（**当前未编写**）。
- 分析消费者关闭，快照冻结。
- 本批不执行；仅列计划。

### 6.3 数据准备

1. 用隔离 DB：`phase5_pilot_<stamp>`；文档 collection 与 memory collection 都带同一 stamp。
2. 创建 user/space/concept。
3. 创建 3 个 conversation；每个预置相同的 2 条历史消息（内容含唯一标记短语，便于核对回答是否真的引用）。
4. 写入：
   - LearningUnderstanding（PARTIAL，内容含唯一标记）；
   - LearningEvidence（USER_EXPLANATION，内容含唯一标记）；
   - evidence_source；
   - understanding_revision，并更新 `current_revision_id`；
   - Memory（ACTIVE，CONCEPT scope，内容含唯一标记）。
5. 上传 1 篇文档，索引为固定 chunk；文档 chunk 含唯一标记。
6. 确认 `build_trajectories()` 能看到 revision 和 evidence（trajectory 仅在 revision 存在时创建，见 `agent/app/context/trajectory.py`）。
7. 问题同时触发 review 与知识使用，例如：
   > `请回顾我之前对 TCP 三次握手的理解，引用我说的原话，指出我卡在哪里；如果需要，可结合我上传的资料。`
8. 确认 `plan.intent == LEARNING_REVIEW`、`history_review_requested == true`，且 Knowledge 检索命中固定 chunk。

### 6.4 命令（计划）

```bash
# 1) 启动隔离环境（补丁后）
python scripts/phase5/pilot/start_services.py

# 2) 运行 3 次验证（专用脚本，待编写）
python scripts/phase5/pre-step3/run_manifest_verification.py <STATE_FILE> --runs 3

# 3) 每次运行后立即读取诊断（串行）
curl -s http://127.0.0.1:8001/phase5/llm-diagnostics

# 4) 清理
python scripts/phase5/pilot/cleanup.py <STATE_FILE>
```

### 6.5 预期结果

每次运行：

- SSE 正常结束，`done` 事件存在，无 `error`。
- `done.contextManifest` 顶层五字段、source 六字段。
- `sources` 中同时存在：
  - `type=trajectory`, `source_id=revision:<id>`；
  - `type=trajectory_source`, `source_id=evidence:<id>`；
  - 两者 `retrieval_status` 一致，`injection_status=SELECTED`，`reason=within_budget`。
- 两条 entry 的 `estimated_tokens` 之和等于该 trajectory section 的估算 token（允许因拆分余数有 0/1 差异，但总和必须等于 section 值）。
- 没有重复 source_id；没有把 evidence 记为独立 `evidence` type 从而重复计入内容。
- 回答包含预置历史中的唯一标记短语，并正确解释 evidence 中的卡点。
- Citation/引用指向 evidence 或文档 chunk 的真实来源，不编造。
- Spring `agent_run` 有唯一 run_id；Manifest 落库可通过后端只读 API 查询。
- Usage：provider 返回则记录 prompt/completion/total；缺失则 `UNKNOWN`，不估算。
- 冻结表哈希与运行前一致（除 agent_run/assistant message/outbox）。
- 默认 memory collection 前后不变；隔离 memory collection 存在且可清理。

### 6.6 失败判据（任一即停）

- 任一运行缺少 `trajectory_source` entry；
- 只有 revision，没有 evidence；
- 两条 entry 的 token 之和与 section 估算不符；
- 同一 source_id 重复；
- 回答没有使用预置历史/证据，或引用错误来源；
- Usage 是本地估算或伪造；
- 无法关联 run_id；
- 默认 memory collection 被读写；
- 分析消费者改变了 Understanding/Memory/Evidence；
- 任何一次运行 error / 超时 / 空输出；
- 模型/生成参数与记录不一致；
- Pilot 自行拼接 Prompt 而不是经 Spring → Agent 链路。

**本轮未执行以上步骤。**

---

## 7. 就绪检查表

| 检查项 | 状态 | 说明 |
|---|---|---|
| Manifest 多 source 修复代码 | **PASS** | `_manifest_entries` / `_record_exclusion` 均展开全部 source_ids |
| Manifest 六字段 / 五顶层字段 | **PASS** | 单测断言 |
| Manifest 单测 | **PASS** | 41 passed |
| Manifest token 不重复 | **PASS（有语义限制）** | 拆分和守恒；`per_type` 是 type 口径 |
| `${type}_source` 限制已记录 | **PASS** | 本报告 §1.6 |
| D4-5 评估器计划已记录 | **PASS** | 未实现 |
| A/B/C 同一数据快照 | **PASS（脚本已交付，未执行）** | `scripts/phase5/pre-step3/`：统一中性问题 + 单会话 + U/E/R/M + 冻结表哈希 + 恢复；实际建库留批次 3（§11.6） |
| A/B/C 同一问题 | **FAIL（脚本已固定，未执行）** | `snapshot_common.NEUTRAL_QUESTION` 已成为唯一问题；三组尚未实际运行 |
| A/B/C 同一 Knowledge 候选 | **FAIL（脚本已支持，未索引）** | `setup_snapshot.py --index-knowledge` 留批次 3；本批不调用 Embedding |
| B 近似旧策略 | **PASS（已实现，三层定义已标注）** | Runtime 分支 + `_fold_legacy_degraded`；报告与 trace 标注模拟近似（§11.4） |
| 真实链路内策略切换 | **PASS（已实现）** | `AILS_CONTEXT_STRATEGY` 仅启动读取；真实 Spring → Agent 链路内切换，Spring/Schema 未改（§11.4） |
| A/B/C 策略开关实现 | **PASS（已实现）** | 配置、fail-closed 组合校验、Runtime 分支、C golden 回归齐备（§11.3/§11.5） |
| Memory 前缀可配置 | **PASS** | `AILS_MEMORY_COLLECTION_PREFIX` |
| Memory 默认值风险 | **FAIL（生产默认值未改）** | Pilot 已强制注入；生产未配置时仍会落到共享 `ails_memories_*` |
| 当前 Pilot Memory 隔离 | **PASS（本批已修）** | `start_services.py` 注入带 stamp 的 memory 前缀 |
| Memory 启动 fail-closed 断言 | **PASS（本批已加）** | 非空、非默认名、含 stamp；失败拒绝启动 |
| Memory 清理 | **PASS（本批已修）** | `cleanup.py` 校验后删除 memory collection、复查 404、检查残留 |
| Memory 隔离机制证明 | **PASS（本批已增强）** | 写入/查询/删除三点隔离；默认 collection 三次快照不变；无残留 |
| Usage 来自 provider | **PASS** | 真实响应字段 |
| Usage 与 run_id 关联 | **FAIL** | 仅 last-call 诊断；`runIdAssociable=false` |
| Usage 缺失显式 UNKNOWN | **PASS（本批已修，脚本层）** | `usageStatus` + `usage="UNKNOWN"`；未伪造 |
| 诊断路由只读 | **PASS** | 只读内存 |
| 诊断路由鉴权 | **FAIL（测试边界）** | 无鉴权，仅 loopback |
| Tavily 集成 | **BLOCKED（溯源方案已确认）** | 不存在；§5.8 六项确认，未实现 |
| 3 次真实验证 | **未执行** | 计划见 §6 |
| 全量 90 次 | **BLOCKED** | 不满足前置 |
| 本轮 commit / push | **PASS（未做）** | 见 §8 |

---

## 8. Git 状态

截至前置整改批次 2（2026-11-09）完成时：

```text
 M agent/app/config.py
 M agent/app/runtime.py
 M docs/testing/phase5-pre-step3-review.md
?? agent/tests/context_strategy_fixtures.py
?? agent/tests/fixtures/context_strategy_c_golden.json
?? agent/tests/test_context_strategy.py
?? scripts/phase5/pre-step3/freeze_check.py
?? scripts/phase5/pre-step3/restore_snapshot.py
?? scripts/phase5/pre-step3/setup_snapshot.py
?? scripts/phase5/pre-step3/snapshot_common.py
```

- 已跟踪文件修改**仅限** `agent/app/config.py`、`agent/app/runtime.py` 与本报告；
- `backend/**`（含 Spring / Schema / SSE）、`ContextBuilder`、Policy 语义**无修改**；
- **未 commit**；
- **未 push**；
- 最近提交仍为：
  - `3c66b58 chore(phase5): Memory 隔离补丁、Usage UNKNOWN 映射与策略开关设计审核`
  - `5afb20e fix(agent): Manifest 记录 section 展开的全部 source 并补充 Phase 5 Pilot 证据`
  - `531dbd0 fix(agent): Manifest 记录所有 source_ids 而非仅第一个`

---

## 9. 需要人工决策

1. **Memory 隔离补丁**：~~是否批准~~ → **已批准并实施**，见 §3.8（`start_services.py` 注入前缀 + fail-closed，`run_agent.py` 进程内复查，`cleanup.py` 校验后删除）。
2. **A/B/C 策略切换方案**：选方案 1（Agent 增加仅测试用的 `AILS_CONTEXT_STRATEGY`，Spring 不改，重启 Agent 切换）还是方案 2（测试启动器包装）？方案 1 更接近真实链路，但触及生产 `runtime.py` / `config.py`，需批准最小改动范围。
3. **A/B/C 问题统一**：是否接受三组使用同一中性问题，并让 A/B 也拥有同一 Knowledge 文档？这会改变上一轮 Pilot 的「A 无知识、B 无知识」语义。
4. **B 近似定义**：是否接受「plan=None + 强制检索全部来源 + 无类型预算/Session policy + DEGRADED→EMPTY」作为旧策略近似，并在报告中明确标注近似点？
5. **分析消费者**：A/B/C 与 3 次验证期间是否保持分析消费者关闭以冻结快照？若必须开启，是否改为每组使用克隆快照（成本更高）？
6. **Usage UNKNOWN 表示**：~~是否接受~~ → **已批准并实施（脚本层）**，见 §4.7（`usageStatus` + `usage="UNKNOWN"` + `runIdAssociable`）。
7. **Tavily 设计方向**：~~是否同意~~ → **已确认，仅设计**，见 §5.8（v1 tool result、不改 Schema、默认 skip、与 A/B/C 分离）；本批未实现。
8. **3 次验证范围**：是否只跑 C（当前生产策略）来验证 Manifest 修复，还是必须同时跑 A/B/C？建议先 C，A/B/C 等策略切换方案落地后单独做。
9. **后端 type-family 过滤**：是否需要在后续批次支持按 section-family 查询 Manifest（当前精确 type 会漏 `trajectory_source`），还是先只在评估器内部聚合？

本批新增待决策：

10. **A/B/C 策略开关设计是否批准**：→ **已批准并实现（前置整改批次 2）**。`AILS_CONTEXT_STRATEGY` 默认 `C`、fail-closed 组合校验、Runtime 检索/构建边界分支、trace + 结构化日志记录，见 §11.4；C 回归见 §11.5。
11. **B 近似三层定义是否接受**：→ **已接受并落地**。§2.7.3 的三层定义保留；实现中 `DEGRADED→EMPTY` 仅在 B 分支、进入 Builder 前折叠，Knowledge 不折叠，见 §11.4。
12. **Tavily 溯源方案是否确认**：是否确认 §5.8 的六项，尤其是第 2 条「v1 采用日志级工具调用记录（含 runId/traceId）、不新增数据库表」足以满足溯源要求？若必须持久化，需另批评审 Schema。

---

## 10. 前置整改批次 1 总结（2026-11-09）

| 任务 | 结果 | 证据 |
|---|---|---|
| Memory 隔离补丁 | **已实施** | `start_services.py` 注入 `AILS_MEMORY_COLLECTION_PREFIX=<stamp>-memory` + fail-closed 断言；state 记录 memory collection；`cleanup.py` 校验后删除/复查/残留检查；§3.8 |
| Memory 隔离验证 | **PASS** | `verify_memory_isolation.py` 实际执行 `passed=true`；默认 collection 前后 404 未变；无残留；§3.8 |
| Usage UNKNOWN 映射 | **已实施（脚本层）** | `run_agent.py` 输出 `usageStatus` / `runIdAssociable`；缺失 `usage="UNKNOWN"`；§4.7 |
| A/B/C 策略开关 | **批次 1 只设计；批次 2 已实现** | §2.7 设计；实现与 C 回归见 §11 |
| Tavily 溯源方案 | **已确认，未实现** | §5.8 六项逐条确认 |
| 3 次真实验证 | **未执行** | 计划见 §6 |
| 90 次全量 | **仍 BLOCKED（批次 1 时点）** | 后续状态：策略开关已实现、快照脚本已交付，仍需 3 次真实验证与 Tavily 集成 |
| 生产代码 | **未修改** | `agent/app/**`、`backend/**` 无 diff |
| commit / push | **未做** | 见 §8 |

**进入全量的最低剩余条件**：批准并实现 §2.7 策略开关 → 按 §2.5/§2.7.5 重建统一快照 → 执行 §6 的 3 次真实验证且全部通过 → 再申请 90 次全量。

---

## 11. 前置整改批次 2 总结（2026-11-09）

本批完成三件事：三个设计细节的最终确认、A/B/C 策略开关实现与 C 回归、统一数据快照脚本。**未**执行任何 LLM / Embedding / Tavily 调用，**未**写 MySQL，**未** commit / push。

### 11.1 细节 1：`test_env_id` 正则的位置

**结论：硬编码在 `config.py` 的模块级常量 `_TEST_ENV_ID_RE`（L26），不接受配置。**

- 它是 Pilot stamp 的命名约定（`start_services.py` 生成 `phase5-pilot-<YYYYmmddHHMMSS>-<6hex>`），不是密钥，也不是环境差异项；生产不设置 `AILS_TEST_ENV_ID`，默认值为空串。
- 若改成可配置：正则本身可能被写坏（误放行/误拒绝），且与 `start_services.py` 的生成格式产生漂移；可配置化换不来任何安全收益。
- 生产安全来自默认值：`test_only=False`、`test_env_id=""`；只要 `context_strategy != "C"`，`_validate_context_strategy` 会因这两项直接拒绝启动。配置泄漏最多暴露「测试开关存在」，无法让生产通过校验。
- 因此 `_TEST_ENV_ID_RE` 留在生产代码中可接受；它是 fail-closed 校验的一部分，而不是运行期开关。

### 11.2 细节 2：delegation secret 条件

**结论：生产 Agent **不使用**该密钥；原设计中的「delegation secret 必须为空」是恒真条件，已从策略校验器删除，保留为字段级全局不变量。**

代码证据：

- `config.py` 的 `_reject_delegation_signing_secret`（L195-208）是字段校验器，对**所有**环境无条件拒绝非空 `internal_delegation_secret`；
- 该密钥由 Spring 持有（`InternalIdentitySigner` 读取 `ails.internal.delegation-secret`），Agent 端不参与签名业务；
- 因此任何能让 A/B 启动的配置，必然已经满足「为空」；再写进 `_validate_context_strategy` 只是死代码。

最终实现：从策略校验器中删除该条件，并在其 docstring 中说明原因；新增测试 `test_delegation_secret_guard_is_unconditional_not_strategy_specific` 证明即使隔离配置全部合法，出现该密钥仍然启动失败，且错误信息不回显密钥值。

### 11.3 细节 3：C 策略默认路径一致性

原计划四处改动位置与 C 下的行为：

| 位置 | C 当前行为 | 加开关后 C 的行为 |
|---|---|---|
| `_retrieve` 计划入口（原 L747-759） | `plan_retrieval()` → `state.retrieval_plan` + `history_review_requested` | 完全相同的调用与赋值，只是包在 `if strategy == "C"` 内 |
| 检索触发标志（原 L774-783） | 由 `plan.knowledge/understanding/memory/evidence/revision` 决定 | 表达式逐字保留；`memory_concept_ids = plan.memory_concept_ids` 不变 |
| 检索 gather（原 L794-825） | 按上述布尔值组装 task | 布尔值来源不变，task 组装不变 |
| 构建前（原 L640） | `_apply_session_policy()` → `builder.build()` | C 仍先 `_apply_session_policy()` 再 `build()`；`plan` 仍是同一个 `RetrievalPlan` |

A/B 才走 `plan=None` 分支：不调用 `plan_retrieval()`、不应用 Session policy；B 额外在进入 Builder 前把检索 `DEGRADED` 折叠为 `EMPTY`（`_fold_legacy_degraded`，runtime L1302），Knowledge 不折叠。`RETRIEVE` trace 在 `plan=None` 时写 `retrievalPolicy=null`、`intent=null`、`policyVersion=null`、`sessionPolicyReason="experimental_strategy_no_policy"`，并始终写 `contextStrategy`。

回归证据：C 的 golden（`agent/tests/fixtures/context_strategy_c_golden.json`）在**实现开关之前**由生产链路生成，覆盖 Manifest、section 顺序、citation、状态、token 计数与 trace；开关后逐字段一致，唯一差异是新增的 `contextStrategy` trace 字段（测试中剥离后单独断言为 `["C","C"]`）。

### 11.4 实现清单

| 文件 | 改动 |
|---|---|
| `agent/app/config.py` | L26-30 常量；L187-193 三个字段；L210-259 `_validate_context_strategy`（C 直接放行；A/B 校验 `test_only`、`test_env_id` 正则、两个 collection 前缀、非生产默认前缀、loopback）；`describe()` 增加 `context_strategy` / `test_only`。请求模型零改动 |
| `agent/app/runtime.py` | `prepare()`：启动日志 + `RUN_START` trace 写 `contextStrategy`；`_retrieve()`：A 清空 history/session、仅 Knowledge；B 强制全部来源、`plan=None`、无 Session policy；C 原路径；`_fold_legacy_degraded()`；`RETRIEVE` trace 兼容 `plan=None`，写 `contextStrategy`，B 额外标注 `bApproximation="simulated-degraded-as-empty"` |
| `agent/tests/context_strategy_fixtures.py` | 确定性 retriever / FakeLLM / golden payload 采集 |
| `agent/tests/fixtures/context_strategy_c_golden.json` | 开关前生成的 C 基线 |
| `agent/tests/test_context_strategy.py` | 19 个用例：默认 C、生产拒绝 A/B、缺任一隔离条件 fail closed、delegation secret 全局守卫、A/B 分支、B 近似标注、每次运行记录策略、C golden 回归 |
| `scripts/phase5/pre-step3/*` | 统一快照准备 / 冻结检查 / 恢复（§11.6） |

未修改：`ContextBuilder`、`AgentState` / Manifest Schema、SSE、Spring 任何文件、Provider 适配层。

### 11.5 测试结果

```text
# 新增用例
python -m pytest tests/test_context_strategy.py -q
19 passed in 4.92s

# Agent 全量回归（排除 tests/eval）
python -m pytest tests --ignore=tests/eval -q
605 passed, 9 skipped, 1 xfailed in 37.11s
```

C 回归中 `terminal.usage=None`、`usageStatus="UNKNOWN"` 属既有 provider 诊断边界，本批未改。

### 11.6 统一数据快照脚本

目录：`scripts/phase5/pre-step3/`

| 文件 | 职责 |
|---|---|
| `snapshot_common.py` | 唯一中性问题（`NEUTRAL_QUESTION`）、固定 fixture 内容、15 张冻结表定义、隔离断言、规范化行哈希、生成参数指纹（非密钥） |
| `setup_snapshot.py` | 默认 dry-run；`--apply` 通过本机 Spring API 建 user/space/concept/session/**单** conversation，并用一个 SQL 事务写历史消息 + U/E/R/M + source；`--index-knowledge` 才上传文档（调用 Embedding，本批未执行）；输出快照清单 JSON |
| `freeze_check.py` | `--write-baseline` 冻结行数 + SHA-256 + 生成参数；`--check` 任一组前后比对，漂移即非零退出；`--dump` 只读打印；`SET SESSION TRANSACTION READ ONLY`，从不 commit |
| `restore_snapshot.py` | 用 `INSERT ... ON DUPLICATE KEY UPDATE`（显式 ID + `snapshotTime`）恢复历史消息与个人上下文；默认不删除任何行；仅 `--prune-run-messages` 删除同对话内不属于快照的消息 |

关键设计：

- 三组共用**同一 conversationId、同一问题、同一文档**；A 的「无个人上下文」由策略实现，数据不删。
- 冻结哈希排除运行期可变列：`conversation.last_message_seq/last_message_at/updated_at`、`learning_session.last_active_at`、`document.index_attempts/index_error/index_started_at/vector_reconciliation_*`、生成列 `active_content_hash`。
- 生成参数指纹含 provider/model/temperature/max_tokens、top_k/min_score、预算、history limit、embedding provider/model/dimension，以及 `BASE_SYSTEM_INSTRUCTION` 的 SHA-256。
- 本批**未**执行 `--apply`、`--index-knowledge` 或任何 MySQL 写入；只做 `--self-test` 与 dry-run。

### 11.7 脚本自检结果

```text
python -m py_compile scripts/phase5/pre-step3/*.py          # COMPILE_OK
python scripts/phase5/pre-step3/setup_snapshot.py --self-test   # 隔离断言 4/4 拒绝；profile 完整
python scripts/phase5/pre-step3/freeze_check.py --self-test     # problems=[]（15 张表）
python scripts/phase5/pre-step3/restore_snapshot.py --self-test # problems=[]（11 条 upsert）
setup_snapshot.py <fake-state>                              # dry-run：只打印计划，不连库
restore_snapshot.py <fake-snapshot>                         # dry-run：只打印语句，不写库
```

### 11.8 本批残余风险

- **分析消费者必须关闭**：若 Understanding/Evidence/Memory 写回开启，冻结表会漂移；`restore_snapshot.py` 只恢复它记录过的行，不清理分析写入的新行。
- **Knowledge 尚未索引**：`setup_snapshot.py` 默认不建文档；批次 3 必须先 `--index-knowledge`，否则 A/B/C 的 Knowledge 都为空，实验无意义。
- **Qdrant 点哈希未纳入**：本批只冻结 MySQL 表；§2.7.5 提到的 Qdrant point/payload 哈希需在批次 3 另行实现或人工核对。
- **测试账号口令**：快照账号位于隔离库，使用固定的实验用口令（常量在 `snapshot_common.py`，不输出、不写日志）；不得用于任何真实环境。
