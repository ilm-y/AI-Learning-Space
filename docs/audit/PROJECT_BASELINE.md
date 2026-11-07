# 项目基线审计（PROJECT_BASELINE）

- 审计对象：`D:\learningMatiarials\AI-Learning-Space`
- 分支 / 提交：`main` / `4732e40`（审计开始时工作区干净）
- 审计日期：2026-11-07
- 审计性质：**只读**。未修改业务代码、未提交、未删除任何文件。
- 交付物：本文件 `docs/audit/PROJECT_BASELINE.md`

## 0. 审计口径与总体结论

### 0.1 本次实际做了什么

| 项目 | 做法 | 结果 |
| --- | --- | --- |
| 后端代码 | 逐文件阅读 Controller / Service / Mapper / MQ / Security / schema | 210 个主代码 Java 文件，约 14,655 行 |
| Agent 代码 | 逐文件阅读 runtime / retriever / context / analysis / documents / vectorstore / messaging | 55 个 Python 模块，约 10,577 行 |
| 数据库 | 对照 `schema.sql`、3 个迁移脚本、Mapper 中的手写 SQL | 22 张业务表，唯一约束 / 外键 / 索引逐条核对 |
| Agent 测试 | **本次重新执行** `python -m pytest -q` | `383 passed, 5 skipped, 1 xfailed`（389 collected，10.90 s） |
| Spring 测试 | **本次重新执行** `mvn -B test` | `Tests run: 255, Failures: 0, Errors: 0, Skipped: 73`（182 executed / 182 passed，42.97 s） |
| 离线评测 | **本次重新执行** `python -m evaluation` | 40 条用例 / 40 通过 / 0 失败 / 0 跳过；断言 124/124 |
| 真实服务 E2E | **未重跑**。仅核对仓库内脚本、产物目录与报告结论 | 见第 1.8 节与附录 B |
| 真实模型 | **未调用**（无 Key，且审计要求只读） | 只引用既有报告，并标注其边界 |
| Docker / Qdrant | Docker CLI 存在但 daemon 未运行；Qdrant 6333 未监听 | Testcontainers 用例 0 executed；真实 Qdrant 用例跳过 |

> 因此：本文的“已实现”指**源码里存在可运行接线**；“已验收”只指仓库内已有报告记录过。
> 两者不能混写。离线评测运行会覆盖 `evaluation/reports/latest.*`，审计结束后已把这两个
> 文件恢复到提交状态，工作区只新增了 `docs/audit/`；评测结果以本节记录为准。

### 0.2 总体判定

| 维度 | 判定 | 一句话结论 |
| --- | --- | --- |
| 后端功能 | **已实现**（3 个 Controller 明确 501） | 17 个 Controller、61 个路由映射；14 个业务域有真实 Service + Mapper |
| Agent 功能 | **已实现** | Runtime、Retriever、ContextBuilder、Analyzer、Worker 均已接线，非空壳 |
| Memory | **已实现，部分能力受限** | 创建 / 候选 / 确认 / 更新 / 归档 / 召回都有代码；自动收敛、向量召回未做；`update` 不校验状态迁移（4.17） |
| RAG | **主链路已闭环，来源回传与生命周期未闭环** | 上传 → 解析 → 切块 → 向量化 → 检索 → 内联引用标签可运行；结构化引用不下发，删除 / 重索引 / 失败恢复缺失 |
| MQ | **已闭环** | 发布确认、消费、TTL 重试、DLQ、幂等、补偿扫描均有实现与单测 |
| 数据库 | **代码与脚本一致** | 22 表、唯一约束、外键、索引一致；无迁移框架，升级靠手工脚本 |
| 文档 | **README / delivery 基本一致，历史设计文档明显陈旧** | README、KNOWN_LIMITATIONS、TEST_MATRIX 的多数数字可复现；`database&API_design.md`、`current-state-audit.md`、`POSTMAN_API_GUIDE.md` 有多处与实现相反 |
| 测试 | **真实执行与环境门控并存** | Agent 383 条真实执行；Spring 182 条真实执行；73 条环境 skip；E2E 全部手工 |

**一句话结论**：这是一个**后端与 Agent 主链路真实可运行、测试有一定厚度、但产品外围与真实模型质量尚未收口**的工程，不是只有设计文档的脚手架；也不能按“完整产品”验收。

---

## 1. 已实现

### 1.1 后端 Controller / Service / Mapper

- 17 个 Controller、61 个 `@*Mapping` 路由。
- 除 `ExportController`、`ImportController`、`LearningPlanController` 明确返回 501 外，其余域均有真实实现。
- 所有 Mapper 继承 MyBatis-Plus `BaseMapper`，复杂并发语义用手写 SQL 补齐。

| 业务域 | Controller | Service | 关键持久化 / 语义 | 证据 |
| --- | --- | --- | --- | --- |
| 认证 / 用户 | `AuthController`、`UserController` | `AuthService`、`UserService` | BCrypt、JWT、资料修改、改密 | `config/SecurityConfig.java:45-66`、`utils/JwtUtil.java` |
| 学习空间 | `SpaceController` | `LearningSpaceService` | 所有权校验、软删 | `service/LearningSpaceService.java`、`mapper/LearningSpaceMapper.java:20` |
| 概念 | `ConceptController` | `ConceptService` | 空间内唯一名、软删复活 | `mapper/ConceptMapper.java:27-37` |
| 对话 / 聊天 | `ConversationController`、`ChatController` | `ConversationService`、`MessageService`、`ChatService` | SSE、序号原子分配、懒建 Session | `service/ChatService.java:114`、`mapper/ConversationMapper.java:17-26` |
| 批注 | `HighlightController` | `HighlightService` | 批注触发同步分析 | `service/LearningAnalysisService.java:187` |
| 证据 | `EvidenceController` | `EvidenceService` | append-only、SHA-256 去重键 | `service/EvidenceService.java:100-140` |
| 理解 / 版本 | `UnderstandingController` | `UnderstandingService` | 快照 + 不可变 Revision、行锁 | `service/UnderstandingService.java:96-334` |
| 学习状态 | `LearningStateController` | `LearningStateService` | 唯一投影写入者、attention 排序 | `service/LearningStateService.java:76-101` |
| 记忆 | `MemoryController` | `MemoryService` | CANDIDATE → ACTIVE → ARCHIVED | `service/MemoryService.java:77-432` |
| 学习活动 | `SessionController` | `SessionService` + `SessionResolver` | 懒创建、空间行锁、归属幂等 | `service/SessionService.java:65-101` |
| 测试 / 验证 | `TestController` | `TestService`、`VerificationService` | 出题、行锁判重、过期独立事务 | `service/TestService.java:137-291` |
| 文档 | `DocumentController` | `DocumentService`、`DocumentStorageService`、`DocumentIndexService` | 上传去重、索引编排、就绪白名单 | `service/DocumentIndexService.java:76-160` |
| 运行轨迹 | 无独立 Controller | — | CHAT / ANALYSIS / TEST 均已写入 | `service/ChatService.java:402`、`mq/AnalysisTaskService.java:129` |
| 导出 / 导入 / 计划 | `ExportController`、`ImportController`、`LearningPlanController` | **无** | 固定 501 | `controller/ExportController.java:19`、`controller/ImportController.java:25,30`、`controller/LearningPlanController.java:24,29,34` |

**并发正确性相关的关键实现**（不是口头设计）：

| 机制 | 实现 |
| --- | --- |
| 消息序号 | `UPDATE conversation SET last_message_seq = last_message_seq + 1` 原子分配（`mapper/ConversationMapper.java:17`） |
| 首次 Session 归属 | 锁 `learning_space` 行（`mapper/LearningSpaceMapper.java:20`），先写 session 再写 conversation（`service/SessionService.java:205-228`） |
| Understanding 并发 | `SELECT ... FOR UPDATE`（`mapper/LearningUnderstandingMapper.java:30`） |
| Revision 序号 | `MAX(revision_no) ... FOR UPDATE`（`mapper/UnderstandingRevisionMapper.java:22`） |
| LearningState 并发 | 行锁 + 唯一键兜底 + 冲突后重取赢家（`service/LearningStateService.java:76-101`） |
| 测试作答并发 | `test_task ... FOR UPDATE`（`mapper/TestTaskMapper.java:17`） |
| 证据幂等 | `uk_evidence_dedupe` + 冲突后重查（`service/EvidenceService.java:130-139`） |
| 文档就绪白名单 | READY 且 `chunk_count` 与 Qdrant 引用数一致才返回（`mapper/DocumentMapper.java:28-56`） |

### 1.2 Agent Runtime / Retriever / ContextBuilder

**Runtime（`agent/app/runtime.py`）**

- `stream()`：先发 `meta`，再逐 token 发 `delta`，最后 `done` / `error`（`:210`）。
- `prepare()`：构建 Retriever、路由选择、上下文组装（`:573`）。
- `_retrieve()`：概念、知识、理解、记忆、按需证据、轨迹与 Revision 回查（`:639`）。
- `generate()`：LLM 调用、流式引用过滤、引用校验、trace 记录（`:289`）。
- 显式个人复盘走缓冲路径：一次受限重生成 + 确定性 fallback（`:359`、`:451`）。
- 知识检索失败只降级为 `knowledge_status`，不打挂对话（`:743`）。
- `analyze()`：把消息 / 批注 / 测试答案转成候选提案（`:539`）。

**Retriever（`agent/app/retriever/`）**

- `http_backend.py` 实现 Agent → Spring 的只读回查：概念、理解、记忆、知识白名单、证据、Revision（`:103-250`）。
- `knowledge.py`：Embedding → Qdrant filter（user + space + allowed document ids）→ Top-K → 阈值 → 校验（`:23-140`）。
- `fixture.py`：仅评测显式注入可用；生产默认 `allow_degradation=False`。
- `routing.py` / `concept_detector.py`：检索模式选择与概念识别。

**ContextBuilder（`agent/app/context/builder.py`）**

- 优先级：user_input 100、history 90、session 85、understanding 70、trajectory 68、memory 50、knowledge_status 45、knowledge 40、evidence 20（`:57-70`）。
- `build()`：收集 → 去重 → 预算裁剪 → 渲染（`:266-286`）。
- `_fit_budget` / `_reduce_one`：按优先级截断或整段丢弃（`:542-568`）。
- `_final_knowledge_state`：产出 `SUCCESS` / `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`（`:897-963`）。
- 文档与学习状态是两条独立输入，互不替代（`docs/delivery/ARCHITECTURE.md:117-134`，与代码一致）。

**分析与守卫**

- `agent/app/analysis/analyzer.py`：严格 JSON 契约、字段别名、未知字段忽略、质量分裁剪、`memoryCandidates` 提取（`:110-381`）。
- `agent/app/analysis/prompts.py`：要求候选记忆带 scope / type / key / action。
- `history_guard.py`：句子级历史断言校验，仅对显式复盘生效。
- `citation_filter.py` + `citations.py`：流式丢弃越界标签、校验引用可回跳。
- `evidence_guard.py`：对证据边界做词面检查。

### 1.3 Memory 生命周期

| 环节 | 实现 | 证据 |
| --- | --- | --- |
| 用户创建 | 直接 `ACTIVE` | `MemoryService.create` (`:77-80`) |
| Agent 候选 | 写入 `CANDIDATE` + `memory_source` | `MemoryService.createCandidate` (`:92-102`) |
| 候选收敛 | ADD / UPDATE / IGNORE；ACTIVE / ARCHIVED 永不被覆盖 | `MemoryService.reconcileCandidate` (`:137-213`) |
| 确认 | `PATCH /api/memories/{id}` 修改 status，CANDIDATE → ACTIVE | `MemoryController.update`、`MemoryService.update` (`:390-418`) |
| 更新 | 内容 / 类型 / 重要性；作用域与身份键保持不变；`status` 任意值直接写入（缺陷见 4.17） | `MemoryService.update` (`:390-413`) |
| 归档 | `DELETE` 只置 `ARCHIVED`，不物理删除 | `MemoryService.archive` (`:419-432`) |
| 召回 | `GET /api/memories/search`，按 GLOBAL / SPACE / CONCEPT 可见性 + 关键词 | `MemoryService.search` (`:307-352`) |
| 注入上下文 | Agent 通过 `/api/memories/search` 取回，进入 `memory:*` 区（优先级 50） | `retriever/http_backend.py:119-142`、`context/builder.py:50` |

**Agent → 后端写入链是通的**：分析结果里的 `memoryCandidates` 由
`AgentAnalysisWriteService.reconcileMemories` 逐条调用
`MemoryService.reconcileCandidate`（`service/AgentAnalysisWriteService.java:134,339-380`），
并限制 scope 只能是 GLOBAL / 当前空间 / 当前空间概念。

### 1.4 RAG 闭环

**写入链**

```text
POST /api/spaces/{id}/documents
  → 内容 SHA-256 + uk_doc_space_active_hash 去重
  → 原文落盘（DocumentStorageService）
  → UPLOADING → submit
  → 条件更新抢占 PROCESSING（含 index_attempts +1）
  → Agent POST /agent/documents/parse（内部 token + 用户声明）
  → 校验 chunk 连续、非空、token 数
  → 保存 document_chunk
  → Agent POST /agent/documents/index
  → Embedding + Qdrant delete-old + upsert
  → 校验返回 vector 数量与 chunk 数量一致
  → 条件更新 READY（index_version +1）
```

证据：`service/DocumentService.java:100-155`、`service/DocumentIndexService.java:76-160`、
`agent/app/documents/routes.py`、`agent/app/documents/chunker.py`、
`agent/app/vectorstore/qdrant_store.py:153-206`。

**读取链**

```text
Agent 知识检索
  → GET /api/spaces/{spaceId}/documents/ready-ids?embeddingModel=
      （只返回当前用户、当前空间、READY 且 chunk/vector 数量一致的文档）
  → Embedding（与索引同一 model / dimension）
  → Qdrant filter: user_id AND space_id AND document_id IN allowed_ids
  → Top-K + min_score 阈值
  → KnowledgeResult（SUCCESS / NO_VALID_DOCUMENTS / NO_MATCH / THRESHOLD_FILTERED / DEGRADED / NOT_RETRIEVED）
  → ContextBuilder 再按预算产出最终状态（SUCCESS / SUCCESS_PARTIAL / SUCCESS_BUT_DROPPED）
  → ContextBuilder knowledge:* 区
  → LLM 输出引用标签 [资料《文件名》#序号]
  → StreamingCitationFilter 丢弃无效标签
  → CitationReport 校验
```

证据：`mapper/DocumentMapper.java:28-56`、`agent/app/retriever/knowledge.py:23-140`、
`agent/app/retriever/http_backend.py:144-184`、`agent/app/citations.py`、
`agent/app/citation_filter.py`、`agent/app/context/builder.py:897-963`。

**失败处理**：索引失败置 `FAILED` 并截断错误摘要；`PROCESSING` 超时由
`DocumentIndexRecoveryService` 回收（`service/DocumentIndexRecoveryService.java`）；
Agent 检索依赖失败返回 `DEGRADED`，不会退化成无过滤搜索。

**闭环边界（经逐调用点核对）**：

| 环节 | 是否闭环 | 事实 |
| --- | --- | --- |
| 上传 → 落盘 → DB → 异步索引 | 是 | 上传同步落盘 + 插 `UPLOADING`，索引异步（`DocumentService.java:100-162`） |
| 解析 → 切块 → Embedding → Qdrant → `vector_ref` 回写 → READY | 是 | 状态机只用 `UPLOADING / PROCESSING / READY / FAILED` |
| 检索 → 上下文 → 回答内联标签 | 是 | `[资料《文件名》#序号]`，流式过滤器丢弃越界 / 残缺标签 |
| 结构化引用回传给客户端 | **否** | `Citation` 含 `chunkId/documentId`，但只写入 Agent 内存 trace / 日志；`done` 事件、后端 `message.end`、DB 都没有引用字段（`runtime.py:254-264,491-521`、`HttpPythonAgentGateway.java:274-298`、`ChatService.java:277-297`、`schema.sql:151,486`） |
| 删除 → 清理 chunk / 文件 / Qdrant | **否** | 无删除接口；`DELETING/DELETED` 从未写入；空间软删不级联；`QdrantDocumentStore.delete_document` 仅测试调用 |
| 失败 → 重试 / 重索引 | **否** | `markProcessing` 允许 `FAILED → PROCESSING`，但生产代码无调用方；恢复服务只标 `FAILED`，错误文案却写“可重新提交索引” |
| 按引用反查 chunk / 文档 | **否** | `DocumentChunkService.listByDocument` 只被测试调用；无对应 Controller |

### 1.5 MQ 闭环

| 环节 | 实现 | 证据 |
| --- | --- | --- |
| 模式开关 | 默认 `inline`；`mq` 时装配 MQ Bean | `application.yml:121-127`、`mq/*.java` 的 `@ConditionalOnProperty` |
| 拓扑 | 请求队列 / 请求重试 TTL 队列 / 结果队列 / 结果重试 TTL 队列 / DLQ，durable | `mq/RabbitMqConfig.java:46-111` |
| 发布确认 | correlated confirm + mandatory return；超时按不确定处理 | `mq/AnalysisTaskPublisher.java:51`、`application.yml:42-44` |
| 投递结果三态 | `CONFIRMED` / `OUTCOME_UNKNOWN` / `FAILED`；只有确认失败才退回 inline | `service/AnalysisDispatchService.java:42-63` |
| 消费 | 手动 ACK；处理成功才确认 | `mq/AnalysisResultConsumer.java:57` |
| 重试 | 请求侧 `x-analysis-retry` + TTL；结果侧 `x-result-retry` + TTL | `agent/app/messaging/worker.py:377-447`、`mq/AnalysisResultConsumer.java:117-189` |
| DLQ | Worker 重试耗尽 → DLQ + FAILED 结果；结果消费者重试耗尽 → 先复制 DLQ 再标 FAILED 再 ACK | `worker.py:468`、`AnalysisResultConsumer.java:97-115` |
| 幂等 | `agent_run` 状态 CAS（PENDING / STALE → RUNNING）+ Evidence 去重键 | `mq/AnalysisResultProcessor.java:72-150`、`EvidenceService.java:108-139` |
| 上下文校验 | 校验 run 类型、schemaVersion、traceId、conversation/message 归属 | `AnalysisResultProcessor.java:199-214` |
| 兜底扫描 | 超时 PENDING / RUNNING 标记 `STALE`，不误杀晚到结果 | `mq/AnalysisStaleRunCompensator.java:60-88` |
| 委托身份 | Spring 签发 `learning-backend` 委托声明，Worker 原样转发；后端只接受 GET | `security/InternalIdentitySigner.java`、`security/InternalServiceAuthenticationFilter.java:82-100` |

Worker 侧还有：连续发布失败触发 `WorkerStalledError`、无法持久化副本时
`basic_nack(requeue=true)`、失败分类 `classify_failure`（`agent/app/messaging/task.py`）。

### 1.6 数据库

- `schema.sql` 中 22 张业务表：

```text
user, learning_space, concept, concept_relation, learning_session,
conversation, message, highlight, learning_understanding,
learning_evidence, understanding_revision, evidence_source,
learning_state, memory, memory_source, test_task, test_answer,
document, document_chunk, learning_plan, import_task, agent_run
```

- 关键唯一约束：`uk_user_username`、`uk_user_email`、`uk_concept_space_name`、
  `uk_relation`、`uk_msg_conv_seq`、`uk_understanding_user_concept`、
  `uk_evidence_dedupe`、`uk_revision_no`、`uk_evidence_source`、
  `uk_state_user_concept`、`uk_memory_identity`、`uk_memory_source`、
  `uk_answer_task`、`uk_doc_space_active_hash`、`uk_chunk_doc_idx`。
- 外键覆盖全部核心从属关系；消息、文档、批注、证据、状态、记忆、测试均有明确归属。
- `message` / `document` / `memory` / `agent_run` 等表有面向查询的普通索引。
- `spring.sql.init.mode=always` 每次启动执行 `schema.sql`；所有建表为
  `CREATE TABLE IF NOT EXISTS`。
- 3 个手工幂等迁移脚本与当前 schema 对齐：
  - `schema-migration-document-rag.sql`
  - `schema-migration-phase7-truncation.sql`
  - `schema-migration-phase54-message-seq.sql`（回填 `MAX(sequence_no)`，含软删行）
- 另有 `schema-dev-reset.sql`：会 DROP 数据，只能用于开发库（`KNOWN_LIMITATIONS.md:155`）。
- 这些迁移脚本都不会被 `spring.sql.init` 执行；启动时只加载 `schema.sql`
  （`application.yml:26-32`）。

### 1.7 文档

- `README.md`、`docs/delivery/ARCHITECTURE.md`、`TEST_MATRIX.md`、
  `KNOWN_LIMITATIONS.md`、`LEARNING_LIFECYCLE.md`、`ENVIRONMENT.md` 均存在且相互引用。
- 本次可复现的硬数字：
  - 22 张表：`schema.sql` 实际建表数一致。
  - Agent `383 passed / 5 skipped / 1 xfailed`：本次重跑一致。
  - Spring `255 / 182 executed / 73 skipped / 0 failed`：surefire 聚合一致。
  - Flutter 仍是计数器模板：`flutter/ails_flutter/lib/main.dart` 一致。
  - Memory 召回是关键词 `LIKE`：`MemoryService.search` 一致。
  - `run_type=MEMORY` 无调用方：全仓库检索一致。
- 文档对“不能声称什么”的约束（`KNOWN_LIMITATIONS.md:36-49`、`:183-189`）与代码现状一致。

### 1.8 测试：哪些真正执行

| 层级 | 执行情况 | 证据 |
| --- | --- | --- |
| Agent 单元 / 集成 | **本次真实执行**：383 通过、5 跳过、1 xfail | `python -m pytest -q`，389 collected |
| Agent 环境门控 | 5 条：真实 Qdrant ×2、真实 broker ×2、知识 E2E ×1 | `TEST_MATRIX.md:44-52`、各测试文件的条件判断 |
| Agent xfail | 1 条：检索成功但片段被裁剪时的语义误述风险 | `test_runtime_budget_contract.py:320`（`xfail(strict=True)`） |
| Spring 单元 / 集成 | **本次真实执行**：182 条通过；73 条环境门控跳过 | `mvn -B test`，39 个 surefire report |
| Spring Testcontainers MQ | 10 个 `@Test`，Docker daemon 未运行 → **0 executed** | `AnalysisMqIntegrationTest.java:73-81` |
| CI（后端） | 跑 `-Pmq-integration`；但**未设置 `AILS_*_IT`**，73 条 MySQL / 安全 / 文档集成在 CI 仍 skipped | `.github/workflows/ci.yml:27` |
| CI（Agent） | 设置 `AILS_TEST_RABBITMQ_URL`，2 条真实 broker 用例会执行；其余 3 条仍跳过 | `.github/workflows/ci.yml:73-74` |
| 离线评测 | **本次真实执行**：40/40、124/124；运行报告已回退，避免改动工作区 | `python -m evaluation`（2026-11-07、`4732e40`） |
| 全量 online 评测 | **未重跑** | 只有 2026-10-21、`34b59b5` 的 49/49 历史结果 |
| E2E | 全部是**手工脚本**，不在 `pytest` / `mvn` / CI 里 | `scripts/phase51-*`、`phase52-*`、`phase53-*`、`phase54-*`、`phase6-demo.sh` |

**`-Pmq-integration` 的实际语义**：有 Docker 时无需该 profile 也会运行
`AnalysisMqIntegrationTest`；profile 的作用只是把“Docker 缺失时跳过”改成“构建失败”
（`pom.xml:125-147`）。`TEST_MATRIX.md:86-88` 的表述不精确。

**结论**：真正自动执行的是 Agent 389 条与 Spring 182 条；73 条 Spring skip 与 5 条
Agent skip 是环境门控，不代表通过；MQ Testcontainers、真实 Qdrant、真实 broker、
真实模型 E2E、Flutter 均未进入默认测试。

---

## 2. 部分实现

| # | 能力 | 已做到 | 没做到 | 证据 |
| --- | --- | --- | --- | --- |
| 2.1 | Memory 自动收敛 | 分析结果可写 CANDIDATE；UPDATE 只覆盖 CANDIDATE | 没有 `run_type=MEMORY` 调用方；没有周期性合并 / 去重 / 冲突收敛；CANDIDATE → ACTIVE 只能人工 PATCH | `MemoryService.java:87,137-213`、`AgentRunType.MEMORY` 无调用方 |
| 2.2 | Memory 召回 | 可见性规则 + 中文二元组 LIKE + 注入上下文 | 无向量召回；`memory.embedding_ref` 未接 Qdrant；LIKE 有假阳 / 假阴 | `MemoryService.java:307-380`、`KNOWN_LIMITATIONS.md:125` |
| 2.3 | 文档解析 | `.md` / `.txt` 解析、切块、Embedding、Qdrant | 不支持 PDF / DOCX / HTML；无 OCR | `agent/app/documents/parser.py:43-98` |
| 2.4 | 检索质量 | Top-K、阈值、模型维度隔离、就绪白名单 | 无 Reranker、无 Hybrid Search、无多 chunk 排序验收 | `config.py:88-95`、`KNOWN_LIMITATIONS.md:131-146` |
| 2.5 | 引用校验 | 标签格式、越界丢弃、可回跳、词面来源支持 | 不是事实正确性证明；同义改写会误报；片段被裁剪时模型仍可能说“没检索到” | `citations.py`、`citation_filter.py`、`phase9-evidence-boundary.md` |
| 2.6 | 学习分析异步化 | inline / mq 两条路都通；MQ 有重试、DLQ、补偿 | inline 与聊天共用 `chat-stream-*` 线程池；分析最长 90 s 会占用回答线程 | `application.yml:115-127`、`config/AsyncConfig.java`、`KNOWN_LIMITATIONS.md:127` |
| 2.7 | 验证调度 | `ASK_NOW` 会建 `test_task` | `ASK_LATER` 只记日志，没有调度器；`next_review_at` 是硬编码状态映射 | `AgentAnalysisWriteService.java:391-422`、`LearningStateService.java:243-252` |
| 2.8 | Session 归属 | 懒创建、幂等、空间行锁、先 session 后 conversation | 归属判定是“命中概念名”的近似，不是“与 Session 内 Evidence 的 Concept 交集” | `service/SessionService.java:65-101`、`model/SessionResolver.java`、`KNOWN_LIMITATIONS.md:128` |
| 2.9 | 测试作答判分 | 选择题 / 判断题精确匹配；简答题关键词覆盖或长度启发式 | 没有语义判分；可能把“啰嗦但错误”判成 PARTIAL | `service/TestService.java:192-237` |
| 2.10 | 内部服务鉴权 | token + 委托声明 + 只读 GET + 常量时间比较 | 不做 `jti` 重放检测；声明在有效期内可重复使用；token 未配置时入口整体关闭 | `InternalServiceAuthenticationFilter.java:22-47,82-100` |
| 2.11 | 分析失败兜底 | inline 失败退 rules-v1；MQ 失败/重试耗尽退 rules-v1 | MQ 任务如果彻底丢失，补偿扫描只标 `STALE`，不会自动重分析 | `TurnAnalysisService.java:90-115`、`AnalysisStaleRunCompensator.java:39-46` |
| 2.12 | 截断回答 | SSE 输出 `truncated`，run 落 `TRUNCATED`，消息持久化 | 截断回答**不会触发学习分析**（`assistantMessage != null && !truncated`） | `ChatService.java:256-320` |
| 2.13 | 文档超时回收 | `PROCESSING` 超过 15 分钟置 FAILED | 阈值以内的线程挂死无法区分；长索引可能被误判 | `application.yml:95-100`、`DocumentIndexRecoveryService.java` |
| 2.14 | 真实模型验收 | 5 场景 / 6 次调用；Spring E2E 47/0×3；MQ 19/0；引用 28/0；并发 10/0×3 | C-only 依赖 fallback；无人工评分、无 SLO、无持续负载、无多实例 | `TEST_MATRIX.md:7-24`、`KNOWN_LIMITATIONS.md:36-61` |
| 2.15 | 引用回传 | 回答正文含 `资料《文件名》#序号`；内部 `Citation` 有 `chunkId/documentId` 并通过 trace 校验 | `done` 事件不带引用，`message.end` 不带引用，DB 无引用列，无 chunk 查询接口；客户端只能看到文本标签 | `runtime.py:254-264,491-521`、`HttpPythonAgentGateway.java:274-298`、`ChatService.java:277-297` |
| 2.16 | 文档删除 | `DELETING/DELETED` 枚举、`deleted_at`、Qdrant `delete_document` 都存在 | 无删除接口，状态从未写入，空间软删不清理子表 / 文件 / 向量，也无孤儿对账 | `DocumentStatus.java:18-20`、`DocumentController.java:40-71`、`LearningSpaceService.java:64-80` |
| 2.17 | 索引失败恢复 | `FAILED` 状态、`index_attempts` 自增、恢复服务标记超时任务 | 无重试 / 重索引入口；同内容重复上传命中 `findActive` 直接返回 `duplicate=true`，无法触发重试；`submit` 异常被吞可能永久停在 `UPLOADING` | `DocumentService.java:105-108,154-159`、`DocumentIndexRecoveryService.java:65-95` |
| 2.18 | Embedding 版本一致性 | ready-ids 精确匹配 `embedding_model`；collection 名含 model + dimension | 索引请求与 DB 只保存模型名，不保存 dimension / provider；同模型名换维度或换 provider 时旧文档仍 READY，检索可能落到不同 collection | `DocumentMapper.java:47-49`、`qdrant_store.py:55-59`、`DocumentIndexService.java:142-143` |

---

## 3. 只存在设计（代码里没有可用实现）

| # | 能力 | 现状 | 证据 |
| --- | --- | --- | --- |
| 3.1 | 文档导出 | `ExportController` 直接 501；`learning_plan` / 导出表结构存在但无服务 | `controller/ExportController.java:19` |
| 3.2 | 文档 / 对话导入 | `ImportController` 两个入口都 501；`import_task` 表未被业务写入 | `controller/ImportController.java:25,30` |
| 3.3 | 学习计划 | `LearningPlanController` 三个入口都 501；`learning_plan` 表无 Service | `controller/LearningPlanController.java:24,29,34` |
| 3.4 | Flutter 业务前端 | `lib/main.dart` 仍是 `flutter create` 计数器模板；`pubspec.yaml` 无业务依赖 | `flutter/ails_flutter/lib/main.dart` |
| 3.5 | Memory 向量检索 | `memory.embedding_ref` 字段存在，但没有任何写入 / 查询向量库的代码 | `MemoryService.search` 只有 LIKE |
| 3.6 | Memory 自动收敛 | `AgentRunType.MEMORY` 枚举存在，无任何创建者 | 全仓库检索无 `run_type = MEMORY` 写入 |
| 3.7 | 语义判分 | 只有长度 / 关键词 / 概念名启发式；没有 Agent 判分链路 | `TestService.java:207-237` |
| 3.8 | 复习调度算法 | `reviewIntervalDays` / `reviewPriority` 是静态 switch；没有调度器或队列 | `LearningStateService.java:242-268` |
| 3.9 | Reranker / Hybrid Search | 配置与代码均无 | `config.py` 检索参数只有 top_k / min_score |
| 3.10 | 多实例协调 | 单进程假设；消息序号、Session 锁、SSE、线程池都没有分布式协调 | `KNOWN_LIMITATIONS.md:157` |
| 3.11 | 生产级 MQ | 只有本地 compose；无高可用、监控、告警、限流 | `KNOWN_LIMITATIONS.md:158` |
| 3.12 | 数据库迁移框架 | 无 Flyway / Liquibase；升级靠手工脚本 | `KNOWN_LIMITATIONS.md:150-155` |
| 3.13 | 文档删除 API 与级联清理 | 无 `DELETE` 接口；`DELETING/DELETED` 与 `delete_document` 是死代码 / 仅测试；无 DB + 文件 + Qdrant 补偿流程 | `DocumentController.java:40-71`、`qdrant_store.py:208-216` |
| 3.14 | 重索引 / 重试入口 | `markProcessing` 支持 `FAILED → PROCESSING`，但无任何 API / 调度器调用；`index_attempts` 无第二次触发 | `DocumentIndexService.java:127`、`DocumentIndexRecoveryService.java:74-77` |
| 3.15 | 结构化引用下发 | `CitationReport.as_dict()` 字段完整，但未接入 Agent `done`、后端 `message.end`、DB 或前端 | `citations.py:114-197` |
| 3.16 | 引用标签回跳 | 标签只有文件名 + 序号；没有“标签 → chunk 内容 / 文档详情”的服务端解析能力 | `citations.py:200-211` |
| 3.17 | PDF / 页码引用 | `page_number` 列、实体、渲染分支都在，但解析器只收 md/txt，切块与 Qdrant payload 从不写页码 | `schema.sql:437`、`parser.py:19`、`DocumentChunkService.java:43-56` |

---

## 4. 已知缺陷与风险

按影响排序；4.16–4.18 为后半程深挖新增，严重度实际高于其序号。

| # | 缺陷 / 风险 | 影响 | 代码 / 文档证据 | 当前缓解 |
| --- | --- | --- | --- | --- |
| 4.1 | `history_guard` 否定句误报 | 模型正确回答“不能反推过去”仍被判违规，触发一次重生成，最终走 fallback；C-only 因此不算模型独立通过 | `agent/app/history_guard.py`、`KNOWN_LIMITATIONS.md:68-71`、`TEST_MATRIX.md:254-259` | 一次重生成 + 确定性 fallback；安全性未受损 |
| 4.2 | 语义误述无法确定性拦截 | 检索成功但片段被裁剪时，模型仍可能自然语言说“没检索到资料” | strict xfail；`KNOWN_LIMITATIONS.md:140-142` | 记录为 XFAIL，不改写模型原文 |
| 4.3 | 简答题判分是启发式 | 长回答或提到概念名就可能被判 CORRECT / PARTIAL，误判会写进 Evidence 并影响 Understanding | `TestService.java:207-237` | 文档标注“不是语义判分” |
| 4.4 | Memory LIKE 召回有假阳 / 假阴 | 中文二元组会匹配跨词组合；同义改写召回不到 | `MemoryService.java:354-380`、`KNOWN_LIMITATIONS.md:125` | 结果只作为上下文参考，不直接改状态 |
| 4.5 | inline 分析占用聊天线程 | 每轮分析最长 90 s，占着 `chat-stream-*` 线程；突发时回答排队 | `application.yml:115-119`、`ChatService.java:318` | 并发基线 24 通过，但无 SLO |
| 4.6 | 截断回答不进入学习分析 | 模型因 token 上限截断时，本轮不产生 Evidence / 理解更新 | `ChatService.java:318-320` | 无明显 UI 提示“本轮未分析” |
| 4.7 | MQ 任务可能永久 STALE | 任务丢失后补偿只标 STALE，不自动重投；需要人工按 DLQ 恢复 | `AnalysisStaleRunCompensator.java:39-46` | 状态可观测；晚到结果仍可收敛 |
| 4.8 | `ASK_LATER` 被丢弃 | Agent 想“稍后再问”时不会生成测试任务；只保留 Evidence 的 `need_follow_up` 标记 | `AgentAnalysisWriteService.java:397-403` | 文档未单独列出 |
| 4.9 | 委托声明可重放 | 15 分钟有效期内同一 `jti` 可重复使用；只做审计关联，不做重放检测 | `InternalServiceAuthenticationFilter.java:43-45` | 只读 GET + 用户身份绑定 + 常量时间比较 |
| 4.10 | 文档双写不一致窗口 | Qdrant 先删除旧点并写入新点，最后才把 MySQL 置 READY；若 MySQL 更新失败，Qdrant 已有新向量但文档保持 FAILED | `qdrant_store.py:169-204`、`DocumentIndexService.java:134-160` | 检索白名单只认 READY，失败文档不会被召回；重试会再次删除重写 |
| 4.11 | 旧库漏跑迁移脚本会启动失败 | 漏跑 Phase 7 报 `Unknown column 'truncated'`；漏跑 5.4 报 `Unknown column 'last_message_seq'` | `schema-migration-phase7-truncation.sql`、`schema-migration-phase54-message-seq.sql`、`KNOWN_LIMITATIONS.md:150-155` | 文档已写明；无自动迁移 |
| 4.12 | 文档只支持 `.md` / `.txt` | PDF / DOCX / HTML 上传会解析失败；这一限制未写进 README / KNOWN_LIMITATIONS | `agent/app/documents/parser.py:94-98` | 无 |
| 4.13 | 无分页 | Memory list / search、部分列表接口一次返回全部 | `MemoryService.list` / `search` | 当前数据量小；未做上限 |
| 4.14 | 真实模型稳定性不足 | 5.4 MQ 轮出现 `ConnectError` + 非法 JSON；并发轮出现 `RemoteProtocolError` | `KNOWN_LIMITATIONS.md:76-80` | 失败样本保留；rules-v1 兜底 |
| 4.15 | 测试口径容易被误读 | Spring `Tests run: 255` 包含 73 skip；Agent 389 包含 5 skip + 1 xfail | `TEST_MATRIX.md:65-70`、`KNOWN_LIMITATIONS.md:160-174` | 文档已反复标注“skip ≠ pass” |
| 4.16 | **结构化来源不返回客户端** | 回答里的 `资料《a.md》#0` 无法被客户端可靠解析回 document / chunk；同名文件 + 同序号时连人工都难区分；`chunkId/documentId` 只存在 trace | `runtime.py:254-264,491-521`、`HttpPythonAgentGateway.java:274-298`、`ChatService.java:277-297`、`schema.sql:151,486`、`DocumentController.java:40-71` | 无 |
| 4.17 | **Memory `update` 接受任意状态值** | `PATCH /api/memories/{id}` 直接写入请求里的 `status`，没有状态迁移校验：`ARCHIVED → ACTIVE`、`ACTIVE → CANDIDATE` 都能成功，绕过“归档不复活 / 候选需确认”的生命周期语义 | `MemoryService.java:407-409`（`update`）、`MemoryController.update` | 只有 `archive` 走专用入口；`update` 无校验 |
| 4.18 | **重索引先删旧 chunk，失败窗口丢数据** | `runIndex` 在调用 Agent `/index` **之前**执行 `replaceChunks`；`replaceChunks` 先删旧 chunk 再插新 chunk。Agent 索引失败时旧 chunk 已丢失、文档置 `FAILED`；同内容重传被 `findActive` 去重挡住，没有重试接口 | `DocumentIndexService.java:107-119`、`DocumentChunkService.java:38-56`、`DocumentService.java:105-108` | 检索白名单只认 READY，失败文档不会被召回；但数据需手工修复 |
| 4.19 | 文档删除 / 清理未闭环 | 空间软删后 document / document_chunk 行、磁盘文件、Qdrant point 永久残留；没有文档删除接口；`DELETING/DELETED` 是死状态 | `LearningSpaceService.java:64-80`、`DocumentStatus.java:18-20`、`qdrant_store.py:208-216`、`schema.sql:428` | 仅靠 `ownedSpace` 404 让数据从 API 不可见 |
| 4.20 | FAILED / UPLOADING 文档不可恢复 | 一次索引失败或一次 `submit` 异常会让文档永久不可用；恢复服务只扫 `PROCESSING`，不处理卡在 `UPLOADING` 的记录 | `DocumentIndexRecoveryService.java:65-95`、`DocumentMapper.java:65-78`、`DocumentService.java:154-159` | 无 |
| 4.21 | 模型 / 维度 / provider 变更后 READY 文档静默失效 | 只按模型名匹配白名单；collection 名含 model + dimension 但不含 provider；换维度 / 换 provider / 名称碰撞后旧文档仍 READY，检索落到错误 collection，表现为“没检索到资料” | `AgentIndexRequest.java:11`、`DocumentIndexService.java:142-143,204-219`、`DocumentMapper.java:47-49`、`qdrant_store.py:55-59` | 无状态或告警 |
| 4.22 | Qdrant 实体丢失不可检测 | ready-ids 只在 MySQL 内校验 `chunk_count = 非空 vector_ref 数`，从不查询 Qdrant；集合被删 / point 丢失后文档仍 READY | `DocumentMapper.java:40-46`、`qdrant_store.py:226-292` | 只能看到 `NO_MATCH` / `THRESHOLD_FILTERED` |
| 4.23 | 引用标签歧义 | 标签只含文件名 + `chunk_index`；同一 Space 同名文件的同序号无法区分；`by_label` 后写覆盖先写，校验可能归错 chunk | `citations.py:200-211,311-315` | 结构化报告未外发，当前主要影响评测；接通后会变成用户可见错误 |
| 4.24 | 检索降级状态不返回客户端 | `meta` 带 `degraded / fallbackReason / retrieverSource`，网关只 debug 日志；`message.end` 不带，前端无法区分“无文档 / 无命中 / 低于阈值 / 依赖故障” | `runtime.py:221-232`、`HttpPythonAgentGateway.java:275-278`、`ChatService.java:277-297` | 只能从模型自然语言回答猜测 |

> 4.17 / 4.19 / 4.20 在 `docs/testing/current-state-audit.md:449-458` 已被记录为
> `NOT_IMPLEMENTED`，但截至本次审计仍**没有代码修复**；本次是独立复核后仍成立的缺陷，
> 不是从文档转抄。

### 4.25 关键回归空白（有代码，无自动测试）

| # | 空白 | 现状 | 证据 |
| --- | --- | --- | --- |
| 4.25.1 | 消息序号分配 | `MessageService.nextSequenceNo()` 依赖计数器的原子 UPDATE，测试目录中**无任何引用**，也没有 `MessageServiceTest` | `service/MessageService.java:206-218`、`mapper/ConversationMapper.java:17-26` |
| 4.25.2 | Session 首次归属与并发 | `SessionService` 在测试目录中**零引用**；`SessionResolverTest` 只测纯函数决策，不覆盖 `lockById` / `create` / `attach` | `service/SessionService.java:65-101,205-247` |
| 4.25.3 | `attach()` 写顺序防死锁 | “先 session 再 conversation”的顺序没有自动断言，只由并发脚本验证 | `service/SessionService.java:219-227` |
| 4.25.4 | `finishRun` 先于 `message.end` | `ChatServiceTest` 只断言最终状态与 `sendCount`，没有 `InOrder`；唯一相关集成测试仍被 `AILS_CHAT_SSE_IT` 门控 | `service/ChatService.java:284-297`、`service/ChatServiceTest.java` |
| 4.25.5 | Phase 5.4 迁移脚本 | `schema-migration-phase54-message-seq.sql` 无静态或集成测试；`DocumentSchemaConsistencyTest` 只检查 `schema.sql` 与 document-rag 脚本 | `entity/DocumentSchemaConsistencyTest.java:38-39` |
| 4.25.6 | MySQL / 安全 / 文档集成 | 73 条集成测试默认跳过；CI 后端也未设置 `AILS_*_IT`，因此在 CI 仍全部跳过 | 附录 B.2、`.github/workflows/ci.yml:27` |
| 4.25.7 | 真实模型 / 真实 Qdrant / 真实 broker | 只有手工脚本或环境门控用例，没有 CI 自动断言 | 附录 B.3 |
| 4.25.8 | Flutter | 无业务代码、无测试 | `flutter/ails_flutter/lib/main.dart` |

---

## 5. 文档与代码不一致

只列**经代码核实**的不一致；未核实的传闻不写入。

| # | 文档位置 | 文档说法 | 代码事实 | 性质 |
| --- | --- | --- | --- | --- |
| 5.1 | `backend/.../service/MemoryService.java:87` | “当前还没有 Agent 往这里写，所以线上不会有 CANDIDATE 数据” | `AgentAnalysisWriteService.reconcileMemories` 调用 `MemoryService.reconcileCandidate`，会创建 CANDIDATE | **代码注释陈旧**，与实现相反 |
| 5.2 | `docs/delivery/TEST_MATRIX.md:170` | “最近结果（2026-10-27，代码版本 `74d99a1`）……40/40” | `evaluation/reports/latest.md` 生成于 **2026-10-29**、代码 `6d05486`、40/40 | 文档正文陈旧；同文件 `:365` 又记录了 10-29 的结果，前后不一致 |
| 5.3 | `docs/delivery/ARCHITECTURE.md:1` | 标题仍是“当前代码，2026-10-27” | 正文 `:144` 已写入 2026-10-30 的真实模型、MQ、RAG、并发结果 | 快照日期陈旧 |
| 5.4 | `docs/delivery/ARCHITECTURE.md:92` | “MQ Worker 用与后端一致的 `AILS_INTERNAL_TOKEN` 调内部检索接口” | 实际还必须携带 `X-Internal-Identity` 委托声明；过滤器只接受 GET，且 audience 必须是 `learning-backend` | 描述不完整，容易让人以为只有共享 token |
| 5.5 | `README.md` / `KNOWN_LIMITATIONS.md` / `ARCHITECTURE.md` | 文档上传能力描述为“文档解析 / 切块 / Embedding / 索引”，未写格式边界 | 解析器只接受 `.md` / `.txt`，其他格式直接报错 | **遗漏的限制**，不是反向声明 |
| 5.6 | `docs/delivery/TEST_MATRIX.md:9` | “5.1 MQ E2E 27 / 0 / 0” | 同文件 `:154-160` 说明首次 18/9 是环境争用，重跑才 27/0；结果本身成立，但速览表未带前置条件 | 表述省略，非错误 |
| 5.7 | `backend/.../service/ChatService.java:48-49` | 类注释写“推 `message.end` → agent_run 置 COMPLETED” | 5.4 修复后代码是**先 `finishRun` 再 `message.end`**（`:284-297`） | 代码注释与实现相反 |
| 5.8 | `backend/.../service/ChatSseIntegrationTest.java:193-194` | 注释写“message.end 在 finishRun 之前发送” | 与当前代码顺序相反；测试只断言最终收敛，没有顺序断言 | 过时注释 + 断言不足 |
| 5.9 | `docs/delivery/TEST_MATRIX.md:86-88` | “`AnalysisMqIntegrationTest` 需要 `-Pmq-integration` 与 Docker” | 有 Docker 时无需 profile 也会运行；profile 只把“Docker 缺失时跳过”变成“构建失败”（`pom.xml:125-147`） | 表述不精确 |
| 5.10 | `docs/delivery/TEST_MATRIX.md:336` | “Spring MySQL / 文档集成（29）” | 73 条 skip 中除 5 条 RabbitMQ 外约 68 条依赖 MySQL / 安全 / Phase2 环境，29 只覆盖文档与部分状态用例 | 容易低估 skipped 范围 |
| 5.11 | `docs/delivery/TEST_MATRIX.md:23,118,300` | MQ 真实模型只列 `19/0` 与 `15/4` | 产物中还有两次 `18/1`（`target/phase54-mq-real-e2e/20261030-215443`、`215815`），报告 §4.4 有解释但矩阵未列 | 样本不完整 |
| 5.12 | `docs/delivery/TEST_MATRIX.md:90-96` | “CI 会跑 `-Pmq-integration`” | 正确，但未说明 CI 后端**没有设置 `AILS_*_IT`**，73 条集成测试在 CI 仍全部 skipped | 覆盖范围被高估 |
| 5.13 | `docs/delivery/TEST_MATRIX.md:111-114` | 5.1 / 5.2 / Demo 的 101、27、94、23 PASS | 当前 `target/` 下没有 `phase51-e2e`、`phase51-mq-e2e`、`phase52-e2e`、`phase6-demo` 产物目录，历史数字无法从当前工作区独立复核 | 产物缺失（不等于结果虚假） |
| 5.14 | `docs/database&API_design.md:102` | “共 **19 张核心表**”，随后延期表另列 | 紧随其前的表清单只有 18 张；`schema.sql` 实际 22 张 | 设计文档陈旧 |
| 5.15 | `docs/database&API_design.md:1112-1116` | `agent_run.status` 枚举只有 `RUNNING / COMPLETED / FAILED` | 实际含 `PENDING / STALE / RUNNING / COMPLETED / TRUNCATED / SUCCEEDED / FAILED` | 状态机落后于 5.2 / 5.4 实现 |
| 5.16 | `docs/database&API_design.md:1397,1425,1454` | `GET /api/spaces/{spaceId}/learning-overview`、`GET /api/spaces/{spaceId}/concepts/{conceptId}/learning`、`POST /api/test-tasks/{taskId}/answer` | 实际是 `GET /api/learning-state/overview`、`GET /api/understandings/{conceptId}`、`POST /api/tests/{testId}/answer` | 路径错误，照文档联调会 404 |
| 5.17 | `docs/database&API_design.md:1506,1553,1707,1732` | 内部接口写作 `POST /internal/agent/chat`、`/internal/agent/analyze` | Agent 实际路由是 `POST /agent/chat`、`/agent/analyze`；同文件 `:1492` 自己已注明“不是 `/internal/agent/*`” | 文档自相矛盾 |
| 5.18 | `docs/delivery/ENVIRONMENT.md:213` | `AILS_INTERNAL_DELEGATION_SECRET`“设置后也不会改变转发语义” | 同文件 `:266` 与 `agent/app/config.py:130-139`：给 Agent 设置该变量会**拒绝启动** | **危险的不一致**，照 `:213` 配置会导致 Agent 起不来 |
| 5.19 | `docs/delivery/ENVIRONMENT.md:47-54` | 已有库升级只列 `schema-migration-document-rag.sql` 与 `schema-migration-phase54-message-seq.sql` | 仓库还有 `schema-migration-phase7-truncation.sql`；漏跑会报 `Unknown column 'truncated'` | 迁移清单遗漏 |
| 5.20 | `docs/delivery/ENVIRONMENT.md:210` | `AILS_INTERNAL_TOKEN` 只用于“内部文档接口 / Worker” | 聊天、分析、context preview 也依赖它；未配置时这些入口整体不可用 | 描述低估影响面 |
| 5.21 | `docs/testing/current-state-audit.md:136,366,454` | Agent `/agent/chat`、`/agent/analyze`、`/agent/context/preview`、`/health` 无鉴权，共享 token 可冒充任意用户 | 实际 `agent/app/main.py:118-170`：前三个入口要求 `X-Internal-Token` + 签名 `X-Internal-Identity`，且 `require_matching_user` 校验 payload userId；仅 `/health` 是公开存活探针 | 历史审计结论已被后续实现推翻 |
| 5.22 | `docs/delivery/TEST_MATRIX.md:122` | “前 5 个脚本使用 Mock LLM” | 该表前 4 个是 Mock 脚本，第 5 个 `phase53-spring-e2e.py` 已使用真实模型 | 计数错误 |
| 5.23 | `docs/delivery/ARCHITECTURE.md:100-106` | 内部入口表未说明鉴权方式 | `/agent/chat`、`/agent/analyze`、`/agent/context/preview`、文档 parse/index 都要求内部 token + 签名身份；`/health` 公开 | 安全边界描述缺失 |
| 5.24 | `backend/.../dto/document/DocumentResponse.java:13-15` | “本阶段没有解析/索引能力，新上传文档只会是 `UPLOADING`” | 已有完整 `PROCESSING / READY / FAILED` 状态机与索引服务 | 代码注释陈旧 |
| 5.25 | `backend/backend/POSTMAN_API_GUIDE.md:1006-1011` | 文档上传 / 列表 / 详情列为“未实现，返回 501” | `DocumentController` 已实现四个接口；真实路径是 `/api/spaces/{spaceId}/documents` | 联调指南误导 |
| 5.26 | `backend/.../service/DocumentIndexRecoveryService.java:74-77` | 错误文案写“可重新提交索引” | 没有任何重索引 / 重试接口；`FAILED` 文档无法通过 API 恢复 | 错误提示承诺了不存在的能力 |

> 反过来的核对也做了：README 声称的 22 表、Agent 383/5/1、Spring 255/182/73、
> Flutter 脚手架、Memory LIKE、`run_type=MEMORY` 无调用方、3 个 501 能力，
> 均与代码或本次重跑结果一致。

---

## 6. 逐项核对表（按用户检查项）

| 检查项 | 结论 | 说明 |
| --- | --- | --- |
| 后端 Controller / Service / Mapper 是否真正实现 | **是** | 14 个业务域有真实实现；3 个 Controller 明确 501 |
| Agent Runtime / Retriever / ContextBuilder 是否完整接线 | **是** | `stream` / `prepare` / `analyze`、HTTP 回查、预算与渲染全部有实现 |
| Memory 创建 / 候选 / 确认 / 更新 / 归档 / 召回 | **是，但确认靠 PATCH，召回是 LIKE，且 update 不校验状态迁移** | 生命周期状态机有实现；自动收敛与向量召回缺失；`ARCHIVED → ACTIVE` 可绕过（4.17） |
| RAG 上传 / 解析 / 切块 / 向量化 / 检索 / 引用 | **主链路闭环；引用回传与删除/重试未闭环** | 上传到检索、内联引用标签可运行；结构化 `chunkId/documentId` 不下发，删除与重索引缺失；仅 `.md` / `.txt` |
| MQ 发布 / 消费 / 重试 / 幂等 / DLQ | **闭环** | 两侧都有 TTL 重试与 DLQ；幂等靠 run 状态 + Evidence 去重 |
| 数据库表结构 / 索引 / 唯一约束 / 外键 / 迁移脚本 | **一致** | 22 表；3 个迁移脚本手工、幂等；无 Flyway |
| README / 架构图 / 测试记录与实际代码 | **README / delivery 基本一致，历史设计文档明显陈旧** | 26 处陈旧 / 遗漏 / 自相矛盾，见第 5 节；其中 `ENVIRONMENT.md:213` 会直接导致 Agent 启动失败 |
| 哪些测试真正执行 / Skip / 只有手动 E2E | **已分清** | Agent 383 真跑；Spring 182 真跑；73 + 5 环境 skip；E2E 全手工 |

---

## 7. 若要把“部分实现”推进到“已实现”，优先级建议

1. **Memory**：给 `update` 加状态迁移白名单（`CANDIDATE → ACTIVE / ARCHIVED`，禁止 `ARCHIVED → ACTIVE`）；补一个真正调用 `run_type=MEMORY` 的收敛任务；评估是否接向量召回。
2. **RAG 引用回传**：把 `CitationReport` 接入 Agent `done` 与后端 `message.end`（或新增加查询 chunk 的接口），让客户端能由标签回到 `documentId/chunkId`；同时把 `meta` 里的 `degraded / fallbackReason` 透出。
3. **RAG 生命周期**：补文档删除（DB + 文件 + Qdrant 补偿）与重索引接口；把 `replaceChunks` 移到 Agent 索引成功之后或改为事务内可回滚；放宽 FAILED 文档的同内容重传。
4. **RAG 一致性**：在 `document` 上持久化 embedding provider / dimension / collection，并与模型名一起参与 ready-ids 匹配；对 Qdrant 实体做抽样或定时对账。
5. **格式与质量**：至少支持 PDF 并写入页码；补多 chunk / 干扰片段的引用评测；明确格式与大小限制。
6. **分析**：把 inline 分析从 `chat-stream-*` 拆到独立线程池；补 `ASK_LATER` 的调度或在文档中明确“不落任务”。
7. **测试**：简答题判分接入 Agent 评价结果，或明确标注“启发式，不作为强证据”。
8. **文档**：修正第 5 节 26 项；优先修 `ENVIRONMENT.md:213`（会致 Agent 拒绝启动）与 `database&API_design.md` 的错误路径 / 枚举；把“仅支持 `.md` / `.txt`”写进 README 与 KNOWN_LIMITATIONS。
9. **工程化**：用 Testcontainers 把 MQ / 文档索引纳入 CI；给 CI 后端补 `AILS_*_IT`
   开关或 Testcontainers 化的 MySQL 集成；补迁移工具的评估。
10. **回归保护**：为消息序号分配、Session 首次归属、`attach()` 写顺序、
    `finishRun → message.end` 顺序补自动测试；至少加 `InOrder` 断言。

---

## 附录 A：22 张表与关键约束

| 表 | 作用 | 关键约束 / 索引 |
| --- | --- | --- |
| `user` | 用户 | `uk_user_username`、`uk_user_email` |
| `learning_space` | 学习空间 | `idx_space_user`、FK user |
| `concept` | 概念 | `uk_concept_space_name`、`idx_concept_space`、FK space |
| `concept_relation` | 概念关系 | `uk_relation`、`idx_rel_source`、`idx_rel_target` |
| `learning_session` | 学习活动 | `idx_sess_user`、`idx_sess_space`、FK user/space |
| `conversation` | 对话 | `idx_conv_user_space_updated`、`idx_conv_session`、FK user/space/session、`last_message_seq` |
| `message` | 消息 | `uk_msg_conv_seq`、FK conversation、`truncated` |
| `highlight` | 批注 | `idx_hl_user`、`idx_hl_message` |
| `learning_understanding` | 当前理解快照 | `uk_understanding_user_concept`、FK user/concept |
| `learning_evidence` | append-only 证据 | `uk_evidence_dedupe`、`idx_ev_concept_created`、FK user/concept/understanding |
| `understanding_revision` | 不可变版本 | `uk_revision_no`、`idx_revision_evidence` |
| `evidence_source` | 证据来源 | `uk_evidence_source`、`idx_source`、级联删除 |
| `learning_state` | 投影状态 | `uk_state_user_concept`、FK user/concept |
| `memory` | 长期记忆 | `uk_memory_identity`、`idx_memory_retrieval`、FK user/space/concept |
| `memory_source` | 记忆来源 | `uk_memory_source`、级联删除 |
| `test_task` | 验证任务 | `idx_test_user/concept/conversation`、FK |
| `test_answer` | 测试作答 | `uk_answer_task`、级联删除 |
| `document` | 文档 | `uk_doc_space_active_hash`、`idx_doc_space_status`、FK space |
| `document_chunk` | 文档切片 | `uk_chunk_doc_idx`、级联删除 |
| `learning_plan` | 学习计划 | 表存在，无业务实现 |
| `import_task` | 导入任务 | 表存在，无业务实现 |
| `agent_run` | 运行轨迹 | `idx_agent_run_trace`、FK user/conversation/session；状态含 PENDING/STALE/RUNNING/COMPLETED/TRUNCATED/SUCCEEDED/FAILED |

## 附录 B：测试执行明细

### B.1 Agent（本次真实执行）

```text
$ cd agent && python -m pytest -q
383 passed, 5 skipped, 1 xfailed, 1 warning in 10.90s
```

- 389 collected。
- 5 skip：`test_knowledge_e2e.py` ×1、`test_qdrant_integration.py` ×2、`test_worker_broker.py` ×2。
- 1 xfail(strict)：检索成功但片段被裁剪时的语义误述风险。

### B.2 Spring（本次真实执行）

```text
$ cd backend/backend && mvn -B test
Tests run: 255, Failures: 0, Errors: 0, Skipped: 73
BUILD SUCCESS，总耗时 42.973s

Surefire 聚合：39 个 report
255 total / 182 executed / 182 passed / 73 skipped / 0 failed
```

- 源码中共有 265 个 `@Test`，与 255 的差额 10 条来自 `AnalysisMqIntegrationTest`。
- 该测试类有 10 个 `@Test`，`@BeforeAll` 检测到 Docker daemon 未运行后
  `Assumptions.abort`，Surefire 记为 `Tests run: 0`，因此不计入 255。
- 默认可执行的是 Mock / 纯函数测试：Controller、Document、Evidence、MQ Consumer、
  SessionResolver、Schema 一致性、Http Gateway 等。

skip 分布：`BackendApplicationTests` 1、`ChatSseIntegrationTest` 8、
`ChatSseOrderingProbeTest` 1、`DocumentIndexE2EIntegrationTest` 1、
`DocumentIndexServiceIntegrationTest` 15、`DocumentServiceIntegrationTest` 7、
`InternalIdentityHttpIntegrationTest` 5、`Phase2MqProcessorIntegrationTest` 5、
`Phase2RabbitMqBrokerTest` 5、`Phase2RealStateIntegrationTest` 6、
`SecurityContextIntegrationTest` 19。

### B.3 离线回归评测（本次真实执行）

```text
$ python -m evaluation
40 条用例 / 40 通过 / 0 失败 / 0 跳过
断言 124 / 124（100%）
运行时间：2026-11-07，代码 4732e40
```

> 该命令会覆盖 `evaluation/reports/latest.md` / `latest.json`；审计结束后已用
> `git checkout --` 恢复，工作区只保留新增的 `docs/audit/`。CI 中该步骤存在
> （`.github/workflows/ci.yml:80-83`）。

### B.4 未进入默认测试的验证

| 项目 | 性质 | 当前产物 |
| --- | --- | --- |
| `AnalysisMqIntegrationTest`（Testcontainers） | Docker 可用即运行；profile 只负责“缺 Docker 时构建失败” | 本次 Docker daemon 未运行 → 0 executed |
| 真实 Qdrant / 真实 broker / 知识 E2E | Agent 环境门控 skip | 本地未设置变量；CI 只执行 broker 2 条 |
| `scripts/phase51-e2e.sh`（101）、`phase51-mq-e2e.sh`（27）、`phase52-e2e.sh`（94）、`phase6-demo.sh`（23） | 手工 E2E（Mock LLM） | **当前 `target/` 无对应产物目录，无法独立复核** |
| `scripts/phase53-spring-e2e.py` | 手工 E2E（真实模型） | `target/phase53-spring-e2e/20261030-221439`、`221521`、`221612` 下的 `summary.json`，均 47/0 |
| `scripts/phase54-rag-citation-e2e.py` | 手工 E2E（真实模型 + 真 Qdrant） | `target/phase54-rag-e2e/20261030-213447/summary.json`，28/0；首轮 `213359` 为 26/2 |
| `scripts/phase54-mq-real-e2e.py` | 手工 E2E（真 broker + Worker + 真实模型） | 19/0（`220957`）；15/4（`220047`）；另有两次 18/1（`215443`、`215815`） |
| `scripts/phase54-concurrency-e2e.py` | 手工并发基线 | 6/12、12/24、24/24 均 10/0（`215226`、`215256`、`215327`） |
| `python -m evaluation --mode all` | 全量 online 评测 | 只有 2026-10-21、`34b59b5` 的 49/49 历史结果；本轮未重跑 |
| `scripts/regress-closed-loop.sh` | 旧闭环脚本 | 本轮未重跑 |
| `backend/backend/scripts/e2e-verify.sh`、`e2e-concurrency.sh` | 旧回归 / 并发脚本 | 未被 TEST_MATRIX 列为当前结果 |
| GitHub Actions | 配置存在；只跑 pytest + evaluation + `-Pmq-integration`，**不跑任何 E2E** | 本轮未触发 |
| Flutter | 脚手架，无业务测试 | — |

## 附录 C：关键证据索引

| 主题 | 文件 |
| --- | --- |
| SSE 聊天链路 | `backend/.../service/ChatService.java:114-350` |
| inline / MQ 分派 | `backend/.../service/AnalysisDispatchService.java:42-63` |
| Agent 分析写入 | `backend/.../service/AgentAnalysisWriteService.java:111-138,339-422` |
| Evidence 去重 | `backend/.../service/EvidenceService.java:100-140,256` |
| Understanding / Revision | `backend/.../service/UnderstandingService.java:96-334` |
| LearningState 投影 | `backend/.../service/LearningStateService.java:76-101` |
| Memory 生命周期 | `backend/.../service/MemoryService.java:77-432` |
| Session 归属 | `backend/.../service/SessionService.java:65-101,205-247` |
| 文档索引编排 | `backend/.../service/DocumentIndexService.java:76-160` |
| MQ 拓扑 | `backend/.../mq/RabbitMqConfig.java:46-111` |
| MQ 消费与 DLQ | `backend/.../mq/AnalysisResultConsumer.java:57-210` |
| MQ 幂等处理 | `backend/.../mq/AnalysisResultProcessor.java:72-214` |
| 补偿扫描 | `backend/.../mq/AnalysisStaleRunCompensator.java:60-88` |
| 内部委托鉴权 | `backend/.../security/InternalServiceAuthenticationFilter.java:22-107` |
| Agent Runtime | `agent/app/runtime.py:210-743` |
| ContextBuilder | `agent/app/context/builder.py:57-70,266-286,542-568,897-963` |
| 知识检索 | `agent/app/retriever/knowledge.py:23-140` |
| 引用生成 / 校验 | `agent/app/citations.py:114-326` |
| 流式引用过滤 | `agent/app/citation_filter.py:53-161` |
| 文档解析器 | `agent/app/documents/parser.py:19,94-98`（仅 md/txt） |
| Qdrant 写入 | `agent/app/vectorstore/qdrant_store.py:153-206` |
| 文档索引状态机 | `backend/.../service/DocumentIndexService.java:76-162` |
| Agent Worker | `agent/app/messaging/worker.py:196-501` |
| 分析器 | `agent/app/analysis/analyzer.py:110-381` |
| 数据库 | `backend/.../resources/schema.sql` |
| 迁移脚本 | `schema-migration-document-rag.sql`、`schema-migration-phase7-truncation.sql`、`schema-migration-phase54-message-seq.sql` |
| 测试矩阵 | `docs/delivery/TEST_MATRIX.md` |
| 已知限制 | `docs/delivery/KNOWN_LIMITATIONS.md` |
| 架构 | `docs/delivery/ARCHITECTURE.md` |
