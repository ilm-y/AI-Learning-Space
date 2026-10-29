# Spring Boot 后端测试方案与 Phase 2 执行结果

- 阶段：Phase 2
- 模块：`backend/`
- 技术栈：Spring Boot 3、Java 17、MyBatis-Plus、MySQL 8、JWT、SSE、RabbitMQ
- 原则：断言业务副作用与最终数据库状态，不把 HTTP 200 当成通过；
  Mock、真实 MySQL、真实 MQ、真实 Qdrant 分开统计；未执行/阻塞不计 PASS

## 1. 实际依赖状态

| 依赖 | 状态 | 用途 |
| --- | --- | --- |
| MySQL 8.0.37 | 真实运行 | `ails_phase2_it`、`ails_phase2_e2e`、文档测试库 |
| RabbitMQ | 真实运行（本地容器，5672/15672） | 发布确认、重复投递、重投、TTL 重试、死信 |
| Qdrant | 真实运行（6333） | 文档索引 E2E、向量点一致性 |
| Docker / Testcontainers | CLI 可用，但 Testcontainers 无法连接 | `AnalysisMqIntegrationTest` 被 abort，记 BLOCKED |
| 真实 LLM | 可发出请求，但持续空流 | 3 条真实模型用例 BLOCKED，不作为质量结论 |
| Agent | 本地 FastAPI（mock LLM / hash embedding） | 文档 E2E、HTTP 演示、鉴权测试 |

数据库凭证只从环境变量或 gitignored 的 `application-local.yml` 读取；
测试代码中不再写默认密码字面量。

## 2. 测试分层与统计口径

| 层级 | 定义 | 本次数量 |
| --- | --- | ---: |
| UNIT | 无真实外部依赖，使用 Mock / 固定对象 | 4 |
| INTEGRATION | 真实 MySQL / 真实 RabbitMQ / Spring 容器；允许局部 Mock | 44 |
| END_TO_END | 真实 HTTP + 真实 MySQL，部分链路接 Agent / Qdrant | 1 个 JUnit + 23 条 HTTP 演示断言 |
| BLOCKED | 环境不可用，未真正执行 | 1 个 Testcontainers 测试类（0 tests） |

Phase 2 实际执行的 JUnit：**49 个，PASS 49，FAIL 0，BLOCKED 0**。
最终完整后端套件（2026-10-27 21:04–21:05）为
**208 tests / 0 failures / 0 errors / 1 skipped**；`BackendApplicationTests`
被 `@Disabled`，`AnalysisMqIntegrationTest` 为 0 tests（Testcontainers 阻塞）。
49 个是 Phase 2 的目标集，208 个是同一工作区的完整套件，两者不混算。

> **Phase 2.1 更新：**上表 208 是 Phase 2 时点快照。加入 SSE 真实 HTTP、
> 签名身份、错误表示等测试后，当前完整套件为 **226 tests / 0 failures /
> 0 errors / 56 skipped（默认门控）/ 170 executed**；专项门控结果
> Security 15/15、SSE 7/7、GlobalExceptionHandler 2/2、Signer 4/4。
> D6/D8 已由 `ChatSseIntegrationTest` 覆盖（落库并收到 `message.end` 后断开），
> 落库前断开仍未覆盖；D7 的空流/模型错误后端终态已由 SSE-2.1-04/05 覆盖。
> 详见 [phase2.1-integration-results.md](phase2.1-integration-results.md)。
E2E 使用独立 Qdrant prefix `ails_e2e_phase2` 与
`ails_e2e_phase2_hash-v1_8`，不删除共享 collection；配置错误与共享删除风险
见 `failure-analysis.md` I-06 / I-07。

| 测试类 | 数量 | 层级 | 外部依赖 |
| --- | ---: | --- | --- |
| `SecurityContextIntegrationTest` | 12 | INTEGRATION | 真实 MySQL + JWT |
| `Phase2RealStateIntegrationTest` | 6 | INTEGRATION | 真实 MySQL |
| `Phase2MqProcessorIntegrationTest` | 5 | INTEGRATION | 真实 MySQL；Memory/Verification/LearningAnalysis 为 Mock |
| `Phase2RabbitMqBrokerTest` | 5 | INTEGRATION | 真实本地 RabbitMQ |
| `DocumentServiceIntegrationTest` | 7 | INTEGRATION | 真实 MySQL；DocumentIndexService 为 Mock |
| `DocumentIndexServiceIntegrationTest` | 9 | INTEGRATION | 真实 MySQL；Agent HTTP 为本地 stub |
| `DocumentIndexE2EIntegrationTest` | 1 | END_TO_END | 真实 MySQL + 真实 Qdrant + 本地 Agent |
| `ChatServiceTest` | 4 | UNIT | Mock SSE / 持久化 |

附加执行：

- `HttpPythonAgentGatewayTest` 9 + `HttpPythonAgentAnalysisGatewayTest` 9：
  UNIT，验证内部请求头传播；
- `scripts/phase6-demo.sh`：真实后端 + 真实 FastAPI + 真实 MySQL，
  **PASS=23 / FAIL=0**，覆盖新会话、显式复盘、SSE 终态、DB 只读核对。

## 3. Phase 2 A–F 矩阵与结果

### A. API 鉴权与权限隔离

| 编号 | 场景 | 测试 | 结果 |
| --- | --- | --- | --- |
| A1 | 无 JWT 访问受保护接口 | `unauthenticatedRequestToProtectedEndpointReturns401Json`、`unauthenticatedReadyIdsRequestReturns401` | PASS |
| A2 | 用户 A 读取用户 B 的空间/文档 | `listAndDetailRespectOwnership`、`otherUsersSpaceCannotBeUploaded` | PASS |
| A3 | 跨空间 ready-ids / 文档过滤 | `readyIdsEndpointUsesJwtIdentityAndKeepsOwnershipCheck`、`readyIdsOnlyReturnFullyIndexedReadyDocumentsOfTheSpace` | PASS |
| A4 | 请求体伪造 `userId` | `forgedBodyUserIdCannotOverrideJwtIdentity` | PASS |
| A5 | 内部服务 token 不能绕过空间归属 | `internalServiceTokenActsAsUserWithoutBypassingSpaceOwnership` | PASS |
| A6 | 内部 token 只能读绑定用户 | `internalServiceTokenCanReadReadyIdsOnlyForItsUser` | PASS |
| A7 | 非法内部 token 拒绝 | `invalidInternalTokenIsRejected` | PASS |
| A8 | 过滤器顺序 | `internalFilterStillRunsBeforeJwtFilter` | PASS |
| A9 | Agent 内部接口 ↔ Spring 边界 | Agent 31 条安全测试 + gateway 18 条单测 | PASS（Agent 侧） |

说明：A9 的 Spring 侧只证明「内部 token 以指定用户身份通过过滤器，但不绕过
空间归属」；没有把共享 token 当成用户级认证。

### B. 数据状态与事务

| 编号 | 场景 | 测试 | 结果 |
| --- | --- | --- | --- |
| B1 | 同一观察重复分析只留一条 Evidence，推进 `last_seen_run_id` | `evidenceDedupeOnRealDatabase` | PASS |
| B2 | 相同内容重复 apply 不新增 Revision，不推进 `updated_at` | `identicalUnderstandingDoesNotCreateRevision` | PASS |
| B3 | Revision 与当前快照的 summary / known_parts / misconceptions / status / 时间一致 | `revisionMatchesSnapshotAfterRealChange` | PASS |
| B4 | 外层事务回滚后无半完成行 | `transactionRollbackLeavesNoPartialState` | PASS |
| B5 | 用户 Memory = ACTIVE，Agent 候选 = CANDIDATE，来源行存在 | `memoryLifecyclePersistsRealStatus` | PASS |
| B6 | 新会话重新查库仍能读到 Understanding / Evidence / LearningState | `learningStateSurvivesNewSession` | PASS |

未覆盖：非法状态迁移（例如 DEMONSTRATED 直接回退到 EXPOSED）没有独立用例；
状态机现有单测覆盖合法生命周期，但 Phase 2 没有补「非法迁移必须拒绝」的真实库测试。

### C. RabbitMQ、幂等与最终状态

| 编号 | 场景 | 测试 | 结果 |
| --- | --- | --- | --- |
| C1 | 发布确认后消息真实进入请求队列并可消费 | `publishIsConfirmedAndConsumed` | PASS |
| C2 | 同一任务重复发布产生两条消息，消费端靠幂等收敛 | `duplicatePublishIsVisibleToBroker` | PASS |
| C3 | 未 ACK 消息在连接断开后重投 | `unackedMessageIsRedelivered` | PASS |
| C4 | 失败消息进入重试队列，TTL 到期回请求队列 | `retryQueueReturnsMessageAfterTtl` | PASS |
| C5 | 结果消息被拒绝后进入死信队列 | `rejectedResultReachesDeadQueue` | PASS |
| C6 | 正常处理一次，重复投递识别为 DUPLICATE | `duplicateDeliveryIsAckedWithoutDuplicateRows` | PASS |
| C7 | 并发重复投递只留一条 Evidence / Revision | `concurrentDuplicateDeliveryDoesNotDuplicateBusinessRows` | PASS |
| C8 | FAILED 结果：run FAILED，无业务行 | `failedResultMarksRunFailedWithoutBusinessRows` | PASS |
| C9 | 写入中途失败：整笔回滚，run 仍 PENDING | `writeFailureRollsBackEvidenceAndClaim` | PASS |
| C10 | traceId 不一致：拒绝，run FAILED，无写入 | `traceMismatchWritesNothing` | PASS |

BLOCKED：`AnalysisMqIntegrationTest`（Testcontainers）在本机无法连接 Docker，
被 JUnit abort，0 tests。没有把它计入 PASS；本地 broker 测试作为替代覆盖，
但独立容器隔离仍未验证。

### D. SSE 与持久化

| 编号 | 场景 | 测试 | 结果 |
| --- | --- | --- | --- |
| D1 | 正常 SSE：助手消息落库一次、分析触发一次 | `normalStreamPersistsOnceAndAnalyzesOnce` | PASS |
| D2 | 首个 delta 发送异常：仍落库并触发分析 | `sendFailureDoesNotSkipPersistenceOrAnalysis` | PASS |
| D3 | 持久化后 `complete()` 异常：仍分析，且不重复保存 | `completeFailureAfterPersistenceStillAnalyzes` | PASS |
| D4 | 助手消息落库失败：不启动分析 | `assistantPersistenceFailureDoesNotAnalyze` | PASS |
| D5 | 真实 HTTP SSE 终态：`message.start` → delta → `message.end`，无 `message.error` | `scripts/phase6-demo.sh` | PASS |
| D6 | 客户端中途断开（落库前） | 未实现 | NOT_COVERED（Phase 2.1） |
| D7 | 真实模型错误 / 空流后的后端终态 | `ChatSseIntegrationTest` 空流/模型错误终态 | PASS（替身网关） |
| D8 | SSE 断开但消息已落库时分析是否继续 | `ChatSseIntegrationTest` 落库后断开 | PASS（Phase 2.1） |

D1–D4 是单元测试（Mock SSE / 持久化），不能证明真实 HTTP 断连行为；
D5 是真实 HTTP，但没有主动断开客户端。

### E. 持久化 E2E

| 编号 | 场景 | 测试 | 结果 |
| --- | --- | --- | --- |
| E1 | 首次对话产生证据并落库 | `scripts/phase6-demo.sh` 第 2 节 + `Phase2RealStateIntegrationTest` | PASS |
| E2 | 结束会话，同一用户新会话仍可读取 Understanding / Memory / State | `scripts/phase6-demo.sh` 第 3 节、`learningStateSurvivesNewSession` | PASS |
| E3 | 上下文数据源与 DB 一致 | 演示第 3/4/6 节：preview 区块 + DB 只读核对 | PASS |
| E4 | Memory 写入后按作用域可读 | `memoryLifecyclePersistsRealStatus` + Agent C01/C02 | PASS（服务层 + Agent 侧） |
| E5 | 清理后无残留 | Phase 2 测试每次 `DELETE` 专用库表；演示每次新建用户/空间 | 部分 |

E5 说明：

- `Phase2RealStateIntegrationTest` / `Phase2MqProcessorIntegrationTest` 在
  `@BeforeEach` 清理自己的专用库，测试数据不残留；
- `phase6-demo.sh` 每次注册新用户、新空间，数据留在隔离库
  `ails_phase2_e2e`，不删除旧运行。它没有污染 `ails` 开发库或真实用户，
  但「绝对无残留」不成立，报告中不写成已清理。

### F. 文档生命周期与 RAG 权限

| 编号 | 场景 | 测试 | 结果 |
| --- | --- | --- | --- |
| F1 | 上传 → 索引 → READY | `successfulIndexWritesChunksVectorRefsAndReadyState` | PASS |
| F2 | 解析失败 / 分块失败 / embedding 或 Qdrant 失败不进入 READY | `parseFailureNeverBecomesReady`、`chunkPersistenceFailureRollsBackAllChunksAndFails`、`embeddingOrQdrantFailureAfterChunkWriteNeverBecomesReady` | PASS |
| F3 | 非 READY 文档不进入检索候选 | `readyIdsOnlyReturnFullyIndexedReadyDocumentsOfTheSpace` | PASS |
| F4 | 跨用户 / 跨空间过滤 | `listAndDetailRespectOwnership`、`otherUsersSpaceCannotBeUploaded`、`readyIds...Ownership` | PASS |
| F5 | 重复索引幂等，只在成功时递增版本 | `repeatedIndexIsIdempotentAndIncrementsVersionOnlyOnSuccess` | PASS |
| F6 | 真实 Agent + 真实 Qdrant 索引一致性与幂等 | `realAgentAndQdrantIndexAreConsistentAndIdempotent` | PASS |
| F7 | 检索不可用时的降级文案 | Agent D05（DEGRADED） | PASS（Agent 侧） |

## 4. 失败与未覆盖项

本次 Phase 2 执行的 49 个 JUnit 与 23 条 HTTP 断言没有 FAIL。未覆盖/阻塞项：

| 编号 | 项 | 状态 | 原因 |
| --- | --- | --- | --- |
| X1 | Testcontainers RabbitMQ 隔离测试 | BLOCKED | Docker 可用但 Testcontainers 无法连接 |
| X2 | SSE 客户端中途断开（落库前） | NOT_COVERED | 未实现该测试 |
| X3 | SSE 断开后分析是否继续 | PASS | Phase 2.1 真实 HTTP 覆盖落库后断开 |
| X4 | 真实模型空流下的后端终态 | PASS（替身网关） | Phase 2.1 `emptyStreamIsTerminalError`；真实模型质量仍 BLOCKED |
| X5 | 非法状态迁移 | NOT_COVERED | 未补真实库用例 |
| X6 | 性能基线 | NOT_RUN | 未执行，不生成估算数字 |
| X7 | SSE 持久化失败的真实 HTTP 链路 | PASS | Phase 2.1 `assistantPersistenceFailureSendsErrorAndSkipsAnalysis` |

## 5. 修复前后对比

| 项 | 修复前 | 修复后 |
| --- | --- | --- |
| Agent 敏感路由 | 无凭据 200，任意 `userId` | 401 / 403；身份绑定；fail closed 503 |
| Analyze Fixture | 可绕过降级保护 | 抛 `RetrievalError`，不调用 LLM |
| 预算丢弃文案 | 谎称「没有历史」 | 说明「检索到但未包含」 |
| 预算口径 | 300 配置 vs 2699 估算 | 动态上下文 ≤ 预算，完整 prompt 如实报告 |
| History Guard | 忽略按需 Evidence | 合并 Evidence，校验计数 |
| auto 回退 | 不可见 | 结构化 `fallbackReason` |
| `recent_history_limit=0` | 注入全部历史 | 不注入；负数非法 |

## 6. 能写进简历 / 不能写进简历

**可以写（有本次真实执行证据）：**

- 在真实 MySQL 上验证 Evidence 去重、Understanding/Revision 幂等、快照一致性、
  事务回滚、Memory 状态；
- 在真实 RabbitMQ 上验证发布确认、重复投递、未 ACK 重投、TTL 重试、死信；
- 用真实 JWT + Spring Security + MySQL 验证跨用户/跨空间隔离与伪造 body userId；
- 用真实 Qdrant 验证文档索引一致性与幂等；
- 设计并执行 Agent HTTP 内部鉴权与身份绑定，修复 P0 跨用户访问风险；
- 统一上下文预算口径，修复「预算丢弃被说成没有历史」。

**不能写：**

- 「全量 226 测试全部通过」：其中 56 个因环境门控 skipped，应说 170 执行、0 失败；
- 「真实 LLM 回答质量通过」：只验证可识别输出，语义质量 BLOCKED；
- 「SSE 断连已完全处理」：只覆盖落库并 `message.end` 后断开，落库前断开未覆盖；
- 「SSE 重复请求已幂等」：无幂等键，重复 POST 会重复落消息；
- 「性能提升 X%」：没有基线；
- 「Testcontainers MQ 集成通过」：Docker 不可用，BLOCKED。

## 7. 复现命令

```bash
# 后端真实集成测试（凭证从环境变量读取）
export AILS_IT_MYSQL_PASSWORD=...
export AILS_SECURITY_IT=true AILS_DOCUMENT_IT=true \
       AILS_DOCUMENT_INDEX_IT=true AILS_DOCUMENT_E2E_IT=true \
       AILS_PHASE2_IT=true AILS_PHASE2_MQ_IT=true AILS_PHASE2_MQ_PASSWORD=...
export AILS_E2E_INTERNAL_TOKEN=... AILS_E2E_QDRANT_COLLECTION=...

./backend/backend/mvnw -Dtest=\
SecurityContextIntegrationTest,DocumentServiceIntegrationTest,\
DocumentIndexServiceIntegrationTest,DocumentIndexE2EIntegrationTest,\
Phase2RealStateIntegrationTest,Phase2MqProcessorIntegrationTest,\
Phase2RabbitMqBrokerTest test

# 真实 HTTP 演示（需要后端 + Agent + MySQL）
BASE=http://127.0.0.1:8099 AGENT=http://127.0.0.1:8010 \
AILS_INTERNAL_TOKEN=... MYSQL_DB=ails_phase2_e2e \
bash scripts/phase6-demo.sh
```

不要提交真实密码、Token 或个人数据；测试只使用合成账号与合成文档。
