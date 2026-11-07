# Phase 1 + Phase 2 集成验证轮报告

- 执行日期：2026-11-07
- 分支：`main`（未提交 Git）
- 本轮性质：在真实 MySQL + Qdrant 环境下执行 Phase 1 / Phase 2 遗留集成测试，补齐 Phase 1 并发验证，归类 skip，更新审计报告。**不是开发轮。**
- 相关报告：[`PHASE1_MEMORY_FIX.md`](PHASE1_MEMORY_FIX.md)、[`PHASE2_RAG_RELIABILITY.md`](PHASE2_RAG_RELIABILITY.md)

## 0. 结论摘要

### 0.1 三种状态严格区分

| 状态 | 内容 | 数量 |
| --- | --- | --- |
| **已真实执行并通过** | Phase 1 Memory 并发 IT；DocumentService IT；Qdrant 集成；knowledge E2E | 4 类 / 15 条 |
| **已真实执行但失败** | DocumentIndexService IT 1 条；DocumentIndex E2E 1 条 | 2 条 |
| **未执行** | 未解锁的门控测试（Security / SSE / MQ / Phase2RealState / Broker 等） | 见 §5 |

失败项**保留失败**，没有改成 skip 或 `@Disabled`；本轮没有修改生产代码的业务逻辑。

### 0.2 关键结论

1. **Phase 1 并发已在真实 MySQL 验证。** 新增 `MemoryStateMachineConcurrencyIT`，4/4 PASS；
   并发断言直接读取数据库最终行，观察到真实 `updateById ... Updates: 0`。
2. **Phase 2 数据库级测试全部通过。** `DocumentServiceIntegrationTest` 7/7；
   `DocumentIndexServiceIntegrationTest` 15/15；`DocumentIndexE2EIntegrationTest` 1/1；
   `agent/tests/test_qdrant_integration.py` 3/3；`agent/tests/test_knowledge_e2e.py` 1/1。
   合计 27/27 PASS。
3. **发现并修复真实生产缺陷 D1。**
   `DocumentChunkMapper.deleteOtherVersions` / `selectOtherVersions` 的 `&lt;&gt;` 未放在 `<script>` 内，
   MyBatis 把 XML 实体原样发给 MySQL，导致旧版本 chunk 清理失败（首轮 DB chunk 2→4）。
   修复轮只改这两条 SQL 映射（`<script>` + `<![CDATA[ <> ]]>`），未改业务逻辑、schema、API；
   修复后两条失败转绿，27 条全量回归无失败。详见 §3.3 与 §6。
4. **skip 基线未变。** 完整后端套件 `309 / 0 / 0 / 73`；Agent 完整套件 `402 passed / 6 skipped / 1 xfailed`。
   skip 只是“未执行”，不计为通过；本轮解锁的条目已单独真实执行。
5. **未提交 Git，未进入 Phase 3。** D1 修复未改变任何业务语义。

---

## 1. 环境与前置状态

### 1.1 工作区保护

执行前运行 `git status`，工作区已有 Phase 2B 的大量未提交修改（生产代码、测试、迁移脚本、审计文档）。
本轮**没有执行** `git checkout -- .`、`git clean`、`git reset` 等会覆盖这些修改的操作；
只新建/修改测试文件与审计文档。

### 1.2 Phase 2B 迁移状态

- 迁移脚本：`backend/backend/src/main/resources/schema-migration-rag-reliability.sql`
- 检查结果：本地 3 个集成测试库尚未应用该脚本（缺少 `document.index_started_at`、
  `document_chunk.index_version` 等列）。
- 处理：**先报告差异，经用户批准后只对 3 个 IT 库执行迁移**：
  - `ails_document_it`
  - `ails_document_index_it`
  - `ails_document_e2e_it`
- 未迁移：开发库 `ails`（用户明确要求保持不动）；`ails_memory_it` 与
  `ails_knowledge_e2e_it` 由 `schema.sql` 新建，不需要该迁移。
- 迁移前备份：`%TEMP%\ails-rag-migration-backup\`（每库一份 dump）。
- 迁移脚本可重复执行（存储过程检查列/索引是否存在），执行后 `Document*` 集成测试上下文可正常启动。

### 1.3 服务可达性

| 依赖 | 地址 / 容器 | 状态 | 凭据 / 配置来源 |
| --- | --- | --- | --- |
| MySQL 8.0.37 | `127.0.0.1:3306` | 可达 | `backend/backend/src/main/resources/application-local.yml`（本地文件，未提交） |
| Qdrant | `127.0.0.1:6333` / `6334`，容器 `ails-qdrant` | 可达 | 无需鉴权；`docker ps` 可见 |
| RabbitMQ | `127.0.0.1:5672`，容器 `ails-rabbitmq` | 可达但本轮未使用 | `docker/docker-compose.yml` |
| FastAPI Agent | `127.0.0.1:8010` | 本轮启动 | `AILS_*` 环境变量 + `agent/.env` |
| Spring Boot | `127.0.0.1:8080` | 仅 knowledge E2E 启动 | `MYSQL_DB=ails_knowledge_e2e_it` 等环境变量 |

说明：

- `docker/docker-compose.yml` 只定义 RabbitMQ，不包含 MySQL / Qdrant；MySQL 是本机服务，
  Qdrant 用独立容器启动（本轮复用了已存在的 `ails-qdrant`）。
- 集成测试的 MySQL 凭据通过 `AILS_IT_MYSQL_PASSWORD` 传入，不使用 mock 绕过。
- 无环境时测试按 `@EnabledIfEnvironmentVariable` 跳过；本轮所有结果都来自设置门控后真实执行。

### 1.4 skip 基线

| 套件 | 轮前基线（PHASE2 报告 9.1 / 9.2） | 本轮完整重跑 | 差值 |
| --- | --- | --- | --- |
| 后端 `./mvnw.cmd test` | 309 run / 0 fail / 0 error / 73 skipped | 309 / 0 / 0 / 73 | 0 |
| Agent `python -m pytest -q` | 402 passed / 6 skipped / 1 xfailed | 402 / 6 / 1 | 0 |

本轮新增的 `MemoryStateMachineConcurrencyIT` 以 `IT` 结尾，不在 Maven Surefire 默认
include 模式（`*Test` / `Test*` / `*Tests` / `*TestCase`）内，因此不会进入 309 的计数；
它通过 `-Dtest=MemoryStateMachineConcurrencyIT` 显式执行，4/4 PASS，不属于 skip。

---

## 2. Phase 1：Memory 并发真实 MySQL 验证

### 2.1 测试类

`backend/backend/src/test/java/com/ails/backend/service/MemoryStateMachineConcurrencyIT.java`（新增）

- 门控：`AILS_MEMORY_IT=true`
- 数据库：`ails_memory_it`（独立库，不连接 `ails`）
- HTTP：standalone MockMvc + 真实 `MemoryController` + 真实 `GlobalExceptionHandler`
- 数据层：真实 `MemoryService`、真实 MyBatis Mapper、真实 MySQL 事务、真实 `@Version` 乐观锁
- 并发：两个线程 `CountDownLatch` 同时启动
- 断言：直接查询数据库最终 `status / content / version / updated_at`

### 2.2 执行命令

```powershell
cd backend/backend
$env:AILS_MEMORY_IT='true'; $env:AILS_IT_MYSQL_PASSWORD='...'
./mvnw.cmd -q -Dtest=MemoryStateMachineConcurrencyIT test
```

### 2.3 逐条结果

| # | 场景 | 方法 | 结果 | 关键数据库断言 |
| --- | --- | --- | --- | --- |
| 1 | 并发合法转换（T15） | `concurrentDoubleConfirmOnCandidateKeepsActive` | **PASS** | 响应 ∈ {200,409}；成功 1–2；`version == 成功次数`；最终 ACTIVE；内容为确认值；仅 1 次成功时必为 200+409 |
| 2 | 合法 vs 非法并发（T14） | `illegalCandidatePatchIsRejectedWhileLegalActivePatchSucceeds` | **PASS** | 响应恰为 200+409；最终 ACTIVE；内容为“合法更新”；版本仅 +1（非法未写库） |
| 3 | 重复 DELETE 并发（T13） | `concurrentDeleteOfArchivedMemoryIsIdempotent` | **PASS** | 两个 200；最终 ARCHIVED；内容、version、updated_at 均未变 |
| 4 | Agent UPDATE vs 用户确认（T21） | `agentUpdateRacingUserConfirmNeverOverwritesConfirmedContent` | **PASS** | 用户 200 → 最终 ACTIVE + 用户内容；用户 409 → Agent UPDATED 且最终 CANDIDATE + Agent 内容；不会出现“ACTIVE 保留 Agent 内容” |

汇总：**4 PASS / 0 FAIL / 0 SKIP**。

真实乐观锁日志证据：

```text
[pool-5-thread-1] MemoryMapper.updateById : <== Updates: 1
[pool-5-thread-2] MemoryMapper.updateById : <== Updates: 0
[pool-6-thread-2] MemoryMapper.updateById : <== Updates: 0
```

> 说明：真实日志中的 `Updates: 0` 来自 T15 的冲突线程；T21 本次交错为 Agent 先提交、
> 用户后提交（Agent `UPDATED`，最终版本 2、用户内容）。两种分支均由测试不变量覆盖。

### 2.4 改造前后对照

| 维度 | Phase 1B 初版 | 本轮 |
| --- | --- | --- |
| 测试类型 | Mockito 单测，`updateById` 手工返回 1/0 | 真实 MySQL + 真实事务 + 真实乐观锁 |
| 并发 | 单线程顺序调用模拟 | 双线程 `CountDownLatch` |
| 断言 | 捕获对象 / `verify(mapper)` | 数据库最终行快照 |
| 结果 | 22/22 单测 PASS | 22/22 单测 + 4/4 真实 MySQL IT PASS |

没有把单测改成顺序测试，也没有把数据库断言降级为响应码断言；单测与 IT 同时保留。

---

## 3. Phase 2：RAG 可靠性集成测试

### 3.1 已真实执行并通过

| 测试 | 方法数 | 结果 | 环境 |
| --- | ---: | --- | --- |
| `DocumentServiceIntegrationTest` | 7 | **7 PASS / 0 FAIL / 0 SKIP** | 真实 MySQL（`ails_document_it`） |
| `agent/tests/test_qdrant_integration.py` | 3 | **3 PASS / 0 FAIL / 0 SKIP** | 真实 Qdrant（随机 collection 前缀） |
| `agent/tests/test_knowledge_e2e.py` | 1 | **1 PASS / 0 FAIL / 0 SKIP** | 真实后端 8080 + DashScope `text-embedding-v4`（1024 维）+ Qdrant |

`DocumentServiceIntegrationTest` 逐条：

| 方法 | 结果 |
| --- | --- |
| `duplicateUploadReturnsExistingDocumentAndLeavesOneFile` | PASS |
| `readyIdsOnlyReturnFullyIndexedReadyDocumentsOfTheSpace` | PASS |
| `concurrentUploadsCreateExactlyOneActiveDocument` | PASS |
| `softDeletedDocumentIsHiddenAndItsHashCanBeReused` | PASS |
| `otherUsersSpaceCannotBeUploaded` | PASS |
| `listAndDetailRespectOwnership` | PASS |
| `sameContentInDifferentSpacesIsAllowed` | PASS |

Qdrant 集成逐条：

| 方法 | 结果 |
| --- | --- |
| `test_real_collection_index_reindex_and_document_scoped_delete` | PASS |
| `test_real_staged_version_switch_and_cleanup` | PASS |
| `test_real_knowledge_search_enforces_permissions_and_ranking` | PASS |

knowledge E2E 逐条：

| 方法 | 结果 |
| --- | --- |
| `test_upload_then_answer_with_traceable_sources_and_degrade_safely` | PASS |

覆盖路径：注册 → 创建 Space → 上传 Markdown → 真实解析/Embedding/Qdrant 索引 → 轮询 READY
→ 无 Concept 的直接事实题 → 来源标注 → 多 chunk 共同参与 → 不可回答问题不伪造来源
→ 空空间不检索 → Qdrant/Embedding 故障降级。

### 3.2 首轮已真实执行但失败（D1 修复后已转绿，见 §3.3）

> 以下保留首轮原始结果，未改成 skip/`@Disabled`；修复后的结果见 §3.3。

#### 3.2.1 `DocumentIndexServiceIntegrationTest`（15 条）

结果：**14 PASS / 1 FAIL / 0 SKIP**。

失败：

| 方法 | 失败原因 |
| --- | --- |
| `repeatedIndexIsIdempotentAndIncrementsVersionOnlyOnSuccess` | 第二次索引后 `chunkService.listByDocument(document.getId())` 返回 4 条，期望 2；旧版本 chunk 未删。根因 D1。 |

通过：`internalAuthFailureNeverBecomesReady`、`malformedVectorMappingFailsAndDoesNotWriteRefs`、
`restartScanRecoversAnInterruptedDocumentToAnExplicitTerminalState`、
`chunkPersistenceFailureRollsBackAllChunksAndFails`、
`embeddingOrQdrantFailureAfterChunkWriteNeverBecomesReady`、
`staleProcessingIsFailedWithTraceableErrorAndExcludedFromReady`、`indexErrorIsTruncatedTo1024Chars`、
`freshProcessingIsNotRecovered`、`parseFailureNeverBecomesReady`、
`recoveryRespectsTheTimeoutBoundary`、`recoveryIsIdempotentAndDoesNotRewriteTheError`、
`uploadKicksOffIndexingAndReturnsRealAsyncStatus`、
`successfulIndexWritesChunksVectorRefsAndReadyState`、`lateCompletionCannotOverwriteARecoveredDocument`。

测试设计修正（不削弱断言）：

- `staleProcessingIsFailedWithTraceableErrorAndExcludedFromReady`：Phase 2B 回收会自增
  `index_attempts` 作为 fencing token，断言由 `0` 改为 `1`；状态、错误信息、chunk 数断言不变。
- `recoveryIsIdempotentAndDoesNotRewriteTheError`：第二次扫描 `index_attempts` 仍为 `1`
  （只在第一次回收时自增）；幂等断言不变。

#### 3.2.2 `DocumentIndexE2EIntegrationTest`（1 条）

结果：**0 PASS / 1 FAIL / 0 SKIP**。

| 方法 | 失败原因 |
| --- | --- |
| `realAgentAndQdrantIndexAreConsistentAndIdempotent` | 首次索引一致性通过（DB chunk=2，Qdrant point=2）；重新索引后 `assertConsistency` 失败：`expected: 2 but was: 4`。失败点在 DB chunk 断言，Qdrant 计数尚未断言；按确定性 point ID 设计预期仍为 2。MySQL 旧版本 chunk 残留。根因 D1。 |

测试设计修正（只修测试启动条件，不改业务断言）：

- 原 `TestApplication` 未注册 `InternalIdentitySigner`，也未配置 `ails.internal.identity-secret`；
  `AgentDocumentClient` 每次调用都签发 `X-Internal-Identity`，缺密钥会 fail closed。
- 本轮在测试中注册 `InternalIdentitySigner`，并新增 `AILS_E2E_IDENTITY_SECRET`，
  测试进程与 Agent 使用同一密钥。
- 断言未改：chunk 数、Qdrant point 数、model、parser、attempts、version 全部保留。

### 3.3 D1 修复后复跑（2026-11-07）

§3.2 是首轮执行记录，失败保留未改成 skip/`@Disabled`。D1 修复后按原命令复跑：

| 测试 | 首轮 | 修复后 |
| --- | --- | --- |
| `DocumentIndexServiceIntegrationTest` | 14 PASS / 1 FAIL | **15 PASS / 0 FAIL / 0 SKIP** |
| `DocumentIndexE2EIntegrationTest` | 0 PASS / 1 FAIL | **1 PASS / 0 FAIL / 0 SKIP** |

Phase 2 全量回归（27 条）：

| 测试 | 结果 |
| --- | --- |
| `DocumentServiceIntegrationTest` | 7 PASS |
| `DocumentIndexServiceIntegrationTest` | 15 PASS |
| `DocumentIndexE2EIntegrationTest` | 1 PASS |
| `agent/tests/test_qdrant_integration.py` | 3 PASS |
| `agent/tests/test_knowledge_e2e.py` | 1 PASS |
| **合计** | **27 PASS / 0 FAIL / 0 ERROR / 0 SKIP** |

修复内容与前后代码对照见 §6。

---

## 4. skip 归类

### 4.1 后端完整套件（73 条）

| 测试类 | 条数 | 门控 / 原因 | 本轮是否解锁 | 结果 |
| --- | ---: | --- | --- | --- |
| `DocumentServiceIntegrationTest` | 7 | `AILS_DOCUMENT_IT=true`，需要 MySQL | **是** | 7 PASS |
| `DocumentIndexServiceIntegrationTest` | 15 | `AILS_DOCUMENT_INDEX_IT=true`，需要 MySQL | **是** | 15 PASS（D1 修复后） |
| `DocumentIndexE2EIntegrationTest` | 1 | `AILS_DOCUMENT_E2E_IT=true`，需要 Agent + Qdrant + MySQL | **是** | 1 PASS（D1 修复后） |
| `Phase2RealStateIntegrationTest` | 6 | `AILS_PHASE2_IT=true`，需要 MySQL | 否（不在本轮清单） | 未执行 |
| `SecurityContextIntegrationTest` | 19 | `AILS_SECURITY_IT=true`，需要完整 Spring Security/HTTP 环境 | 否（不在本轮清单） | 未执行 |
| `InternalIdentityHttpIntegrationTest` | 5 | `AILS_SECURITY_IT=true`，需要真实 HTTP + 内部身份环境 | 否（不在本轮清单） | 未执行 |
| `ChatSseIntegrationTest` | 8 | `AILS_CHAT_SSE_IT=true`，需要 SSE/集成环境 | 否（不在本轮清单） | 未执行 |
| `ChatSseOrderingProbeTest` | 1 | `AILS_CHAT_SSE_IT=true` + `AILS_P8_ORDER_PROBE=true` | 否（探针，不在本轮清单） | 未执行 |
| `Phase2MqProcessorIntegrationTest` | 5 | `AILS_PHASE2_IT=true`，需要 RabbitMQ/处理器环境 | 否（不在本轮清单） | 未执行 |
| `Phase2RabbitMqBrokerTest` | 5 | `AILS_PHASE2_MQ_IT=true`，需要真实 RabbitMQ Broker | 否（不在本轮清单） | 未执行 |
| `BackendApplicationTests` | 1 | 类上显式 `@Disabled`（需要本地 MySQL 与库表已初始化） | 否 | 未执行 |
| **合计** | **73** | — | — | — |

> skip ≠ 通过。上表“未执行”条目没有任何通过结论；只有“本轮解锁”行有真实执行结果。

### 4.2 Agent 完整套件（6 skipped）

| 测试 | 条数 | 门控 / 原因 | 本轮是否解锁 | 结果 |
| --- | ---: | --- | --- | --- |
| `test_knowledge_e2e.py` | 1 | `AILS_KNOWLEDGE_E2E=true` + 真实后端/Embedding/Qdrant | **是** | 1 PASS |
| `test_qdrant_integration.py` | 3 | `AILS_QDRANT_IT=true` + 真实 Qdrant | **是** | 3 PASS |
| `test_worker_broker.py` | 2 | 未设置 `AILS_TEST_RABBITMQ_URL` | 否（真实 Broker 测试，不在本轮清单） | 未执行 |

另有 1 条 `xfailed`（预期失败）属于既有测试语义，不计为 skip，也不计为本轮通过。

### 4.3 skip 基线对比

| 套件 | 轮前 | 轮后 | 说明 |
| --- | --- | --- | --- |
| 后端 | 309 / 0 / 0 / 73 | 309 / 0 / 0 / 73 | 新增 `*IT` 类不在 Surefire 默认模式，完整套件计数不变；显式执行 4/4 PASS |
| Agent | 402 / 6 / 1 | 402 / 6 / 1 | 门控测试单独执行时通过，但不改变无环境变量基线的 skip 数 |

---

## 5. 未执行清单

以下测试**没有执行**，不构成任何通过结论：

1. `Phase2RealStateIntegrationTest`（6）——需要 `AILS_PHASE2_IT=true`，不属于本轮指定清单。
2. `SecurityContextIntegrationTest`（19）、`InternalIdentityHttpIntegrationTest`（5）——
   需要 `AILS_SECURITY_IT=true` 与完整安全/HTTP 环境。
3. `ChatSseIntegrationTest`（8）、`ChatSseOrderingProbeTest`（1）——需要 `AILS_CHAT_SSE_IT=true`。
4. `Phase2MqProcessorIntegrationTest`（5）、`Phase2RabbitMqBrokerTest`（5）——
   需要 `AILS_PHASE2_IT=true` / `AILS_PHASE2_MQ_IT=true`；本轮 RabbitMQ 虽在运行，但未执行。
5. `BackendApplicationTests`（1）——类上显式 `@Disabled`。
6. `agent/tests/test_worker_broker.py`（2）——需要 `AILS_TEST_RABBITMQ_URL`；本轮未设置。

这些条目与 Phase 2 文档索引链路无直接冲突，但仍是验证缺口；后续如需发布，应按各自门控补跑。

---

## 6. 生产代码缺陷清单（D1 已修复，2026-11-07）

### D1：`DocumentChunkMapper` 的 `&lt;&gt;` 未在 `<script>` 内

- 文件：`backend/backend/src/main/java/com/ails/backend/mapper/DocumentChunkMapper.java`
- 方法：
  - `deleteOtherVersions`
  - `selectOtherVersions`
- 代码特征：两条注解都**没有** `<script>` 包裹，`&lt;&gt;` 作为普通文本发送给 MySQL。

- 现象：MyBatis 只在 `<script>` 内解析 XML 实体；修复前实际 SQL 为 `index_version &lt;&gt; ?`，
  MySQL 报：

  ```text
  java.sql.SQLSyntaxErrorException: You have an error in your SQL syntax;
  ... near '&gt; 1' at line 3
  ```

- 影响范围（修复前）：
  - `DocumentIndexService.cleanupOldVersions` → `DocumentChunkService.deleteOtherVersions` 删除旧版本 chunk 失败；
  - `DocumentIndexCleanupService.cleanup` → `DocumentChunkService.listOtherVersions` 扫描失败；
    异常被上层的“单文档失败不阻塞其余文档”捕获并记 warn，下一轮仍会失败；
  - 结果是 MySQL 旧版本 chunk 持续累积。`DocumentIndexE2EIntegrationTest` 已真实复现
    第二次索引后 DB chunk 为 4（期望 2）；Qdrant point 因确定性 ID 覆盖仍为 2。
  - 检索正确性不受影响：检索按活跃 `doc_version` 过滤；但存储与清理任务不可用。

**修复前：**

```java
@Delete("""
        DELETE FROM document_chunk
        WHERE document_id = #{documentId}
          AND index_version &lt;&gt; #{activeVersion}
        """)
int deleteOtherVersions(
        @Param("documentId") Long documentId,
        @Param("activeVersion") Integer activeVersion);

@Select("""
        SELECT DISTINCT index_version
        FROM document_chunk
        WHERE document_id = #{documentId}
          AND index_version &lt;&gt; #{activeVersion}
        """)
List<Integer> selectOtherVersions(
        @Param("documentId") Long documentId,
        @Param("activeVersion") Integer activeVersion);
```

**修复后：**

```java
@Delete("""
        <script>
        DELETE FROM document_chunk
        WHERE document_id = #{documentId}
          AND index_version <![CDATA[ <> ]]> #{activeVersion}
        </script>
        """)
int deleteOtherVersions(
        @Param("documentId") Long documentId,
        @Param("activeVersion") Integer activeVersion);

@Select("""
        <script>
        SELECT DISTINCT index_version
        FROM document_chunk
        WHERE document_id = #{documentId}
          AND index_version <![CDATA[ <> ]]> #{activeVersion}
        </script>
        """)
List<Integer> selectOtherVersions(
        @Param("documentId") Long documentId,
        @Param("activeVersion") Integer activeVersion);
```

- 修复范围：只改这两条 SQL 映射，未改业务逻辑、schema、API。
- 验证：
  - MyBatis 实际下发 SQL 为 `DELETE FROM document_chunk WHERE document_id = ? AND index_version <> ?`；
  - `DocumentIndexServiceIntegrationTest` 15/15 PASS；
  - `DocumentIndexE2EIntegrationTest` 1/1 PASS；
  - Phase 2 全量 27 条回归 PASS，详见 §3.3。
- 对照证据：`DocumentMapper` 的 `selectReadyDocuments` / `selectDocumentsWithStaleVersions`
  使用 `<script>` + `&lt;`，工作正常；本次采用同样的 `<script>` 包装方式。

---

## 7. 剩余风险

1. **D1 已修复并回归**：旧版本 chunk / Qdrant points 清理链路恢复，27 条集成测试全部通过；
   仍建议在长期运行环境观察 `DocumentIndexCleanupService` 的清理日志。
2. **T21 竞争的不确定性**：真实 MySQL IT 断言的是不变量，而非每次强制 Agent 进入 `SKIPPED`；
   若需要固定交错，需要在生产代码中加入测试钩子（本轮不做）。
3. **未解锁的门控测试**：Security / SSE / MQ / Phase2RealState / Broker 共 50 条后端测试
   与 2 条 Agent Broker 测试仍未执行，不能声称这些路径已验证。
4. **knowledge E2E 依赖外部 Embedding 服务**：本轮使用 DashScope `text-embedding-v4`，
   结果受网络与配额影响；测试通过不代表生产环境长期可用。
5. **数据库差异**：集成测试使用独立库与已迁移 schema；开发库 `ails` 未迁移，
   在开发库上直接运行新代码前仍需执行迁移脚本。
6. **完整套件与 IT 的计数口径**：`MemoryStateMachineConcurrencyIT` 不在 Surefire 默认模式内，
   仅看 `mvnw.cmd test` 的 309 会漏掉它；CI 需要显式加入该命令或改为 Failsafe。

---

## 8. 边界遵守

| 要求 | 执行情况 |
| --- | --- |
| 不修改生产代码业务逻辑 | 遵守；D1 修复轮只改两处 SQL 映射，未改业务逻辑/schema/API |
| 不削弱断言 | 遵守；单测保留，IT 断言数据库最终状态；仅修正过时的 fencing 预期与测试启动配置 |
| 不把未执行写成通过 | 遵守；§5 单独列出未执行 |
| 不把失败改成 `@Disabled` / skip | 遵守；首轮 2 条失败原样保留，D1 修复后按原命令复跑转绿 |
| 不自动提交 Git | 遵守；工作区未提交 |
| 不进入 Phase 3 | 遵守 |
| 不处理本轮无关审计问题 | 遵守 |

---

## 附录：复现命令

```powershell
# Phase 1 Memory 并发 IT
cd backend/backend
$env:AILS_MEMORY_IT='true'; $env:AILS_IT_MYSQL_PASSWORD='...'
./mvnw.cmd -q -Dtest=MemoryStateMachineConcurrencyIT test

# Phase 2 文档 IT
$env:AILS_DOCUMENT_IT='true'; $env:AILS_DOCUMENT_INDEX_IT='true'; $env:AILS_IT_MYSQL_PASSWORD='...'
./mvnw.cmd -q '-Dtest=DocumentServiceIntegrationTest,DocumentIndexServiceIntegrationTest' test

# Phase 2 E2E（需先启动 Agent 8010 与 Qdrant 6333）
$env:AILS_DOCUMENT_E2E_IT='true'
$env:AILS_E2E_AGENT_BASE_URL='http://127.0.0.1:8010'
$env:AILS_E2E_INTERNAL_TOKEN='...'
$env:AILS_E2E_IDENTITY_SECRET='...'
$env:AILS_E2E_QDRANT_URL='http://127.0.0.1:6333'
$env:AILS_E2E_QDRANT_COLLECTION='<prefix>_hash-v1_64'
$env:AILS_IT_MYSQL_PASSWORD='...'
./mvnw.cmd -q -Dtest=DocumentIndexE2EIntegrationTest test

# Agent Qdrant 集成
cd ../agent
$env:AILS_QDRANT_IT='true'; $env:AILS_QDRANT_URL='http://127.0.0.1:6333'
python -m pytest tests/test_qdrant_integration.py -v

# Agent knowledge E2E（需先启动后端 8080 与真实 Embedding 的 Agent 8010）
$env:AILS_KNOWLEDGE_E2E='true'
$env:AILS_E2E_BACKEND_URL='http://127.0.0.1:8080'
$env:AILS_QDRANT_URL='http://127.0.0.1:6333'
$env:AILS_QDRANT_COLLECTION_PREFIX='ails_knowledge_e2e_verify'
python -m pytest tests/test_knowledge_e2e.py -v

# 完整基线
cd ../backend/backend; ./mvnw.cmd test
cd ../agent; python -m pytest -q
```
