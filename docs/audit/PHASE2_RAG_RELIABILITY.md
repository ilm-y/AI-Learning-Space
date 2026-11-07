# 阶段 2B：RAG 文档索引可靠性落地报告

- 项目：`D:\learningMatiarials\AI-Learning-Space`
- 分支 / 基线提交：`main` / `81a983e`
- 完成日期：2026-11-07
- 本轮性质：**实现 + 测试 + 文档**。未提交 Git，未进入下一阶段。
- 设计依据：[`docs/design/RAG_MINIMAL_RELIABILITY.md`](../design/RAG_MINIMAL_RELIABILITY.md)
- 核心保证：**新索引构建失败，不影响旧索引继续提供检索。**

> 事实边界：本文件的“通过”只代表对应命令在本机真实执行并返回成功。
> 需要 MySQL / Qdrant / 真实 Agent 的测试若未执行，会明确标注为“未执行”，
> 不把跳过的测试写成通过。

---

## 0. 结论摘要

1. 索引写入改为 **按 `index_version` 分阶段构建**：新 chunk 与新 Qdrant points 都是非活跃版本；
   只有一条 MySQL 条件 UPDATE 成功后才切换活跃版本。切换前任何失败都只回滚本次 attempt，
   旧活跃版本原样服务。
2. 没有跨存储事务。**MySQL 是事实源，Qdrant 是派生存储**；提交点只有
   `document.index_version = attempt` 这一条条件 UPDATE。
3. 删除采用 **DELETING 先退出检索、清理异步可重试、DELETED 最后落库**。
   删除失败不会让文档重新可检索。
4. Embedding 兼容性由 `provider + model + dimension + endpoint` 指纹约束：
   索引时记录，检索白名单按指纹（旧数据按 model/provider/dimension）过滤，
   Agent 端再按 payload 指纹二次拒绝。
5. 本机真实执行结果：
   - 后端完整套件：`./mvnw.cmd -q test` → **309 tests, 0 failures, 0 errors, 73 skipped**，退出码 0。
   - Agent 完整套件：`python -m pytest -q` → **402 passed, 6 skipped, 1 xfailed**。
   - 集成验证轮（2026-11-07）：MySQL / Qdrant / 真实 Agent 已就绪，Phase 2 指定 27 条测试全部真实执行通过：
     `DocumentServiceIntegrationTest` 7/7、`DocumentIndexServiceIntegrationTest` 15/15、
     `DocumentIndexE2EIntegrationTest` 1/1、Qdrant 集成 3/3、knowledge E2E 1/1。
     首轮暴露的生产缺陷 D1（`DocumentChunkMapper` 的 `&lt;&gt;` 未在 `<script>` 内）已按最小改动修复，
     修复前后代码对照与回归结果见 9.4.6。

---

## 1. 数据模型变更

### 1.1 `document`

| 列 | 类型 | 用途 |
| --- | --- | --- |
| `embedding_provider` | `VARCHAR(64) NULL` | Embedding provider 名 |
| `embedding_dimension` | `INT NULL` | 向量维度 |
| `embedding_fingerprint` | `VARCHAR(128) NULL` | 配置指纹，`v1:<sha256>` |
| `vector_collection` | `VARCHAR(255) NULL` | 实际写入的 Qdrant collection |
| `index_started_at` | `DATETIME NULL` | 构建租约；非空表示有 writer |
| `delete_attempts` | `INT NOT NULL DEFAULT 0` | 删除清理失败次数 |
| `delete_error` | `VARCHAR(1024) NULL` | 最近一次删除失败摘要 |

新增索引：`idx_doc_status_started (status, index_started_at)`。

### 1.2 `document_chunk`

| 变更 | 内容 |
| --- | --- |
| 新增列 | `index_version INT NOT NULL DEFAULT 0` |
| 唯一键替换 | `uk_chunk_doc_idx(document_id, chunk_index)` → `uk_chunk_doc_ver_idx(document_id, index_version, chunk_index)` |

### 1.3 迁移

- 新库：直接由 [`schema.sql`](../../backend/backend/src/main/resources/schema.sql) 创建。
- 已有库：手工执行 [`schema-migration-rag-reliability.sql`](../../backend/backend/src/main/resources/schema-migration-rag-reliability.sql)。
  - 幂等：每步先查 `information_schema`，已存在则跳过。
  - 回填：现有 chunk 归入 `document.index_version`（默认 0）。
  - 不重写 Qdrant：迁移前的 point 仍使用旧 ID 公式与旧 payload。
  - 不删除、不改写业务数据；只加列、加索引、替换唯一键。

---

## 2. 索引生命周期

### 2.1 状态

`UPLOADING → PROCESSING → READY`，失败 `FAILED`；删除 `DELETING → DELETED`。

### 2.2 构建令牌与 fencing

- `document.index_attempts` 每次抢占租约时 `+1`，同时作为本次构建的 `index_version` 与 fencing token。
- `acquireLease` 是唯一并发边界：条件 UPDATE 同时检查
  1. 行未软删；
  2. 状态属于 `UPLOADING / FAILED / READY`；
  3. `index_started_at IS NULL` 或已超过 `reindex-cooldown-ms`。
- 命中 1 行才获得令牌；否则返回 `null`，本次提交静默跳过。
- `READY` 重建使用 `status = CASE WHEN status = 'READY' THEN 'READY' ELSE 'PROCESSING' END`：
  构建期间保持 READY，旧版本继续服务。

### 2.3 写入顺序（关键差异）

```text
抢占租约（index_attempts + 1）
  → 读取原文 + Agent 解析
  → 新 chunk 写入 index_version = attempt（不碰旧版本）
  → Agent Embedding + 写 Qdrant 新版本 points
      Agent 返回前用精确 count 校验该版本 point 数
  → 校验响应的 collection / model / provider / dimension / fingerprint / 向量数量与 ID
  → 写回本版本 chunk.vector_ref
  → 条件 UPDATE：index_version = attempt 且 status = READY（唯一生效点）
  → 成功后清理旧版本（尽力而为，可定时重试）
```

失败路径：只回滚本次 `attempt` 的 chunk 与 points；旧活跃版本不动。
`fail`/`complete` 都带 `index_attempts = attempt` 条件，过期 worker 无法改状态。

### 2.4 Qdrant 版本隔离

- point ID：
  - `index_version <= 0`：`ails-document:{docId}:{chunkIndex}`（迁移前公式，保留兼容）；
  - `index_version >= 1`：`ails-document:{docId}:v{version}:{chunkIndex}`。
- payload 新增：`index_version`、`doc_version = "{docId}:{version}"`、`embedding_fingerprint`。
- 写入新版本前**不删除旧版本**；只清理同版本残留，再精确校验写入数量。
- 检索过滤：`user_id AND space_id AND (doc_version IN 活跃版本 OR document_id IN legacy 白名单)`；
  空白名单直接返回空，绝不发起无文档过滤的查询。

---

## 3. 八类故障恢复矩阵

| # | 故障 | 活跃索引 / 文档状态 | 恢复方式 | 代码位置 |
| --- | --- | --- | --- | --- |
| 1 | Embedding 调用失败 | 旧版本仍活跃；READY 保持 READY，首次索引转 FAILED | 用户重试 / 重建；失败 attempt 的 chunk 与 points 被回滚 | `DocumentIndexService.runIndex` |
| 2 | Qdrant 写入失败 | 同上 | 同上；Agent 返回 502，后端不切换 | `runIndex` + `qdrant_store.index_document` |
| 3 | Qdrant 部分写入 | 旧版本仍活跃，新版本绝不生效 | Agent 写入后精确 `count` 校验，数量不符直接失败；后端再校验响应向量数与 ID | `qdrant_store.index_document`、`validateIndexResponse` |
| 4a | Qdrant 成功但 `chunk.vector_ref` 更新失败 | 旧版本仍活跃 | 回滚新版本；若向量清理失败，保留 staged chunk 供定时清理重试 | `runIndex`、`rollbackStagedVersion` |
| 4b | Qdrant 成功但 MySQL 切换失败 / 被 fence | 旧版本仍活跃 | `complete` 条件更新 0 行则抛错并回滚；`fail` 同样带 attempt 条件，旧 worker 无法覆盖 | `complete`、`fail` |
| 5 | MySQL 切换成功但旧索引清理失败 | **新版本已活跃** | 只记日志；旧 points 因 `doc_version` 过滤不参与检索；`DocumentIndexCleanupService` 定时重试 | `cleanupOldVersions`、`DocumentIndexCleanupService` |
| 6 | worker 重复执行 | 不变 | `isLeaseOwner` 校验 attempt + 租约；`complete/fail` 条件更新 fencing | `runIndex`、`complete`、`fail` |
| 7 | 同一文档并发重建 | 只有一个 winner | 租约条件 UPDATE + 冷却窗口；落败者 0 行直接返回 | `acquireLease` |
| 8 | 重建中途崩溃 | 崩溃时文档可能停在 PROCESSING（首次）或 READY（重建） | 回收任务：`PROCESSING` 超时 → FAILED；`READY` 超时 → **保持 READY**、清租约并 fence；staged 版本由清理任务删除 | `DocumentIndexRecoveryService`、`DocumentIndexCleanupService` |

补充说明：

- 回收任务的每个 UPDATE 都自增 `index_attempts`，因此多实例/重复扫描只会成功一次。
- `UPLOADING` 超时：未超过 `max-index-attempts` 则重新提交，否则 FAILED。
- 旧版本清理顺序改为 **先清 Agent/Qdrant、再删 MySQL chunk**：
  Agent 失败时 chunk 记录仍在，下一轮扫描还能发现并重试；反过来会留下无法定位的孤儿 points。

---

## 4. 删除流程

### 4.1 接口

`DELETE /api/documents/{id}`

1. 用 `selectAnyById` 读取（绕过 `@TableLogic`），区分“已删除”和“不存在”。
2. 校验文档所有者与 Space 归属；他人资源统一 404。
3. 已是 `DELETED` / 有 `deleted_at`：幂等成功。
4. 已是 `DELETING`：再排一次清理后返回。
5. 其它状态：一条条件 UPDATE 切到 `DELETING`，同时 `index_attempts + 1`（fence 运行中的构建）、
   清租约、清 `delete_error`；然后异步清理。

### 4.2 检索侧

`selectReadyDocuments` 只接受 `status = 'READY'` 且 `deleted_at IS NULL`：
请求删除的同步提交完成后，文档立即退出检索白名单。

### 4.3 清理

`DELETING → Agent 删除该文档全部版本 → MySQL chunk 全删 → 文件删除 → 条件 UPDATE 置 DELETED`。

- Agent 删除对 collection 不存在 / points 已删除幂等。
- 任一步抛错：`delete_attempts + 1`、写 `delete_error`，**保持 DELETING**，不写 DELETED。
- 定时任务每 `delete-retry-interval-ms` 扫描 `delete_attempts < delete-max-attempts` 的 DELETING 行。
- 达到最大次数后仍保持 DELETING（不参与检索），等待人工处理。
- 并发重复清理由 `markDeleted` 的 `status = 'DELETING' AND deleted_at IS NULL` 条件保证幂等。

---

## 5. Embedding 兼容性

### 5.1 指纹

`v1:<sha256(provider|model|dimension|endpoint_identity)>`

- `provider`：`hash` / `openai-compatible`。
- `endpoint_identity`：规范化 `scheme://host/path`，忽略 query、用户名密码、末尾斜杠；
  Hash provider 为空。
- **不包含 API key。**

### 5.2 索引时记录

- Agent 在索引响应中返回 `embeddingDimension` 与 `embeddingFingerprint`。
- 后端校验长度与维度后写入 `document.embedding_provider/dimension/fingerprint/vector_collection`。

### 5.3 检索时校验

- Agent 从 `ready-ids` 读取当前生效配置的白名单。
- 后端 SQL：
  - 有 fingerprint 且文档也有 fingerprint：必须完全相等；
  - 迁移前 fingerprint 为 NULL：退回 `model` 匹配，并额外校验
    `embedding_provider` / `embedding_dimension`（文档列有值时必须相等）。
- Agent 收到 Qdrant 候选后：
  - 只接受白名单文档的活跃 `doc_version`；
  - payload 带 fingerprint 且与当前不一致 → 丢弃；
  - 白名单外的 `document_id` → 丢弃。
- collection 名仍由 `model + dimension` 决定，不同模型/维度天然隔离；
  fingerprint 用于拦截“同名模型、不同网关”的混用。

---

## 6. 兼容迁移前数据

| 数据 | 兼容策略 |
| --- | --- |
| 旧 chunk | 迁移脚本归入 `document.index_version`（0） |
| 旧 point ID | `index_version = 0` 保留旧公式 |
| 旧 point payload | 无 `index_version` / `doc_version` 时，检索退回 `document_id IN 白名单` |
| 旧 document 列 | `embedding_provider/dimension/fingerprint` 为 NULL 时按 model 兼容 |
| `ready-ids` 旧响应 | Agent 同时接受 `[101, 202]` 与 `[{id, indexVersion}]`；旧整数视为版本 0 |

---

## 7. 实际修改清单

### 7.1 后端主代码

修改：

- `backend/backend/src/main/java/com/ails/backend/config/DocumentIndexProperties.java`
- `backend/backend/src/main/java/com/ails/backend/controller/DocumentController.java`
  - `ready-ids` 返回 `{id, indexVersion}`，接收 4 个 Embedding 身份参数；
  - 新增 `POST /api/documents/{id}/reindex`；
  - 新增 `DELETE /api/documents/{id}`。
- `backend/backend/src/main/java/com/ails/backend/dto/document/DocumentResponse.java`
- `backend/backend/src/main/java/com/ails/backend/dto/document/agent/AgentIndexRequest.java`
- `backend/backend/src/main/java/com/ails/backend/dto/document/agent/AgentIndexResponse.java`
- `backend/backend/src/main/java/com/ails/backend/entity/Document.java`
- `backend/backend/src/main/java/com/ails/backend/entity/DocumentChunk.java`
- `backend/backend/src/main/java/com/ails/backend/mapper/DocumentChunkMapper.java`
- `backend/backend/src/main/java/com/ails/backend/mapper/DocumentMapper.java`
- `backend/backend/src/main/java/com/ails/backend/service/AgentDocumentClient.java`
- `backend/backend/src/main/java/com/ails/backend/service/DocumentChunkService.java`
- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexRecoveryService.java`
- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexService.java`
- `backend/backend/src/main/java/com/ails/backend/service/DocumentService.java`
- `backend/backend/src/main/resources/application.yml`
- `backend/backend/src/main/resources/schema.sql`

新增：

- `backend/backend/src/main/java/com/ails/backend/dto/document/ReadyDocument.java`
- `backend/backend/src/main/java/com/ails/backend/dto/document/agent/AgentDeleteRequest.java`
- `backend/backend/src/main/java/com/ails/backend/service/DocumentDeletionService.java`
- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexCleanupService.java`
- `backend/backend/src/main/resources/schema-migration-rag-reliability.sql`

### 7.2 Agent

修改：

- `agent/app/documents/routes.py`：索引传递 `indexVersion` / fingerprint；新增 `/agent/documents/delete`。
- `agent/app/documents/schemas.py`：`indexVersion`；`DeleteDocumentRequest`。
- `agent/app/embedding/base.py`：指纹函数与 `fingerprint` 属性。
- `agent/app/embedding/openai_compatible.py`：endpoint 身份规范化。
- `agent/app/retriever/http_backend.py`：发送 Embedding 身份参数；解析两种 `ready-ids` 响应。
- `agent/app/retriever/knowledge.py`：`AllowedDocument`；按版本与指纹校验候选。
- `agent/app/vectorstore/qdrant_store.py`：版本化 point ID / payload；精确写入校验；
  `delete_versions_except`；版本化检索过滤。

### 7.3 测试

修改：

- `backend/.../controller/DocumentControllerTest.java`
- `backend/.../mapper/DocumentMapperSqlTest.java`
- `backend/.../service/DocumentIndexServiceIntegrationTest.java`
- `backend/.../service/DocumentIndexServiceTest.java`
- `backend/.../service/DocumentServiceIntegrationTest.java`
- `backend/.../service/DocumentServiceTest.java`
- `agent/tests/test_document_api.py`
- `agent/tests/test_embedding.py`
- `agent/tests/test_knowledge_retrieval.py`
- `agent/tests/test_qdrant_integration.py`
- `agent/tests/test_qdrant_store.py`

新增：

- `backend/.../service/DocumentDeletionServiceTest.java`
- `backend/.../service/DocumentIndexCleanupServiceTest.java`
- `backend/.../service/DocumentIndexRecoveryServiceTest.java`

---

## 8. 新增配置（`ails.document.index.*`）

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `stale-processing-timeout-ms` | 900000 | PROCESSING / 重建租约超时 |
| `recovery-interval-ms` | 300000 | 回收扫描间隔 |
| `recovery-initial-delay-ms` | 60000 | 首次扫描延迟 |
| `recovery-batch-size` | 100 | 单次扫描上限 |
| `upload-stale-timeout-ms` | 120000 | UPLOADING 滞留阈值 |
| `max-index-attempts` | 3 | UPLOADING 自动重试上限 |
| `reindex-cooldown-ms` | 30000 | 同文档构建冷却 |
| `delete-retry-interval-ms` | 60000 | DELETING 重试间隔 |
| `delete-retry-initial-delay-ms` | 30000 | 删除首次扫描延迟 |
| `delete-batch-size` | 50 | 删除扫描上限 |
| `delete-max-attempts` | 20 | 删除自动重试上限 |
| `cleanup-interval-ms` | 300000 | 旧版本清理间隔 |
| `cleanup-initial-delay-ms` | 120000 | 旧版本清理首次延迟 |
| `cleanup-batch-size` | 50 | 旧版本清理扫描上限 |

全部可用环境变量覆盖，见 `application.yml`。

---

## 9. 测试证据

### 9.1 后端单元 / 本地测试

执行：

```powershell
cd backend/backend
./mvnw.cmd -q test
# EXIT=0
```

真实汇总（surefire 报告）：

```text
total=309  failures=0  errors=0  skipped=73
```

文档相关类：

| 测试类 | 结果 |
| --- | --- |
| `DocumentControllerTest` | 9 / 0 / 0 / 0 |
| `DocumentSchemaConsistencyTest` | 6 / 0 / 0 / 0 |
| `DocumentMapperSqlTest` | 3 / 0 / 0 / 0 |
| `DocumentDeletionServiceTest` | 12 / 0 / 0 / 0 |
| `DocumentIndexCleanupServiceTest` | 6 / 0 / 0 / 0 |
| `DocumentIndexRecoveryServiceTest` | 4 / 0 / 0 / 0 |
| `DocumentIndexServiceTest` | 11 / 0 / 0 / 0 |
| `DocumentServiceTest` | 16 / 0 / 0 / 0 |
| `DocumentStorageServiceTest` | 16 / 0 / 0 / 0 |
| `DocumentIndexServiceIntegrationTest` | 15 skipped（gated；带门控补跑见 9.4.2） |
| `DocumentServiceIntegrationTest` | 7 skipped（gated；带门控补跑见 9.4.1） |
| `DocumentIndexE2EIntegrationTest` | 1 skipped（gated；带门控补跑见 9.4.3） |

上述“skipped”只是无环境变量时的完整套件基线；**不等于未验证**。带门控的真实执行结果以 9.4 为准。

### 9.2 Agent

执行：

```bash
cd agent
python -m pytest -q
# 402 passed, 6 skipped, 1 xfailed
```

跳过的 6 条：

- `test_knowledge_e2e.py`：需要 `AILS_KNOWLEDGE_E2E=true`；
- `test_qdrant_integration.py` ×3：需要 `AILS_QDRANT_IT=true` 与真实 Qdrant；
- `test_worker_broker.py` ×2：需要 `AILS_TEST_RABBITMQ_URL`（与本轮无关）。

### 9.3 需求中的九类测试对照

| 需求 | 测试（真实执行并通过） | 说明 |
| --- | --- | --- |
| 1. 新构建失败，旧索引仍可用 | `DocumentIndexServiceTest.indexFailureRollsBackOnlyNewVersionAndKeepsOldIndex`；`vectorRollbackFailureKeepsStagedChunkForCleanupRetry` | 断言只删 attempt 版本、READY 保持 READY |
| 2. 部分写入不能激活 | `DocumentIndexServiceTest.partialVectorResponseFailsWithoutSwitchingActiveVersion`；`test_qdrant_store.partial_write_raises_instead_of_reporting_success` | Agent 精确 count + 后端响应校验 |
| 3. 激活后不混入旧索引 | `test_knowledge_retrieval.payload_from_inactive_version_is_rejected`；`payload_with_matching_active_version_is_accepted`；`test_qdrant_store.versioned_search_filters_by_active_doc_version` | 活跃 `doc_version` 之外的候选被丢弃 |
| 4. 旧索引清理失败不影响检索 | `DocumentIndexServiceTest.cleanupFailureAfterSwitchDoesNotFlipReadyBack`；`DocumentIndexCleanupServiceTest.*`；`DocumentIndexE2EIntegrationTest`（真实执行） | 清理失败不写 fail：E2E 中清理真实报错后文档仍为 READY、version 2；D1 修复后旧版本清理链路恢复，见 9.4.6 |
| 5. 重复重建不产生错误状态 | `DocumentIndexServiceTest.submitIsNoOpWhenLeaseCannotBeAcquired`、`runIndexAbandonsWhenLeaseOwnerChanged`、`completeFailsWhenAttemptWasFenced` | 租约 + attempt 条件 |
| 6. 删除后不可检索 | `DocumentMapperSqlTest.sqlRequiresReadyUndeletedAndFullyIndexedActiveVersion`；`DocumentDeletionServiceTest.requestDeleteSwitchesToDeletingAndFencesRunningIndex`、`cleanupDeletesVectorsChunksFileThenMarksDeleted`；`DocumentServiceIntegrationTest.readyIdsOnlyReturnFullyIndexedReadyDocumentsOfTheSpace` | SQL 级 + ready-ids 真实 MySQL 已验证；删除流程本身的 DB 级集成仍未执行 |
| 7. Qdrant 清理失败可重试 | `DocumentDeletionServiceTest.vectorCleanupFailureKeepsDeletingAndRecordsError`、`scheduledRetryEnqueuesEveryPendingDocument` | 保持 DELETING，不写 DELETED |
| 8. 不兼容 Embedding 不能静默参与 | `test_embedding.test_embedding_fingerprint_changes_with_incompatible_configuration`、`test_embedding.test_openai_fingerprint_ignores_credentials_but_not_gateway`；`DocumentMapperSqlTest.embeddingIdentityFilterIsAppliedOnlyWhenModelProvided`、`legacyDocumentsWithoutFingerprintStillMatchByModelProviderAndDimension`；`test_knowledge_retrieval.payload_with_incompatible_embedding_fingerprint_is_rejected` | SQL 过滤 + payload 二次校验 |
| 9. 不跨用户召回 | `test_qdrant_store.search_applies_user_space_and_document_allowlist_filters`；`test_knowledge_retrieval.payload_outside_allowlist_is_rejected` | Qdrant filter 必须含 user_id / space_id；白名单外文档丢弃 |

### 9.4 集成验证轮补跑结果（2026-11-07）

本节替代原“未执行的测试”章节。所有条目均为**真实执行**。首轮失败项在 D1 修复后复跑转绿，
未改成 skip/`@Disabled`；末段给出修复前后代码对照与 27 条全量回归结果。

环境：

- MySQL 8.0.37，`127.0.0.1:3306`，用户 `root`；
- Qdrant，Docker 容器 `ails-qdrant`，`127.0.0.1:6333`；
- FastAPI Agent，`127.0.0.1:8010`；
- Spring Boot，`127.0.0.1:8080`（仅 knowledge E2E 使用，独立库 `ails_knowledge_e2e_it`）；
- Phase 2B 迁移脚本经批准只应用到 `ails_document_it` / `ails_document_index_it` / `ails_document_e2e_it`，开发库 `ails` 未动；备份见 `INTEGRATION_VERIFICATION.md`。

#### 9.4.1 `DocumentServiceIntegrationTest`（7 条，真实 MySQL）

命令：

```powershell
$env:AILS_DOCUMENT_IT='true'; $env:AILS_IT_MYSQL_PASSWORD='...'
./mvnw.cmd -q -Dtest=DocumentServiceIntegrationTest test
```

结果：**7 PASS / 0 FAIL / 0 SKIP**。

| 方法 | 结果 |
| --- | --- |
| `duplicateUploadReturnsExistingDocumentAndLeavesOneFile` | PASS |
| `readyIdsOnlyReturnFullyIndexedReadyDocumentsOfTheSpace` | PASS |
| `concurrentUploadsCreateExactlyOneActiveDocument` | PASS |
| `softDeletedDocumentIsHiddenAndItsHashCanBeReused` | PASS |
| `otherUsersSpaceCannotBeUploaded` | PASS |
| `listAndDetailRespectOwnership` | PASS |
| `sameContentInDifferentSpacesIsAllowed` | PASS |

#### 9.4.2 `DocumentIndexServiceIntegrationTest`（15 条，真实 MySQL + Fake Agent）

命令：

```powershell
$env:AILS_DOCUMENT_INDEX_IT='true'; $env:AILS_IT_MYSQL_PASSWORD='...'
./mvnw.cmd -q -Dtest=DocumentIndexServiceIntegrationTest test
```

结果（D1 修复后复跑）：**15 PASS / 0 FAIL / 0 SKIP**。

| 方法 | 结果 | 说明 |
| --- | --- | --- |
| `repeatedIndexIsIdempotentAndIncrementsVersionOnlyOnSuccess` | PASS | 首轮 FAIL：第二次索引后 `chunkService.listByDocument` 返回 4 条，期望 2；D1 修复后旧版本 chunk 被清理，恢复为 2 条。见 9.4.6。 |
| `internalAuthFailureNeverBecomesReady` | PASS | |
| `malformedVectorMappingFailsAndDoesNotWriteRefs` | PASS | |
| `restartScanRecoversAnInterruptedDocumentToAnExplicitTerminalState` | PASS | |
| `chunkPersistenceFailureRollsBackAllChunksAndFails` | PASS | |
| `embeddingOrQdrantFailureAfterChunkWriteNeverBecomesReady` | PASS | |
| `staleProcessingIsFailedWithTraceableErrorAndExcludedFromReady` | PASS | 测试设计修正：Phase 2B 回收会自增 `index_attempts` 作为 fencing token，断言由 0 改为 1；未削弱状态/错误断言。 |
| `indexErrorIsTruncatedTo1024Chars` | PASS | |
| `freshProcessingIsNotRecovered` | PASS | |
| `parseFailureNeverBecomesReady` | PASS | |
| `recoveryRespectsTheTimeoutBoundary` | PASS | |
| `recoveryIsIdempotentAndDoesNotRewriteTheError` | PASS | 测试设计修正：第二次扫描 `index_attempts` 仍为 1（只在第一次回收时自增）；未削弱幂等断言。 |
| `uploadKicksOffIndexingAndReturnsRealAsyncStatus` | PASS | |
| `successfulIndexWritesChunksVectorRefsAndReadyState` | PASS | |
| `lateCompletionCannotOverwriteARecoveredDocument` | PASS | |

#### 9.4.3 `DocumentIndexE2EIntegrationTest`（1 条，真实 Agent + Qdrant + MySQL）

命令要点：

```powershell
$env:AILS_DOCUMENT_E2E_IT='true'
$env:AILS_E2E_AGENT_BASE_URL='http://127.0.0.1:8010'
$env:AILS_E2E_INTERNAL_TOKEN='...'
$env:AILS_E2E_IDENTITY_SECRET='...'
$env:AILS_E2E_QDRANT_URL='http://127.0.0.1:6333'
$env:AILS_E2E_QDRANT_COLLECTION='..._hash-v1_64'
$env:AILS_IT_MYSQL_PASSWORD='...'
./mvnw.cmd -q -Dtest=DocumentIndexE2EIntegrationTest test
```

结果（D1 修复后复跑）：**1 PASS / 0 FAIL / 0 SKIP**。

| 方法 | 结果 | 说明 |
| --- | --- | --- |
| `realAgentAndQdrantIndexAreConsistentAndIdempotent` | PASS | 首轮 FAIL：重新索引后 `assertConsistency` 在 `dbChunks == 2` 处失败（`expected: 2 but was: 4`），MySQL 旧版本 chunk 残留。D1 修复后复跑，首次与重建后 DB chunk、Qdrant point 均为 2，完整一致性断言通过。 |

测试设计修正（不改变断言）：原 `TestApplication` 未注册 `InternalIdentitySigner`，也未提供 `ails.internal.identity-secret`，而 `AgentDocumentClient` 对每次调用都签发身份声明，缺密钥会 fail closed。本轮在测试中补注册 `InternalIdentitySigner` 并新增 `AILS_E2E_IDENTITY_SECRET` 环境变量（测试进程与 Agent 使用同一密钥）。该修正只让测试具备可启动条件，未降低任何业务断言。

#### 9.4.4 Agent 真实 Qdrant 集成（3 条）

```powershell
$env:AILS_QDRANT_IT='true'; $env:AILS_QDRANT_URL='http://127.0.0.1:6333'
python -m pytest tests/test_qdrant_integration.py -v
```

结果：**3 PASS / 0 FAIL / 0 SKIP**。

| 方法 | 结果 |
| --- | --- |
| `test_real_collection_index_reindex_and_document_scoped_delete` | PASS |
| `test_real_staged_version_switch_and_cleanup` | PASS |
| `test_real_knowledge_search_enforces_permissions_and_ranking` | PASS |

#### 9.4.5 Agent knowledge E2E（1 条，真实后端 + 真实 Embedding + Qdrant）

```powershell
$env:AILS_KNOWLEDGE_E2E='true'
$env:AILS_E2E_BACKEND_URL='http://127.0.0.1:8080'
$env:AILS_QDRANT_URL='http://127.0.0.1:6333'
$env:AILS_QDRANT_COLLECTION_PREFIX='ails_knowledge_e2e_verify'
python -m pytest tests/test_knowledge_e2e.py -v
```

真实 Embedding Provider：DashScope `text-embedding-v4`，维度 1024（非 Hash）。
结果：**1 PASS / 0 FAIL / 0 SKIP**。

| 方法 | 结果 |
| --- | --- |
| `test_upload_then_answer_with_traceable_sources_and_degrade_safely` | PASS |

覆盖：上传 → 真实解析/Embedding/Qdrant 索引 → READY → 无 Concept 的直接事实题检索 → 来源标注 → 多 chunk 共同参与 → 不可回答问题不伪造来源 → 空空间不检索 → Qdrant/Embedding 故障降级。

#### 9.4.6 生产缺陷 D1（已修复，2026-11-07）

**D1：`DocumentChunkMapper` 的 `&lt;&gt;` 在非 `<script>` 注解中被当作字面量发给 MySQL。**

- 位置：`backend/backend/src/main/java/com/ails/backend/mapper/DocumentChunkMapper.java`
  - `deleteOtherVersions`
  - `selectOtherVersions`
- 现象：MyBatis 只在 `<script>` 内解析 XML 实体；两条注解没有 `<script>`，SQL 实际发送 `&lt;&gt;`，MySQL 报
  `SQLSyntaxErrorException: ... near '&gt; 1'`。
- 影响范围（修复前）：
  - `DocumentIndexService.cleanupOldVersions` 删除旧版本 chunk 失败（记录 warn，不影响 READY）；
  - `DocumentIndexCleanupService` 的旧版本扫描无法得到候选，孤儿版本不会被清理；
  - 检索正确性不受影响（检索按活跃 `doc_version` 过滤），但 MySQL 会持续累积旧版本 chunk，E2E 已复现 2→4。
- 修复范围：只改这两条 SQL 映射，未改业务逻辑、schema、API。

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

修复后验证：

- MyBatis 实际下发 SQL 为
  `DELETE FROM document_chunk WHERE document_id = ? AND index_version <> ?`，
  变量日志中不再出现字面量实体。
- `DocumentIndexServiceIntegrationTest`：**15/15 PASS**。
  `repeatedIndexIsIdempotentAndIncrementsVersionOnlyOnSuccess` 第二次索引后 chunk 数由 4 恢复为期望的 2。
- `DocumentIndexE2EIntegrationTest`：**1/1 PASS**。
  真实 Agent + Qdrant 重建后 DB chunk 与 Qdrant point 均为 2，一致性断言完整通过。
- Phase 2 全量回归：**27/27 PASS**（DocumentService 7 + DocumentIndexService 15 +
  DocumentIndexE2E 1 + Qdrant 集成 3 + knowledge E2E 1），无回归。
- 对照：`DocumentMapper` 的同类 SQL 使用 `<script>` + `&lt;` 工作正常，本次采用同样的 `<script>` 包装方式。

---

## 10. 已知限制与残余风险

1. **不是跨存储事务。** 若 Qdrant 已写入新版本、MySQL 切换成功、随后进程崩溃，
   这是正常状态；若崩溃发生在切换前，新版本 points 属于非活跃版本，不会被检索，
   由清理任务删除。无法做到“Qdrant 与 MySQL 同时回滚”。
2. **旧数据兼容是保守的。** 迁移前 `embedding_fingerprint` 为 NULL 的文档按
   `model + provider + dimension` 匹配；若历史数据连 provider/dimension 也为 NULL，
   仅凭模型名无法识别“同名不同网关/维度”的不兼容索引。建议逐步重建旧文档。
3. **collection 身份未进入白名单过滤。** 检索使用当前配置的 `model + dimension`
   计算 collection；若文档记录的是另一个 collection（例如前缀配置变更），
   白名单仍可能包含它，但 Qdrant 查询返回空，表现为“无命中”而非报错。
4. **DELETING 达到 `delete-max-attempts` 后不会自动收敛。** 文档永久保持 DELETING
   （不参与检索），需要人工排查；本轮没有告警/运维面板。
5. **文件删除是 best-effort。** `deleteQuietly` 吞掉 IO 异常，数据库仍会置 DELETED，
   可能留下孤儿文件。向量与 chunk 已被删除，不影响检索正确性。
6. **定时任务多实例并发。** 所有状态推进都是条件 UPDATE，语义安全；
   但多实例会重复扫描、重复调用 Agent 删除（幂等），带来额外开销。
7. **回滚旧代码前必须处理版本化 points。** 旧版 Agent 只按 `document_id` 过滤，
   会同时命中 legacy points 与 `index_version >= 1` 的 points，造成同一文档重复片段。
   回滚步骤见第 11 节。
8. **数据库级集成测试已全部补跑通过。** 见 9.4：Phase 2 指定 27 条全部 PASS——
   `DocumentServiceIntegrationTest` 7/7、`DocumentIndexServiceIntegrationTest` 15/15、
   `DocumentIndexE2EIntegrationTest` 1/1、Qdrant 集成 3/3、knowledge E2E 1/1。
   首轮暴露的 D1（`DocumentChunkMapper` 的 `&lt;&gt;` 未在 `<script>` 内）已按最小改动修复：
   只改两条 SQL 映射，旧版本 chunk 删除与清理扫描恢复，回归无失败。详见 9.4.6。

---

## 11. 回滚方法

### 11.1 代码

本轮**没有提交 Git**。回滚即恢复工作区：

```powershell
git checkout -- <本轮修改的文件>
# 新增文件删除：
git clean -fd -- backend/backend/src/main/java/com/ails/backend/dto/document/ReadyDocument.java `
  backend/backend/src/main/java/com/ails/backend/dto/document/agent/AgentDeleteRequest.java `
  backend/backend/src/main/java/com/ails/backend/service/DocumentDeletionService.java `
  backend/backend/src/main/java/com/ails/backend/service/DocumentIndexCleanupService.java `
  backend/backend/src/main/resources/schema-migration-rag-reliability.sql `
  backend/backend/src/test/java/com/ails/backend/service/DocumentDeletionServiceTest.java `
  backend/backend/src/test/java/com/ails/backend/service/DocumentIndexCleanupServiceTest.java `
  backend/backend/src/test/java/com/ails/backend/service/DocumentIndexRecoveryServiceTest.java
```

> `git checkout -- .` 会一并丢弃其它未提交改动；执行前先确认 `git status`。

### 11.2 数据库结构

迁移脚本只新增列/索引，不删除业务数据。应用回滚到旧代码后，新列仍可保留：
旧代码不读取它们，`document_chunk.index_version` 默认 0，旧的 `(document_id, chunk_index)`
唯一性在 `index_version = 0` 下仍成立。

如需彻底移除新列（**会丢失新功能数据**，先备份）：

```sql
ALTER TABLE document_chunk DROP INDEX uk_chunk_doc_ver_idx;
ALTER TABLE document_chunk ADD UNIQUE KEY uk_chunk_doc_idx (document_id, chunk_index);
-- 若存在 index_version > 0 的 chunk，先删除/归档：
-- DELETE FROM document_chunk WHERE index_version <> 0;
ALTER TABLE document_chunk DROP COLUMN index_version;

ALTER TABLE document DROP INDEX idx_doc_status_started;
ALTER TABLE document
  DROP COLUMN embedding_provider,
  DROP COLUMN embedding_dimension,
  DROP COLUMN embedding_fingerprint,
  DROP COLUMN vector_collection,
  DROP COLUMN index_started_at,
  DROP COLUMN delete_attempts,
  DROP COLUMN delete_error;
```

### 11.3 数据 / Qdrant

- 升级前备份：`mysqldump -uroot -p ails document document_chunk > backup-rag.sql`。
- 若已产生版本化 Qdrant points（`payload.index_version >= 1`），回滚到旧 Agent 前
  必须先清理，否则旧检索会混入重复片段。可用本轮新增的 Agent 删除接口：

```bash
# 对每个文档保留当前活跃版本，删除其它版本（需内部 token 与身份签名）
curl -X POST "$AGENT_BASE/agent/documents/delete" \
  -H "X-Internal-Token: $TOKEN" \
  -H "X-Internal-User-Id: $USER_ID" \
  -H "X-Internal-Identity: $SIGNATURE" \
  -H "Content-Type: application/json" \
  -d '{"documentId":123,"collection":"ails_documents_xxx_1024","keepIndexVersion":0}'
```

或恢复 Qdrant 快照。静态文件存储不受影响。

### 11.4 发布顺序建议（升级）

1. 备份 MySQL / Qdrant。
2. 执行 `schema-migration-rag-reliability.sql`。
3. 先发布 **Agent**：新 Agent 同时接受旧 `ready-ids` 整数响应与新增 `indexVersion` 对象响应；
   旧后端会忽略新增的 provider/dimension 参数。
4. 再发布 **后端**：`ready-ids` 响应升级为对象；旧 Agent 若仍在运行会把解析失败按
   fail closed 降级（不会召回错误文档）。

---

## 12. 本轮未做

- 未提交 Git。
- 未进入下一阶段。
- 未新增数据库、消息队列，未替换 Qdrant。
- 未实现跨存储分布式事务（明确不做）。
- 未重构无关的 Retriever / Agent 模块。
- 未执行需要真实 MySQL / Qdrant / 完整 Agent 链路的测试（见 9.4）。
