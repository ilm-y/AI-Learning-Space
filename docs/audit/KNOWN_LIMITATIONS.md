# 已知限制与残余风险

- 整理日期：2026-11-07
- 仓库版本：`13cec31`
- 范围：三阶段改造（Memory 状态机、RAG 索引可靠性、Citation 引用闭环）后仍然存在、
  但本轮**不修**的已知问题。
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
| 状态 | 未修复（有意兼容） |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/mapper/DocumentMapper.java:50-67`；`agent/app/vectorstore/qdrant_store.py:79`；`DocumentIndexService.java:294`、`:409-413` |

### 问题描述

Embedding 指纹包含 provider、model、dimension、endpoint 等信息，但
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

### 当前权衡（为什么现在不修）

- 直接要求 `embedding_fingerprint IS NOT NULL` 会让所有 legacy 文档立即退出召回，
  对已有用户是破坏性变更。
- 回填 fingerprint 需要重新计算/重新索引，成本高；在当前数据规模下，先保留按
  model/provider/dimension 的兼容匹配，避免“升级即不可用”。
- collection 名加入 endpoint 会改变现有 collection 布局，需要迁移与双写/切换方案，
  不适合作为本轮最小修复。

### 后续方向

1. 提供 fingerprint 回填任务：按文档记录重新计算并写入，回填后逐步要求非空。
2. collection 命名加入 endpoint 指纹（或至少加入 fingerprint 前缀），避免不同
   endpoint 共用 collection。
3. 在检索白名单 SQL 中对 legacy 行加显式告警/降级标记，而不是静默放行。
4. 迁移完成后删除“fingerprint 为空也匹配”的分支。

### 测试覆盖

- **行为覆盖，风险未覆盖**：
  `DocumentMapperSqlTest.legacyDocumentsWithoutFingerprintStillMatchByModelProviderAndDimension`
  明确断言 legacy 行会按 model/provider/dimension 匹配——这验证的是当前兼容行为，
  不是“endpoint 不同也安全”。
- 没有“同 model/dimension、不同 endpoint、legacy 无 fingerprint”的集成测试。
- 结论：测试锁定了兼容行为，但没有覆盖 RAG-04 的风险场景。

---

## MEM-01：Memory 身份唯一键并发冲突返回 500

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（错误码/可用性；不丢数据） |
| 状态 | 未修复 |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:395-423`（`update`）；`:416`（`requireIdentityFree`）；`:422`（`updateById`）；schema `memory.uk_memory_identity` |

### 问题描述

`MemoryService.update` 先调用 `requireIdentityFree(userId, scope, memoryKey, memoryId)`
做应用层查重，再 `memoryMapper.updateById(memory)`。查重与更新之间没有锁/事务级
唯一性保护之外的原子操作：

- 两个并发 PATCH 同时把不同记忆改成同一 `(user_id, scope_key, memory_key)`；
- 两边都通过 `requireIdentityFree`；
- 其中一个 `updateById` 命中 `uk_memory_identity`，抛出 `DuplicateKeyException`；
- `update` 没有捕获该异常，最终以 500 返回。

数据不会重复（唯一键兜底），但调用方看到的是服务器错误，而不是可识别的 409/冲突。

### 影响范围

- 并发编辑/Agent 写入与用户 PATCH 竞争时，用户可能收到 500。
- 不影响状态机正确性；不会写入非法状态或重复行。
- 重试可能成功，但错误语义不清晰，前端难以提示“标识已被占用”。

### 当前权衡（为什么现在不修）

- 应用层预查 + 数据库唯一键已经保证**不产生重复数据**；500 是错误映射问题，
  不是数据一致性问题。
- 捕获 `DuplicateKeyException` 需要区分“身份唯一键冲突”和“其他唯一键/外键冲突”，
  否则会把真正的基础设施错误误报成 409；当前 `MemoryService.persist` /
  `insertCandidate` 已有针对插入路径的捕获，更新路径尚未统一。
- 本轮范围是状态机最小修复，未扩大错误码改造。

### 后续方向

1. 在 `update` 的 `updateById` 外层捕获 `DuplicateKeyException`，仅当约束为
   `uk_memory_identity` 时映射为 409（或领域冲突），其他约束继续抛出。
2. 或把“查重 + 更新”改为数据库原子 upsert/条件更新，减少竞争窗口。
3. 统一 Memory 创建/更新/Agent reconcile 三条路径的冲突语义。
4. 增加并发 PATCH 同一身份的真实 MySQL 测试。

### 测试覆盖

- **部分覆盖**：`MemoryStateMachineConcurrencyIT` 4 条覆盖状态迁移并发（CANDIDATE→ACTIVE、
  ARCHIVED 等），不覆盖“两个不同 memory 改成同一 identity”的更新冲突。
- `MemorySourceConcurrencyTest` 覆盖的是 source 插入的 `DuplicateKeyException` 吞掉/区分，
  不是 `MemoryService.update`。
- **无覆盖**：update 身份冲突返回 500 的路径没有测试。
- 结论：不能把 Memory 并发 IT 4/4 当作 MEM-01 已覆盖。

---

## CIT-01：SSE `message.end` 的引用可用性硬编码为 `true`

| 项 | 内容 |
| --- | --- |
| 严重度 | 低-中（实时/历史不一致；不泄露权限） |
| 状态 | 未修复 |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/service/ChatService.java:288-292` |

### 问题描述

SSE 结束时，`ChatService` 对本次检索得到的每条引用统一调用
`CitationResponse.from(citation, true)`，即实时事件里的 `available` 永远是 `true`。
历史读取 `GET /messages` 才通过 `CitationService.isAvailable` 重新计算。

如果文档在“检索完成”和“发送 `message.end`”之间被删除、转为非 READY、或 chunk 被
重建清理，实时事件仍告诉客户端“来源可用”；刷新历史后才变成 `false`。同一份引用
在两条链路上可能出现不同结论。

### 影响范围

- 客户端可能短暂显示一个实际不可用的来源链接。
- 不造成越权：引用身份仍来自当前用户检索白名单；历史读取还会校验归属。
- 影响范围限于实时 SSE 与历史读取的一致性，属于展示层/体验问题。

### 当前权衡（为什么现在不修）

- 设计假设是“这些来源刚刚通过检索白名单”，实时链路不再做一次逐条数据库校验，
  以避免在流式结束路径增加 N 次查询、拉长尾延迟。
- 文档删除/重建与流式结束撞车的窗口很小；历史读取会纠正，且不会产生权限绕过。
- 本轮 Citation 目标是“结构化身份 + 历史可判定”，未把实时链路的强一致可用性
  作为必须项。

### 后续方向

1. 在发送 `message.end` 前批量调用 `CitationService` 计算可用性（一次批量查文档/chunk），
   而不是硬编码 `true`。
2. 或实时事件只返回身份，不返回 `available`，由客户端在需要时走历史/单条查询。
3. 增加“检索后、end 前删除文档”的确定性测试。

### 测试覆盖

- **部分覆盖**：`ChatSseIntegrationTest.citationsRoundTripAndReflectDocumentAvailability`
  验证 SSE 下发 → 落库 → 历史恢复 → 软删后历史 `available=false`。
- **无覆盖**：没有在 `message.end` 之前删除文档/改状态，断言实时事件应立即 `false`；
  也没有测试实时事件中的跨用户/非法引用。
- 结论：历史路径覆盖，实时路径的硬编码行为未被测试挑战。

---

## CIT-02：历史引用可用性只看“存在 + 归属”，不看 READY / 活跃版本

| 项 | 内容 |
| --- | --- |
| 严重度 | 中（历史引用可能指向非活跃/不可检索来源） |
| 状态 | 未修复 |
| 代码位置 | `backend/backend/src/main/java/com/ails/backend/service/CitationService.java:120-132` |

### 问题描述

`CitationService.isAvailable` 的判定是：

1. 文档存在（`@TableLogic` 已排除软删）且属于当前用户；
2. chunk 存在且 `chunk.documentId == citation.documentId`。

它**没有**要求：

- `document.status == READY`；
- `chunk.index_version == document.index_version`（当前活跃版本）；
- `chunk.vector_ref` 非空/可检索。

因此，在文档重建（PROCESSING + 旧版本仍服务）、索引失败（FAILED）、删除中
（DELETING）等窗口，历史引用可能被判定为 `available=true`，但该 chunk 已不是
当前活跃版本，或文档已不可检索。用户点击后可能看到旧内容/空结果。

### 影响范围

- 历史消息中的引用可能显示为“可用”，但指向非活跃版本或非 READY 文档。
- 不造成跨用户泄露；文档归属与 chunk 归属仍校验。
- 与 CIT-01 不同：CIT-02 是历史读取路径的判定过宽，不是硬编码。

### 当前权衡（为什么现在不修）

- 当前语义是“来源身份仍存在且属于你”，而不是“来源当前一定可检索”；实现成本低，
  避免在消息列表读取时额外 join 文档状态/版本。
- 软删已由 `@TableLogic` 覆盖，最常见的“文档被删”会正确返回 `false`。
- 重建窗口通常较短，旧版本在清理前仍可能存在；本轮优先保证引用身份与权限，
  未把“活跃版本一致性”纳入判定。

### 后续方向

1. 在 `isAvailable` 中增加 `document.status == READY` 与
   `chunk.indexVersion == document.indexVersion` 的校验。
2. 如需区分“文档存在但当前不可检索”，可在响应中增加原因字段，而不是只有布尔值。
3. 对重建中的文档，明确产品语义：引用旧版本算可用还是不可用。
4. 增加非 READY、旧 index_version、DELETING 三种状态的单元/集成测试。

### 测试覆盖

- **部分覆盖**：`CitationServiceTest` 覆盖文档软删、chunk 缺失、chunk 属于另一文档、
  跨用户；`ChatSseIntegrationTest` 覆盖软删后历史 `false`。
- **无覆盖**：没有 `status != READY`、`chunk.index_version != document.index_version`、
  `DELETING` 状态的测试。
- 结论：最常见删除路径覆盖，版本/状态窗口未覆盖。

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
3. 提供统一的迁移编排脚本/文档，固定 Phase 7 → Phase 3 → RAG reliability 的顺序。
4. 增加“Pre-Phase7 schema”迁移测试库。

### 测试覆盖

- **部分覆盖**：Phase 3 迁移在专用库 `ails_phase3_migration_it` 上从
  “当前 schema 去掉 `message.citations`”升级，9/9 通过且幂等；该库有 `truncated`。
- **无覆盖**：没有“没有 `truncated` 的 Phase 7 之前旧库”升级测试。
- 结论：当前库形态覆盖，旧库顺序依赖未覆盖。

---

## 汇总

| ID | 严重度 | 是否影响正确性 | 测试覆盖 | 本轮处理 |
| --- | --- | --- | --- | --- |
| RAG-02 | 中 | 存储残留，不影响召回 | 部分（同 collection） | 记录 |
| RAG-03 | 中 | 可能静默丢失召回 | 无 | 记录 |
| RAG-04 | 中 | 可能降低召回质量 | 行为覆盖、风险未覆盖 | 记录 |
| MEM-01 | 中 | 错误码 500，不丢数据 | 部分（状态并发） | 记录 |
| CIT-01 | 低-中 | 实时/历史展示不一致 | 部分（历史路径） | 记录 |
| CIT-02 | 中 | 历史引用指向非活跃来源 | 部分（软删） | 记录 |
| MIG-01 | 中 | 旧库升级失败 | 部分（当前库形态） | 记录 |

以上均为**已知限制**，不是“已修复但未写文档”的项。后续修复时应同时补对应测试，
不能只改代码。
