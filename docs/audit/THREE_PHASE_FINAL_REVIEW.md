# 三阶段后端审查最终报告

> 审查对象：Memory 状态机修复、RAG 文档索引可靠性修复、Citation 结构化引用闭环  
> 审查方式：直接阅读真实代码与测试、核对 Surefire/Failsafe XML 与 pytest 输出、独立复跑可执行测试  
> 审查日期：2026-11-07  
> 审查约束：原审查轮只读；随后证据链修复轮只改测试基础设施、测试等待、`pom.xml` 与文档，
> 未改生产业务逻辑；始终未执行 `git add` / `git commit`。  
> 状态更新（2026-11-07）：
> 1. RAG-01 已按最小改动修复，并以真实 MySQL + Qdrant 回归测试验证；
> 2. **测试证据链已修复**：`*IT` 由 Failsafe 执行并产出 XML；`AnalysisMqIntegrationTest`
>    的 10 条不再静默消失；最终单次全量为 Surefire **335 / 0 / 0 / 11** +
>    Failsafe **4 / 0 / 0 / 0**，Agent **415 / 0 / 1**；
> 3. RAG-02、RAG-03 仍未修复。

---

## 0. 结论

**最终判定：CONDITIONAL PASS（有条件通过）**

- **Memory 状态机**：通过。状态写入已收敛到单一入口，状态迁移矩阵、非法迁移 409、用户归属过滤、Agent 仅更新 CANDIDATE、乐观锁插件注册均已在代码中确认；22 条相关单测/控制器测试在当前 Surefire XML 中通过。
- **Citation 闭环**：通过（存在一个低危实时展示问题）。Agent → Spring 网关 → 消息 JSON 持久化 → SSE `message.end` → 历史读取重算 `available` 的链路完整；9 条真实 MySQL + 真实 Tomcat 的 `ChatSseIntegrationTest` 全部通过；历史读取的文档/ chunk 所有权校验正确。
- **RAG 索引可靠性**：**RAG-01 已修复并验证，但整体仍为有条件通过**。失败回滚与旧版本清理原先使用“删除除某版本之外的全部版本”，且没有 attempt/租约 fencing；现已改为 attempt fencing + 精确版本删除，并用真实 MySQL + Qdrant 的重叠 attempt 回归测试证明旧 worker 不会删除新 worker 的活跃/暂存数据。RAG-02（collection 快照）与 RAG-03（缺少 Qdrant → MySQL 对账）仍未修复。

**因此：**

- RAG-01 不再作为阻断项；其修复证据见 2.RAG-01 状态更新与 9.4.7（Phase 2 报告）。
- 仍不建议把 RAG 索引对外表述为“任意并发下绝对安全”：RAG-02/RAG-03 的风险仍在。
- 本轮已独立复跑 Phase 2 原 27 条集成测试（全部通过）与新增 1 条 Qdrant 精确删除测试，合计 **28/28 PASS**；另新增 RAG-01 fencing IT **4/4 PASS**。

---

## 1. 发现清单

| ID | 级别 | 区域 | 位置 | 问题摘要 |
|---|---|---|---|---|
| RAG-01 | **高（已修复）** | 索引清理/回滚 | `DocumentIndexService.java`、`DocumentIndexCleanupService.java`、`AgentDocumentClient.java` | 回滚/清理原用“删除除保留版本外的所有版本”，未按 attempt fencing；已改为 attempt fence + 精确版本删除，真实 MySQL + Qdrant 回归 4/4 通过 |
| RAG-02 | 中 | 索引回滚 | `DocumentIndexService.java:193-213`、`DocumentIndexCleanupService.java:61-78` | 回滚与定时清理使用开始时的 `vectorCollection` 快照；若嵌入配置/collection 中途变化，失败 attempt 的点会落在新 collection，清理却去旧 collection，留下孤儿点 |
| RAG-03 | 中 | 恢复机制 | `DocumentIndexRecoveryService.java`、`DocumentMapper.selectReadyDocuments` | 没有 Qdrant → MySQL 的反向对账；Qdrant 集合/点丢失后，MySQL 仍返回 READY/ready-ids，检索静默无结果 |
| MEM-01 | 低 | 记忆更新 | `MemoryService.java:395-424`、`schema.sql:324` | `requireIdentityFree` 预检查与 `updateById` 之间存在竞态；并发改同一身份时唯一键异常未捕获，返回 500 而不是 409 |
| CIT-01 | 低 | 实时 SSE 引用 | `ChatService.java:275-292` | 实时 SSE 将所有 Agent 引用硬编码为 `available=true`，后端未校验文档/chunk 是否存在及归属；历史读取才会重算 |
| TST-01 | 中（RAG-01 部分已补） | 测试覆盖 | `DocumentIndexServiceIntegrationTest.java`、`DocumentIndexE2EIntegrationTest.java`、`DocumentIndexFencingIntegrationTest.java` | 原先没有重叠 attempt 的清理/回滚测试；RAG-01 已新增真实 MySQL + Qdrant fencing IT 4/4。“READY 文档重建失败，旧版本仍可检索”的独立场景仍建议补齐 |
| TST-02 | 中 | 跨语言契约 | `HttpPythonAgentGatewayTest.java`、`agent/tests/test_runtime_citations.py` | 没有真实 Python `done` 事件进入 Java 网关的跨语言契约测试；字段重命名会静默退化为空引用 |
| TST-03 | **已解决（残余低）** | 测试计数 | `target/surefire-reports/`、`target/failsafe-reports/`、`AnalysisMqIntegrationTest.java`、`BackendApplicationTests.java` | 原有 321/74 与 321/65 的混合快照已由单次 `clean verify` 取代；10 条 MQ 测试现在明确计为 10 skipped；残余：`BackendApplicationTests` 仍永久 `@Disabled` 1 条，诊断探针仍计入 1 pass |
| TST-04 | **已解决** | 集成测试复现性 | `MemoryStateMachineConcurrencyIT.java`、各 `*IntegrationTest` | `*IT` 已由 Failsafe 在 `mvn clean verify` 中执行并产出独立 XML；Memory 并发 IT 4/4 已复跑；所有门控 IT 在最终全量中真实执行（MQ 10 条除外，已显式 skip） |

---

## 2. 详细发现

### RAG-01（高，已修复）破坏性版本清理没有 fencing，旧 worker 可删除新 worker 的索引

> **状态更新（2026-11-07）：已修复并验证。** 下面“代码事实 / 复现路径 / 修复建议”保留为
> 修复前的审查记录；当前实现已改为 attempt fencing + 精确版本删除，证据见本节末尾的
> “修复落地与验证”。总判定不再因 RAG-01 阻断，但 RAG-02/RAG-03 仍未修复。

**位置**

- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexService.java:140-225`
- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexCleanupService.java:61-78`
- `backend/backend/src/main/java/com/ails/backend/service/AgentDocumentClient.java:128-146`
- Agent 侧：`agent/app/documents/routes.py` 的 `/agent/documents/delete`、`agent/app/vectorstore/qdrant_store.py:275-310`

**代码事实**

1. `runIndex` 的失败分支调用：
   ```java
   rollbackStagedVersion(document, version);
   fail(documentId, attempt, summarize(e));
   ```
2. `rollbackStagedVersion` 的注释写的是“只清理本次 attempt 的 MySQL chunk 与 Qdrant points”，但实际调用：
   ```java
   agentClient.deleteOtherVersions(document, document.getIndexVersion(), document.getVectorCollection());
   chunkService.deleteVersion(document.getId(), attempt);
   ```
   `deleteOtherVersions` 的语义是“保留 `indexVersion`，删除该文档其余所有版本”，不是“只删除 `attempt`”。
3. `cleanupOldVersions` 同样调用 `deleteOtherVersions(document, activeVersion, collection)`，然后 `chunkService.deleteOtherVersions(documentId, activeVersion)`。它会删除“除当前活跃版本外的所有版本”，包括另一个 worker 正在构建的版本。
4. `complete` / `fail` 有 `index_attempts` 条件更新，具备 fencing；但 `rollbackStagedVersion` 和 `cleanupOldVersions` 的破坏性 I/O **没有任何 attempt/租约条件**。
5. `acquireLease` 允许在 `index_started_at IS NULL` 或早于 `reindex-cooldown-ms`（默认 30000ms）时重新抢占；`DocumentIndexRecoveryService` 还会把超时 PROCESSING 标记为 FAILED 并释放/重试。也就是说，旧 worker 完全可能在“已经失去 lease”之后继续运行到失败/完成分支。

**复现路径 A：失败 worker 删除新 worker 的活跃版本**

1. 准备一个 READY 文档 D，`index_version = N`，Qdrant 有版本 N 的点。
2. 触发重建，worker A 抢到 attempt N+1；让 Agent `/documents/index` 延迟超过 `reindex-cooldown-ms`（默认 30s），或把 `AILS_DOCUMENT_REINDEX_COOLDOWN_MS` 调小。
3. 在 A 仍等待 Agent 时再次触发重建；worker B 抢到 attempt N+2，写入版本 N+2 的 chunk 与 Qdrant 点，并成功 `complete`；MySQL 变为 READY、`index_version = N+2`。
4. A 随后失败，进入 `rollbackStagedVersion`；它传入的 `document` 是 A 开始时的快照，`document.getIndexVersion()` 仍是旧值（例如 N）。Agent 收到 `keepIndexVersion=N`，`delete_versions_except` 删除所有 `index_version != N` 的点，**包括 B 刚写入的 N+2 活跃点**。
5. A 的 `fail` 因 `index_attempts` 已变而 0 行、只记日志；MySQL 保持 READY、`index_version = N+2`。
6. `POST /documents/ready-ids` 仍返回 D（MySQL chunk 计数与 vector_ref 可能仍匹配），但 Qdrant 已无 N+2 点；检索返回空结果，且没有错误。

**复现路径 B：成功 worker 的旧版本清理删除新 worker 的暂存版本**

1. A 完成版本 N，`complete` 清空 `index_started_at`。
2. B 立即 `acquireLease`（`index_started_at IS NULL` 时没有冷却限制），开始写入版本 N+1 的 chunk，并向 Qdrant 写入 N+1 点。
3. A 在 `complete` 之后执行 `cleanupOldVersions(document, activeVersion=N, ...)`；`deleteOtherVersions(keep=N)` 删除所有非 N 版本，**包括 B 的 N+1 点**；随后 `chunkService.deleteOtherVersions(N)` 删除 B 的 chunk。
4. 如果 B 此时已经 `complete`，文档变为 READY、`index_version=N+1`，但 Qdrant/chunk 已空；如果 B 尚未完成，B 的索引响应仍可能成功，`complete` 仍会条件更新成功，结果同样是 READY 但缺少数据。

**复现路径 C：定时清理与新构建的 TOCTOU**

1. `DocumentIndexCleanupService.cleanup` 先 `selectById`，检查 `index_started_at == null`，再列出 stale 版本。
2. 检查通过后、删除之前，B 抢到租约并 staged 新版本。
3. 清理任务调用 `deleteOtherVersions(document, activeVersion, document.getVectorCollection())` 与 `chunkService.deleteOtherVersions(...)`，删除 B 的新版本。
4. 类注释“不会误删正在构建的版本”只是读时检查，不是原子条件；没有 SQL 条件更新或版本白名单保护。

**实际影响**

- 直接违反本阶段最重要的承诺：“新索引失败不影响旧索引”。在并发重建、超时回收、冷却窗口重叠时，旧索引/新索引都可能被静默删除。
- 文档仍显示 READY，`ready-ids` 仍可能包含它，用户提问时检索为空、引用为空，前端表现为“模型没有找到资料”，排障困难。
- 不涉及跨用户数据泄露；受影响的是同一文档的检索可用性。
- 不是单纯“清理失败可重试”的问题：`fail` 已被 fencing，MySQL 不会回退；破坏性删除却已发生，且系统没有反向对账（见 RAG-03）自动发现。

**修复建议**

1. **把删除粒度改成“精确版本”**：新增 Agent 接口 `deleteVersions(collection, documentId, versions: [N+1])`，只删除枚举出的版本；`rollbackStagedVersion` 只删除 `attempt`，不要用 `deleteOtherVersions`。
2. **清理旧版本时显式列出待删版本**：在 `complete` 前查清旧版本号（或由 `complete` 返回旧版本），只删除这些版本；不要用“除 active 外全部删除”。
3. **给破坏性清理加 fencing 前置条件**：删除前用条件 SQL 确认 `index_attempts = attempt` 且 `index_started_at` 仍属于本次租约；对定时清理，检查 `activeVersion`/租约未变化，或把“扫描 + 删除”改为条件状态机。
4. **租约续期或延长**：`reindex-cooldown-ms` 不应短于 Agent 索引总超时（当前 `index-timeout-ms` 默认 120s，冷却 30s）；更根本的是版本级删除，不能只靠租约时间。
5. **Agent 侧保留兼容**：旧 `keepIndexVersion` 接口可保留给历史调用，但新代码不再使用；`keepIndexVersion = 0` 的 legacy 语义要明确只用于真正的全量清理。
6. **补测试**：READY v1 → 慢速 v2 失败 + v3 成功，断言 v3 点/chunk 仍存在；A 完成后 B 立即构建，A 的 cleanup 不删除 B；定时清理与并发 acquire 的竞态测试。

**修复落地与验证（2026-11-07）**

- `DocumentIndexService` 新增 `stillOwnsAttempt(...)`：删除前重新读取文档，要求未删除、
  状态不是 DELETING/DELETED、`index_attempts` 仍等于当前 worker 的 attempt；不满足则
  记录 warn 并放弃删除。
- `rollbackStagedVersion` 只删除本次 attempt；`cleanupOldVersions` 只删除
  `version < activeVersion` 的版本。二者不再使用“删除除保留版本外全部”的语义。
- 新增精确版本删除能力：Java `AgentDeleteRequest.indexVersions/deleteLegacy`、
  `AgentDocumentClient.deleteVersions`、`DocumentChunkMapper.deleteVersions`；
  Agent `DeleteDocumentRequest`、`/agent/documents/delete`、
  `QdrantDocumentStore.delete_versions`。Qdrant 过滤条件为
  `must(document_id)` + `should(MatchAny(index_version), IsEmpty(index_version))`。
- `DocumentIndexCleanupService` 同样加入二次读取 fence（租约、活跃版本、attempt 均未变化），
  且只删除 `version <= 捕获的 attempts`，覆盖定时清理的 TOCTOU 路径。
- 测试：`DocumentIndexFencingIntegrationTest` 使用真实 MySQL `ails_document_fencing_it` +
  真实 Qdrant；Fake Agent 的删除请求会真实转发到 Qdrant。
  - worker A lease 回收 → worker B 接管并激活 → A 恢复 cleanup：B 的活跃 Qdrant points
    与 MySQL chunks 保留，A 未发起删除；
  - 同样场景下 A 恢复 rollback：B 的数据保留；
  - 当前 worker 正常 cleanup：只删旧版本；
  - 当前 worker 正常 rollback：只删本次 attempt。
  - 结果：**4/4 PASS**。
- 回归：Phase 2 原 27 条集成测试全部通过；新增 1 条真实 Qdrant 精确删除测试后
  **28/28 PASS**。最终单次全量：后端 Surefire **335 / 0 / 0 / 11** +
  Failsafe **4 / 0 / 0 / 0**，Agent **415 passed / 0 skipped / 1 xfailed**。

---

### RAG-02（中）回滚/定时清理使用旧的 collection 快照，配置变化会留下孤儿点

**位置**

- `DocumentIndexService.java:193-213`：`rollbackStagedVersion` 使用 `document.getVectorCollection()`
- `DocumentIndexCleanupService.java:61-78`：使用 `document.getVectorCollection()`
- `AgentDocumentClient.java:128-146`：`deleteOtherVersions` 的 collection 参数
- Agent：`agent/app/documents/routes.py` 的 `/agent/documents/index` 按**当前** provider 配置计算 collection

**代码事实**

- `runIndex` 开始时读取 `Document document = documentMapper.selectById(documentId)`，该快照包含当时的 `vector_collection`。
- Agent 的 `/documents/index` 在每次请求时根据当前 embedding provider/model 计算 collection；`complete` 成功后才会把新 collection 写入 MySQL。
- 失败回滚调用 `rollbackStagedVersion(document, version)`，仍使用旧快照的 collection；如果构建期间配置已从 collection A 切到 B，Agent 写入的是 B，回滚却去 A 删除。
- 定时清理同样读取 `document.getVectorCollection()`；如果失败 attempt 的点在 B，而 MySQL 的 `vector_collection` 还是 A，清理永远不会找到 B 的点。

**复现路径**

1. READY 文档 D 使用 embedding 配置 A、collection A。
2. 修改配置为模型 B、collection B（或切换 provider），触重建。
3. Agent 在 B 中写入 staged 点，然后返回失败（或索引响应校验失败）。
4. `rollbackStagedVersion` 向 collection A 发 `keepIndexVersion=旧版本`，删除 A 中的非保留点；B 中的 staged 点成为孤儿。
5. `DocumentIndexCleanupService` 之后也只清理 A；B 的孤儿点长期存在。

**实际影响**

- Qdrant 存储泄漏；如果 collection B 之后被复用，旧点可能与新点混在一起（检索侧还有 `doc_version`/fingerprint 过滤，通常不会直接召回，但会增加扫描量与排障成本）。
- 不是当前最主要故障路径，但会让“失败清理”在配置切换时不可靠。

**修复建议**

- 让 Agent 的 index 响应或错误响应返回实际使用的 collection；失败回滚使用该 collection，而不是 `document` 快照。
- 或者在 MySQL 增加“本次 attempt 的 collection”字段（如 `index_collection`），在 acquire/stage 时写入，complete/rollback/cleanup 都按 attempt 读取。
- 定时清理应清理“所有已知 collection 中该文档的旧版本”，或至少按 attempt 记录逐项清理。
- 补测试：fake Agent 返回与文档快照不同的 collection，断言回滚调用的是实际 collection。

---

### RAG-03（中）缺少 Qdrant → MySQL 反向对账，派生数据丢失后 READY 文档静默失效

**位置**

- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexRecoveryService.java`
- `backend/backend/src/main/java/com/ails/backend/mapper/DocumentMapper.java` 的 `selectReadyDocuments`
- `agent/app/vectorstore/qdrant_store.py` 的检索路径

**代码事实**

- `selectReadyDocuments` 的 READY 判定只查 MySQL：状态 READY、未删除、`indexed_at` 非空、embedding model 非空、`chunk_count` 等于当前 active 版本 chunk 数且 `vector_ref` 非空、fingerprint 匹配。
- 它不检查 Qdrant collection 是否存在、该 document/version 的点数是否与 chunk 数一致。
- `DocumentIndexRecoveryService` 只处理状态/租约超时；没有“READY 但 Qdrant 无点”的修复路径。
- 检索侧对 Qdrant 返回空结果是正常路径，不会报错；因此丢失派生数据后没有告警。

**复现路径**

1. 文档 D 索引成功，MySQL READY，`ready-ids` 包含 D。
2. 删除 Qdrant collection，或仅删除 D 的 points（卷重置、误操作、从旧快照恢复）。
3. 再次检索：`ready-ids` 仍包含 D，Qdrant 查不到点，返回空结果；后端/前端只看到“没有引用”，没有错误。
4. 定时恢复、清理都不会发现；只有人工重建才能修复。

**实际影响**

- 整个 collection 的 RAG 可能静默失效，且从业务指标上不易发现。
- 与 RAG-01 叠加时更危险：RAG-01 制造“READY 但无点”，RAG-03 让系统没有自愈能力。
- 数据不泄露，但可用性风险为中等。

**修复建议**

- 增加定时对账任务：对 READY 文档抽样或按 active version 统计 Qdrant 点数，与 `chunk_count` 比较；不一致时标记 `FAILED` 并重新入队，或至少打点告警。
- 对 Qdrant collection 不存在做健康检查/启动检查；检索连续空结果可按文档维度计数报警。
- 增加测试：索引成功后删除 Qdrant 点，触发对账，断言文档被重新索引或标记异常。

---

### MEM-01（低）记忆身份并发更新返回 500 而非 409

**位置**

- `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:395-424`
- `backend/backend/src/main/resources/schema.sql:324` 的 `uk_memory_identity (user_id, scope_key, memory_key)`

**代码事实**

- `update` 先调用 `requireIdentityFree(userId, scope, memoryKey, memoryId)` 做“是否已被占用”的 SELECT，再 `memoryMapper.updateById(memory)`。
- 唯一键 `uk_memory_identity` 是最终防线，但 `updateById` 的 `DuplicateKeyException` 没有被捕获并转换成业务异常。
- `createCandidate` 在类似路径上捕获了唯一键冲突，`update` 没有。

**复现路径**

1. 同一用户已有记忆 M1（identity K1）和 M2（identity K2）。
2. 两个并发 PATCH：一个把 M1 改成 K2，另一个把 M2 改成 K2（或两个请求把不同记忆改成同一 identity）。
3. 两个 `requireIdentityFree` 都读到 0 行；两个 `updateById` 竞争，一个成功，另一个触发 `DuplicateKeyException`。
4. 失败请求由全局异常处理返回 500，而不是“身份已被占用”的 409/400。

**实际影响**

- 数据不会损坏（唯一键阻止了重复）；只是错误码/错误信息不符合业务契约，客户端可能把可重试冲突当成服务器故障。
- 属于低概率并发边界，严重级别低。

**修复建议**

- 在 `update` 中捕获 `DuplicateKeyException`，转换为与 `requireIdentityFree` 相同的业务异常（409 或 400）。
- 或把预检查改为 `UPDATE ... WHERE NOT EXISTS` 的原子条件更新；但捕获异常更简单且能覆盖所有入口。
- 补并发测试：两个不同记忆并发改成同一 identity，断言一个成功、一个 409，且数据库只有一条该 identity。

---

### CIT-01（低）实时 SSE 的 `available=true` 未由后端校验

**位置**

- `backend/backend/src/main/java/com/ails/backend/service/ChatService.java:275-292`

**代码事实**

```java
end.put("citations", citations.stream()
        .map(citation -> CitationResponse.from(citation, true))
        .toList());
```

- 实时 `message.end` 对所有 Agent 引用硬编码 `available=true`。
- 后端没有在发送前调用 `CitationService.isAvailable` 检查 document/chunk 是否存在、是否属于当前用户。
- 历史读取 `MessageService.listByConversation` 会通过 `CitationService.resolveForMessages` 重算 `available`，因此历史页会纠正。
- `HttpPythonAgentGateway.parseCitations` 只校验 documentId/chunkId 为正数、title 非空，不校验归属。

**复现路径**

1. 使用桩 Agent 网关在 `done.citations` 中返回一个属于其他用户的 documentId（或一个不存在的 id）。
2. 实时 SSE 的 `message.end.citations` 显示该引用 `available=true`，前端可能把它当成可打开的来源。
3. 刷新历史消息时，`available` 变为 false，因为按当前用户/文档状态重算。
4. 正常 Agent 只会返回检索到的 chunk，因此这不是当前可被普通用户利用的权限绕过；但后端没有把“实时展示”与“历史校验”统一。

**实际影响**

- 正常路径无内容泄露；异常/被篡改的 Agent 可以让实时界面短暂展示不属于当前用户或不存在的来源标题。
- 文档在生成过程中被删除时，实时 SSE 仍可能显示 available=true，直到刷新历史。
- 低危，属于纵深防御/一致性问题。

**修复建议**

- 在 `ChatService` 发送 `message.end` 前，用 `CitationService` 批量解析并计算 `available`；不可用的引用可选择保留但标 false，或直接丢弃。
- 让实时与历史共用同一个 `resolveForMessages`/`isAvailable` 逻辑，避免两套规则。
- 补测试：桩 Agent 返回他人/不存在 documentId，断言实时 SSE 的 available 为 false（或引用被丢弃）。

---

### TST-01（中）缺少“READY 重建失败保留旧索引”和“重叠 attempt”集成测试

**位置**

- `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexServiceIntegrationTest.java`
- `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexE2EIntegrationTest.java`
- `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexServiceTest.java`

**代码事实**

- `DocumentIndexServiceIntegrationTest` 覆盖首次索引成功、上传异步、解析失败、chunk 持久化失败、embedding/Qdrant 失败、畸形向量映射、鉴权失败、重复幂等、错误截断、stale 恢复、late completion fencing。
- 这些失败用例大多从 UPLOADING/PROCESSING 开始，没有一条是“文档已经 READY，重建失败后旧版本仍可检索”。
- `DocumentIndexE2EIntegrationTest` 只有成功/重建成功路径，没有失败路径。
- `DocumentIndexServiceTest` 的“失败回滚只删除新版本、保留旧索引”是 Mockito 交互测试，不能验证真实 SQL/Qdrant 行为。
- 没有测试覆盖两个 attempt 重叠时的 `rollbackStagedVersion` / `cleanupOldVersions`。
- 因此 RAG-01 的三条复现路径都不会让现有测试失败。

> **状态更新（2026-11-07）**：已新增 `DocumentIndexFencingIntegrationTest`，用真实 MySQL +
> 真实 Qdrant 覆盖“A 租约回收 → B 接管激活 → A 恢复 cleanup/rollback”和当前 worker
> 精确删除两个正向场景，4/4 通过。上述“没有测试覆盖”的描述保留为修复前记录。

**实际影响**

- 报告中的“新索引失败不影响旧索引”目前只有部分代码路径和 mock 断言支撑，缺少能防回归的真实集成测试。
- 这是中危测试缺口：它不是新的运行时缺陷，但会让 RAG-01 这类高危问题长期漏网。

**修复建议**

- 增加真实 MySQL + fake Agent/Qdrant 的集成测试：
  - READY v1 → v2 失败 → 断言 v1 chunk、Qdrant 点、ready-ids 仍可用；
  - v2 慢失败期间 v3 成功 → 断言 v3 数据不被 v2 回滚删除；
  - v1 完成后 v2 立即开始 → 断言 v1 的 cleanup 不删除 v2；
  - 定时清理扫描后 v2 抢租约 → 断言清理不删除 v2。
- 如果暂时无法接真实 Qdrant，至少用可断言的 fake 记录“删除请求的版本集合”，并断言不是“除 active 外全部”。

---

### TST-02（中）缺少 Python → Spring 的引用契约端到端测试

**位置**

- `backend/backend/src/test/java/com/ails/backend/agent/HttpPythonAgentGatewayTest.java`
- `agent/tests/test_runtime_citations.py`、`test_citation_filter.py`、`test_citations.py`

**代码事实**

- Java 侧测试用硬编码 JSON 验证 `parseCitations` 的字段名、类型、去重和畸形丢弃。
- Python 侧测试独立验证 `structured_citations()` / `StreamingCitationFilter` 的输出。
- 没有测试真正启动 Python Agent，把它的 `done` SSE 事件喂给 `HttpPythonAgentGateway`，再断言 Java 侧引用非空。
- 当前字段名在两侧是一致的（`documentId`、`chunkId`、`chunkIndex`、`sourceTitle`），所以不是现存 bug；但任何一侧重命名或改成 snake_case，Java 的解析会静默返回空列表，测试仍全绿。

**实际影响**

- 跨语言契约漂移风险中等；引用功能可能在集成环境“无声失效”，而单元测试无法发现。
- 与 CIT-01 叠加时，实时引用问题更难定位。

**修复建议**

- 增加一个跨语言契约测试：Python 生成固定 `done` 事件（或启动 fake LLM 走真实运行时），通过 HTTP 发给 Spring 的测试实例，断言 `message.end.citations` 的字段与数量。
- 或者维护一份共享 JSON schema/golden fixture，两侧都按它断言；CI 中至少跑一次真实 Python Agent 的引用输出。
- 在 Agent 输出契约变更时，把 Java `parseCitations` 的字段名纳入同一份契约测试。

---

### TST-03（已解决，残余低）测试计数与产物不一致，且存在不可见测试

> 本节保留修复前的事实记录，并在末尾给出修复后证据。修复只动测试基础设施与文档，
> 未改生产业务逻辑。

**修复前记录**

- 当前 Surefire XML 是混合快照：`tests=321`、`skipped=65`，与报告写的 74 不一致。
- 差额来自 `ChatSseIntegrationTest` 被一次定向运行覆盖（9 tests / 0 skipped），
  而默认全量中它应跳过 9 条。
- `AnalysisMqIntegrationTest` 的 10 个 `@Test` 在 `@BeforeAll` 调用 `Assumptions.abort`，
  Surefire XML 记录 `tests="0"`、`skipped="0"`，10 条从统计中消失。
- `BackendApplicationTests` 无条件 `@Disabled`，任何配置下都不执行。

**修复动作（2026-11-07）**

1. `AnalysisMqIntegrationTest`：`@BeforeAll` 只记录 `unavailableReason` 并返回；
   新增 `@BeforeEach skipWhenBrokerUnavailable()` 执行 assumption。Docker 不可用时
   Surefire XML 现在为 `tests=10 / skipped=10`，skip 原因可读；`-Pmq-integration`
   的 `ails.it.require-docker=true` 仍会在 `@BeforeAll` 显式 fail。
2. `MemoryStateMachineConcurrencyIT`：改用 Maven Failsafe（`**/*IT.java`），
   在 `mvn clean verify` 的 `integration-test` 阶段执行，产物落到
   `target/failsafe-reports/`，不再依赖 `-Dtest=...` 临时命令。
3. 最终用一次 `mvn clean verify` 取代混合快照；Surefire 与 Failsafe XML 均来自同一命令。

**修复后证据**

| 项 | 结果 |
| --- | --- |
| Surefire | 46 classes / **335 tests / 0 failures / 0 errors / 11 skipped** |
| Failsafe | 1 class / **4 tests / 0 failures / 0 errors / 0 skipped** |
| `AnalysisMqIntegrationTest` | `tests=10 / skipped=10`（可见 skip，未真实执行） |
| `BackendApplicationTests` | `tests=1 / skipped=1`（永久 `@Disabled`，残余） |
| `ChatSseOrderingProbeTest` | `tests=1 / failures=0`（诊断探针，不计入契约结论） |

**残余影响**

- 10 条 MQ 测试仍未真实执行；原因是本机 Testcontainers Npipe 对 Docker API 返回
  HTTP 400，不是代码失败。它们已不再从统计中消失。
- `BackendApplicationTests` 仍是永久 `@Disabled`，建议改为环境门控或删除。
- 诊断探针仍计入 1 pass；报告已单独标注，不并入契约结论。

---

### TST-04（已解决）门控集成测试无法从单次产物独立复跑

**修复前记录**

- 当时环境没有可用 MySQL 凭据，真实 MySQL 测试无法复跑。
- `MemoryStateMachineConcurrencyIT` 以 `*IT` 结尾，默认 Surefire 不执行，且没有 XML。
- `DocumentIndexServiceIntegrationTest` / `DocumentServiceIntegrationTest` /
  `DocumentIndexE2EIntegrationTest` 在当时的 XML 中均为 skipped（15 / 7 / 1）。
- Phase 2 报告中的 27 条门控集成测试无法从当前 Surefire 产物复现。

**修复动作（2026-11-07）**

1. 在 `pom.xml` 增加 Maven Failsafe 3.2.5，`includes=**/*IT.java`，绑定
   `integration-test` + `verify`。
2. 在独立测试库上 `DROP DATABASE` 后由 `schema.sql` 重建，消除
   `last_message_seq` 等旧 schema 漂移；开发库 `ails` 未动。
3. 最终执行一次 `./mvnw.cmd -B clean verify`，同时开启全部 `AILS_*_IT` 门控；
   Surefire 与 Failsafe XML 来自同一次运行、同一组环境变量。

**修复后证据**

| 测试类 | 结果 | 产物 |
| --- | --- | --- |
| `MemoryStateMachineConcurrencyIT` | 4 / 0 / 0 / 0 | `target/failsafe-reports/` |
| `DocumentIndexServiceIntegrationTest` | 15 / 0 / 0 / 0 | Surefire |
| `DocumentServiceIntegrationTest` | 7 / 0 / 0 / 0 | Surefire |
| `DocumentIndexE2EIntegrationTest` | 1 / 0 / 0 / 0 | Surefire |
| `DocumentIndexFencingIntegrationTest` | 4 / 0 / 0 / 0 | Surefire |
| `SecurityContextIntegrationTest` | 19 / 0 / 0 / 0 | Surefire |
| `InternalIdentityHttpIntegrationTest` | 5 / 0 / 0 / 0 | Surefire |
| `ChatSseIntegrationTest` | 9 / 0 / 0 / 0 | Surefire |
| `Phase2RealStateIntegrationTest` | 6 / 0 / 0 / 0 | Surefire |
| `Phase2MqProcessorIntegrationTest` | 5 / 0 / 0 / 0 | Surefire |
| `Phase2RabbitMqBrokerTest` | 5 / 0 / 0 / 0 | Surefire |

**残余边界**

- 关键路径已在“本机 + 真实 MySQL/Qdrant/RabbitMQ/Agent”上复跑；CI 尚未固定这些服务，
  长期可复现性仍依赖 CI 配置。
- `AnalysisMqIntegrationTest` 的 10 条因 Testcontainers Docker 检测失败而 skip，
  不是执行通过；见 TST-03。

---

## 3. 测试可信度专项

### 3.1 本轮实际执行/核对结果

| 套件 | 结果 | 备注 |
|---|---|---|
| 后端 Surefire XML | **335 tests / 0 failures / 0 errors / 11 skipped / 46 classes** | 单次 `mvn clean verify`，全门控开启 |
| 后端 Failsafe XML | **4 tests / 0 failures / 0 errors / 0 skipped / 1 class** | `MemoryStateMachineConcurrencyIT` |
| `ChatSseIntegrationTest` | 9 / 0 skipped / 0 failures | 真实 MySQL `ails_chat_sse_it` + 真实 Tomcat + 桩 Agent |
| Agent pytest 全量 | **415 passed / 0 skipped / 1 xfailed** | 真实 Qdrant、knowledge E2E、RabbitMQ 门控均开启 |
| 真实 Qdrant 集成 | 4 / 4 passed | 临时 collection，测试后清理 |
| 真实 MySQL 门控 IT | **全部真实执行并通过** | 见 TST-04 表 |
| 真实 FastAPI + Qdrant + MySQL E2E | **1 / 1 passed** | `DocumentIndexE2EIntegrationTest` |

### 3.2 11 条 skip 的逐项原因

| 测试类 | skip 数 | 门控/原因 |
|---|---:|---|
| `BackendApplicationTests` | 1 | 无条件 `@Disabled`（需要本地 MySQL 与已初始化库表） |
| `AnalysisMqIntegrationTest` | 10 | Docker/Testcontainers Npipe 返回 HTTP 400；逐条 assumption skip，原因可读 |
| **合计** | **11** | 最终单次 `clean verify` XML |

`BackendApplicationTests` 只有 1 条，且永久禁用。
`AnalysisMqIntegrationTest` 的 10 条**未真实执行**，只作为可见 skip 计入；
不得把这 10 条写成通过。

Agent 侧最终全量为 **0 skipped**：

- `test_knowledge_e2e.py` 1 条：已设置 `AILS_KNOWLEDGE_E2E=true`，真实通过；
- `test_qdrant_integration.py` 4 条：已设置 `AILS_QDRANT_IT=true`，真实通过；
- `test_worker_broker.py` 2 条：已设置 `AILS_TEST_RABBITMQ_URL`，真实通过。

Agent 1 xfail：

- `agent/tests/test_runtime_budget_contract.py::test_runtime_must_not_deliver_no_retrieval_claim_when_chunks_dropped`，是已记录的剩余语义风险，不是意外失败。

### 3.3 测试执行真实性判断

- **没有发现伪造测试结果或“只跑 mock 却声称集成通过”的直接证据。** Surefire/Failsafe XML、pytest 输出与代码中的门控逻辑一致。
- **数字现在来自单次运行**：Surefire 335 + Failsafe 4 均来自同一次 `mvn clean verify`；
  Agent 415 来自同轮全门控 pytest。321/74 与 321/65 的混合快照不再作为当前证据。
- **10 条 MQ 测试已可见**：XML 为 `tests=10 / skipped=10`，不再是 0 tests/0 skipped；
  但由于 Testcontainers Docker 检测失败，它们没有真实执行。
- **4 条 Memory 并发 IT 已有 XML**：Failsafe `tests=4 / skipped=0`，4/4 PASS。
- **28 条门控集成测试**：最终全量中真实执行并通过；RAG-01 fencing IT 4/4 另行通过。
  命令与结果见 Phase 2 报告 9.4。

### 3.4 mock 测试与真实 DB 断言分布

| 类型 | 代表测试 | 断言对象 | 可信度边界 |
|---|---|---|---|
| Mockito 单测 | `MemoryStateMachineTest`（19）、`DocumentIndexServiceTest`（11）、`DocumentIndexCleanupServiceTest`（6）、`DocumentIndexRecoveryServiceTest`（4）、`DocumentDeletionServiceTest`（12）、`CitationServiceTest`（8）、`ChatServiceTest`（6）、`HttpPythonAgentGatewayTest`（15） | 交互、捕获的 update wrapper、返回 DTO | 不验证真实 SQL、唯一键、乐观锁版本、Qdrant 状态 |
| 真实 DB 集成 | `ChatSseIntegrationTest`（9，已跑）、`DocumentIndexServiceIntegrationTest`（15，已跑）、`DocumentServiceIntegrationTest`（7，已跑）、`DocumentIndexE2EIntegrationTest`（1，真实 Agent + Qdrant）、`MemoryStateMachineConcurrencyIT`（4，Failsafe XML，已跑） | 真实 MySQL 行、SSE、HTTP 404 | 全部计入最终单次全量证据 |
| 真实 Qdrant | `agent/tests/test_qdrant_integration.py`（4，已跑） | 真实 Qdrant 临时 collection 的写入/删除/检索 | 不覆盖 Java 编排与 MySQL 状态 |
| MySQL + Qdrant fencing | `DocumentIndexFencingIntegrationTest`（4，已跑） | Fake Agent 真实转发删除到 Qdrant；断言 overlap attempt 下新 worker 数据保留 | 专门覆盖 RAG-01 |
| 真实 Python + Java | 无 | — | 缺跨语言 citation 契约测试（TST-02） |
| 诊断探针 | `ChatSseOrderingProbeTest` | 顺序探测，不构成契约 | 不应计入通过/失败结论 |

**关键缺口**：RAG-01 已用真实 MySQL + Qdrant 的 overlap attempt 测试补齐；MEM-01 仍属于
“真实 SQL/并发/唯一键”问题，Mockito 测试难以发现，`MemoryStateMachineConcurrencyIT`
本轮未复跑。因此“单测全绿”仍不能推出 MEM-01 的并发安全。

---

## 4. 各阶段结论

### 4.1 Memory 状态机

**通过（代码层面）**，测试证据部分可复现。

已验证：

- `MemoryService.applyStatusTransition` 是唯一状态写入点；`memory.setStatus` 仅出现在 `MemoryService.java:602` 一处。
- `MemoryStatus` 的迁移矩阵：同状态允许；CANDIDATE → ACTIVE/ARCHIVED；ACTIVE → ARCHIVED；ARCHIVED 无出边。非法迁移抛 409。
- `update` 在修改前保存 `statusBeforeUpdate`，状态迁移后再用原状态做可编辑性校验，避免“先改成 ACTIVE 绕过 ARCHIVED 不可编辑”的路径。
- `ownedMemory` 按 `userId` 过滤；`search` 同时按 `userId` 和 visibility 过滤；Agent 侧 `updateCandidate` 只更新 CANDIDATE。
- `MybatisPlusConfig` 注册 `OptimisticLockerInnerInterceptor`，`Memory` 使用 `@Version`；`updateById` 返回 0 时抛 409。
- `MemoryStateMachineTest` 19 条 + `MemoryControllerTest` 3 条在当前 XML 中通过。

未独立验证：

- `MemoryStateMachineConcurrencyIT` 4 条真实 MySQL 并发测试：最终已由 Failsafe 执行，
  `target/failsafe-reports/TEST-*.xml` 记录 `tests=4 / failures=0 / errors=0 / skipped=0`，
  可以作为已验证事实。
- `MemoryStateMachineTest` 的 t14/t15 用 Mockito 模拟 `selectById`/`updateById` 和 barrier，验证的是服务层对 0 行更新的处理，不是真实 MySQL 乐观锁行为。

### 4.2 RAG 文档索引

**有条件通过。** 单 writer 设计正确；RAG-01 已修复并验证，但 RAG-02/RAG-03 仍存在。

已验证：

- 分阶段/版本化：`stageChunks(version)` 先写非活跃 chunk；Agent 索引返回后 `validateIndexResponse` 校验；`updateVectorRefs` 要求恰好 1 行；`complete` 用 `index_attempts` 条件更新作为唯一生效点。
- 旧索引在“新版本未 complete”时保持 READY；`selectReadyDocuments` 要求 READY、未删除、fingerprint 匹配、active 版本 chunk 数与 vector_ref 完整。
- Agent 侧 `_parse_hit` 对 `doc_version` 做精确校验：期望版本 >0 时要求 payload `doc_version` 精确匹配；期望 0 时拒绝带非零 `index_version` 的点；fingerprint 不匹配拒绝。暂存/部分写入不会参与检索。
- 删除：`requestDelete` 先条件更新为 DELETING，`selectReadyDocuments` 排除 DELETING/deleting_at；删除失败保留 DELETING 并记录错误，定时重试。
- `DocumentDeletionService` 清理顺序为 Qdrant → chunks → 文件 → markDeleted；失败不标记 DELETED。
- D1 修复（`selectReadyDocuments` SQL 条件）已在代码中体现；相关单元测试通过。
- 真实 Qdrant 集成 4/4 通过（独立复跑）；`DocumentIndexServiceIntegrationTest` 15/15、
  `DocumentServiceIntegrationTest` 7/7、`DocumentIndexE2EIntegrationTest` 1/1 已真实执行。
- RAG-01 修复：`rollbackStagedVersion` / `cleanupOldVersions` 增加 attempt fence，
  删除粒度改为精确版本；`DocumentIndexFencingIntegrationTest` 4/4 通过（真实 MySQL + Qdrant）。
- 定时清理同样增加二次读取 fence 与版本上界，覆盖 TOCTOU 路径。

未通过/风险：

- RAG-02：回滚/定时清理仍使用 `document` 的 collection 快照。
- RAG-03：缺少 Qdrant → MySQL 反向对账，派生数据丢失后 READY 文档会静默失效。
- RAG-01 的 fence 不是跨存储事务；fence 校验后仍有极窄窗口，精确版本删除负责兜底，
  不会删除新版本，但无法保证 Qdrant/MySQL 同时回滚。

### 4.3 Citation 闭环

**通过（正常路径与历史读取）**，实时展示有一处低危一致性问题。

已验证：

- Agent `structured_citations()` 输出 `documentId`/`chunkId`/`chunkIndex`/`sourceTitle`；`StreamingCitationFilter` 过滤非法标签；`validate_citations()` 校验。
- `HttpPythonAgentGateway.parseCitations` 只接受正数 documentId/chunkId 和非空 title，逐条丢弃畸形，按 `documentId:chunkId` 去重。
- `ChatService` 在完成时构造 `CitationMetadata`，`MessageService.appendAssistantMessage` 持久化 `message.citations` JSON；空引用写 NULL。
- `ChatService` 的 `message.end` 带 `citations`；`MessageService.listByConversation` 读取时通过 `CitationService.resolveForMessages` 批量加载文档/chunk，按当前状态重算 `available`。
- `CitationService.isAvailable` 要求文档 owner == 当前 userId、chunk.documentId == citation.documentId、文档/chunk 存在；跨用户历史消息 `ownedConversation` 返回 404。
- `ChatSseIntegrationTest` 9/9 通过（真实 MySQL + Tomcat + 桩 Agent）：SSE available=true、DB JSON 不存 available、历史重载 available=true、文档软删后 available=false、他人访问 404。
- `CitationServiceTest` 8、`ChatServiceTest` 6、`HttpPythonAgentGatewayTest` 15 在当前 XML 中通过；Agent 最终全量 415 passed。

未验证/风险：

- CIT-01：实时 SSE 硬编码 `available=true`，未在发送前校验归属/存在性；历史读取会纠正，正常 Agent 不产生越权内容，但属于纵深防御缺口。
- 没有真实 Python → Spring 的引用契约 E2E（TST-02）。
- 数据库迁移脚本（V?__citation.sql）报告称 9/9、幂等；本轮未独立执行迁移测试，不能计入已验证成果。

---

## 5. 可用于简历的成果清单

> 以下只写“代码可确认 + 最终单次全量产物可确认”的内容；未执行的部分不写成“已通过”。

### Memory

- 将记忆状态写入收敛到单一 `applyStatusTransition` 入口，建立 CANDIDATE/ACTIVE/ARCHIVED 的 3×3 迁移矩阵，非法迁移统一返回 409，ARCHIVED 为终态。
- 在更新路径保留“迁移前状态”做可编辑性校验，避免通过状态变更绕过 ARCHIVED 保护。
- 所有记忆读写按 `userId` 过滤，Agent 侧仅允许更新 CANDIDATE，防止越权写入。
- 接入 MyBatis-Plus `@Version` 乐观锁与 `OptimisticLockerInnerInterceptor`，更新冲突返回 409。
- 22 条状态机/控制器测试在当前 Surefire XML 中通过；另有 4 条真实 MySQL 并发 IT
  已在 Failsafe XML 中 4/4 通过。

### RAG

- 实现分阶段、版本化的文档索引：chunk 先以非活跃版本落库，Qdrant 点带 `index_version`/`doc_version` payload，MySQL 条件更新 `complete` 作为唯一生效点。
- 在检索入口做 active 版本、`doc_version`、embedding fingerprint 三重校验，拒绝暂存/部分写入/旧模型向量参与召回。
- READY 白名单同时校验 chunk 数、`vector_ref`、embedding fingerprint；删除文档先进入 DELETING 并立即退出检索，删除失败保留 DELETING 定时重试。
- 增加 stale 恢复、租约 fencing、旧版本延迟清理等机制；D1 修复后 ready-ids 与检索版本一致。
- 修复 RAG-01：破坏性回滚/清理增加 attempt fencing，Qdrant 与 MySQL 均改为精确版本删除；
  新增真实 MySQL + Qdrant 的 overlap attempt 回归测试 4/4，通过 Phase 2 全量 IT 28/28。
- 真实 Qdrant 集成 4/4 通过；11 条索引单测、6 条清理单测、4 条恢复单测在当前 XML 中通过。
- 注意：RAG-02（collection 快照）与 RAG-03（反向对账）仍未修复，不能把“任意并发下绝对安全”作为成果表述。

### Citation

- 打通 Agent 结构化引用 → Spring 网关解析/去重 → 消息 JSON 持久化 → SSE `message.end` → 历史读取的完整链路。
- 历史读取按“当前文档/chunk 状态 + 当前用户所有权”重算 `available`，文档删除后引用自动变为不可用；跨用户访问返回 404。
- `ChatSseIntegrationTest` 9/9 通过（真实 MySQL + Tomcat + 桩 Agent）；CitationService 8、ChatService 6、Gateway 15 单测通过；Agent 415 passed / 0 skipped / 1 xfailed。
- 实时 SSE 的 `available` 目前为硬编码 true，后续应复用历史校验逻辑（CIT-01）。

### 测试工程

- 后端最终单次全量：Surefire **335 / 0 / 0 / 11** + Failsafe **4 / 0 / 0 / 0**；
  Agent pytest **415 passed / 0 skipped / 1 xfailed**；真实 Qdrant 4/4；
  RAG-01 fencing IT 4/4；Phase 2 IT 28/28。
- 建立了环境变量门控的真实 MySQL/Qdrant/Agent 集成测试与 `*IT` 类，并用 Failsafe
  在 `mvn clean verify` 中产出独立 XML；单次全量已可复核，CI 固定服务仍是后续工作。

---

## 6. 建议的修复顺序与退出条件

1. **P0（已完成）修复 RAG-01**：版本级精确删除 + 清理前 fencing；补 overlap attempt 真实 MySQL + Qdrant 回归测试（4/4）。
2. **P1 修复 RAG-02**：回滚/定时清理使用实际 attempt collection；补配置切换测试。
3. **P1 修复 RAG-03**：增加 Qdrant → MySQL 对账/告警；补丢失点恢复测试。
4. **P2 修复 MEM-01**：捕获唯一键异常转 409；补并发身份测试。
5. **P2 修复 CIT-01**：实时 SSE 复用 `CitationService` 校验。
6. **P2 补 TST-02**：Python → Spring 引用契约 E2E。
7. **P3 修测试统计**：MQ 测试改为可见的 skip/fail；保留每次运行的 XML；把探针/永久禁用测试从契约统计中分离。

**从 CONDITIONAL PASS 升级为 PASS 的最低条件**：

- ~~RAG-01 修复，且有真实 MySQL + fake Agent/Qdrant 的重叠 attempt 回归测试通过；~~
  **已完成**：`DocumentIndexFencingIntegrationTest` 4/4，Phase 2 IT 28/28。
- RAG-02、RAG-03 至少有一项修复或有明确的风险接受记录；
- 提供可复现的测试矩阵与 XML，证明 READY 重建失败、真实 MySQL 并发、真实 Qdrant 路径均已执行。
  **已满足**：最终单次 `mvn clean verify` 产出 Surefire 335 / 11 skipped 与 Failsafe 4 / 0；
  Memory 并发 IT 4/4 有独立 XML；Phase 2 路径 28/28 已执行。唯一未执行的是
  `AnalysisMqIntegrationTest` 10 条（Testcontainers Docker 检测失败，已显式 skip）。

---

## 7. 附录：关键代码索引

| 主题 | 文件:行 |
|---|---|
| Memory 单一状态入口 | `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:594-603` |
| Memory 更新与身份预检查 | `MemoryService.java:395-424` |
| Memory 迁移矩阵 | `backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java` |
| Memory 乐观锁插件 | `backend/backend/src/main/java/com/ails/backend/config/MybatisPlusConfig.java` |
| Memory 唯一键 | `backend/backend/src/main/resources/schema.sql:324` |
| 索引租约 | `DocumentIndexService.java:110-135` |
| 索引主流程 | `DocumentIndexService.java:140-180` |
| 失败回滚（RAG-01/02） | `DocumentIndexService.java:198-223` |
| 旧版本清理（RAG-01） | `DocumentIndexService.java:233-254` |
| 删除前 attempt fence | `DocumentIndexService.java:263-277` |
| 条件 complete/fail | `DocumentIndexService.java:281-...` |
| 定时清理 TOCTOU | `DocumentIndexCleanupService.java:62-...` |
| 恢复服务 | `DocumentIndexRecoveryService.java:85-130` |
| Agent 精确版本删除 | `AgentDocumentClient.java:155-...` |
| Qdrant 版本删除 | `agent/app/vectorstore/qdrant_store.py:312-...` |
| 检索版本校验 | `agent/app/retriever/knowledge.py:294-309` |
| READY 白名单 | `backend/backend/src/main/java/com/ails/backend/mapper/DocumentMapper.java:30-68` |
| 删除状态机 | `backend/backend/src/main/java/com/ails/backend/service/DocumentDeletionService.java` |
| 实时 SSE 引用 | `backend/backend/src/main/java/com/ails/backend/service/ChatService.java:275-292` |
| 历史引用重算 | `backend/backend/src/main/java/com/ails/backend/service/MessageService.java:53-68` |
| 引用可用性 | `backend/backend/src/main/java/com/ails/backend/service/CitationService.java:120-132` |
| Agent 引用解析 | `backend/backend/src/main/java/com/ails/backend/agent/HttpPythonAgentGateway.java:315-334` |
| ChatSse 集成测试 | `backend/backend/src/test/java/com/ails/backend/service/ChatSseIntegrationTest.java:75,365-410` |
| 真实 MySQL 并发 IT | `backend/backend/src/test/java/com/ails/backend/service/MemoryStateMachineConcurrencyIT.java:68` |
| MQ 不可见测试 | `backend/backend/src/test/java/com/ails/backend/mq/AnalysisMqIntegrationTest.java` |

---

**报告结束。** 原始审查轮未修改业务代码；2026-11-07 的状态更新仅落地 RAG-01 修复及其测试，
未执行 Git 提交。RAG-02、RAG-03 仍为未修复风险。
