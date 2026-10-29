# Phase 3.2 跳过测试审计

- 日期：2026-10-28
- 范围：后端全量套件 41 条 skipped；Agent 全量套件 5 条 skipped
- 原则：跳过 ≠ 通过；逐条标明依赖、覆盖的业务风险、是否有其他真实证据；
  关键 E2E 场景如果没有等价覆盖，就列为未覆盖风险，不笼统归为“外部依赖测试”

## 1. 统计与来源

| 套件 | 总数 | passed | skipped | failed | 来源 |
| --- | --- | --- | --- | --- | --- |
| 后端 | 243 | 202 | **41** | 0 | `backend/backend/target/surefire-reports/*.xml` |
| Agent | 333 | **328** | **5** | 0 | `cd agent && python -m pytest -q -rs` |

后端 41 条的分布：

| 测试类 | skipped | 门控变量 | 主要依赖 |
| --- | --- | --- | --- |
| `BackendApplicationTests` | 1 | 无（`Assumptions`） | MySQL 库表 |
| `Phase2MqProcessorIntegrationTest` | 5 | `AILS_PHASE2_IT` | 真实 MySQL |
| `Phase2RabbitMqBrokerTest` | 5 | `AILS_PHASE2_MQ_IT` | 真实 RabbitMQ |
| `ChatSseIntegrationTest` | 7 | `AILS_CHAT_SSE_IT` | 真实 Tomcat + MySQL + Security |
| `DocumentIndexE2EIntegrationTest` | 1 | `AILS_DOCUMENT_E2E_IT` | 真实 Agent + 真实 Qdrant + MySQL |
| `DocumentIndexServiceIntegrationTest` | 9 | `AILS_DOCUMENT_INDEX_IT` | 真实 MySQL + 假 Agent |
| `DocumentServiceIntegrationTest` | 7 | `AILS_DOCUMENT_IT` | 真实 MySQL + Mock Qdrant/存储 |
| `Phase2RealStateIntegrationTest` | 6 | `AILS_PHASE2_IT` | 真实 MySQL |
| **合计** | **41** | | |

Agent 5 条：`test_knowledge_e2e.py` 1、`test_qdrant_integration.py` 2、
`test_worker_broker.py` 2。

## 2. 后端 41 条逐条审计

“其他证据”只列**真实执行过**的测试或本轮 E2E；单元测试用 Mock，不能等同于
真实依赖证据。

### 2.1 `BackendApplicationTests`（1）

| 方法 | 跳过原因 | 风险 | 其他证据 | 结论 |
| --- | --- | --- | --- | --- |
| `contextLoads` | 需要本地 MySQL 与库表已初始化 | Spring 上下文/Bean 装配失败 | `SecurityContextIntegrationTest`、`InternalIdentityHttpIntegrationTest`、本轮 E2E 都用真实 MySQL 启动了完整上下文 | **低危，已有等价证据** |

### 2.2 `Phase2MqProcessorIntegrationTest`（5）

全部因 `AILS_PHASE2_IT` 未设置而跳过；该门控要求真实 MySQL。

| 方法 | 覆盖的关键风险 | 本轮 E2E 是否覆盖 | 结论 |
| --- | --- | --- | --- |
| `concurrentDuplicateDeliveryDoesNotDuplicateBusinessRows` | 并发重复投递只产生一条业务行 | 覆盖了**顺序重复投递**（2 次消费、1 份数据），未覆盖并发竞争 | **部分覆盖**；并发仍缺口 |
| `failedResultMarksRunFailedWithoutBusinessRows` | 失败结果不写业务行 | 覆盖（5 个非法场景均 FAILED、无 Evidence/Revision/Memory） | **已覆盖** |
| `writeFailureRollsBackEvidenceAndClaim` | 写库中途失败时事务整体回滚、claim 不残留 | **未覆盖** | **关键缺口** |
| `duplicateDeliveryIsAckedWithoutDuplicateRows` | 重复结果被 ACK 且不重复写 | 覆盖了重复结果识别与不重复写；ACK 语义未直接断言 | **部分覆盖** |
| `traceMismatchWritesNothing` | traceId 不一致必须拒绝且不写库 | **未覆盖** | **关键缺口** |

### 2.3 `Phase2RabbitMqBrokerTest`（5）

全部因 `AILS_PHASE2_MQ_IT` 未设置而跳过；需要真实 RabbitMQ。

| 方法 | 覆盖的关键风险 | 本轮 E2E 是否覆盖 | 结论 |
| --- | --- | --- | --- |
| `retryQueueReturnsMessageAfterTtl` | 重试队列 TTL 到期后消息回到 request 队列 | 覆盖了 retry 队列副本被新 Worker 消费；**未等待 TTL 自然到期** | **部分覆盖** |
| `rejectedResultReachesDeadQueue` | 被拒绝的结果进入死信 | 覆盖（5 条坏消息 DLQ +5） | **已覆盖** |
| `duplicatePublishIsVisibleToBroker` | 重复发布对 Broker 可见 | 覆盖（同一 taskId 两次投递均被消费） | **已覆盖** |
| `unackedMessageIsRedelivered` | 未 ACK 消息在连接断开后重投 | **未覆盖** | **缺口**；影响至少一次投递语义 |
| `publishIsConfirmedAndConsumed` | 发布确认与消费闭环 | 覆盖了生产→消费闭环；publisher confirm 模式未单独断言 | **部分覆盖** |

### 2.4 `ChatSseIntegrationTest`（7）

全部因 `AILS_CHAT_SSE_IT` 未设置而跳过；需要真实 Tomcat + MySQL + Security。
本轮 E2E 只把 SSE 当作“产生消息与 ANALYSIS run 的入口”，没有断言 SSE 顺序、
断开或错误终态。单元测试 `ChatServiceTest`（4）与 `GlobalExceptionHandlerTest`（2）
不覆盖真实 HTTP 流。

| 方法 | 覆盖的关键风险 | 其他真实证据 | 结论 |
| --- | --- | --- | --- |
| `normalStreamPersistsBeforeEndThenAnalyzes` | `start→delta→end` 顺序；`end` 前已落库 | 无 | **关键缺口** |
| `disconnectAfterAssistantPersistedStillAnalyzesAndReconnectIsNotDuplicated` | 落库后客户端断开仍继续分析，重连不重复 | 无 | **关键缺口** |
| `assistantPersistenceFailureSendsErrorAndSkipsAnalysis` | 持久化失败必须报错且不启动分析 | 无 | **关键缺口** |
| `modelErrorIsTerminalError` | 模型错误是终态错误 | 无 | **关键缺口** |
| `emptyStreamIsTerminalError` | 空流是终态错误，不返回 200 假成功 | 无 | **关键缺口** |
| `crossUserConversationIsRejectedBeforePersistence` | 跨用户会话在落库前拒绝 | 部分：`SecurityContextIntegrationTest` 覆盖跨用户空间/资源权限，但不覆盖 SSE 预流路径 | **关键缺口**（SSE 特有路径） |
| `duplicatePostWithoutIdempotencyKeyCreatesDuplicateMessages` | 明确记录“无幂等键会重复”的当前行为 | 无 | **契约记录缺口**；不是要求去重，而是要求行为可验证 |

### 2.5 `DocumentIndexE2EIntegrationTest`（1）

| 方法 | 跳过原因 | 覆盖的关键风险 | 其他证据 | 结论 |
| --- | --- | --- | --- | --- |
| `realAgentAndQdrantIndexAreConsistentAndIdempotent` | `AILS_DOCUMENT_E2E_IT` 未设置；需要真实 Agent + 真实 Qdrant + MySQL | 上传→解析→分块→embedding→Qdrant 全链路一致；重复索引幂等；DB chunk 数与 Qdrant point 数一致 | 无；本轮 E2E 使用 hash embedding 且无知识文档命中 | **关键缺口**：RAG 知识链路没有真实端到端证据 |

### 2.6 `DocumentIndexServiceIntegrationTest`（9）

全部因 `AILS_DOCUMENT_INDEX_IT` 未设置而跳过；真实 MySQL + 假 Agent（类注释明确
“不要求 FastAPI/Qdrant 已启动”）。这些测试覆盖索引状态机，但**不覆盖真实 Qdrant**。

| 方法 | 覆盖的关键风险 | 其他证据 | 结论 |
| --- | --- | --- | --- |
| `successfulIndexWritesChunksVectorRefsAndReadyState` | 成功索引写 chunk/vector ref 并进入 READY | 单元 `DocumentIndexServiceTest`（6）只覆盖逻辑分支 | **关键缺口**（真实库状态） |
| `repeatedIndexIsIdempotentAndIncrementsVersionOnlyOnSuccess` | 重复索引幂等，版本只在成功时递增 | 无 | **关键缺口** |
| `uploadKicksOffIndexingAndReturnsRealAsyncStatus` | 上传后异步状态真实可查 | 无 | **关键缺口** |
| `parseFailureNeverBecomesReady` | 解析失败不得 READY | 单元测试部分覆盖 | **关键缺口** |
| `chunkPersistenceFailureRollsBackAllChunksAndFails` | chunk 写失败整体回滚 | 无 | **关键缺口**（事务） |
| `embeddingOrQdrantFailureAfterChunkWriteNeverBecomesReady` | 向量写入失败不得 READY | 无（且假 Agent 不等于真实 Qdrant） | **关键缺口** |
| `internalAuthFailureNeverBecomesReady` | 内部鉴权失败不得 READY | 无 | **关键缺口** |
| `malformedVectorMappingFailsAndDoesNotWriteRefs` | 向量映射畸形时失败且不写 ref | 无 | **缺口** |
| `indexErrorIsTruncatedTo1024Chars` | 错误信息截断，避免超长入库 | 单元测试可覆盖 | **低危** |

### 2.7 `DocumentServiceIntegrationTest`（7）

全部因 `AILS_DOCUMENT_IT` 未设置而跳过；真实 MySQL，Qdrant/存储为 Mock。

| 方法 | 覆盖的关键风险 | 其他证据 | 结论 |
| --- | --- | --- | --- |
| `duplicateUploadReturnsExistingDocumentAndLeavesOneFile` | 重复上传不产生重复文档/文件 | 单元 `DocumentServiceTest`（14） | **部分覆盖**；真实库唯一性未验证 |
| `concurrentUploadsCreateExactlyOneActiveDocument` | 并发上传只创建一个活跃文档 | 无 | **缺口**（并发） |
| `listAndDetailRespectOwnership` | 列表/详情按归属过滤 | `SecurityContextIntegrationTest` 覆盖跨用户空间读取，但不覆盖文档列表/详情 | **部分覆盖** |
| `otherUsersSpaceCannotBeUploaded` | 不能向他人空间上传 | 安全测试覆盖空间归属；文档上传路径未覆盖 | **部分覆盖** |
| `sameContentInDifferentSpacesIsAllowed` | 不同空间同内容允许 | 无 | **低危** |
| `softDeletedDocumentIsHiddenAndItsHashCanBeReused` | 软删文档隐藏，hash 可复用 | 单元测试部分覆盖 | **缺口** |
| `readyIdsOnlyReturnFullyIndexedReadyDocumentsOfTheSpace` | 只返回本空间、完全索引的 READY 文档 | 无 | **关键缺口**（检索候选正确性） |

### 2.8 `Phase2RealStateIntegrationTest`（6）

全部因 `AILS_PHASE2_IT` 未设置而跳过；真实 MySQL。

| 方法 | 覆盖的关键风险 | 本轮 E2E 是否覆盖 | 结论 |
| --- | --- | --- | --- |
| `evidenceDedupeOnRealDatabase` | 同一观察只留一条 Evidence，推进 `last_seen_run_id` | 覆盖了单次写入与重复任务不新增；未覆盖“同一观察再次分析”的推进 | **部分覆盖** |
| `identicalUnderstandingDoesNotCreateRevision` | 内容相同不新增 Revision，不推进 `updated_at` | **未覆盖** | **关键缺口** |
| `revisionMatchesSnapshotAfterRealChange` | 内容变化时当前快照与 Revision 字段/时间一致 | 只覆盖了初始 Revision 状态一致 | **部分覆盖** |
| `transactionRollbackLeavesNoPartialState` | 事务失败无半完成行 | **未覆盖** | **关键缺口** |
| `memoryLifecyclePersistsRealStatus` | Memory 生命周期状态真实持久化 | 覆盖了 CANDIDATE/GLOBAL/来源；未覆盖后续状态迁移 | **部分覆盖** |
| `learningStateSurvivesNewSession` | 新会话能读到持久化学习状态 | **未覆盖** | **关键缺口**（跨会话） |

## 3. Agent 5 条逐条审计

| 文件/方法 | 跳过条件 | 依赖 | 覆盖的关键风险 | 其他真实证据 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `test_knowledge_e2e.py::test_upload_then_answer_with_traceable_sources_and_degrade_safely` | `AILS_KNOWLEDGE_E2E != true` | 真实后端 + 真实 Qdrant + 真实 embedding | 上传后回答能给出可追溯来源；检索失败时安全降级 | 无 | **关键缺口**：RAG 可追溯性未验证 |
| `test_qdrant_integration.py::test_real_collection_index_reindex_and_document_scoped_delete` | `AILS_QDRANT_IT != true` | 真实 Qdrant | collection 索引/重建/按文档删除的一致性 | 无 | **关键缺口** |
| `test_qdrant_integration.py::test_real_knowledge_search_enforces_permissions_and_ranking` | `AILS_QDRANT_IT != true` | 真实 Qdrant | 检索权限过滤与排序 | 无 | **关键缺口**：跨用户知识泄露/排序质量 |
| `test_worker_broker.py::test_retry_copy_returns_to_request_queue_and_original_is_acked` | `AILS_TEST_RABBITMQ_URL` 未设置 | 真实 RabbitMQ | 重试副本回到 request 队列、原消息 ACK | 本轮 E2E 覆盖了 retry 副本被消费；未断言原消息 ACK 细节 | **部分覆盖** |
| `test_worker_broker.py::test_when_no_copy_can_be_published_the_message_stays_in_the_queue` | `AILS_TEST_RABBITMQ_URL` 未设置 | 真实 RabbitMQ | 无法发布副本时消息不得丢失 | 无 | **缺口**（可靠性边界） |

## 4. 关键未覆盖风险（按严重度）

| 编号 | 风险 | 相关 skipped | 为什么重要 | 现有真实证据 |
| --- | --- | --- | --- | --- |
| S-01 | 真实 HTTP SSE 顺序、断开、持久化失败、空流/模型错误终态 | `ChatSseIntegrationTest` 7 | 用户可见链路的核心失败模式；Mock 单测不能证明真实流行为 | 无 |
| S-02 | RAG 知识链路端到端：真实 Qdrant 索引、检索权限、来源可追溯 | `DocumentIndexE2EIntegrationTest` 1 + Agent 3 | “有资料时引用资料”是产品核心；本轮 E2E 无知识命中 | 无 |
| S-03 | 文档索引状态机在真实 MySQL 下的事务/幂等/READY 边界 | `DocumentIndexServiceIntegrationTest` 9 | 半完成索引会把不可用文档送进检索 | 仅单元分支 |
| S-04 | MQ 写库失败回滚、traceId 不一致拒绝 | `Phase2MqProcessorIntegrationTest` 2 条 | 错误消息可能污染学情或留下半完成 claim | 无 |
| S-05 | 相同 Understanding 不新增 Revision、跨会话状态读取 | `Phase2RealStateIntegrationTest` 2 条 | 直接影响版本历史与“新会话记得我” | 无 |
| S-06 | Broker 未 ACK 重投、TTL 到期回队列 | `Phase2RabbitMqBrokerTest` 2 条 + Agent 2 条 | 至少一次投递语义与消息不丢失 | 部分（retry 副本被消费） |
| S-07 | 文档归属/软删在真实库的过滤 | `DocumentServiceIntegrationTest` 7 | 跨用户文档泄露或软删文档被检索 | 单元 + 安全测试部分覆盖 |

**判定：** S-01、S-02、S-03、S-04、S-05 是**关键未覆盖风险**，不能因为
“本轮 MQ E2E 109/0”就宣称这些链路已通过。S-06/S-07 为中等风险，已有部分真实证据。

## 5. 与 MQ E2E 的关系

本轮 109/0 的 E2E 只覆盖：

- 合法 v2 的 Spring→RabbitMQ→Worker→HTTP→MySQL 主路径；
- 非法 v2/旧版本消息的拒绝、死信、不写 Agent 学情；
- 顺序重复投递、401 重试恢复、rules-v1 兜底、清理。

它**不覆盖**上表 S-01 至 S-07。尤其：

- 本轮 SSE 只用于制造消息，没有断言 SSE 契约；
- 本轮没有上传知识文档，`knowledgeStatus=NO_MATCH`；
- 本轮没有注入写库失败或 traceId 不一致；
- 本轮没有并发、没有 TTL 到期等待、没有未 ACK 重投。

## 6. 建议

1. 优先启用 `ChatSseIntegrationTest`（S-01）和 `DocumentIndexE2EIntegrationTest` +
   Agent Qdrant 测试（S-02）：这两组是用户可见且当前完全没有真实证据的链路；
2. 在独立 MySQL 上启用 `Phase2RealStateIntegrationTest` 与
   `Phase2MqProcessorIntegrationTest` 的回滚/幂等/trace 用例（S-04、S-05）；
3. 为 `ChatSseIntegrationTest` 的空流/模型错误用例补一条“HTTP 200 但 0 输出必须
   记终态错误”的断言，避免把空流当成功；
4. 在 CI 中至少保留 MQ E2E 的 sub-set（合法 v2 + 非法 v2 + 重复投递），
   其余真实依赖测试用独立 job；
5. 不把 skipped 数量从报告中隐藏；每次全量运行都重新统计。

## 7. 产物与统计口径

- 后端统计来自 `backend/backend/target/surefire-reports/*.xml` 的
  `tests/skipped` 属性；41 条逐方法名与 `skipped message` 已核对；
- Agent 统计来自 `python -m pytest -q -rs` 的 short summary；
- 所有 skipped 均未计入 PASS；
- 本轮没有为了减少 skipped 而删除测试或放宽门控。
