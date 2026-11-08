# 已知限制与残余风险

- 整理日期：2026-11-07；封版追加：2026-11-08；E2 追加：2026-11-08；
  Chat 幂等 / 事务边界追加：2026-11-08；MEM-01 修复追加：2026-11-08
- 仓库版本：审查基线 `13cec31`；封版追加基于 `6f6e940`（P0/P1/P2 修复已合入）；
  E2 基于 `d7a8384`（E1 + E3 已合入）；CHAT-01/02 基于 `dc8a495`；
  MEM-01 基于 `2d1f6a4`
- 范围：三阶段改造（Memory 状态机、RAG 索引可靠性、Citation 引用闭环）以及
  Web 客户端 / 混合检索封版后仍然存在、但本轮**不修**的已知问题。
- 相关报告：[`THREE_PHASE_FINAL_REVIEW.md`](THREE_PHASE_FINAL_REVIEW.md)、
  [`PHASE2_RAG_RELIABILITY.md`](PHASE2_RAG_RELIABILITY.md)、
  [`PHASE3_CITATION_CLOSURE.md`](PHASE3_CITATION_CLOSURE.md)、
  [`TEST_EVIDENCE_MODES.md`](TEST_EVIDENCE_MODES.md)

> 说明：本文件只记录“当前代码的真实行为”。每条都写清影响范围、为什么现在不修、
> 以后往哪走，以及测试覆盖到什么程度。不要把“测试覆盖了相邻路径”写成“该问题已覆盖”。

---

## RAG-02：旧 collection 中的旧版本 points 可能永久残留

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（存储/成本；不影响当前检索正确性） |
| 状态 | 未修复 |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexService.java:175`、`:198-207`、`:233-253`；`DocumentIndexCleanupService.java:94-95`；`agent/app/vectorstore/qdrant_store.py:79` |

### 问题描述

索引版本与 collection 是两套维度：`index_version` 区分同一次配置下的新旧构建，
collection 由 `embedding model + dimension` 决定。当 Embedding 配置变化（尤其是
模型或维度变化）时，新构建会写入新的 collection，而 `document.vector_collection`
只在 `complete` 成功后才切到新 collection。

- 成功路径：`runIndex` 把 `validated.collection()`（**新 collection**）传给
  `cleanupOldVersions`；清理时按 MySQL 中列出的旧 `index_version` 去**新 collection**
  删除 points。旧版本 points 实际在**旧 collection**，删不到；随后 MySQL 旧 chunk
  记录被删除，旧 collection 中的 points 失去定位依据，成为永久孤儿。
- 回滚路径：`rollbackStagedVersion` 使用 `document.getVectorCollection()`（本次
  `runIndex` 开始时读到的**旧快照**）。如果本次 attempt 实际写入了新 collection，
  回滚删除也会打空；由于删除失败时保留 chunk 记录，这一路通常留下的是“可重试的
  残留”，而不是直接永久孤儿。
- 定时清理：`DocumentIndexCleanupService` 同样使用 `document.getVectorCollection()`
  （当前 collection），不会回头清理旧 collection。

### 影响范围

- 不造成“旧向量被召回”：检索按当前 collection + 活跃 `doc_version` 过滤，旧 points
  不参与召回。
- 造成 Qdrant 存储泄漏与成本上升；在频繁切换 Embedding 配置或多租户长期运行时更明显。
- 旧 collection 可能被反复写入/保留，运维难以判断哪些 collection 可删。

### 当前权衡（为什么现在不修）

- 彻底修复需要记录“每个 index_version 实际写入了哪个 collection”，即给
  `document_chunk` 或新增版本表加 `index_collection` 字段，并让清理按
  `(collection, versions)` 精确删除；这涉及 schema 迁移、Agent 删除接口和清理任务，
  范围大于 RAG-01 的“删除条件”修复。
- 当前优先级是“不误删新版本、不让旧版本参与召回”；RAG-02 只造成存储残留，
  不影响这两个目标，因此本轮选择记录而不扩大改造。
- 逐 attempt 记录 collection 也会增加每次索引的写入与迁移成本；在单机/小规模阶段
  收益低于成本。

### 后续方向

1. 给版本化索引增加 `index_collection`（按 `document_id + index_version` 记录），
   清理时按版本记录定位 collection。
2. 或让 collection 命名包含 Embedding 指纹（含 endpoint），并在切换配置时保留
   旧 collection 直到清理完成。
3. 增加离线对账任务：按旧 collection 扫描 `document_id`，与 MySQL 版本记录比对，
   删除无记录 points。
4. 迁移完成后删除空 collection，并记录 collection 生命周期。

### 测试覆盖

- **部分覆盖**：`DocumentIndexFencingIntegrationTest` 的 4 条测试覆盖“精确版本删除”
  与“旧 worker 不误删新版本”，但全部在**同一个 collection** 内构造，没有覆盖
  “模型/维度变化 → 新 collection”的场景。
- **无覆盖**：成功清理跨 collection、回滚跨 collection、定时清理跨 collection
  均无测试。
- 结论：不能把 Fencing 4/4 当作 RAG-02 已覆盖。

---

## RAG-03：没有 Qdrant → MySQL 的反向对账

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（可能静默丢失召回） |
| 状态 | 未修复 |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexRecoveryService.java`（全文）；`DocumentMapper.selectReadyDocuments`（`DocumentMapper.java:30-70`） |

### 问题描述

系统把 MySQL 当事实源、Qdrant 当派生存储，但只实现了“MySQL 状态/租约回收”，
没有实现“Qdrant 实际 points 是否还在”的反向检查。若 Qdrant collection 被误删、
points 丢失、或写入后未真正持久化，MySQL 中的文档仍可能是 `READY`，且
`selectReadyDocuments` 的 SQL 只检查 MySQL 侧的
`chunk_count = COUNT(index_version = active 且 vector_ref 非空)`，不会访问 Qdrant。

结果是：文档在 UI 上显示 READY，但检索返回空；没有告警、没有自动重建。

### 影响范围

- 静默召回缺失：用户以为文档已就绪，实际问答没有该来源。
- 单文档/单 collection 故障可能长期不被发现；多租户下影响面取决于 collection 划分。
- 不会造成错误召回（不会召回不存在的 points），但会造成“看起来正常、实际不可用”。

### 当前权衡（为什么现在不修）

- 反向对账需要 scroll/采样 Qdrant、与 MySQL 版本表逐条比对，涉及跨服务分页、
  大批量成本与幂等修复策略；当前阶段没有运维告警体系承接。
- 现有 `DocumentIndexRecoveryService` 已覆盖进程崩溃/租约超时等更常见的故障；
  Qdrant 整体丢失属于低频事件，本轮优先保证“不误删、不误召回”。
- 直接在每个检索请求里校验 Qdrant 会引入额外延迟，不适合作为常规路径。

### 后续方向

1. 增加定时对账任务：按 READY 文档抽样/分页 scroll Qdrant，比较
   `document_id + index_version + chunk_index` 集合与 MySQL。
2. 对账失败时把文档标记为 `FAILED`/待重建，而不是继续 READY；或触发自动重建。
3. 增加健康检查指标（READY 文档数 vs Qdrant points 数、空召回率）。
4. 对 collection 删除/重建加运维审计与告警。

### 测试覆盖

- **无覆盖**：没有 Qdrant points 丢失、collection 被删、MySQL READY 但 Qdrant 为空的
  集成测试。
- `DocumentIndexE2EIntegrationTest` 只验证“正常索引后 DB chunk 数 == Qdrant points 数”，
  不验证故障后的对账/恢复。
- 结论：RAG-03 是完全未覆盖的残余风险。

---

## RAG-04：legacy 文档没有 fingerprint，可能混入不同 endpoint 的向量

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（语义检索质量；不报错） |
| 状态 | **部分缓解（阶段 A）**：默认关闭的严格开关 + 只读观测 + 重建 runbook；风险本身尚未消除 |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/mapper/DocumentMapper.java`（`selectReadyDocuments` / `selectLegacyReadySummary` / `selectLegacyReadyDocuments`）；`backend/backend/src/main/java/com/ails/backend/service/DocumentService.java`（`listReadyDocuments` / `legacyReadyDiagnostics` / `observeLegacyReadyDocuments`）；`backend/backend/src/main/java/com/ails/backend/config/RetrievalProperties.java`；`agent/app/retriever/knowledge.py`；`agent/app/retriever/http_backend.py` |

### 问题描述

Embedding 指纹包含 provider、model、dimension、endpoint 等信息，但默认模式下
`selectReadyDocuments` 对 `d.embedding_fingerprint IS NULL` 的历史文档放行：
只要 `embedding_model` 相同，且 provider/dimension 为空或相同，就认为兼容。
同时 collection 名只由 `model + dimension` 决定，不含 endpoint。

因此，同一 model/dimension、不同 endpoint 的两批向量可能写入同一个 collection，
并被一起检索；legacy 文档没有 fingerprint，无法识别 endpoint 差异。结果不是报错，
而是召回质量静默下降（不同向量空间被当作同一空间）。

### 影响范围

- 历史数据在切换 Embedding 网关/endpoint 后可能混用向量。
- 只影响检索质量，不影响权限、状态机或引用身份。
- 新文档有 fingerprint，风险主要落在 legacy 行和未回填数据上。

### 阶段 A 已交付（本批）

1. **默认关闭的严格开关**
   - 后端：`ails.retrieval.require-embedding-fingerprint`
     （`AILS_RETRIEVAL_REQUIRE_EMBEDDING_FINGERPRINT`，默认 `false`）。
   - Agent：`AILS_REQUIRE_EMBEDDING_FINGERPRINT`，默认 `false`。
   - `false` 时行为与历史完全一致；`true` 时三层同时 fail closed：
     Backend SQL 精确指纹白名单 → Agent payload 校验 → Runtime 必须携带当前指纹。
2. **只读观测**
   - `ready-ids` 路径统计当前用户 + Space 内 fingerprint 为 NULL 的 READY 文档，
     日志输出 `legacyReadyDocs`、`spaceId`、`model`、`legacyModels`、
     `legacyCollections`。
   - 观测查询失败只记 warning，不影响检索；仓库没有 Micrometer，未新增指标依赖。
3. **诊断端点（与检索路径隔离）**
   - `GET /api/spaces/{spaceId}/documents/diagnostics/legacy`，强制 Space 所有权，
     跨用户 404；结果是结构化清单，仅供 owner/运维使用。
   - Agent 的正常检索只调用 `ready-ids`，**从不调用或消费诊断端点**；
     Java/Python 测试均守护该隔离。
4. **重建 runbook**
   - `docs/operations/rag-04-legacy-reindex.md`：前置条件、观测、逐文档
     `POST /api/documents/{id}/reindex`、批量循环、失败处理、回滚与验证。
   - 记录 `max-index-attempts=3`、`reindex-cooldown-ms=30000`。
5. **测试**
   - 9 场景严格模式矩阵（后端 SQL/Service/索引状态机 + Agent payload/版本/调用契约）；
   - 开关 false 的历史行为对照；
   - T4 不变量：`validateIndexResponse` 拒绝空 fingerprint；`complete()` 再守一道，
     即使未来新增路径绕过响应校验，也不能把空 fingerprint 写入 READY。

### 仍需阶段 B / 产品决策

1. **是否开启严格模式、何时开启**：涉及“未重建 legacy 文档暂时不可检索”，
   必须由产品签字并准备公告。
2. **失败文档的处理策略**：阻塞上线 / 按 Space 灰度 / 接受暂时不可检索；
   三选一需要写入变更单，不能由工程默认。
3. **生产规模未知**：仓库没有生产文档量、重建耗时、Qdrant collection 规模的
   可信数据；本阶段无法给出重建窗口估算。
4. **场景 2 是否真实发生仍是条件性的**：观测到“同 model 多 collection / 多
   endpoint”才说明风险已实际出现；目前只有检测手段，没有生产观测结论。
5. **collection 命名是否加入 endpoint 身份**、是否引入 `deployment_id` /
   `model_revision`：本批明确不做，留待阶段 B 评估。

### 指纹身份边界

`v1:sha256(provider|model|dimension|endpoint_identity)` 能区分 provider、model、
dimension 与规范化 endpoint 身份，但**不能**保证：

- 供应商静默升级模型权重时指纹不变，无法检测；
- 同一 URL 按 API key / tenant 路由到不同后端时，指纹相同但向量空间可能不同；
- 本批未引入 `deployment_id` / `model_revision`，无法表达部署级差异。

因此严格指纹降低混用概率，不等价于“证明向量空间一致”。

### 当前权衡（为什么仍默认 false）

- 直接要求 `embedding_fingerprint IS NOT NULL` 会让所有 legacy 文档立即退出召回，
  对已有用户是破坏性变更。
- 回填 fingerprint 需要重新索引，成本高；先提供观测、开关与 runbook，
  由产品选择窗口，避免“升级即不可用”。
- collection 名加入 endpoint 会改变现有 collection 布局，需要迁移与双写/切换方案，
  不适合作为本轮最小修复。

### 后续方向

1. 用诊断端点/日志确认风险规模，完成 legacy 文档重建。
2. 产品拍板失败策略与开启窗口，再翻转严格开关。
3. 评估 collection 命名加入 endpoint 指纹（或 fingerprint 前缀）。
4. 评估 `deployment_id` / `model_revision` 是否能覆盖“同 URL 不同租户”的边界。
5. 迁移完成后删除“fingerprint 为空也匹配”的非严格兼容分支。

### 测试覆盖

- **9 场景矩阵已新增并真实运行**：
  - 后端：`DocumentMapperSqlTest`（严格/非严格 SQL）、`DocumentServiceTest`
    （严格拒绝、观测失败不影响检索、诊断端点 owner 校验）、
    `DocumentIndexServiceTest`（T4 空 fingerprint 不变量、重建窗口场景 7-9）、
    `DocumentControllerTest`（严格 400、诊断路由与 404）。
  - Agent：`agent/tests/test_rag04_strict_fingerprint.py`
    （场景 1-9、开关 false 对照、诊断端点不被消费）。
- **仍未覆盖/未运行**：
  - 真实 MySQL 集成测试 `DocumentServiceIntegrationTest`、
    `DocumentIndexIntegrationTest` 需要 `AILS_DOCUMENT_IT=true` 与独立 MySQL；
    本轮环境未提供，已跳过，未声称运行。
  - 没有“同 model/dimension、不同 endpoint、legacy 无 fingerprint”的端到端生产数据验证。
- 结论：阶段 A 锁定了开关语义、fail-closed 行为与诊断隔离；RAG-04 的
  **数据层面风险仍存在**，直到 legacy 文档完成重建并开启严格模式。

---

## MEM-01：Memory 身份唯一键并发冲突返回 500（本批已修复）

| 项 | 内容 |
| --- | --- |
| 严重度 | 低-中（错误码 / 可用性；不丢数据） |
| 状态 | 已修复（方案 A：按唯一键名捕获并翻译为 409；修复基线 `2d1f6a4`） |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java`（`update` 的 `updateById` 外层、`translateIdentityConflict`）；schema `memory.uk_memory_identity` |

### 问题描述（修复前）

`MemoryService.update` 先调用 `requireIdentityFree(userId, scope, memoryKey, memoryId)`
做应用层查重，再 `memoryMapper.updateById(memory)`。查重与更新之间只有一次普通
SELECT，没有锁住目标身份：

- 两个并发 PATCH 同时把不同记忆改成同一 `(user_id, scope_key, memory_key)`；
- 两边都通过 `requireIdentityFree`；
- 其中一个 `updateById` 命中 `uk_memory_identity`，抛出 `DuplicateKeyException`；
- `update` 没有捕获该异常，全局处理器把它兜底成 500。

数据不会重复（唯一键兜底），但调用方看到的是服务器错误，而不是可识别的 409/冲突。

### 修复方案：方案 A（按唯一键名捕获）

在 `update()` 的 `updateById` 外层捕获 `DuplicateKeyException`，读取
`getMostSpecificCause().getMessage()`，**仅当 message 包含 `uk_memory_identity` 时**
翻译为 `BusinessException.conflict(...)`；其余情况原样抛出。最终响应复用仓库既有
的统一业务异常路径（`BusinessException` → `GlobalExceptionHandler` → `ApiResponse`），
没有新增异常类型或错误码枚举。

**为什么选择方案 A 而不是方案 B（`SELECT ... FOR UPDATE`）：**

- 目标身份尚不存在时，`SELECT ... FOR UPDATE` 只能拿到索引上的 gap 锁；gap 锁之间
  不互斥，两个并发事务仍会都通过预查，冲突最终还是会在 `UPDATE` 时以 1062 出现。
  因此方案 B 省不掉 `DuplicateKeyException` 兜底，反而增加锁等待与死锁风险。
- 方案 A 以数据库唯一索引作为最终裁判，只在异常翻译层区分约束，改动最小，
  与 `persist` / `insertCandidate` 已有“靠唯一键兜底”的语义一致。
- 失败安全：若 message 不含约束名，异常原样抛出，行为回退为原有 500；
  宁可 500 也不把外键 / 其他唯一键冲突误报成 409。

### 捕获范围

- **只识别** message 中含 `uk_memory_identity` 的冲突；其他唯一键、外键或无法识别
  约束名的完整性错误全部原样抛出，维持原有语义。
- MySQL 5.7 风格 `for key 'uk_memory_identity'` 与 MySQL 8.0 风格
  `for key 'memory.uk_memory_identity'` 都能命中（按索引名包含判断）。
- 当前 `memory` 表除主键外只有 `uk_memory_identity`；主键在更新时不会冲突，
  外键错误由 Spring 映射为其他 `DataIntegrityViolationException` 子类而不是
  `DuplicateKeyException`。未来新增唯一键时，只要键名不同就不会被误捕获。
- **风险：依赖异常 message 中的 constraint 名称，MySQL 版本升级需回归。**
  若升级后措辞变化导致识别失败，行为回退为 500（不会误报 409），
  因此升级流水线必须重跑本节的真实 MySQL 并发测试。

### 影响范围

- 并发编辑 / Agent 写入与用户 PATCH 竞争时，输掉的一方现在收到 409，而不是 500。
- 不改变状态机、不改变写入结果；唯一索引仍然保证不会产生重复身份。
- 响应体带可定位的 `scope_key` 与 `memory_key`，不包含任何用户 id。

### 后续方向

1. 若未来新增 memory 相关唯一键，复核 `translateIdentityConflict` 的捕获范围；
2. 评估把“查重 + 更新”改为数据库原子条件更新，进一步缩小竞争窗口（非必须）；
3. MySQL 大版本升级时把本节 IT 纳入回归清单。

### 测试覆盖

**真实 MySQL（MySQL 8.0.37，独立库 `ails_memory_it`，`AILS_MEMORY_IT=true`）**：
`MemoryStateMachineConcurrencyIT` 7/7 通过，其中 MEM-01 新增 3 条：

- `concurrentPatchToSameIdentityAllowsExactlyOne`：两个并发 PATCH 把不同 Memory 改成
  同一身份 → 恰好 `200 + 409`；数据库最终只有 1 行目标身份；输家保留原
  `memory_key` / `content` / `version`，不存在“scope 改了、key 没改”的半更新；
  409 响应体包含 `scope_key` 与 `memory_key`；
- `singlePatchToOccupiedIdentityReturns409`：单线程 PATCH 到已被占用身份 → 409 且不写库；
- `singlePatchToFreeIdentitySucceeds`：单线程 PATCH 到空闲身份 → 200 并正常落库。

**单元测试**：`MemoryUpdateIdentityConflictTest` 6/6 通过：

- 命中 `uk_memory_identity` → 409，message 含 `scope_key` / `memory_key`；
- 命中其他唯一键 → `DuplicateKeyException` 原样抛出（不转 409）；
- 其他完整性错误（无索引名 / 外键）→ 原样抛出；
- MySQL 5.7 风格 message 同样识别为 409；
- 空闲身份更新成功（正常路径未被误伤）；
- `create` / `persist` 创建路径仍映射为 409（未被破坏）。

**未覆盖**：MySQL 版本升级后 message 措辞变化的回归（需在升级时重跑上述 IT）；
其他数据库（本项目只支持 MySQL）。

**非身份唯一键冲突的并发场景**：当前 `memory` 表除主键外只有 `uk_memory_identity`，
`update` 路径没有其他会触发 `DuplicateKeyException` 的约束，因此没有伪造数据库
唯一键来构造并发用例；该场景以单元测试固定异常分类逻辑（见上）。

---

## CIT-01：SSE `message.end` 的引用可用性与历史读取不一致

| 项 | 内容 |
| --- | --- |
| 严重度 | 低-中（实时/历史展示不一致；不泄露权限） |
| 状态 | 已修复（方案 C：统一为「当前活跃版本可用」+ `isHistorical` 三态） |
| 代码位置 | `ChatService.java` 实时 `message.end`（约 331、357-361 行）；`CitationService.java` 版本盖章 / 批量 resolver；`CitationMetadata.java`、`CitationResponse.java`；`web/src/views/chat/ChatView.vue` |

### 原问题

SSE 结束时，`ChatService` 对本次检索得到的每条引用统一调用
`CitationResponse.from(citation, true)`，实时事件里的 `available` 永远是 `true`；
历史读取才重新判断。文档在「检索完成」和「发送 `message.end`」之间被删除、
转为非 READY、或 chunk 被重建清理时，实时与历史会给出不同结论。

### 修复内容

1. **统一 resolver**：`CitationService.resolve(userId, citations)` 成为实时与历史
   共用的判定入口。实时 `message.end` 不再硬编码 `true`；
   `resolveForMessages` 复用同一套 `loadLookups` / `toResponses`。
2. **批量查询**：一次 `selectBatchIds` 查文档、一次查 chunk，与引用条数无关；
   历史读取也是所有消息合并成两次批量查询，不产生 N+1。
3. **可用性收紧**：`available=true` 当且仅当引用身份合法、文档存在且属于当前用户、
   `document.status == READY`、chunk 存在且 `chunk.documentId == citation.documentId`、
   `chunk.indexVersion == document.indexVersion`。
4. **实时降级**：实时 resolver 抛异常时记录 warning 并降级为
   `available=true / isHistorical=false`，避免整条回答因引用查询失败而丢失。
   这是有意的可用性优先取舍，已记录在下方「已知边界」。
5. **版本快照**：写入前在 Spring 侧从 MySQL chunk 盖章 `indexVersion`；
   Agent / Python 跨语言契约不变，不要求 Agent 返回版本。
6. **前端三态**：`available / isHistorical` 组合渲染为当前、历史版本、来源已删除，
   不提供点击跳转。

### 最终语义

| 状态 | `available` | `isHistorical` | 前端展示 |
| --- | --- | --- | --- |
| 当前活跃版本 | `true` | `false` | 普通来源标签 |
| 历史版本（引用后重建过） | `false` | `true` | 灰色/蓝色标签 +「历史版本」 |
| 已删除 / 已清理 / 无法判断 | `false` | `false` | 红色标签 +「来源已删除」 |

`available=false, isHistorical=true` 只表达「当时确实引用过更早版本」，
**不表示旧 chunk 仍被保留或可打开**。

### 已知边界

- **不保留旧 chunk**：未引入 `retired_at` / `vector_cleaned_at` 等 DDL，
  未修改 `DocumentIndexService` / `DocumentIndexCleanupService` 的清理策略，
  因此不与 RAG-02 的存储残留问题冲突。
- **无 citation 表**：引用仍以 `message.citations` JSON 快照存储。
- **不可逆信息损失**：功能上线前的旧记录没有版本快照；若其 chunk 已被清理，
  无法区分「历史版本」与「来源已删除」，只能返回 `false/false`。
  这是方案 C 明确接受的代价。
- **窄竞态**：实时判定与历史读取之间仍可能因文档状态变化而不同；
  两端都只输出布尔值/派生标签，前端按布尔值渲染，不会出现协议错误。
- **滚动升级**：新 `CitationMetadata` 已加 `@JsonIgnoreProperties(ignoreUnknown = true)`；
  尚未升级的旧实例读取含 `indexVersion` 的 JSON 时仍可能因未知字段失败，
  但 `parse` 会降级为空引用，不影响消息列表整体可用。
- **完整方案 B 的路径仍保留**：若未来要求「历史版本内容可回看」，再引入
  版本化 chunk 保留与引用版本解析；本批不做。

### 测试覆盖

- **单元**：`CitationServiceTest` 覆盖 READY / PROCESSING / DELETING、
  版本一致 / 不一致 / null、旧版本 chunk 仍在、旧记录无快照（chunk 已清理 /
  仍存在兜底）、READY 重建失败、批量查询次数、旧 JSON 兼容。
- **ChatService**：`ChatServiceTest` 覆盖实时使用 resolver 结果、resolver
  异常降级、无引用不查库。
- **集成**：`ChatSseIntegrationTest`（`AILS_CHAT_SSE_IT=true` + 真实 MySQL）
  覆盖重建后历史标签、清理旧 chunk 后仍为历史、DELETING 不可用、
  READY 重建失败保持可用、实时与历史同数据状态一致。

---

## CIT-02：历史引用可用性只看“存在 + 归属”，不看 READY / 活跃版本

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（历史引用可能指向非活跃/不可检索来源） |
| 状态 | 已修复（方案 C，与 CIT-01 同一套 resolver） |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/service/CitationService.java`（`isAvailable` / `isHistorical`） |

### 原问题

`CitationService.isAvailable` 只校验文档存在、归属当前用户、chunk 归属匹配，
没有要求 `document.status == READY` 与
`chunk.indexVersion == document.indexVersion`。重建、FAILED、DELETING 窗口内，
历史引用可能仍被判定为 `available=true`。

### 修复内容

- 增加 `document.status == READY` 校验；
- 增加 `chunk.indexVersion == document.indexVersion` 校验；
- `vector_ref` 不重复校验：READY 文档的活跃版本由索引流程保证所有 chunk
  都有向量引用，检索白名单也已用 `chunk_count` 做过同一约束；
- 版本不一致的旧 chunk 不再算「可用」，但若快照证明它属于更早版本，
  返回 `isHistorical=true`，由前端显示「历史版本」而不是「已删除」。

### 剩余边界

- 旧记录无快照且 chunk 已清理时，无法判断是否历史版本，返回 `false/false`。
- 没有引入原因枚举；客户端只能依赖 `available` + `isHistorical` 两个布尔值。
- 完整保留旧版本的方案 B 不在本批范围，见 CIT-01「已知边界」。

---

## MIG-01：Phase 3 迁移依赖 `message.truncated`，旧库直接执行会 1054

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（升级失败；可手工绕过） |
| 状态 | 未修复 |
| 代码位置 | `backend/backend/src/main/resources/schema-migration-phase3-citations.sql`（`ADD COLUMN citations ... AFTER truncated`）；`schema-migration-phase7-truncation.sql` |

### 问题描述

Phase 3 的 citation 迁移使用：

```sql
ALTER TABLE `message`
  ADD COLUMN `citations` JSON DEFAULT NULL ... AFTER `truncated`;
```

`AFTER truncated` 要求 `message.truncated` 已存在。若目标库是 Phase 7 之前的旧库
（没有 `truncated`），迁移不会走到“列已存在则跳过”的分支，而是直接
`ERROR 1054 (42S22): Unknown column 'truncated' in 'message'`。

迁移脚本的前置检查只确认 `message` 表存在，没有检查 `truncated` 是否存在，也没有
自动先执行 Phase 7 迁移。

### 影响范围

- 从 Phase 7 之前的旧库升级到 Phase 3 引用功能时，迁移失败，后端无法写入 citations。
- 新库/已执行 Phase 7 的库不受影响。
- 失败是显式的（1054），不会静默写坏数据；但需要运维介入按正确顺序执行。

### 当前权衡（为什么现在不修）

- 当前开发/验证库已经包含 `truncated`，本轮迁移验证是在“当前 schema 去掉 citations”
  的库上做的，没有覆盖 Phase 7 之前的形态。
- 去掉 `AFTER truncated` 会让列顺序与 `schema.sql` 不一致；保留 `AFTER` 可以保持
  新旧库列顺序一致，便于对比。
- 迁移脚本已声明“手工执行、先备份”，把顺序责任交给运维；在现有部署规模下，
  先记录限制、在升级文档中写清顺序，比修改 DDL 的风险更低。

### 后续方向

1. 在迁移开头检查 `message.truncated` 是否存在；不存在时 `SIGNAL` 明确提示
   “先执行 `schema-migration-phase7-truncation.sql`”。
2. 或直接去掉 `AFTER truncated`，让 MySQL 追加到末尾，避免顺序依赖。
3. 提供统一的迁移编排脚本/文档，固定根 `README.md` 第 7.3 节的六步顺序
   （`document-rag` → `rag-reliability` → `memory-fulltext` → `phase7-truncation` →
   `phase3-citations` → `phase54-message-seq`）。
4. 增加“Pre-Phase7 schema”迁移测试库。

### 测试覆盖

- **部分覆盖**：Phase 3 迁移在专用库 `ails_phase3_migration_it` 上从
  “当前 schema 去掉 `message.citations`”升级，9/9 通过且幂等；该库有 `truncated`。
- **无覆盖**：没有“没有 `truncated` 的 Phase 7 之前旧库”升级测试。
- 结论：当前库形态覆盖，旧库顺序依赖未覆盖。

---

## WEB-01：前端未实现注册页与学习空间管理页

| 项 | 内容 |
| --- | --- |
| 严重度 | 低（上手成本 / 可用性；不影响后端能力） |
| 状态 | 未修复（MVP 范围） |
| 代码位置 | `web/src/router/index.ts`（无 `/register` 路由）；`web/src/views/LoginView.vue:124`（提示调用 `POST /api/auth/register`）；`web/src/views/PlaceholderView.vue`（`/spaces` 占位）；`web/src/api/auth.ts`（仅封装登录） |

### 问题描述

`web/` 已实现登录、SSE 对话、文档管理、记忆管理与学习状态查看，但没有注册页，
也没有学习空间的创建 / 编辑页面。新用户必须先通过 `POST /api/auth/register`
注册，再通过 `POST /api/spaces` 创建空间，之后才能在页面里完成登录和后续操作。
`LOCAL_DEMO_GUIDE.md` 第 4 节因此保留了 PowerShell / curl 的一次性初始化步骤。

### 影响范围

- 新用户无法在 UI 内完成自助注册与空间初始化，需要开发者或运维介入；
- 不影响已注册用户的登录、对话、文档、记忆与学习状态功能；
- 不影响后端 API 的完整性；注册与空间 CRUD 接口均已存在。

### 当前权衡（为什么现在不修）

- 当前定位是可本地复现的 MVP / 演示基线，优先保证登录后的主链路闭环；
- 注册页涉及用户名 / 密码 / 邮箱校验、错误码映射与风控策略，空间管理页还涉及
  成员、可见性与概念管理，均超出本轮“文档对齐、代码冻结”的范围；
- 用 API 完成一次性初始化成本低，且 `LOCAL_DEMO_GUIDE.md` 已提供可直接复制的命令。

### 后续方向

1. 增加 `/register` 页面，复用后端 `POST /api/auth/register` 的校验与 409 语义；
2. 增加学习空间管理页（列表 / 创建 / 编辑 / 删除），替换 `/spaces` 占位页；
3. 注册成功后自动登录并引导创建首个空间；
4. 补充前端表单校验与后端错误码的映射测试。

### 测试覆盖

- **无前端自动化覆盖**：仓库没有 Vue 组件 / E2E 测试；
- 登录守卫、退出登录、401 跳转由 `LOCAL_DEMO_GUIDE.md` 第 5 节手测清单验收；
- 注册与空间创建目前由 `LOCAL_DEMO_GUIDE.md` 第 4 节的 API 命令与
  `scripts/phase6-demo.sh` 覆盖，不是 UI 验收。

---

## DOC-01：文档删除时本地文件 IO 异常可能残留磁盘孤儿

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（存储 / 隐私残留；不影响检索与权限） |
| 状态 | 未修复（依赖人工巡检） |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/service/DocumentDeletionService.java`（`cleanup` 第 3 步）；`DocumentStorageService.java:243-273`（`deleteQuietly`）；`DocumentService.java:138`、`:146`（上传失败清理） |

### 问题描述

文档删除采用「先退出检索、再异步清理」：`DocumentDeletionService.cleanup`
依次删除 Qdrant 向量、MySQL chunk 和本地文件，最后把文档标记为 `DELETED`。
文件删除走 `DocumentStorageService.deleteQuietly`，该方法**吞掉 `IOException`**，
只记录一条 `log.warn`，不会让整个清理失败或让文档停留在 `DELETING`。

因此，如果本地磁盘在删除瞬间不可写、文件被外部进程占用、权限变化或文件系统
异常，数据库记录会正常软删，但物理文件仍留在存储根目录。上传失败后的
`deleteQuietly` 也是同样的 best-effort 语义。

### 影响范围

- 磁盘空间泄漏；被删除文档的原文可能继续留在服务器上；
- 不造成检索召回：数据库 `deleted_at` 与 `status=DELETED` 已使其退出白名单；
- 不造成跨用户越权：文件路径仍受存储根目录约束，且没有对应元数据入口；
- 在多租户或合规场景下，属于需要定期审计的残留风险。

### 当前权衡（为什么现在不修）

- 数据库是事实源；如果因为文件删除失败就让文档永久停留在 `DELETING`，
  会让用户看到无法完成的删除，并反复触发清理；
- 当前没有文件系统索引或对象存储清单，无法在删除请求内判断“哪些文件已无主”；
- 引入定时扫描需要定义宽限期、并发安全、误删保护与告警通道，超出封版范围。

### 后续方向

1. 增加定时对账任务：扫描存储根目录，与 `document.storage_uri`（含软删行）比对，
   删除超过宽限期且无元数据引用的文件；
2. `deleteQuietly` 失败时写入待清理清单 / 指标，而不是只写日志；
3. 对象存储场景优先使用生命周期策略与版本化删除；
4. 为 IO 异常路径补充可注入的文件系统故障测试。

### 测试覆盖

- **部分覆盖**：`DocumentStorageServiceTest` 覆盖正常删除与越界路径拒绝；
  `DocumentDeletionServiceTest` 覆盖删除流程会调用 `deleteQuietly`；
- **无覆盖**：`IOException` / 权限不足 / 文件占用导致残留的分支没有自动化断言；
- 没有磁盘文件与数据库记录的定期对账测试。

---

## DDL-01：`schema.sql` 随应用启动执行，生产环境缺少迁移框架

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（部署 / 升级风险；开发环境是有意设计） |
| 状态 | 未修复（开发便利优先） |
| 代码位置 | `backend/backend/src/main/resources/application.yml`（`spring.sql.init.mode: always`、`schema-locations: classpath:schema.sql`）；`backend/backend/src/main/resources/schema.sql`；`schema-migration-*.sql` |

### 问题描述

后端每次启动都会执行 `schema.sql`。脚本主体是 `CREATE TABLE IF NOT EXISTS`，
另有一条针对 `agent_run.status` 枚举的幂等 `ALTER`；因此对全新库友好、可重复执行。
但它**不会为已存在的表补列 / 加索引 / 改普通列类型**，已有库升级仍必须手工执行
`schema-migration-*.sql`。仓库没有 Flyway / Liquibase，也没有 schema 版本表。

### 影响范围

- 生产部署要求应用账号具备 DDL 权限，权限面偏大；
- 多实例同时启动时可能并发执行 DDL，增加锁等待与启动抖动；
- 没有版本化迁移记录，无法回答“当前库执行到哪一版”，也无法自动回滚；
- 新库与旧库可能因漏跑迁移脚本产生结构漂移，错误通常在运行期才暴露；
- 开发 / 演示环境因此获得“零手工建库”的便利，这是当前有意保留的权衡。

### 当前权衡（为什么现在不修）

- 封版阶段不引入迁移框架，避免 baseline、历史脚本重写与 CI 流程改造；
- 现有手工迁移脚本已按 `information_schema` 做幂等检查，可在小规模部署中工作；
- 迁移框架需要与现有 `schema.sql`、测试库初始化和多环境配置统一，属于独立工程任务。

### 后续方向

1. 引入 Flyway 或 Liquibase，以当前 `schema.sql` 作为 baseline，后续变更走版本化脚本；
2. 生产 profile 设置 `spring.sql.init.mode=never`，开发 profile 保留启动建表；
3. 应用账号与迁移账号分离，运行时账号收回 DDL 权限；
4. 在 CI 中增加“空库 → 全量迁移 → 结构比对”的流水线。

### 测试覆盖

- **无迁移框架测试**：`DocumentSchemaConsistencyTest` 只静态检查
  `schema-migration-document-rag.sql` 与当前 schema 的部分一致性；
- 各迁移脚本的幂等性由审计报告中的手工验证记录覆盖（如 Phase 3 的 9/9），
  但没有纳入自动化回归；
- 没有多实例并发启动执行 DDL 的测试。

---

## DIST-01：Agent 的 Memory 版本防回退使用进程内锁

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（多实例下的数据一致性风险；单实例安全） |
| 状态 | 未修复（当前单进程部署） |
| 代码位置 | `agent/app/vectorstore/memory_store.py:458`（`_memory_lock`）；`upsert_memory` / `delete_memory` 内的 `async with self._memory_lock(memory_id)` |

### 问题描述

Memory 向量写入采用「读取现有 payload → 比较 `memory_version` → 写入 / 墓碑」
的读改写流程，并用按 `memory_id` 分配的 `asyncio.Lock` 串行化同一进程内的并发。
该锁是**进程内锁**：只对同一个 Python 进程 / 事件循环生效。

如果 Agent 以多实例部署，两个实例可能同时处理同一 `memory_id` 的不同版本事件：
两边都读到旧版本、都通过版本比较，后写入者会覆盖先写入者。当前写入路径没有
“按 payload 版本条件写入”的原子 CAS，因此进程内锁无法跨实例提供排序保证。

### 影响范围

- 单实例部署：正确性由锁 + 版本比较 + 墓碑保证；
- 多实例部署：乱序事件可能导致旧版本覆盖新版本，或墓碑被迟到写入绕过；
- 不影响权限与 MySQL 事实源；回源复核仍会以 MySQL 为准，但向量排名可能短暂失真；
- 当前部署模型是单进程，因此该风险尚未在生产路径暴露。

### 当前权衡（为什么现在不修）

- 引入 Redis 分布式锁会增加一个强依赖及其可用性、超时、续租与故障转移问题；
- 锁的粒度、TTL、fencing token 与重试语义需要与 Outbox 的至少一次投递模型一起设计；
- 当前单实例演示 / 本地基线没有多实例需求，封版阶段不扩大基础设施范围。

### 后续方向

1. 使用 Redis（或数据库 advisory lock）实现按 `memory_id` 的分布式互斥，
   锁内完成读取、版本比较与写入，并设置合理 TTL 与续租；
2. 在锁值中加入 fencing token，写入前校验 token，防止过期持有者继续写；
3. 或改造存储层为支持条件更新的后端（例如带版本条件的 CAS / 事务）；
4. 增加多进程 / 多实例并发写入同一 `memory_id` 的集成测试。

### 测试覆盖

- **单进程覆盖充分**：`agent/tests/test_memory_store_versioning.py` 覆盖
  V2 后到 V1 丢弃、墓碑阻止复活、删除先于首次写入、旧 DELETE 不降低墓碑版本；
- **无多实例覆盖**：没有跨进程 / 跨实例的并发写入测试；
- 结论：单实例行为已验证，多实例一致性未被测试证明。

---

## EVD-01：跨概念 Evidence 召回目前只做到“受控一跳关系扩展”

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（影响证据覆盖范围，不改变 Evidence 的归属与写入语义） |
| 状态 | 部分缓解（E1 + E2 + E3）；多跳 / 真实关系数据 / 真实模型评测仍未覆盖 |
| 代码位置 | `agent/app/runtime.py`（`_retrieve` / `_select_expansion_concepts` / `_mark_cross_concept_evidences` / `_safe_evidences`）、`agent/app/retriever/base.py`（`EvidenceStatus` / `EvidenceRetrievalResult`）、`agent/app/retriever/http_backend.py`（`get_evidences` / `get_related_concepts`）、`agent/app/state/agent_state.py`、`agent/app/context/builder.py`（`evidence_status` / `related_evidence` 区块）、`backend/backend/src/main/java/com/ails/backend/service/ConceptRelationService.java`、`ConceptRelationMapper.java`、`EvidenceService.java`（`listRecentByConcepts`）、`EvidenceController.java`、`LearningEvidenceMapper.java`、`EvidenceSourceMapper.java` |

### 问题描述

改造前，Evidence 检索只按当前消息识别出的 `conceptId` 查询。当用户只提到
「导数」时，作为前置的「极限」上已有的历史证据不会进入上下文，跨概念证据完全缺失。

E1 增加了**受控一跳扩展**：

- 只在 Evidence 检索被触发时（`_wants_evidence`）执行，不改变普通聊天路径；
- 使用现有 `concept_relation` 表，不新建图谱表、不替换关系模型、不引入 Qdrant
  Evidence 向量索引；
- 只接受 `PREREQUISITE`（且必须是从当前概念看来的 `INCOMING`）与 `RELATED`
  （双向）；
- 最多扩展 `evidence_related_concepts_limit`（默认 3）个概念，扩展概念按
  `conceptId` 去重并排除本轮已识别概念；
- 跨概念 Evidence 使用独立预算 `evidence_related_evidence_limit`（默认 2），
  不挤占原始概念的 `evidence_top_k`；
- 跨概念 Evidence 在 Agent 侧标记 `is_cross_concept` / `source_concept_id` /
  `related_via`，上下文单独渲染为「相关概念判定依据（历史参考）」，并明确禁止
  用它改写当前概念的 Understanding；`concept_id` 始终是证据的真实归属。

E3 在本批补齐了检索状态与有界批量读取，并消除了 E1 引入的串行往返：

- Agent 的 `get_evidences` 返回 `EvidenceRetrievalResult`，显式区分
  `SUCCESS` / `EMPTY` / `DEGRADED`；连接失败、响应缺失、响应格式异常或部分
  条目解析失败统一为 `DEGRADED` + warning，部分已解析条目仍保留；
- `runtime.py` 把状态与 warning 写入 `AgentState` 和 RETRIEVE trace；
  `ContextBuilder` 输出「本轮证据检索状态」区块，并在没有认知记录时使用
  「本轮证据检索发生降级，不能据此判断学习者没有相关历史」的措辞，
  **不再落成「没有任何历史认知记录」**；
- 后端新增 `GET /api/evidences?spaceId=&conceptIds=&limit=`：服务端校验
  `spaceId` 属于当前用户、`conceptIds` 属于该空间；SQL 内完成
  `ORDER BY created_at DESC, id DESC` + `LIMIT`，Sources 用一次
  `WHERE evidence_id IN (...)` 批量加载，不再逐条 `loadSources`；
  `limit` 默认 3、服务端封顶 50；旧单概念接口保留兼容；
- Runtime 第一轮检索把 Knowledge / Understanding / Memory / 原始 Evidence /
  相关概念查询并行发出。原始 Evidence 不等待关系结果，关系查询失败也不会
  阻塞其余四路；只有跨概念 Evidence 查询依赖关系结果，并与原始结果按
  evidence id 合并去重。

E2 在本批新增**结构化 Concept Resolver**，补规则识别的同义表达与模糊指代：

- 模块：`agent/app/retriever/concept_resolver.py`；Runtime 在
  `detect_concepts` 之后、构造 `concept_ids` 之前调用它。规则检测器本身未改写。
- 输入：`user_id` / `space_id`（只用于日志与作用域，不写进提示词）、当前消息、
  最近 N 轮对话（默认 4 条消息，单条截断 160 字符）、当前 Space 候选概念
  （`conceptId + name + canonical + aliases + 简要描述`）。
- 输出：严格 JSON
  `{current_concepts:[{conceptId,confidence,reason}], related_concepts:[...],
  needs_history_reference:bool, intent:"knowledge_question|prerequisite_question|
  review_request|chitchat|other"}`。`current_concepts` 与 `related_concepts`
  是两个独立字段，服务端不会把它们混在一起。
- 服务端确定性校验（不信任模型）：`conceptId` 必须是本次实际呈现的**当前 Space
  候选列表**中的 id（编造 / 跨 Space 一律丢弃并记 warning）；`confidence`
  低于 `concept_resolver_confidence_threshold`（默认 0.6）丢弃；每字段最多
  `concept_resolver_max_concepts_per_field`（默认 3，配置上限也是 3）；
  `related` 与 `current` 重复项从 `related` 移除；非法 `intent` 归为 `other`；
  当前意图不允许依赖历史时，`needs_history_reference=true` 被强制改回 `false`。
- 合并顺序：规则结果优先，Resolver 只补充规则未命中的 `current_concepts`；
  所有 Resolver 概念在 `matched_on` 中标为 `resolver:<reason>`，可追溯。
- 与 E1 的安全边界：`related_concepts` **不进入** `concept_ids`，也不直接决定
  Evidence 查询范围；它只作为 `_select_expansion_concepts` 的稳定排序提示，
  在 `concept_relation` 真实返回的候选里把命中的 id 排到前面。不在关系结果里的
  Resolver 提示会被忽略，方向过滤、去重、数量上限完全不变；关系表与 Resolver
  冲突时以关系表为准（`tests/test_concept_resolver.py::
  test_related_hint_cannot_bypass_relation_table`）。
- 按需调用策略（每轮最多一次）：

  | 规则命中 | intent | 是否调用 | 说明 |
  | --- | --- | --- | --- |
  | 是 | `prerequisite_question` / `review_request` | 是 | 规则命中也可能漏掉前置 / 复盘对象 |
  | 是 | 其他 | 否 | 普通知识问题不额外花一次调用 |
  | 否 | `knowledge_question` / `prerequisite_question` / `review_request` | 是 | 补同义表达与模糊指代 |
  | 否 | `chitchat` / `other` | 否 | 「你好」「继续」「嗯」与非知识问题不调用 |

- 预算与超时：单轮提示词预算 `concept_resolver_max_prompt_tokens=1600`
  （超预算按「候选 → 历史 → 当前消息」顺序收缩，校验白名单与呈现列表一致）；
  完成侧 `max_tokens=400`；硬超时
  `concept_resolver_timeout_seconds=5.0`；候选最多 60 个。关闭
  `concept_resolver_enabled` 即完全退化为 E1 行为。
- 降级语义：`NOT_CALLED`（策略决定不调用）/ `SUCCESS` / `DEGRADED`
  （超时、模型错误、非法 JSON、非对象 JSON）/ `FAILED`（Resolver 自身未预期
  异常）。`DEGRADED` / `FAILED` 不阻塞主链路：规则结果、E1 扩展、Evidence 三态
  全部照常；状态写入 `AgentState.resolver_status` / `resolver_warning` 与
  `CONCEPT_RESOLVE` trace。ContextBuilder 对降级输出「本轮概念解析状态」区块，
  并在没有认知记录时使用「本轮概念语义解析未完成，规则识别可能漏掉概念，
  不能据此判断学习者没有相关历史」的措辞，**不会落成普通的「没有任何历史认知
  记录」**。
- 离线评测（`agent/tests/eval/`，9 个用例 × 3 种模式，每用例重复 3 次）：

  | 模式 | Recall | Precision | 误召回率 | P95 prepare 耗时 | 平均 Resolver Token | Resolver 调用率 |
  | --- | --- | --- | --- | --- | --- | --- |
  | 仅规则 | 0.385 | 1.000 | 0.000 | 9.82 ms | 0 | 0.000 |
  | 规则 + E1 | 0.692 | 1.000 | 0.000 | 9.19 ms | 0 | 0.000 |
  | 规则 + Resolver + E1 | 1.000 | 1.000 | 0.000 | 10.54 ms | 564.4 | 0.778 |

  报告：`agent/tests/eval/reports/20261108T133221622730Z.json`。
  **必须诚实解读**：Resolver 输出来自脚本化 LLM（按用例预置 JSON），不是真实
  模型；Token 是 `estimate_tokens` 估算值；延迟是本地 `prepare()` 墙钟耗时，
  不包含真实模型 / 网络延迟。该表证明的是「管线接线、确定性校验、跨 Space
  隔离与 E1 边界正确」，**不能**当作真实模型的概念识别准确率。真实模型效果
  尚未评测。

仍未解决的问题：

1. **关系数据本身可能为空**：仓库内没有 `concept_relation` 的写入路径、Service
   或种子数据；E1 只提供读取与扩展链路。若表里没有关系，扩展不会发生。
2. **只支持一跳**：不会沿「极限 -> 导数 -> 微分」继续传递；多跳、权重衰减、
   环路处理都未实现。
3. **方向语义是假设而非数据库约束**：DDL 只声明 `source_concept_id` /
   `target_concept_id`，没有方向说明。E1 采用的假设是
   `PREREQUISITE(source=A, target=B)` 表示 A 是 B 的前置，并已用后端与 Agent
   测试钉住；它不是由表结构强制保证的。
4. **Concept Resolver（E2）已实现，但真实模型效果未验证**：规则识别仍是第一条
   防线，Resolver 只在按需策略判定时补充。离线评测使用脚本化 LLM，只能证明
   管线与校验正确；真实模型在跨语言、长尾别名、多概念歧义下的召回 / 误召回
   仍需在真实模型与真实 Space 数据上评测。若 Resolver 返回空或识别错，E1 仍会
   从错误起点扩展，E1 本身不能纠正概念识别错误。
5. **Resolver 的 related 只是排序提示，不是关系补全**：它不能新增
   `concept_relation` 里不存在的关系；关系表为空时，Resolver 再准确也无法触发
   跨概念扩展。反之，关系表与 Resolver 冲突时以关系表为准。
6. **Resolver 调用仍是同步开销**：命中调用策略时，本轮会多一次 LLM 往返
   （硬超时 5 秒），没有跨轮缓存、批处理或流式；降级保证不阻塞，但不保证零延迟。
   每轮最多一次调用，未做多轮迭代或自我纠错。
7. **历史证据仍不能完整回看（已部分实现）**：E3 已统一检索状态并实现
   「SQL 内 LIMIT + 批量 Sources」的有界查询，但接口只返回最近 N 条
   （默认 3、服务端上限 50），没有分页游标；需要完整回看用户历史证据时，
   仍缺分页接口。旧单概念接口为兼容保留，未分页。
8. **失败语义已区分，但未重试**：关系查询失败返回 `FAILED` + warning，原始概念
   Evidence 继续；没有重试、熔断或跨请求缓存。

### 影响范围

- 有正确关系数据时，能补充「当前概念的前置 / 相关概念」的历史证据，降低
  「只看见当前概念、看不见基础概念」的盲区；
- 没有关系数据、关系方向填反、或概念识别失败时，效果与改造前一致；
- 跨概念证据只作为历史参考进入上下文，不能改变 Evidence 的 `conceptId` 归属，
  也不能改写当前概念的 Understanding；
- E2 提升「用户其实在问哪个概念」的召回：同义表达（「求导」→「导数」）与
  模糊指代（「那个东西」）在规则未命中时由 Resolver 补候选，但候选必须经过
  当前 Space 白名单、置信度阈值与数量上限校验；
- E2 的 `related_concepts` 只能影响 E1 真实关系候选的排序，**不能扩大** Evidence
  检索范围；关系表仍是唯一的多概念扩展依据；
- Resolver 降级（超时 / 5xx / 非法 JSON）时，本轮退化为规则 + E1，Evidence
  三态语义不变；上下文会明确标记「概念解析未完成」，不会误报「没有历史」；
- 不涉及写入链路，不改变后端 Evidence / Understanding 的持久化语义。

### 当前权衡（为什么只做这一批）

- E2 采用「规则优先 + Resolver 按需补充」而不是替换规则识别：规则版本零成本、
  可预测，且已有测试；直接换成 LLM 会让每轮都多一次调用与不可预测的输出；
- Resolver 的 `related` 只做 E1 排序提示，不直接发起 Evidence 查询：否则 LLM
  就事实上决定了检索范围，绕过 `concept_relation` 的唯一真相；
- 服务端校验、阈值、每字段上限、超时、Token 预算都放在 Python 侧，不依赖模型
  自觉；这是本批安全边界的一部分；
- 离线评测使用脚本化 LLM 而不是真实模型：它能在 CI 中稳定复现管线行为，
  但真实模型质量需要另行评测，不能把脚本化满分当成模型能力；
- 引入 Qdrant Evidence 向量索引、Graphiti / Mem0 / 新图数据库都会扩大存储与
  一致性边界，且会绕过现有 `concept_relation` 的唯一真相；
- 多跳扩展需要权重、环路、时效与预算模型，当前没有真实关系数据支撑校准，
  贸然实现只会制造不可解释的召回；
- 批量 Evidence 接口只做「最近 N 条」的有界查询，不引入游标分页：Agent 的
  Evidence 是低预算按需召回，默认 `evidence_top_k=3`，完整历史回看不是本轮
  对话链路的职责。

### 后续方向

- E2 后续：在真实模型与真实 Space 数据上评测 Resolver 的召回 / 误召回、Token
  与延迟，按语料校准 `concept_resolver_confidence_threshold`；评估是否需要
  跨轮缓存或批量调用；当前不替换规则识别，也不允许 Resolver 直接决定检索范围；
- 为 `concept_relation` 建立受控写入路径（人工维护或分析链路提议 + 审核），
  明确方向语义并补数据校验；
- 在需要完整回看历史证据时，为 Evidence 批量接口增加游标分页；
- 在上述基础上再评估多跳扩展与是否需要独立的 Evidence 向量索引。

### 测试覆盖

- `agent/tests/test_evidence_related_concepts.py`：
  仅识别「导数」时扩展到前置「极限」；多概念时原始检索不变；无关系数据不扩展；
  查询按请求 user / space 发出；扩展数量上限、去重、原始概念优先；Evidence
  未触发时不查询关系；关系查询失败时原始 Evidence 继续且 warning 可见；
  `PREREQUISITE` 方向假设被显式测试钉住；跨概念证据单独渲染并带标记；
  HTTP 客户端参数 / 解析 / PARTIAL / FAILED 语义；`limit=0` 关闭扩展；
- `backend/.../ConceptRelationServiceTest.java`：方向、跨 Space 排除、跨用户
  404、默认类型与 limit 裁剪、显式类型过滤、非法类型 400、软删概念跳过；
- `backend/.../ConceptRelationMapperSqlTest.java`：静态校验 SQL 同时约束两端
  `learning_space_id`、软删、类型过滤、limit 与方向 CASE；
- `backend/.../SpaceControllerConceptRelationTest.java`：HTTP 字段契约与跨用户
  404；
- `agent/tests/test_evidence_status_and_parallel.py`：Evidence
  `SUCCESS` / `EMPTY` / `DEGRADED` 三态传播到 state 与 ContextBuilder；DEGRADED
  输出「不能据此判断没有历史」且不落成普通无历史措辞；跨概念降级可见；四路检索
  同时到达栅栏（证明并行）；关系失败不阻塞原始 Evidence / U / M；原始与跨概念
  Evidence 合并去重、跨概念标记不变；HTTP 客户端只发一次有界批量请求、空结果 /
  失败 / null / 部分解析的状态语义；
- `backend/.../EvidenceBatchQueryTest.java`：服务层一次 LIMIT 查询 + 一次 IN
  Sources，不逐条 `loadSources`；跨 space / 跨用户 / 空 conceptIds 拒绝；默认与
  上限裁剪；conceptIds 去重；
- `backend/.../EvidenceMapperBatchSqlTest.java`：静态校验证据 SQL 含
  `user_id`、`concept_id IN`、`ORDER BY created_at DESC, id DESC`、`LIMIT ?`，
  Sources SQL 为一次 `evidence_id IN`；
- `backend/.../EvidenceControllerBatchTest.java`：`GET /api/evidences` 路径、
  逗号分隔 conceptIds、可选 limit、sources 字段与跨用户 404；
- `agent/tests/test_concept_resolver.py`：编造 conceptId 丢弃；跨 Space
  conceptId 丢弃；置信度阈值过滤；`current` / `related` 字段分离；规则命中 +
  普通问题不调用；规则未命中 + 前置问题调用并把 `current_concepts` 并入
  `concept_ids`；超时 / LLM 错误 / 非法 JSON 降级且主链路继续；Resolver 的
  related 提示无法绕过 `concept_relation`（999 不在关系结果中 → 不扩展、不查
  证据）；每字段最多 3 个；不允许依赖历史时 `needs_history_reference` 被强制
  改回 false；未知 intent 归为 other；按需调用决策表；**真实降级复现**（真实
  `MockLLMAdapter` + 真实 Runtime：`resolver_status=DEGRADED`，原始 Evidence
  仍为 `SUCCESS`，上下文不落成「没有历史」，`stream()` 以 `done` 结束）；
- `agent/tests/eval/test_eval_repeatable.py`：9 个评测用例全部有断言并通过；
  同一数据集两次运行的非延迟结果完全一致；用例覆盖前置 / 相关 / TCP / TLB /
  同义表达 / 模糊指代 / 跨 Space / 闲聊 / 规则命中不调用；
- 未覆盖：真实模型下的 Resolver 准确率 / Token / 延迟；真实
  `concept_relation` 数据写入；多跳扩展；概念识别错误的自动纠错；真实 MySQL
  上的分页与批量查询执行计划 —— 这些属于真实模型评测、数据治理或需要独立
  MySQL 的集成环境。

---

## CHAT-01：Chat POST 无幂等键（本批已修复，残余风险见下）

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（重复消息 / 重复生成 / 重复计费） |
| 状态 | 已修复（本批）；崩溃遗留的 RUNNING run 仍无补偿 |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/dto/conversation/MessageRequest.java`；`service/ChatService.java`（`stream` / `replay`）；`service/ChatTurnService.java`；`common/ChatReplayException.java`；`common/GlobalExceptionHandler.java`；`mapper/MessageMapper.java`、`mapper/AgentRunMapper.java`；`schema.sql`、`schema-migration-chat-idempotency.sql` |

### 问题描述与修复

`POST /api/conversations/{id}/messages` 现在要求必填 `clientRequestId`
（UUID 字符串，8–64 字符），并在数据库层用
`UNIQUE(conversation_id, client_request_id)` 兜底。唯一键只取
`conversation_id + client_request_id`，不含 `user_id`：`conversation.id` 全局唯一，
且每次写入前都校验对话归属，加入 `user_id` 只会扩大索引、不增加约束力；
`message` 表本身也没有 `user_id` 列。

重放不重新生成，直接以 JSON 提前返回（`ChatReplayException` +
`@ExceptionHandler`，强制 `application/json`，避免被请求的
`Accept: text/event-stream` 协商成流式）：

| 状态 | 数据条件 | HTTP | `state` / 关键字段 | 是否重跑 |
| --- | --- | --- | --- | --- |
| A 处理中 | USER 消息 + run ∈ `RUNNING/PENDING/STALE` | **202** | `PROCESSING`，`message=请求处理中，请勿重复提交`，带 `userMessageId/runId` | 否 |
| B 已完成 | USER + run ∈ `COMPLETED/TRUNCATED` + 助手消息存在 | **200** | `COMPLETED` / `TRUNCATED`，带 `userMessageId/assistantMessageId/runId/runStatus` | 否 |
| C 数据不一致 | USER 消息存在但无 run | **409** | `INCONSISTENT`，`runId=null`，提示换新 id | 否 |
| 失败 | run=`FAILED` 且无助手消息 | **409** | `FAILED`，提示换新 id | 否 |
| 内容冲突 | 同 id、不同 `content` | **409** | `CLIENT_REQUEST_CONFLICT` | 否 |
| 终态不一致 | run 终态但助手消息缺失 / 不可见 | **409** | `INCONSISTENT` | 否 |

所有重放响应都带 `idempotentReplay=true`。实现上先 `findExisting`，命中即回放；
并发时输掉 `UNIQUE` 插入的一方捕获 `DuplicateKeyException`，再回查赢家回放。
数据库唯一键同时写在 `message`（仅 USER 消息写入该列）和 `agent_run`
（仅 CHAT run）上，MySQL 唯一索引允许多个 NULL，因此 ASSISTANT 消息与
ANALYSIS run 不受影响。

### 残余风险（为什么仍保留在已知限制里）

- **进程崩溃窗口**：预创建事务提交后、生成线程启动前若进程崩溃，会留下一条
  永久 `RUNNING` 的 CHAT run。当前没有 CHAT run 的 STALE 补偿扫描（只有
  ANALYSIS 有），该 `clientRequestId` 会一直命中 A 态返回 202。恢复方式是换一个
  `clientRequestId` 重新提问；自动补偿需要定义「多久算遗留」与安全抢占语义，
  不在本批范围。
- **无 Web 入口限流 / 请求体上限**：DTO 的 `content ≤ 8000` 与
  `clientRequestId` 长度校验只是第一道防线。真正的请求体大小限制
  （`server.max-http-request-size` 或网关）与按用户/对话的速率限制**尚未实现**，
  属于 TODO；当前超长 JSON 仍会完整读入内存后才被校验拒绝。
- `clientRequestId` 为必填是**有意的破坏性变更**：仓库内没有需要兼容的旧客户端；
  服务端随机生成会彻底破坏「客户端重试」的语义。旧客户端不传该字段会得到 400。

### 测试覆盖

真实 Tomcat + 真实 MySQL（`ChatSseIntegrationTest`）：

- `completedReplayReturns200AndDoesNotWrite`：B 态 200，id 与首次一致，消息/run 不增加；
- `inFlightReplayReturns202Processing`：A 态 202 `PROCESSING`，不返回 200；
- `userMessageWithoutRunReturns409Inconsistent`：C 态 409，不补 run、不重跑；
- `sameClientRequestIdWithDifferentContentReturns409`：409 `CLIENT_REQUEST_CONFLICT`；
- `differentClientRequestIdsCreateSeparateTurns`：不同 id 正常创建第二轮；
- `concurrentSameClientRequestIdCreatesExactlyOneTurn`：两个并发 POST，一个 SSE 赢家、
  一个 202，最终 1 USER / 1 ASSISTANT / 1 run；
- `duplicateClientRequestIdRejectedByDatabaseConstraint`：直接重复插入命中
  `uk_msg_conv_client_request`；
- `invalidRequestFieldsReturn400`：缺 id / 长度越界 / 8001 字符内容均 400 且不落库；
- `ChatServiceTest` 两条单测覆盖 B 态 200 与 A 态 202 的回放分支不调用 Agent。

---

## CHAT-02：用户消息与 Run 创建缺少事务边界（本批已修复）

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（留下「有提问、无回答、无 run」的半截数据） |
| 状态 | 已修复（采用方案 1：预创建事务） |
| 代码位置 | `service/ChatTurnService.java`（`prepare`）；`service/MessageService.java`（`appendUserMessage`）；`service/ConversationService.java`（`ownedConversationForUpdate`）；`mapper/ConversationMapper.java`（`selectByIdForUpdate`） |

### 问题描述与方案选择

旧实现中 `appendUserMessage`、`beginTurn`、`createRun` 三步各自独立提交。若 run
创建失败，数据库会留下 USER 消息但没有 run：客户端收到错误，消息列表却已经多了一条
「没人回答的提问」，重试还会再插一条。

本批选择**方案 1（预创建事务）**而不是补偿：

- 这三步只依赖 `conversation` 与 `content`，不依赖流式生成结果，可以安全地放进
  一个短事务；
- ASSISTANT 消息不参与该事务，仍在流结束后独立写入；
- 任一步失败整体回滚，不会产生需要标记 / 清理的孤儿数据，也不需要新增状态字段；
- 补偿方案需要额外的「失败标记 + 扫描 + 幂等删除」，复杂度和观测成本都更高。

事务内第一步是 `SELECT conversation ... FOR UPDATE`（当前读）：同一对话的预创建
被串行化，并且能读到并发事务刚提交的 `session_id`，避免两个并发首条消息各自创建
重复 Session。事务提交后才创建 `SseEmitter` 并提交生成线程。

失败语义：预创建失败时记录 `conversationId + clientRequestId` 的 error 日志并把
异常抛给全局处理器（500），**不会**出现「USER 已落库、客户端收到 200」的情况。
若历史脏数据仍有「USER 无 run」，幂等查询会将其识别为 C 态并返回 409，而不是静默
补 run 或重跑。

### 残余风险

- 崩溃窗口仍存在：预创建提交后、生成线程启动前进程退出，会留下 `RUNNING` run
  （与 CHAT-01 同一条风险，不重复计）。
- `beginTurn` 在预创建事务内执行概念匹配与 Session 解析，事务比纯写入略长；
  当前为短查询，未观察到锁竞争，但高并发同一对话时会被 `FOR UPDATE` 串行化。
  这是为了保证 `session_id` 与序号正确性而有意接受的代价。

### 测试覆盖

- `ChatSseIntegrationTest.runCreationFailureRollsBackWholeTurn`：用 `@SpyBean`
  让 `agentRunMapper.insert` 抛异常，断言 500 且 USER/ASSISTANT/run/Session 均为 0、
  `conversation.last_message_seq=0`、`session_id` 仍为 NULL；
- `ChatSseIntegrationTest` 其余正常路径同时验证预创建后 `message.end` 到达时
  助手消息已落库、run 最终收敛为 `COMPLETED/TRUNCATED/FAILED`；
- `ChatServiceTest` 验证 `prepare` 只调用一次，且 run 上记录 `assistantMessageId`。

---

## 汇总

| ID | 严重度 | 是否影响正确性 | 测试覆盖 | 本轮处理 |
| --- | --- | --- | --- | --- |
| RAG-02 | 中 | 存储残留，不影响召回 | 部分（同 collection） | 记录 |
| RAG-03 | 中 | 可能静默丢失召回 | 无 | 记录 |
| RAG-04 | 中 | 可能降低召回质量 | 阶段 A：默认关闭严格开关、观测、诊断隔离、9 场景矩阵已覆盖；数据风险未消除 | 部分缓解（阶段 A） |
| MEM-01 | 低-中 | 错误码曾为 500，不丢数据 | 真实 MySQL 并发 + 异常分类单测充分；MySQL 版本升级需回归 | 已修复（方案 A）；依赖异常 message 中的约束名 |
| CIT-01 | 低-中 | 实时/历史展示不一致 | 单测 + 真实 MySQL 集成覆盖实时/历史一致性与降级 | 已修复（方案 C：统一活跃版本语义 + `isHistorical` 三态；旧记录无快照场景不可区分） |
| CIT-02 | 中 | 历史引用指向非活跃来源 | 单测覆盖 READY / 版本 / DELETING | 已修复（方案 C：READY + 活跃版本校验） |
| MIG-01 | 中 | 旧库升级失败 | 部分（当前库形态） | 记录 |
| WEB-01 | 低 | 不影响后端能力，增加上手成本 | 无（前端无自动化测试） | 记录 |
| DOC-01 | 中 | 不影响检索 / 权限，存在磁盘残留 | 部分（正常删除；IO 异常无覆盖） | 记录 |
| DDL-01 | 中 | 不影响查询，升级 / 部署有风险 | 无迁移框架测试 | 记录 |
| DIST-01 | 中 | 单实例安全，多实例可能版本回退 | 单进程充分、多实例无覆盖 | 记录 |
| EVD-01 | 中 | 跨概念证据覆盖有限，不改变归属 | 受控一跳 + 三态/批量查询 + E2 契约/降级/E1 边界充分；真实模型 / 多跳 / 真实关系数据无覆盖 | 部分缓解（E1 + E2 + E3）；真实模型评测 / 分页待做 |
| CHAT-01 | 中 | 重复请求曾会重复落库 / 重复生成 | 幂等 A/B/C、并发、唯一键、校验 400、回放单测均覆盖 | 已修复；崩溃遗留 RUNNING run 与 Web 入口限流仍待做 |
| CHAT-02 | 中 | run 创建失败曾留下 USER 孤儿 | 注入 run 插入失败断言全量回滚 | 已修复（方案 1：预创建事务） |

以上为**当前已知限制与残余风险清单**；标注“已修复”的项（CHAT-01、CHAT-02、
MEM-01）保留的是修复后仍未覆盖的残余风险，不再是未修复缺陷。后续修复其余项时
应同时补对应测试，不能只改代码。

> 说明：RAG / MEM / CIT / MIG 系列来自三阶段改造的内部审查；WEB / DOC / DDL / DIST
> 为本轮封版评审确认、并明确**不在本轮修复**的边界。它们不构成本次交付的阻塞项，
> 但任何一类在进入生产前都应按各节的“后续方向”完成收敛。
