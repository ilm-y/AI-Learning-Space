# AI Learning Space — 三阶段最终独立审查（第二轮）

> 审查对象：Memory 状态机修复、RAG 文档索引可靠性、Citation 结构化引用闭环，以及第一轮审计后的修复。
>
> 审查日期：2026-11-07
>
> 审查方式：只读检查真实代码、测试源码与保留的测试产物；独立运行 Agent 非门控测试；未修改代码、未提交 Git、未改动数据库。
>
> **2026-11-07 19:54–19:56 补记（证据对齐轮）**：本报告结论不变，补充两项可复核证据：
> 默认模式 `mvn -B test` 实测 Surefire **335 / 0 / 0 / 88**（247 passed），Failsafe 不执行；
> 默认模式 Agent pytest **408 passed / 7 skipped / 1 xfailed**，原始 stdout/XML 已保存到
> [`artifacts/`](artifacts/)。门控全量 415/0/1 仍是无原始产物的历史声明（C 级）。
> `DocumentIndexFencingIntegrationTest` 运行在 Surefire（类名不匹配 `**/*IT.java`），
> 不是 Failsafe。模式对照见 [`TEST_EVIDENCE_MODES.md`](TEST_EVIDENCE_MODES.md)。
>
> **最终判定：CONDITIONAL PASS（有条件通过）**

---

## 0. 结论

### 0.1 总判定

**CONDITIONAL PASS。**

第一轮的阻断项 **RAG-01 已真实修复**：旧 worker 的失败回滚与旧版本清理由“删除除某版本外的全部版本”改为 **attempt/lease fencing + 精确版本删除**，并用真实 MySQL + 真实 Qdrant 的 4 条回归测试验证。该阻断项可以关闭。

但第一轮给出的“升级为 PASS 的最低条件”并未满足：

> RAG-02、RAG-03 至少有一项修复，或有明确的风险接受记录。

实际情况是：

- **RAG-02 仍未修复**：回滚与定时清理仍使用 `document` 的 collection 快照；并且本轮发现成功路径的清理改为使用“新 collection”，在 embedding 配置切换时会漏删旧 collection 中的旧版本点，属于修复 RAG-01 时引入的新变体。
- **RAG-03 仍未修复**：没有 Qdrant → MySQL 的反向对账；派生数据丢失后 READY 文档仍会静默失效。
- 仓库中没有找到对 RAG-02 / RAG-03 的明确风险接受记录；Phase 2 报告与 `INTEGRATION_VERIFICATION.md` 均将它们列为“未修复的残余风险”。

因此不能升级为 PASS。另一方面，核心 happy path、版本切换、删除退出白名单、引用持久化等主链路是成立的，测试证据链也比第一轮明显改善，所以也不构成 FAIL。

### 0.2 分阶段结论

| 阶段 | 结论 | 说明 |
|---|---|---|
| Memory 状态机 | **PASS（带低风险）** | 状态迁移入口唯一、矩阵完整、用户隔离成立；真实 MySQL 并发 IT 4/4。MEM-01（并发唯一键冲突返回 500）仍是低风险。 |
| RAG 索引可靠性 | **CONDITIONAL PASS** | RAG-01 已修复并验证；RAG-02、RAG-03 未修复，且发现 RAG-02 的成功路径新变体；RAG-04（legacy fingerprint 绕过）仍需处理。 |
| Citation 闭环 | **PASS（带低风险）** | 结构化引用从 Agent → Java → SSE → MySQL → 历史读取的链路完整，字段无丢失，权限边界成立；CIT-01（实时 SSE 硬编码 `available=true`）与 CIT-02（历史可用性未校验活跃版本）是低风险语义问题。 |
| 测试证据链 | **CONDITIONAL PASS** | Maven XML 计数与文档一致；但 335 是“77 条环境门控测试全部打开”的模式，不是默认模式；pytest 的 415/0/1 没有保留产物；迁移脚本升级路径没有可复核产物；MQ 10 条仍未执行。 |

### 0.3 第一轮阻断项处置

| 第一轮问题 | 本轮状态 | 依据 |
|---|---|---|
| RAG-01：清理/回滚缺少 attempt/lease fencing，可能误删新 worker 数据 | **已修复，阻断解除** | `stillOwnsAttempt` + 精确版本删除；Fencing IT 4/4；代码路径已逐一核对 |
| RAG-02：collection 快照 | **未修复，且出现新变体** | 回滚仍用快照；成功清理改用新 collection，旧 collection 的旧点会变孤儿 |
| RAG-03：缺少 Qdrant → MySQL 对账 | **未修复** | `DocumentIndexRecoveryService` 只做状态/租约回收 |
| MEM-01：并发唯一键冲突返回 500 | **未修复** | `MemoryService.update` 没有捕获 `DuplicateKeyException` |
| CIT-01：实时 SSE 硬编码 `available=true` | **未修复** | `ChatService.java:290-292` |
| TST-01：缺少 READY 重建失败的真实 IT | **未修复** | 只有 Mockito 单测；15 条 RAG 真实 IT 中没有该场景 |
| TST-02：缺少跨语言 citation 契约测试 | **未修复** | Agent 与 Java 各自测试，Chat SSE 使用 stub gateway |

---

## 1. 审查边界与证据分级

本轮遵循“只读”约束：

- 未修改任何代码文件；
- 未执行 Git 提交；
- 未改动数据库（没有执行迁移、DDL 或数据写入）；
- 只运行了 Agent 的非门控 pytest（会生成 `__pycache__`，但这些文件未被 Git 跟踪；`git status` 仍为 clean）；
- 报告文件本身是本轮唯一写入。

证据分级：

| 级别 | 含义 | 本轮对应 |
|---|---|---|
| A：独立复核 | 本轮直接读代码或重跑测试得到 | 代码路径、Surefire/Failsafe XML 计数、Agent 非门控测试 408/7/1 |
| B：产物存在但未重跑 | 有 XML/日志，但本轮没有重新执行 | 19:20–19:21 的 Maven 单次运行结果、Memory 并发 IT、RAG E2E |
| C：仅文档声明 | 只有审计报告/说明，没有可复核产物 | Agent 415/0/1 的门控运行、RAG 迁移脚本在旧库上的升级、Phase 3 迁移的 9/9 |

> 重要：本报告不会把“文档写了”当成“已验证”。C 级内容在结论中一律降级为“未从产物复核”。

---

## 2. 第一轮阻断项：RAG-01 的修复核对

### 2.1 代码事实

RAG-01 的核心修复在 `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexService.java`：

1. **租约获取**（`acquireLease`，110–135 行）
   - 条件 UPDATE：`deleted_at IS NULL`、状态属于 `UPLOADING/FAILED/READY`、`index_started_at IS NULL` 或早于冷却窗口；
   - 每次成功抢占执行 `index_attempts = index_attempts + 1`，并把该值作为本次构建的 fencing token；
   - 0 行表示没抢到，直接返回 `null`。

2. **失败回滚**（`rollbackStagedVersion`，198–223 行）
   - 删除前调用 `stillOwnsAttempt(documentId, attempt, ...)`；
   - 只删除 **本次 attempt** 的精确版本：`agentClient.deleteVersions(document, List.of(attempt), false, ...)`；
   - 只有向量删除成功后才删除 MySQL chunk；向量删除失败则保留 chunk，交给定时清理重试。

3. **旧版本清理**（`cleanupOldVersions`，233–254 行）
   - 删除前同样做 `stillOwnsAttempt`；
   - `stale` 只取 `version < activeVersion` 的版本；
   - 只删除这些精确版本，不再使用“删除除 keep 之外全部版本”的语义。

4. **状态收敛**（`complete` 281–303 行、`fail` 313–326 行）
   - 两者都带 `index_attempts = attempt` 条件；
   - `complete` 是唯一的“生效”动作：把 `index_version` 切到本次 attempt；
   - READY 重建失败时 `fail` 保持 READY，旧版本继续服务。

5. **定时清理**（`DocumentIndexCleanupService.cleanup`，62–99 行）
   - 扫描条件排除 `index_started_at IS NOT NULL`；
   - 删除前重新读一次文档，确认 `index_started_at`、`index_version`、`index_attempts` 都没有变化；
   - 只删除 `version <= 扫描时 attempts` 的版本，避免把新 worker 的版本列进删除清单。

`stillOwnsAttempt`（263–277 行）的语义是：

- 文档存在、未软删；
- 不是 `DELETING/DELETED`；
- `current.index_attempts == attempt`。

### 2.2 安全性判断

**RAG-01 的阻断可以解除。** 关键不是 fence 检查本身是原子的（它不是），而是“精确版本删除”把最坏情况限制住了：

- `rollbackStagedVersion` 只删 `attempt` 这一个版本；新 worker 的 attempt 单调递增，不可能等于旧 attempt。
- `cleanupOldVersions` 只删 `version < activeVersion`；新 worker 即使在校验之后接管，写入的版本也必然 `> activeVersion`，不在删除清单里。
- `DocumentIndexCleanupService` 只删 `version <= 捕获的 attempts`；新 worker 的版本是 `attempts + 1`，不在清单里。

因此，**“fence 读与删除之间的 TOCTOU 窗口”不会导致新版本被删**。这是一个可接受的窄窗口，且代码注释已经明确说明“真正的安全边界是精确版本删除”。

### 2.3 测试证据与过度表述

`DocumentIndexFencingIntegrationTest` 的 4 条测试全部通过（Surefire XML：`tests=4 / failures=0 / errors=0 / skipped=0`，同一单次运行窗口 19:20:55）：

- `staleWorkerCleanupDoesNotDeleteNewWorkersActivePoints`
- `staleWorkerRollbackDoesNotDeleteNewWorkersActivePoints`
- `currentWorkerCleanupDeletesOnlyOlderVersions`
- `currentWorkerRollbackDeletesOnlyItsOwnAttempt`

测试使用真实 MySQL + 真实 Qdrant，FakeAgent 会把删除请求真实转发给 Qdrant，因此断言的是 Qdrant point 数量，不是 mock 交互。这一点是可信的。

**但“完整时序”这一表述过度。** 测试并不是：

1. 真的启动 worker A，拿到 attempt 1；
2. 让 recovery 回收 A 的租约；
3. 启动 worker B，抢占 attempt 2；
4. 让 A 在某个真实时间点恢复并执行清理；
5. 在 fence 检查与删除之间制造竞争。

实际做法是：

- 直接构造 `readyDocument(2, 2)`，让数据库已经是“B 已完成”的状态；
- 手工构造 `snapshotA`（attempts=1 / activeVersion=1）；
- 单线程直接调用 `indexService.cleanupOldVersions(snapshotA, 1, 1, 0, collection)` 或 `rollbackStagedVersion(snapshotA, 1)`；
- 断言 FakeAgent 没有收到删除请求。

4 条测试的耗时分别为 0.238s、0.192s、0.153s、0.252s，这也说明它们不是并发时序测试。

这组测试足以证明“**旧快照 + 新数据库状态** 下 fence 会拦住删除”，也足以证明“精确版本选择不会删活跃版本”；但它不能证明“真实租约回收 → 新 worker 接管 → 旧 worker 恢复”的完整时序，也没有覆盖 `acquireLease`、`DocumentIndexRecoveryService` 与清理之间的交互。

结论：**RAG-01 代码修复成立，阻断解除；测试证据对“状态不变量”充分，对“完整时序”不充分。** 该测试表述问题列为 TST-05，不作为阻断。

---

## 3. 问题清单

| ID | 严重度 | 状态 | 位置 | 摘要 |
|---|---|---|---|---|
| RAG-02 | 中 | 未修复，且新增成功路径变体 | `DocumentIndexService.java:175,198-223,233-254`；`DocumentIndexCleanupService.java:94-95`；`DocumentDeletionService` 删除路径 | 回滚/定时清理仍用旧 collection 快照；成功清理改用新 collection，配置切换时旧 collection 的旧点变永久孤儿 |
| RAG-03 | 中 | 未修复 | `DocumentIndexRecoveryService.java`；`DocumentMapper.selectReadyDocuments` | 没有 Qdrant → MySQL 反向对账；Qdrant 点丢失后 READY 文档静默失效 |
| RAG-04 | 中 | 未修复 | `DocumentMapper.java:50-67`；`agent/app/retriever/knowledge.py:305-309` | legacy 文档/point 没有 fingerprint 时，同 model+dimension 但不同 endpoint 的向量不会被阻断 |
| MEM-01 | 低 | 未修复 | `MemoryService.java:395-423` | `update` 未捕获 `DuplicateKeyException`，并发身份冲突返回 500 而非 409 |
| CIT-01 | 低 | 未修复 | `ChatService.java:290-292` | 实时 SSE 的 `available` 硬编码为 `true`，未按当前文档状态计算 |
| CIT-02 | 低 | 新发现 | `CitationService.java:120-132` | 历史引用只检查文档/chunk 存在，未检查文档 READY 或 chunk 属于活跃版本 |
| MIG-01 | 低 | 新发现 | `schema-migration-phase3-citations.sql` | `AFTER truncated` 依赖 Phase 7 列；真正的 pre-Phase7 旧库会报 1054 |
| MIG-02 | 低（证据） | 未复核 | `schema-migration-rag-reliability.sql` | 最终全量运行用 `schema.sql` 重建测试库；RAG 迁移脚本的升级路径没有保留可复核产物 |
| TST-05 | 中（证据） | 新发现 | `DocumentIndexFencingIntegrationTest.java:172-244` | 测试是单线程状态构造，不是完整租约回收时序；`DocumentIndexCleanupService` 的二次读 fence 没有测试 |
| TST-01 | 中（覆盖） | 未修复 | `DocumentIndexServiceIntegrationTest.java` | 没有“READY 重建失败后旧索引仍可检索”的真实 IT，只有 Mockito |
| TST-02 | 中（覆盖） | 未修复 | `ChatSseIntegrationTest.java`、`HttpPythonAgentGatewayTest.java` | 没有真实 Python Agent → Spring 的 `done.citations` 契约测试 |
| EV-01 | 低（证据） | 新发现 | `agent/` 测试产物 | 415/0/1 没有保留 pytest XML/输出；本轮非门控运行为 408/7/1 |
| EV-02 | 低（说明） | 新发现 | `target/surefire-reports/` | 335/11 是 77 条环境门控测试全部打开的模式；默认模式会多出 77 条 skip |

---

## 4. 详细问题

### RAG-02（中）collection 快照问题未修复，并出现成功路径的新变体

#### 位置

- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexService.java`
  - `runIndex` 第 175 行：`cleanupOldVersions(document, version, version, previousVersion, validated.collection())`
  - `cleanupOldVersions` 第 250 行：`agentClient.deleteVersions(document, stale, deleteLegacy, collection)`
  - `rollbackStagedVersion` 第 206–207 行：`agentClient.deleteVersions(document, List.of(attempt), false, document.getVectorCollection())`
- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexCleanupService.java:94-95`：`document.getVectorCollection()`
- `backend/backend/src/main/java/com/ails/backend/service/AgentDocumentClient.java:160-161`：collection 为空时回退到 `document.getVectorCollection()`
- `agent/app/vectorstore/qdrant_store.py:79`：`collection_name_for(prefix, model, dimension)`，collection 名由 **model + dimension** 决定
- `backend/backend/src/main/java/com/ails/backend/service/DocumentDeletionService`：`agentClient.deleteDocument(document)` 只按当前 `document.vectorCollection` 删除

#### 代码事实

- 索引开始时读取的 `Document` 快照包含当时的 `vectorCollection`（旧 collection）。
- Agent 的 `/documents/index` 每次按**当前** embedding provider/model/dimension 计算 collection；索引成功后才由 `complete` 把新 collection 写入 MySQL（`DocumentIndexService.java:295`）。
- 失败回滚仍使用快照里的旧 collection；如果构建期间配置从 A 切到 B，Agent 写入的是 B，回滚却去 A 删除。
- 成功路径的清理现在改为使用 `validated.collection()`（新 collection B）；但旧版本的 points 是在旧 collection A 中写入的。于是清理会去 B 删除旧版本（0 个匹配），然后删除 MySQL 中的旧 chunk 记录。**旧 collection A 中的旧点从此没有任何记录可以定位。**

#### 复现路径 A：成功重建 + 配置切换（新变体）

1. 文档 D 以 embedding 配置 A（model M1 / dimension 4）索引成功，collection 为 C1，active version = 1，v1 points 在 C1。
2. 修改配置为 M2 / dimension 8，collection 变为 C2；触发重建（attempt 2）。
3. Agent 把 v2 points 写入 C2；`complete` 成功，MySQL `index_version=2`、`vector_collection=C2`。
4. `cleanupOldVersions` 以 C2 为目标删除 version 1；C2 中没有 v1 points，删除 0 个。
5. 随后 `chunkService.deleteVersions(documentId, [1])` 删除 MySQL 中的 v1 chunk 记录。
6. C1 中仍保留 v1 points；MySQL 不再有任何字段记录 C1（`vector_collection` 已被覆盖为 C2），定时清理也只会去 C2。**孤儿永久存在。**

#### 复现路径 B：失败重建 + 配置切换（原 RAG-02）

1. 文档 D 当前 active version 1，collection C1。
2. 配置切换到 C2，attempt 2 把 v2 points 写入 C2，然后解析/索引校验失败。
3. `rollbackStagedVersion` 使用 `document.getVectorCollection()` = C1 删除 version 2；C1 中没有 v2 points，Qdrant 删除返回成功。
4. 由于“删除成功”，`rollbackStagedVersion` 继续删除 MySQL 中的 v2 chunk 记录。
5. 定时清理读取 `document.vectorCollection`（仍是 C1，因为失败没有 `complete`），而 v2 的 chunk 记录已被删除，扫描不会再把该文档列为“有旧版本”。**C2 中的 v2 points 永久孤儿。**

#### 复现路径 C：删除文档 + 配置切换

1. 文档在 C1 中还有旧版本 v1 points，当前 `vector_collection=C2`。
2. 用户删除文档；`deleteDocument` 只按 C2 删除全部版本。
3. C1 中的 v1 points 不会被清除；文档随后置 DELETED。
4. 检索白名单已排除该文档，因此不会召回，但 Qdrant 存储泄漏。

#### 实际影响

- Qdrant 存储泄漏；随着 embedding 配置切换次数增加，孤儿点会累积。
- 检索侧有 `doc_version` / `index_version` / fingerprint 过滤，通常不会把孤儿点召回；因此这不是数据泄露或错误召回，而是**清理不可靠 + 存储与排障成本上升**。
- 成功路径的新变体比原 RAG-02 更隐蔽：原问题是“回滚去旧 collection 找新点”，现在是“成功清理去新 collection 找旧点”；两者都说明“collection 必须按 attempt 记录，而不是从当前文档快照推断”。

#### 修复建议

1. 在 `document` 增加 `index_collection`（或 `index_attempts` 维度的 attempt 表），在索引开始时记录本次 attempt 的实际 collection；`complete`、`rollback`、`cleanup`、`delete` 都按 attempt 读取。
2. 如果不想加列，至少让 Agent 的 index 响应在失败时也返回实际 collection；回滚使用该值。
3. 成功清理旧版本时，不能只用新 collection：
   - 方案 A：按每个旧版本记录其写入时的 collection，逐个清理；
   - 方案 B：清理所有已知 collection 中该文档的旧版本；
   - 方案 C：至少在配置切换时禁止“静默清理”，把旧 collection 名写入清理任务。
4. 补测试：FakeAgent 返回与 `document` 快照不同的 collection，断言回滚/清理调用的是实际 collection；再加一条成功重建 + collection 切换 + 旧点清理的真实 Qdrant 测试。

---

### RAG-03（中）缺少 Qdrant → MySQL 反向对账

#### 位置

- `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexRecoveryService.java`
- `backend/backend/src/main/java/com/ails/backend/mapper/DocumentMapper.java` 的 `selectReadyDocuments`（30–70 行）
- `agent/app/retriever/knowledge.py` 的检索路径

#### 代码事实

- `selectReadyDocuments` 的 READY 判定只查 MySQL：状态 READY、未删除、`indexed_at` 非空、embedding model 非空、`chunk_count` 等于当前 active 版本中带 `vector_ref` 的 chunk 数、fingerprint/model 匹配。
- 它不检查 Qdrant collection 是否存在，也不检查该 document/version 的 points 数是否与 `chunk_count` 一致。
- `DocumentIndexRecoveryService` 只处理：
  - 超时 PROCESSING → FAILED；
  - 超时 READY 重建 → 释放租约、保持 READY；
  - 超时 UPLOADING → 重试/FAILED。
- 没有“READY 但 Qdrant 无点”的修复或告警路径。
- 检索侧对 Qdrant 空结果是正常路径，不会报错。

#### 复现路径

1. 文档 D 索引成功，MySQL READY，`ready-ids` 包含 D。
2. 删除 Qdrant collection，或只删除 D 的 points（卷重置、误操作、从旧快照恢复）。
3. 再次检索：`ready-ids` 仍包含 D，Qdrant 查不到点，返回空结果；后端/前端只看到“没有引用”，没有错误。
4. 定时恢复、清理都不会发现；只有人工重建才能修复。

#### 实际影响

- 整个 collection 的 RAG 可能静默失效，业务指标上不易发现。
- 与 RAG-01 叠加时风险更高：RAG-01 曾可能制造“READY 但无点”，RAG-03 让系统没有自愈能力。
- 不泄露数据，但可用性风险为中等。

#### 修复建议

1. 增加定时对账任务：对 READY 文档按 active version 统计 Qdrant points，与 `chunk_count` 比较；不一致时标记异常并重新入队，或至少打点告警。
2. 对 Qdrant collection 不存在做启动/健康检查。
3. 检索连续空结果可按文档维度计数报警。
4. 增加测试：索引成功后删除 Qdrant 点，触发对账，断言文档被重新索引或标记异常。

---

### RAG-04（中）legacy 数据没有 fingerprint 时，embedding 不兼容不会被阻断

#### 位置

- `backend/backend/src/main/java/com/ails/backend/mapper/DocumentMapper.java:50-67`：当 `d.embedding_fingerprint IS NULL` 时，退回 `embedding_model` / provider / dimension 匹配。
- `agent/app/retriever/knowledge.py:305-309`：只有 payload 中存在非空 `embedding_fingerprint` 时才比较；缺失时直接放行。
- `schema-migration-rag-reliability.sql`：旧 READY 文档的 `embedding_provider/dimension/fingerprint/collection` 保持 NULL，靠“legacy + model 匹配”兼容。

#### 代码事实

- collection 名由 `model + dimension` 决定；同一个 model 名被不同网关复用时，collection 名相同。
- fingerprint 本来是用来区分“同 model 名、不同 endpoint”的。但 legacy 行没有 fingerprint，legacy points 也没有 fingerprint。
- 因此：**同 provider / 同 model / 同 dimension、但 endpoint 不同**时：
  - `selectReadyDocuments` 会因为 `d.embedding_fingerprint IS NULL` 且 model 匹配而放行；
  - Agent 查询同一个 collection；
  - `_parse_hit` 因为 payload 没有 fingerprint 而接受 legacy point；
  - 查询向量来自新 endpoint，文档向量来自旧 endpoint，两者被直接比较。

#### 复现路径

1. 迁移前文档 D 使用 provider=P、model=M、dimension=4，endpoint=E1；DB fingerprint 为 NULL，Qdrant point 没有 `embedding_fingerprint`。
2. 把 endpoint 切到 E2（仍声明 model=M、dimension=4），provider 不变。
3. 查询时后端传入新 fingerprint；`selectReadyDocuments` 仍返回 D（因为 D 的 fingerprint 是 NULL，model 匹配）。
4. Agent 在同一个 collection 中检索到 D 的 legacy points，`_parse_hit` 接受它们。
5. 结果：旧 endpoint 的向量与新 endpoint 的查询向量混用，检索质量静默下降，没有任何错误或告警。

#### 实际影响

- 不泄露数据，但 RAG 结果可能明显变差；这正好违反了“embedding 不兼容必须阻断”的目标。
- 迁移后旧文档越多，风险越大；而“legacy + model 匹配”是迁移脚本明确写下的兼容策略，说明这是已知设计缺口，不是偶发 bug。

#### 修复建议

1. 对 READY 检索要求 fingerprint 非空；legacy 文档先重建/回填，或显式标记为“需要重建”。
2. 如果必须兼容 legacy，至少把 endpoint 纳入 collection 名或写入 point payload；缺失 fingerprint 时按“不可检索”处理，而不是放行。
3. 增加测试：legacy point + 缺失 fingerprint + 不同 endpoint，断言被拒绝或触发重建。

---

### MEM-01（低）并发身份冲突在 update 路径返回 500

#### 位置

- `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:395-423`
  - 416 行：`requireIdentityFree(userId, scope, memoryKey, memoryId)`
  - 422 行：`requireUpdated(memoryMapper.updateById(memory), ...)`
- 对比：`persist`（493–511 行）和 `insertCandidate`（165–187 行）都捕获了 `DuplicateKeyException`。

#### 代码事实

- `requireIdentityFree` 是先查后写，不是原子条件。
- 两个并发 PATCH 可以把两条不同的记忆改成相同的 `(user_id, scope_key, memory_key)`；两边都通过预检查，随后一方命中 `uk_memory_identity`，抛出 `DuplicateKeyException`。
- `update` 没有捕获该异常，Spring 默认映射为 500。
- 数据库唯一约束保证不会写坏数据；问题只是错误码/用户体验。

#### 复现路径

1. 用户有两条不同 key 的记忆 M1、M2。
2. 两个并发 PATCH：一个把 M1 改成 key=K，另一个把 M2 改成 key=K；scope 相同。
3. 两者都通过 `requireIdentityFree`。
4. 先提交者成功；后提交者触发 `DuplicateKeyException`，客户端收到 500。
5. 预期应为 409（冲突）或可重试的明确错误。

#### 实际影响

- 客户端看到 500，可能误判为服务端故障并重试；事务回滚，数据不会损坏。
- 与状态机本身无关，但属于 Memory API 的可靠性缺口。

#### 修复建议

1. 在 `update` 中捕获 `DuplicateKeyException`，转换为 `BusinessException.conflict("记忆标识已存在")`。
2. 或把预检查改成原子 `UPDATE ... WHERE NOT EXISTS`；捕获异常更简单且能覆盖所有入口。
3. 增加一条真实 MySQL 并发 IT：两个线程同时改 key，断言一个成功、一个 409，且数据库只有一条身份。

---

### CIT-01（低）实时 SSE 的引用可用性硬编码为 true

#### 位置

- `backend/backend/src/main/java/com/ails/backend/service/ChatService.java:288-292`

```java
end.put("citations", citations.stream()
        .map(citation -> CitationResponse.from(citation, true))
        .toList());
```

#### 代码事实

- 实时链路在 `message.end` 中把每条引用都标记为 `available=true`。
- 历史链路 `CitationService.resolveForMessages` 会按当前文档/chunk 状态重新计算。
- 实时链路的注释说“这些来源刚刚通过检索白名单，因此 available=true”；但“检索时通过”与“message.end 发送时仍然可用”不是同一件事，且没有校验引用是否真的属于本轮白名单。

#### 复现路径

1. 检索完成后、`message.end` 发送前，文档被删除（进入 DELETING）或 chunk 被清理。
2. 实时 SSE 仍下发 `available=true`；用户点击后可能 404 或看到“删除中”。
3. 刷新历史消息后，同一条引用变成 `available=false`。实时与历史不一致。

另一个更极端的路径：Agent 因 bug/提示注入返回一个不在本轮白名单中的 `documentId/chunkId`。实时 SSE 会把它显示为可用，并带上 `sourceTitle`；历史读取时因 owner 校验才会变成 false。持久化本身不校验 owner，但读取时会校验，因此不构成跨用户读取，只是实时展示的错误信息。

#### 实际影响

- 引用 UI 短暂或长期显示“可用”，与真实状态不一致；不泄露文档内容，但会误导用户。
- 对“实时与历史看到同一份事实”的设计目标是一个缺口。

#### 修复建议

1. 在发送 `message.end` 前调用 `CitationService` 的可用性判断，至少对 `documentId/chunkId` 做存在性 + owner + 活跃版本检查。
2. 或者把本轮检索白名单的 document IDs 传入 SSE 层，引用不在白名单中则 `available=false`。
3. 增加测试：实时流中引用一个已删除/他人文档，断言 `available=false`，且历史读取一致。

---

### CIT-02（低）历史引用可用性未校验活跃版本/文档状态

#### 位置

- `backend/backend/src/main/java/com/ails/backend/service/CitationService.java:120-132`

```java
Document document = documents.get(citation.documentId());
if (document == null || userId == null || !userId.equals(document.getUserId())) {
    return false;
}
DocumentChunk chunk = chunks.get(citation.chunkId());
return chunk != null && citation.documentId().equals(chunk.getDocumentId());
```

#### 代码事实

- 只检查：文档存在（`@TableLogic` 排除软删）、owner 匹配、chunk 存在且属于该文档。
- 不检查：
  - `document.status == READY`；
  - `chunk.indexVersion == document.indexVersion`（引用是否仍属于当前活跃版本）。
- `chunkMapper.selectBatchIds` 不按用户过滤，但 `isAvailable` 通过文档 owner 做了补偿；权限边界本身没有问题。

#### 复现路径 A：重新索引后的旧 chunk

1. 文档 v1 索引成功，消息引用 chunk v1。
2. 重新索引成功到 v2；`cleanupOldVersions` 删除 v1 chunk 可能有延迟（Agent 不可用时会一直重试）。
3. 在清理完成前读取历史消息：v1 chunk 仍存在，`isAvailable` 返回 true。
4. 但 v1 已不是活跃版本，用户打开文档看到的是 v2 内容，引用的片段可能已经不存在或含义改变。

#### 复现路径 B：删除进行中

1. 用户删除文档，状态变为 DELETING；异步清理尚未完成。
2. 历史引用读取：文档行仍存在、chunk 仍存在，`available=true`。
3. 清理完成后才变为 false；如果 Agent 长期不可用，DELETING 会长期保留，引用长期显示可用。

#### 实际影响

- 引用 UI 可能把“已被新版本取代”或“正在删除”的来源显示为可用。
- 不涉及跨用户读取；属于语义正确性与用户体验问题。

#### 修复建议

1. `isAvailable` 增加 `document.getStatus() == READY`。
2. 增加 `chunk.getIndexVersion() == document.getIndexVersion()`；或者在 `CitationMetadata` 中保存引用时的 `indexVersion`，读取时与当前活跃版本比较。
3. 补测试：重新索引后旧 chunk 未清理时读取历史，断言 `available=false`；DELETING 状态同样断言 false。

---

### MIG-01（低）Phase 3 引用迁移依赖 `truncated` 列，旧库顺序不当会失败

#### 位置

- `backend/backend/src/main/resources/schema-migration-phase3-citations.sql`
  - `ALTER TABLE message ADD COLUMN citations JSON ... AFTER truncated`
  - 前置检查只检查 `message` 表存在，没有检查 `truncated` 是否存在。

#### 代码事实

- `truncated` 是 `schema-migration-phase7-truncation.sql` 新增的列。
- 如果目标库是真正的 pre-Phase7 旧库（没有 `truncated`），直接执行 Phase 3 引用迁移会报 `ERROR 1054 Unknown column 'truncated' in 'message'`。
- Phase 3 报告中的迁移验证方式是：
  1. 用当前 `schema.sql` 建完整库；
  2. `ALTER TABLE message DROP COLUMN citations`；
  3. 再执行迁移脚本。
  这个库已经包含 `truncated`，所以验证的是“当前 schema 减去 citations”这一增量，不是真正的 pre-Phase7 旧库。

#### 复现路径

1. 准备一个只到 Phase 6 的旧库（`message` 没有 `truncated`）。
2. 执行 `schema-migration-phase3-citations.sql`。
3. `ALTER ... AFTER truncated` 失败，迁移中断。
4. 执行 Phase 7 迁移后再重跑 Phase 3 脚本可以成功（脚本本身幂等）。

#### 实际影响

- 部署顺序错误时，升级会失败，需要人工介入；不会破坏数据（DDL 失败，列未添加）。
- 对当前已应用 Phase 7 的开发库没有影响。

#### 修复建议

1. 去掉 `AFTER truncated`（列顺序对功能无影响），或先判断 `truncated` 是否存在再决定位置。
2. 在脚本头部明确写出“必须先执行 Phase 7 迁移”。
3. 增加一条真正的旧 schema 升级测试：从没有 `truncated`、没有 `citations` 的 schema 开始，按交付文档顺序执行迁移，断言成功。

---

### MIG-02（低，证据）RAG 迁移脚本升级路径没有可复核产物

#### 位置

- `backend/backend/src/main/resources/schema-migration-rag-reliability.sql`
- `docs/audit/INTEGRATION_VERIFICATION.md` 第 134 行左右：最终运行是 `DROP DATABASE` 后用 `schema.sql` 重建测试库
- `docs/audit/PHASE2_RAG_RELIABILITY.md`：声明曾在 `ails_document_it` / `ails_document_index_it` / `ails_document_e2e_it` 上手工执行迁移

#### 代码事实

- 脚本本身是幂等的：先查 `information_schema`，再 `ALTER`；回填 `document_chunk.index_version = document.index_version`；检查重复后加 `uk_chunk_doc_ver_idx`，删除旧 `uk_chunk_doc_idx`。
- 代码审查未发现明显的 SQL 逻辑错误；旧基线（commit `81a983e`）的 `uk_chunk_doc_idx` 与新脚本假设一致。
- 但最终全量 `mvn clean verify` 之前重建了 9 个测试库，用的是 `schema.sql`，不是迁移脚本。迁移脚本之前在测试库上的执行记录已被 `DROP DATABASE` 覆盖。
- `DocumentSchemaConsistencyTest` 只静态检查 `schema-migration-document-rag.sql`，不检查 `schema-migration-rag-reliability.sql`，也不检查 `schema-migration-phase3-citations.sql`。

#### 实际影响

- “全新建库”路径有最终运行产物支持；“迁移脚本升级”路径只有文档声明，没有可复核的 XML/日志/SQL 输出。
- 如果迁移脚本在某类旧库上失败，当前证据链无法提前发现。

#### 修复建议

1. 在一台独立 MySQL 上保留一次迁移升级运行记录：旧 schema → 执行脚本 → `SHOW CREATE TABLE` diff → 集成测试 → 再次执行脚本验证幂等。
2. 把输出保存为 CI artifact 或审计附件，而不是只在报告里写“已手工验证”。
3. 为两个新迁移脚本增加静态一致性测试：脚本必须包含 `schema.sql` 中对应的列/索引/唯一键关键片段。
4. 文档中明确区分“fresh schema.sql 已验证”和“migration upgrade 未验证”，不要用前者替代后者。

---

### TST-05（中，证据）fencing IT 不是完整时序，定时清理 fence 没有测试

#### 位置

- `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexFencingIntegrationTest.java:172-244`
- `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexCleanupServiceTest.java`（6 条 Mockito 测试）

#### 代码事实

- 4 条 fencing 测试都是单线程、手工构造快照、直接调用包可见方法；没有真实调用 `acquireLease`/`recoverStale`，没有两个 worker 并行，也没有在 fence 检查与删除之间制造 TOCTOU 窗口。
- `DocumentIndexCleanupService` 的二次读 fence（`cleanup` 75–85 行）没有任何测试：`DocumentIndexCleanupServiceTest` 的 6 条测试分别覆盖“只删非活跃版本 / 有活跃租约时跳过 / 没有活跃版本时跳过 / 没有旧版本时跳过 / Agent 失败保留 chunk / 扫描失败不阻塞”，没有“第一次读与第二次读之间文档变化 → 放弃清理”的用例。
- 也没有测试“第二次读通过后、删除前新 worker 接管”的窄窗口（虽然代码的 `version <= attempts` 上限能保护，但未被测试固定）。

#### 实际影响

- 测试能证明状态不变量，但不能证明报告里“完整时序”的说法；未来重构如果把精确版本删除改回“删除除 keep 之外全部版本”，现有测试仍可能通过。
- 定时清理是生产路径，缺少二次读 fence 的回归测试，风险高于其代码复杂度。

#### 修复建议

1. 增加真实并发测试：用 latch/CountDownLatch 控制 worker A 在 fence 检查后暂停，让 recovery + worker B 接管并完成，再放行 A 执行删除；断言 B 的 active points 仍在。
2. 为 `DocumentIndexCleanupService.cleanup` 增加真实 MySQL 测试：第一次读后修改 `index_attempts`/`index_version`/`index_started_at`，断言第二次读放弃清理。
3. 增加“fence 通过后新 worker 写入更高版本”的测试，断言 `version < activeVersion` / `<= attempts` 的过滤阻止误删。
4. 把测试类名从 `*IntegrationTest` 改为 `*IT`，或明确说明它为什么留在 Surefire。

---

### TST-01（中，覆盖）缺少“READY 重建失败，旧索引仍可检索”的真实 IT

#### 位置

- `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexServiceIntegrationTest.java`（15 条，真实 MySQL + FakeAgent）
- `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexServiceTest.java:197`：`indexFailureRollsBackOnlyNewVersionAndKeepsOldIndex`（Mockito）

#### 代码事实

15 条真实 IT 覆盖：

- 成功索引写 chunk/vectorRef/READY；
- 上传触发的异步索引；
- 解析失败不 READY；
- chunk 持久化失败回滚；
- embedding/Qdrant 失败不 READY；
- 向量映射畸形不写 ref；
- 内部鉴权失败不 READY；
- 重复索引幂等、只有成功才递增版本；
- 错误截断；
- 恢复/超时/late completion 等。

没有一条是：

1. 文档已经 READY、active version=1；
2. 触发重建，Agent 失败；
3. 断言文档仍 READY、`index_version` 仍为 1；
4. 断言旧 chunk 仍在、旧版本仍能被 `selectReadyDocuments` 返回/检索。

Mockito 测试 `indexFailureRollsBackOnlyNewVersionAndKeepsOldIndex` 验证了调用序列，但没有验证真实 MySQL 状态，也没有验证检索白名单。

#### 实际影响

RAG 的核心承诺“重建失败不破坏旧索引”缺少真实持久层证据；如果 `fail` 的条件更新或恢复逻辑回归，单测可能仍然通过。

#### 修复建议

1. 在 `DocumentIndexServiceIntegrationTest` 增加一条真实 IT：预置 READY v1 + FakeAgent 在 v2 索引时返回失败，断言 `status=READY`、`index_version=1`、`chunk_count` 不变、`listReadyDocuments` 仍返回该文档、旧 chunk 仍存在。
2. 再增加一条：重建失败后旧版本 points 在 Qdrant 中仍存在（可复用 Fencing IT 的 FakeAgent/Qdrant 设施）。
3. 增加一条：重建成功但旧版本清理失败，文档仍 READY 且新版本可检索（已有 `cleanupFailureAfterSwitchDoesNotFlipReadyBack` 是 Mockito，可升级为真实 IT）。

---

### TST-02（中，覆盖）缺少真实 Python → Spring 的 citation 契约测试

#### 位置

- `backend/backend/src/test/java/com/ails/backend/service/ChatSseIntegrationTest.java:71`：明确使用 `ControllableAgentGateway` 替换 Agent。
- `backend/backend/src/test/java/com/ails/backend/agent/HttpPythonAgentGatewayTest.java`：用本地 HttpServer 返回手写 JSON。
- `agent/tests/test_runtime_citations.py`：验证 Agent 自己的 `done` 事件。

#### 代码事实

- Agent 侧测试证明 `done.citations` 的字段名是 `documentId/chunkId/chunkIndex/sourceTitle`，并且只输出合法、可解析的引用。
- Java 侧测试证明 `parseCitations` 能解析这些字段、丢弃畸形、按 `documentId:chunkId` 去重。
- 但没有任何测试把 **真实 Python Runtime 产生的 done 事件** 喂给 **真实 Spring `HttpPythonAgentGateway`**，再走 SSE/DB。
- `ChatSseIntegrationTest` 使用的是 stub gateway；它验证了后端 SSE 状态机与 MySQL，但不验证跨语言字段契约。

#### 实际影响

- 两侧各自的字段名/类型测试都通过，但真实序列化差异（例如 Python 的 `None`、数字类型、额外字段、事件顺序）仍可能在集成时暴露。
- 一旦 Agent 侧改字段名或 Java 侧改解析规则，现有测试不会同时失败。

#### 修复建议

1. 增加一条受限的跨语言契约测试：启动真实 FastAPI（或录制真实 SSE 响应），让 Spring `HttpPythonAgentGateway` 解析，断言 `StreamCompletion.citations` 与 Agent 报告一致。
2. 或维护一份共享的 JSON Schema/夹具，由两侧测试共同消费；但夹具不如真实进程有说服力。
3. 至少增加一条“Agent 返回真实 done 事件文本 → Java 解析”的 golden file 测试，减少手写 JSON 与真实输出的偏差。

---

### EV-01（低，证据）Agent 415/0/1 没有保留 pytest 产物

#### 事实

- `agent/` 下没有保留 pytest XML 或完整输出文件；只有审计报告中的一行“415 passed, 1 xfailed”。
- 本轮独立运行非门控测试：

```
408 passed, 7 skipped, 1 xfailed, 1 warning in 16.75s
```

- `pytest --collect-only -q`：`416 tests collected`。
- 7 条 skip 的原因：
  - 4 条 `test_qdrant_integration.py`：`AILS_QDRANT_IT=true` 未设置；
  - 1 条 `test_knowledge_e2e.py`：`AILS_KNOWLEDGE_E2E=true` 未设置；
  - 2 条 `test_worker_broker.py`：`AILS_TEST_RABBITMQ_URL` 未设置。
- 1 条 xfail 是 `strict=True`，原因是“检索成功但被预算裁剪时，模型可能用自然语言误述没有检索到资料；Runtime 不做关键词纠正”，属于已记录的剩余风险，不是伪装通过。
- **2026-11-07 19:54 补记**：默认模式再次运行并保留了原始产物：
  `408 passed, 7 skipped, 1 xfailed in 17.51s`，stdout/XML/元数据在
  `docs/audit/artifacts/agent-pytest-default-20261107T195424.*`。
  这解决了“默认模式无产物”的问题，但仍不能证明门控 415/0/1 的那 7 条真实执行。

#### 判断

- 415/0/1 在算术上与“408 + 7 条门控测试全部通过”一致，但没有保留证据证明那 7 条在门控模式下真的执行并通过。
- 本轮不能把 415/0/1 当作 A 级证据；只能作为 C 级文档声明。

#### 建议

- 在 CI 中保留 `pytest --junitxml=...` 产物，或至少保存带时间戳的完整输出。
- 报告里同时写出“非门控 408/7/1”和“门控 415/0/1”，并注明后者依赖哪些环境变量。

---

### EV-02（低，说明）335/11 是门控全开模式，不是默认模式

#### 事实

`target/surefire-reports/` 共 46 个 XML：

```
tests=335, failures=0, errors=0, skipped=11
mtime: 2026-11-07 19:20:12 – 19:21:03
```

11 条 skip：

| 类 | 数量 | 原因 |
|---|---:|---|
| `BackendApplicationTests` | 1 | 无条件 `@Disabled`（需要本地 MySQL 与已初始化库表） |
| `AnalysisMqIntegrationTest` | 10 | Docker 不可用；每条 skip 元素内含 `Assumption failed: Docker 不可用，跳过真实 RabbitMQ 集成测试（跳过 ≠ 通过）` |

`target/failsafe-reports/` 只有 1 个 XML：

```
com.ails.backend.service.MemoryStateMachineConcurrencyIT: tests=4, failures=0, errors=0, skipped=0
mtime: 2026-11-07 19:21:25
completed=4, errors=0, failures=0, skipped=0
```

**但 335 不是默认 `mvn test` 的计数。** 以下 77 条测试都带 `@EnabledIfEnvironmentVariable`，且在本轮产物中 `skipped=0`，说明运行时这些环境变量全部被打开：

| 类 | 数量 | 门控变量 |
|---|---:|---|
| `Phase2MqProcessorIntegrationTest` | 5 | `AILS_PHASE2_IT` |
| `Phase2RabbitMqBrokerTest` | 5 | `AILS_PHASE2_MQ_IT` |
| `InternalIdentityHttpIntegrationTest` | 5 | `AILS_SECURITY_IT` |
| `SecurityContextIntegrationTest` | 19 | `AILS_SECURITY_IT` |
| `ChatSseIntegrationTest` | 9 | `AILS_CHAT_SSE_IT` |
| `ChatSseOrderingProbeTest` | 1 | `AILS_CHAT_SSE_IT` |
| `DocumentIndexE2EIntegrationTest` | 1 | `AILS_DOCUMENT_E2E_IT` |
| `DocumentIndexFencingIntegrationTest` | 4 | `AILS_DOCUMENT_FENCING_IT` |
| `DocumentIndexServiceIntegrationTest` | 15 | `AILS_DOCUMENT_INDEX_IT` |
| `DocumentServiceIntegrationTest` | 7 | `AILS_DOCUMENT_IT` |
| `Phase2RealStateIntegrationTest` | 6 | `AILS_PHASE2_IT` |

因此：

- **门控全开模式**：335 tests / 0 fail / 0 error / 11 skip（77 条门控测试计入 pass）。
- **默认模式**（上述环境变量不设置）：这 77 条会被 `@EnabledIfEnvironmentVariable` 禁用（通常计为 skip），因此 335 不是默认模式下“已执行”的数字。**2026-11-07 19:56 已补跑默认模式**：Surefire 335 / 0 / 0 / **88 skipped**（247 passed），Failsafe 不执行；原始输出见 `docs/audit/artifacts/maven-default-test-20261107T195424.txt`。不要把 335 理解为“默认 `mvn test` 会执行 335 条”。
- `DocumentIndexFencingIntegrationTest` 虽然名字带 `IntegrationTest`，但运行在 Surefire，不在 Failsafe；Failsafe 的 4/0 是 Memory 并发 IT。报告里把两者分开写才不会误读。
- `ChatSseOrderingProbeTest` 是诊断探针，也被计入 335；它不应被当作业务契约通过数。

#### 建议

- 在最终摘要中明确标注“门控全开 335/11”与“默认模式”的差异。
- 保留每个环境变量的值（至少保留变量名与是否设置），否则外部无法复现。
- 把诊断探针单独计数，不并入契约测试总数。

---

## 5. 测试与证据链复核

### 5.1 真实执行与产物

| 测试集 | 证据 | 本轮判断 |
|---|---|---|
| Backend Surefire | 46 XML，335/0/0/11，单次 19:20–19:21 | 真实运行；但为门控全开模式 |
| Backend Failsafe | 1 XML，Memory IT 4/0/0/0，19:21:25 | 真实运行 |
| RAG Fencing IT | Surefire XML 4/0/0/0，19:20:55 | 真实 MySQL + 真实 Qdrant；状态构造，不是完整时序 |
| RAG E2E | Surefire XML 1/0/0/0 | 真实 FastAPI + 真实 Qdrant + 真实 MySQL；断言 DB chunk 数 == Qdrant point 数，重复索引一致 |
| Chat SSE | Surefire XML 9/0/0/0 | 真实 MySQL；Agent 为 stub |
| Memory 并发 IT | Failsafe XML 4/0/0/0 | 真实 MySQL，latch 并发，断言 DB 行 |
| MQ | 10 skip | 未执行；不能计入通过 |
| Agent pytest | 本轮 408/7/1；collect 416 | 真实运行；门控 7 条未启用 |
| Agent 415/0/1 | 文档声明 | 无保留产物，C 级 |

### 5.2 每个 skip 的可见性与原因

**Backend 11 条：**

- `BackendApplicationTests#contextLoads`：1 条，`@Disabled` 无条件跳过。它不验证任何配置下的 Spring 上下文，不能作为启动证据。
- `AnalysisMqIntegrationTest`：10 条，每条 `<skipped>` 元素都带 `TestAbortedException: Assumption failed: Docker 不可用，跳过真实 RabbitMQ 集成测试（跳过 ≠ 通过）`。**可见、可计数、有原因**；`-Pmq-integration` 会把 Docker 缺失变成构建失败，避免“跳过 = 通过”。这一点修复是有效的。

**Agent 7 条：**

| 测试 | 原因 |
|---|---|
| `test_knowledge_e2e.py` 1 条 | `AILS_KNOWLEDGE_E2E=true` 未设置 |
| `test_qdrant_integration.py` 4 条 | `AILS_QDRANT_IT=true` 未设置 |
| `test_worker_broker.py` 2 条 | `AILS_TEST_RABBITMQ_URL` 未设置 |

这些 skip 在 `-rs` 输出中可见，原因明确。它们不能算作通过；本轮没有启用这些外部服务。

### 5.3 真实 vs Mock 的边界

| 范围 | 真实执行 | Mock/Stub | 评价 |
|---|---|---|---|
| Memory 状态机 | 真实 MySQL 并发 IT 4 条 | 单测若干 | 关键并发路径有真实证据 |
| RAG 索引核心 | 真实 MySQL + FakeAgent 15 条 | `DocumentIndexServiceTest` 11 条 | 真实持久层覆盖较好 |
| RAG 跨语言 | 真实 FastAPI + 真实 Qdrant 1 条 | — | 有真实 E2E，但只有 1 条 |
| RAG fencing | 真实 MySQL + 真实 Qdrant 4 条 | FakeAgent 转发删除 | 真实存储，但时序是构造的 |
| RAG 定时清理 | 无真实测试 | `DocumentIndexCleanupServiceTest` 6 条 Mockito | 二次读 fence 未被真实测试覆盖 |
| 删除链路 | 真实 MySQL 的状态过滤 1 条 | `DocumentDeletionServiceTest` 12 条 Mockito | 删除后不可召回有真实 SQL 证据；异步清理细节是 Mockito |
| Citation SSE/DB | 真实 MySQL 9 条 | `ControllableAgentGateway` | 持久化与历史可用性真实；跨语言契约未覆盖 |
| Citation 解析 | 本地 HttpServer 15 条 | 手写 JSON | 解析逻辑覆盖好，但不是真实 Python 输出 |
| MQ | 0（10 skip） | — | 未验证 |

### 5.4 DB 变更验证路径：fresh schema vs migration upgrade

| 路径 | 本轮是否可复核 | 说明 |
|---|---|---|
| `schema.sql` 全新建库 | **部分可复核（B 级）** | 最终运行前重建 9 个测试库，测试 XML 证明 IT/E2E 在重建后的库上通过；`schema.sql` 包含 `document.index_version`、`document_chunk.index_version`、`uk_chunk_doc_ver_idx`、`message.citations`。本轮没有重新执行 `DROP/CREATE`，也没有查询 live DB（root 无密码不可用），所以不是 A 级。 |
| RAG 迁移脚本升级 | **未复核（C 级）** | 只有 Phase 2 报告声明曾在测试库手工执行；最终运行用 `schema.sql` 重建，覆盖了那些测试库。没有保留 `SHOW CREATE TABLE` diff 或迁移日志。 |
| Phase 3 引用迁移升级 | **部分验证（B/C 级）** | Phase 3 报告记录了在 `ails_phase3_migration_it` 上“当前 schema 减去 citations → 迁移 → 9/9 → 幂等”的过程；但它使用的是包含 `truncated` 的当前 schema，不是真正的 pre-Phase7 旧库。见 MIG-01。 |
| 旧库漏跑迁移的启动失败 | **已知风险** | `PROJECT_BASELINE.md` 已记录“漏跑 Phase 7 报 Unknown column 'truncated'”；没有自动迁移。 |

**结论：不能把“fresh schema.sql 通过”当成“migration upgrade 通过”。** 两类路径必须分别保留证据。

### 5.5 报告计数与 XML 的对照

| 文档声明 | XML 复核 | 差异/说明 |
|---|---|---|
| Surefire 335 / 0 / 0 / 11 | 335 / 0 / 0 / 11 | 一致 |
| Failsafe 4 / 0 / 0 / 0 | 4 / 0 / 0 / 0 | 一致 |
| Fencing IT 4/4 | Surefire 4/0/0/0 | 一致；但不在 Failsafe |
| Phase 2 IT 28/28 | 无法按“28”分组复核 | 相关类 15+1+7+4 等均通过；28 是文档聚合 |
| Agent 415 / 0 / 1 | 无 pytest 产物 | 本轮非门控 408/7/1；415 算术一致但未复核 |
| Memory IT 4/4 | Failsafe 4/0/0/0 | 一致 |

### 5.6 陈旧报告检查

- `target/surefire-reports/` 的 46 个 XML 修改时间全部落在 19:20:12–19:21:03；
- `target/failsafe-reports/` 的 XML 为 19:21:25；
- 没有发现更早阶段的 XML 残留；
- Failsafe 目录有一个 `2026-11-07T19-21-08_981.dumpstream`，内容是 Boot Manifest 的 classpath 警告（`'other' has different root`），不是测试失败。

因此“单次运行”这一说法在产物层面成立。但“单次运行”不等于“默认模式”：见 EV-02。

---

## 6. 各阶段结论

### 6.1 Memory：PASS（带低风险）

成立的部分：

- `MemoryStatus.canTransitionTo` 定义 3×3 矩阵，`ARCHIVED` 是终态；
- `MemoryService.applyStatusTransition` 是运行时 `setStatus` 的唯一入口（`grep` 只在该方法内发现 `memory.setStatus`）；
- `update` 在状态迁移后按迁移前状态执行 `requireEditable`，ARCHIVED 整体拒绝；
- 创建路径把“无前态”视为 CANDIDATE 基线，`create` 的 ACTIVE 等价于 CANDIDATE → ACTIVE；
- `@Version` 乐观锁 + `requireUpdated` 保证并发更新不会静默覆盖；
- `ownedMemory` / `search` 都按 `userId` 过滤；Controller 使用 `SecurityUtil.currentUserId`；没有 Agent 可写的 Memory 接口，未发现权限提升路径；
- 真实 MySQL 并发 IT 4/4：
  - 并发双确认 CANDIDATE 只保留 ACTIVE；
  - 非法 CANDIDATE → ? 被拒绝，合法 ACTIVE → ARCHIVED 成功；
  - 并发归档幂等；
  - Agent 更新与用户确认竞争时不会覆盖已确认内容。

残余：

- MEM-01：update 路径的 `DuplicateKeyException` → 500。
- `search` 在 `spaceId` 为空时返回该用户全部 ACTIVE 记忆（代码注释明确这是设计选择），不是跨用户泄露；但调用方需要知道这一语义。

### 6.2 RAG：CONDITIONAL PASS

成立的部分：

- 分阶段、版本化索引：`stageChunks(version)` 先写非活跃版本；`complete` 是唯一生效点；失败时旧 `index_version` 不变；
- READY 重建失败保持 READY，旧版本继续服务；
- `acquireLease` 条件更新 + `index_attempts` 自增，重复提交/多 worker 只有一个能拿到租约；
- `complete` / `fail` 带 attempt 条件；
- 失败回滚与旧版本清理使用精确版本删除 + attempt fence；
- 删除先把状态切到 DELETING（退出 `selectReadyDocuments`），再异步清 Qdrant/chunk/文件；失败保留 DELETING 重试；
- `selectReadyDocuments` 真实 SQL 测试覆盖 DELETING/DELETED/PROCESSING/未完成索引/模型不匹配/跨用户/跨 Space；
- 真实 E2E：FastAPI + Qdrant + MySQL，DB chunk 数 == Qdrant point 数，重复索引幂等。

残余：

- RAG-02：collection 快照 + 成功路径新变体；
- RAG-03：无反向对账；
- RAG-04：legacy fingerprint 缺失时 embedding 不兼容不阻断；
- TST-05：fencing 测试不是完整时序，定时清理 fence 无测试；
- TST-01：READY 重建失败的真实 IT 缺失。

### 6.3 Citation：PASS（带低风险）

成立的部分：

- Agent `structured_citations` 只输出合法且带数据库身份的引用，字段为 camelCase `documentId/chunkId/chunkIndex/sourceTitle`，按 `(documentId, chunkId)` 去重；
- `done.citations` 在 `runtime.py` 中携带；
- Java `parseCitations` 只接受正数 documentId/chunkId、非空标题、非负 chunkIndex，逐条丢弃畸形，按 `documentId:chunkId` 去重；
- `message.citations` JSON 只存身份快照，不存正文；
- SSE `message.end` 与历史 `GET /messages` 返回同一份引用；
- 历史读取按当前文档/chunk 状态计算 `available`；软删文档 → false；跨用户会话 → 404；没有“按引用 id 直接读 chunk”的绕过入口；
- 测试：`ChatSseIntegrationTest` 9/9（真实 MySQL，含 SSE → 落库 → 历史 → 软删 → 跨用户）；`HttpPythonAgentGatewayTest` 15/15；`CitationServiceTest` 8/8；Agent `test_runtime_citations.py` 5 条 + `test_citations.py` 19 条。

残余：

- CIT-01：实时 SSE 硬编码 `available=true`；
- CIT-02：历史可用性不校验 READY/活跃版本；
- TST-02：没有真实 Python → Spring 的 citation 契约测试。

---

## 7. 可续用成果（仅限代码与测试已验证）

以下是可以在后续 Resume 中直接引用的成果，均已核对代码，且有对应测试或真实运行产物；未验证的内容不列入。

### Memory

- 三状态矩阵（CANDIDATE / ACTIVE / ARCHIVED），`ARCHIVED` 终态；
- `applyStatusTransition` 是唯一运行时状态写入口，`update` 的字段白名单不含 status；
- `@Version` 乐观锁 + `requireUpdated`；
- 真实 MySQL 并发 IT 4/4，使用 latch 制造竞争，断言 DB 的 `status/content/version/updated_at`；
- 用户隔离：`ownedMemory` / `search` 按 `userId` 过滤；无 Agent 写 Memory 的接口。

### RAG

- 版本化、分阶段索引：先写非活跃 chunk，`complete` 条件切换 `index_version`；
- attempt/lease fencing：`acquireLease` 条件 UPDATE 自增 `index_attempts`；`complete`/`fail` 带 attempt 条件；
- 精确版本删除：`rollbackStagedVersion` 只删本次 attempt；`cleanupOldVersions` 只删 `< activeVersion`；
- 定时清理二次读 fence + `version <= attempts` 上限；
- 删除先 DELETING 退出检索白名单，再异步清理；失败保留 DELETING 重试；
- 真实 MySQL + 真实 Qdrant fencing IT 4/4；
- 真实 FastAPI + Qdrant + MySQL E2E 1/1，断言 DB chunk 数 == Qdrant point 数、重复索引幂等；
- `DocumentIndexServiceIntegrationTest` 15/15；`DocumentServiceIntegrationTest` 7/7；`DocumentDeletionServiceTest` 12/12（Mockito）；`DocumentIndexRecoveryServiceTest` 4/4。

### Citation

- Agent `structured_citations` → `done.citations`；
- Java `HttpPythonAgentGateway.parseCitations` 逐条校验/去重；
- `CitationMetadata` JSON 持久化到 `message.citations`；
- SSE `message.end` 与历史 `GET /messages` 的引用回放；
- `CitationService.resolveForMessages` 批量计算可用性，owner 校验；
- 测试覆盖：SSE 9/9、Gateway 15/15、CitationService 8/8、Agent 24 条（5 + 19）。

### 测试基础设施

- Surefire / Failsafe 分工，Failsafe XML 独立可审计；
- `AnalysisMqIntegrationTest` 的 10 条 skip 在 XML 中真实可见、可计数、带原因；`-Pmq-integration` 在 Docker 缺失时 fail-closed；
- 关键集成测试使用真实 MySQL / Qdrant / FastAPI，而不是全 Mock；
- 单次运行的 Maven XML 时间窗口一致，没有陈旧 XML 混入。

### 不能作为成果表述的内容

- 不能写“任意并发下绝对安全”：RAG-02/RAG-03 仍在；
- 不能写“所有旧索引清理都精确”：配置切换时 collection 选择仍会漏删；
- 不能写“embedding 不兼容全部阻断”：legacy 无 fingerprint 数据仍可混用；
- 不能写“415/0/1 已独立复核”：没有 pytest 产物；
- 不能写“迁移脚本升级路径已验证”：最终运行用的是 `schema.sql` 重建；
- 不能写“MQ 链路已验证”：10 条仍是 skip。

---

## 8. 升级为 PASS 的最低条件

1. **修复 RAG-02（P1）**
   - 按 attempt 记录实际 collection（新增 `index_collection` 或等价机制）；
   - 回滚、成功清理、定时清理、删除都按 attempt 读取；
   - 补“FakeAgent 返回不同 collection”的测试 + 成功重建 + 配置切换 + 旧点清理的真实 Qdrant 测试。
2. **处理 RAG-03（P1）**
   - 增加 Qdrant → MySQL 对账/告警；或写入明确的风险接受记录（包含监控、告警阈值、人工恢复流程）。
3. **修复/接受 RAG-04**
   - 禁止无 fingerprint 的 legacy 文档进入检索，或强制重建/回填；补兼容性测试。
4. **补真实时序测试（P1）**
   - 用 latch/真实 recovery 模拟“A 失租 → B 接管 → A 恢复清理”；
   - 为 `DocumentIndexCleanupService` 的二次读 fence 增加真实 MySQL 测试。
5. **补 READY 重建失败真实 IT（P1）**
   - 断言 `status=READY`、`index_version` 不变、旧 chunk 仍在、`selectReadyDocuments` 仍返回该文档。
6. **补跨语言 citation 契约测试（P2）**
   - 真实 FastAPI `done.citations` → Spring 解析 → SSE/DB。
7. **修复低风险项（P2）**
   - MEM-01：`DuplicateKeyException` → 409；
   - CIT-01：实时 `available` 按当前状态计算；
   - CIT-02：历史可用性要求 READY + 活跃版本；
   - MIG-01：去掉 `AFTER truncated` 依赖，或在脚本中检查/说明顺序。
8. **补齐证据（P2）**
   - 保留 pytest XML/输出（门控 415/0/1 与非门控 408/7/1 都要）；
   - 保留 RAG/Phase 3 迁移升级的 `SHOW CREATE TABLE` diff 与幂等重跑输出；
   - 最终摘要明确标注“门控全开 335/11”与默认模式的差异。

满足 1–5 后，才具备从 CONDITIONAL PASS 升级为 PASS 的条件；6–8 是证据与低风险收尾。

---

## 9. 关键代码位置索引

| 主题 | 位置 |
|---|---|
| 租约获取 | `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexService.java:110-135` |
| 索引主流程 | `DocumentIndexService.java:140-180` |
| 失败回滚 | `DocumentIndexService.java:198-223` |
| 旧版本清理 | `DocumentIndexService.java:233-254` |
| attempt fence | `DocumentIndexService.java:263-277` |
| complete / fail | `DocumentIndexService.java:281-326` |
| 定时清理 | `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexCleanupService.java:44-99` |
| Agent 删除精确版本 | `backend/backend/src/main/java/com/ails/backend/service/AgentDocumentClient.java:155-172` |
| chunk 精确删除 | `backend/backend/src/main/java/com/ails/backend/mapper/DocumentChunkMapper.java`（`deleteVersions` / `deleteByVersion`） |
| READY 白名单 | `backend/backend/src/main/java/com/ails/backend/mapper/DocumentMapper.java:30-70` |
| 旧版本扫描 | `DocumentMapper.java:263-276` |
| 恢复服务 | `backend/backend/src/main/java/com/ails/backend/service/DocumentIndexRecoveryService.java` |
| Memory 更新 | `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:395-423` |
| Memory 状态矩阵 | `backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java` |
| Memory 并发 IT | `backend/backend/src/test/java/com/ails/backend/service/MemoryStateMachineConcurrencyIT.java` |
| 引用 SSE | `backend/backend/src/main/java/com/ails/backend/service/ChatService.java:288-292` |
| 引用历史可用性 | `backend/backend/src/main/java/com/ails/backend/service/CitationService.java:120-132` |
| Agent 引用解析 | `backend/backend/src/main/java/com/ails/backend/agent/HttpPythonAgentGateway.java:315-334` |
| Agent 结构化引用 | `agent/app/citations.py:214-247` |
| Agent done 事件 | `agent/app/runtime.py:242-267` |
| Qdrant 精确删除 | `agent/app/vectorstore/qdrant_store.py:312-354` |
| collection 命名 | `agent/app/vectorstore/qdrant_store.py:79` |
| Phase 3 迁移 | `backend/backend/src/main/resources/schema-migration-phase3-citations.sql` |
| RAG 迁移 | `backend/backend/src/main/resources/schema-migration-rag-reliability.sql` |
| Fencing IT | `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexFencingIntegrationTest.java:172-244` |
| RAG E2E | `backend/backend/src/test/java/com/ails/backend/service/DocumentIndexE2EIntegrationTest.java:147-205` |
| Chat SSE IT | `backend/backend/src/test/java/com/ails/backend/service/ChatSseIntegrationTest.java:365-410` |

---

## 10. 声明

- 本轮只读审查，未修改任何代码文件，未提交 Git，未改动数据库；
- 本报告是唯一写入的文件；
- 报告中的“已修复/已通过”只基于代码与测试产物；未保留产物的内容均标注为 C 级或“未复核”；
- 第一轮报告仍保存在 Git 历史（commit `13cec31`）中，可随时对照。

**最终判定：CONDITIONAL PASS。**
