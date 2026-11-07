# Phase 3：Outbox 异步同步 + RRF 混合检索实施报告

- 日期：2026-11-07
- 范围：Memory 写路径的 Transactional Outbox、Relay Worker、MySQL FULLTEXT + Agent 向量双路召回、RRF 融合与回源复核
- 设计依据：[`../design/MEMORY_HYBRID_SEARCH_DESIGN.md`](../design/MEMORY_HYBRID_SEARCH_DESIGN.md)
- 前置阶段：[`PHASE1_MEMORY_FIX.md`](PHASE1_MEMORY_FIX.md)、[`PHASE2_AGENT_SYNC.md`](PHASE2_AGENT_SYNC.md)

> **状态：已完成，未提交 Git，等待审查。**

---

## 1. 目标与不变量

本轮要解决两个问题：

1. **写路径不能“记忆成功、索引丢失”**：Memory 与 outbox_event 必须同一事务提交，Relay 独立异步投递。
2. **检索不能只信向量库**：向量召回只作候选，最终结果必须回 MySQL 按 `user_id / status='ACTIVE' / 可见性` 复核。

实现中坚持的硬性约束：

- `outbox_event.payload` 是**自包含快照**，显式携带 `userId`、`scopeType`；Relay 不从处理时的库内状态反推。
- 同一 `memory_id` 的事件**严格按 id 顺序处理**（head-of-line），旧事件失败时后续事件不得先成功。
- 事件领取使用条件 `UPDATE`；所有回写带 `locked_by`（fencing），租约被回收后旧 worker 不能覆盖新结果。
- 检索永远不返回未经 SQL 复核的数据；复核失败返回 `UNAVAILABLE`，不伪装成“没有结果”。

---

## 2. 事件规则（与设计 §4.1.1 一致）

| 入口 | 写前 | 写后 | 事件 |
| --- | --- | --- | --- |
| `create` | 无 | ACTIVE | `MEMORY_UPSERT` |
| `createCandidate` / `reconcileCandidate` ADD | 无 | CANDIDATE | 无 |
| `updateCandidate`（Agent） | CANDIDATE | CANDIDATE | 无 |
| `update` | CANDIDATE | ACTIVE | `MEMORY_UPSERT` |
| `update` | ACTIVE | ACTIVE，索引字段变化 | `MEMORY_UPSERT` |
| `update` | ACTIVE | ACTIVE，无变化 | 无 |
| `update` | ACTIVE | ARCHIVED | `MEMORY_DELETE` |
| `update` | CANDIDATE | ARCHIVED | 无 |
| `archive` | ACTIVE | ARCHIVED | `MEMORY_DELETE` |
| `archive` | CANDIDATE / ARCHIVED | ARCHIVED | 无 |

索引字段 = `content / memoryType / importance / scopeType / scopeKey / learningSpaceId / conceptId`。

---

## 3. 新增与修改文件

### 3.1 新增（主代码）

| 文件 | 职责 |
| --- | --- |
| `backend/backend/src/main/java/com/ails/backend/enums/MemoryEventType.java` | `MEMORY_UPSERT` / `MEMORY_DELETE` |
| `backend/backend/src/main/java/com/ails/backend/enums/OutboxEventStatus.java` | `PENDING / PROCESSING / DONE / DEAD` |
| `backend/backend/src/main/java/com/ails/backend/enums/MemoryHybridStatus.java` | `OK / DEGRADED_VECTOR / DEGRADED_KEYWORD / UNAVAILABLE / BLANK_QUERY` |
| `backend/backend/src/main/java/com/ails/backend/entity/OutboxEvent.java` | outbox_event 实体；payload 以 String 承载 JSON |
| `backend/backend/src/main/java/com/ails/backend/mapper/OutboxEventMapper.java` | 领取、抢占、完成、重试、死信、租约回收、清理 SQL |
| `backend/backend/src/main/java/com/ails/backend/config/MemoryOutboxProperties.java` | `ails.memory.outbox.*` 配置与默认值 |
| `backend/backend/src/main/java/com/ails/backend/config/MemorySearchProperties.java` | `ails.memory.search.*` 配置与默认值 |
| `backend/backend/src/main/java/com/ails/backend/config/MemorySearchConfig.java` | 注册配置属性、开启调度、提供有界检索线程池 |
| `backend/backend/src/main/java/com/ails/backend/dto/memory/agent/MemoryEventPayload.java` | 自包含事件快照；SHA-256 原始 content |
| `backend/backend/src/main/java/com/ails/backend/dto/memory/HybridMemoryHit.java` | 单条融合结果（Memory + RRF 分数 + 两路排名） |
| `backend/backend/src/main/java/com/ails/backend/dto/memory/HybridMemorySearchResponse.java` | 状态、结果、warnings、耗时 |
| `backend/backend/src/main/java/com/ails/backend/service/MemoryOutboxPublisher.java` | 在同一业务事务内插入事件 |
| `backend/backend/src/main/java/com/ails/backend/service/OutboxRelayWorker.java` | `@Scheduled` Relay：head-of-line、租约、退避、死信、清理 |
| `backend/backend/src/main/java/com/ails/backend/service/RrfFuser.java` | 纯函数 RRF：`Σ w/(k+rank)`，k=60 |
| `backend/backend/src/main/java/com/ails/backend/service/HybridSearchService.java` | 并行双路召回、超时降级、RRF、强制回源复核 |

### 3.2 修改（主代码）

| 文件 | 修改内容 |
| --- | --- |
| `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java` | `create` 发 UPSERT；`update` 写前快照并决定 UPSERT/DELETE；`archive` 仅 ACTIVE 发 DELETE；`search` 委派给 `HybridSearchService`，保持 `List<MemoryResponse>` 契约 |
| `backend/backend/src/main/java/com/ails/backend/controller/MemoryController.java` | 新增 `GET /api/memories/search/hybrid`，返回状态与 warnings；旧 `/search` 不变 |
| `backend/backend/src/main/java/com/ails/backend/mapper/MemoryMapper.java` | 新增 `selectVisibleByIds`（回源复核）与 `selectVisibleList`（空查询旧语义） |
| `backend/backend/src/main/java/com/ails/backend/utils/JsonUtil.java` | 新增 `fromJson(String, Class<T>)` |
| `backend/backend/src/main/resources/application.yml` | 新增 `ails.memory.search.*` / `ails.memory.outbox.*`，全部支持环境变量覆盖 |

### 3.3 新增（测试）

| 文件 | 覆盖点 |
| --- | --- |
| `src/test/java/com/ails/backend/service/RrfFuserTest.java` | RRF 公式、排序、去重、权重、空输入 |
| `src/test/java/com/ails/backend/service/HybridSearchServiceTest.java` | 融合排序、回源剔除、降级、双路失败、空查询、复核失败、陈旧 hash、limit |
| `src/test/java/com/ails/backend/service/OutboxRelayWorkerTest.java` | 成功、DELETE、504 重试、401 死信、重试耗尽、抢占失败、非法 payload、关闭开关、退避/抖动公式 |
| `src/test/java/com/ails/backend/service/MemoryOutboxPublisherTest.java` | 事件类型、PENDING 初态、payload 显式 identity、无 id 拒绝 |
| `src/test/java/com/ails/backend/service/MemoryOutboxPublishingTest.java` | §4.1.1 各入口事件规则（含无变化不发、乐观锁失败不发） |
| `src/test/java/com/ails/backend/mapper/OutboxEventMapperIT.java` | 真实 MySQL：head-of-line、条件领取、退避到期、租约回收、fencing、清理 |
| `src/test/java/com/ails/backend/service/MemoryFulltextSearchIT.java` | 新增 F9：跨用户 / ARCHIVED / 不可见 id 被 `selectVisibleByIds` 过滤 |

### 3.4 修改（测试）

- `MemoryControllerTest`：新增 hybrid 端点契约测试；构造器适配。
- `MemoryStateMachineTest` / `MemorySourceConcurrencyTest`：补充 publisher / hybrid 依赖。
- `MemoryStateMachineConcurrencyIT`：清理表加入 `outbox_event`；上下文导入新增组件。
- `MemoryFulltextSearchIT`：上下文导入新增组件。

---

## 4. Outbox Relay 实现要点

### 4.1 Head-of-line

`OutboxEventMapper.selectClaimable` 的 `NOT EXISTS` 子查询要求同 `(aggregate_type, aggregate_id)` 没有更早的 `PENDING/PROCESSING` 事件：

```sql
AND NOT EXISTS (
    SELECT 1 FROM outbox_event p
    WHERE p.aggregate_type = e.aggregate_type
      AND p.aggregate_id   = e.aggregate_id
      AND p.id             < e.id
      AND p.status IN ('PENDING', 'PROCESSING')
)
```

即使较晚事件已经到期，只要较早事件未终态，它就不会被选中。这消除了“旧事件重试把索引写回旧内容”的窗口。

### 4.2 抢占、租约与 fencing

- `claim(id, workerId, now)`：`UPDATE ... SET status='PROCESSING', locked_by, locked_at, attempt_count=attempt_count+1 WHERE id=? AND status='PENDING' AND next_attempt_at<=?`。返回 0 表示被其他实例抢先。
- `markDone` / `markPending` / `markDead` 均要求 `status='PROCESSING' AND locked_by=#{workerId}`。
- `recoverExpiredLeases(cutoff, now)`：`PROCESSING` 且 `locked_at < cutoff` → `PENDING`，**不重置** `attempt_count`，避免“崩溃→立即重试→再崩溃”死循环。
- 租约回收后旧 worker 的回写返回 0，直接放弃。

### 4.3 失败分类与退避

| 异常 | 处理 |
| --- | --- |
| `AgentMemoryException.isPermanent()`（4xx，408/429 除外；契约漂移） | 直接 `DEAD` |
| 408 / 429 / 5xx / 连接失败 / 超时 | 可重试 |
| 其他 `RuntimeException` | 可重试 |
| payload 非法（缺字段、aggregate 不一致、未知事件类型、UPSERT 缺 content） | 直接 `DEAD` |
| 尝试次数达到 `maxAttempts` | `DEAD` |

退避公式：`min(base × 2^(attempt-1), max) × (1 ± jitter)`，默认 `2s → … → 300s`，`±20%`。

### 4.4 清理

每日删除 7 天前 `DONE` 事件；`DEAD` 永久保留，供人工排查/重放。

---

## 5. 混合检索实现要点

### 5.1 并行与超时

- 词法与向量两路提交到专用有界线程池 `memorySearchExecutor`。
- 各自 `completeOnTimeout`：词法 1500ms、向量 2000ms。
- 线程池拒绝（`RejectedExecutionException`）转换为该路失败，不向请求线程抛异常。

### 5.2 降级矩阵

| 词法 | 向量 | 状态 | 行为 |
| --- | --- | --- | --- |
| 成功 | 成功 | `OK` | RRF 融合 |
| 成功 | 失败 | `DEGRADED_VECTOR` | 纯 MySQL FULLTEXT |
| 失败 | 成功 | `DEGRADED_KEYWORD` | 纯向量候选（仍需回源） |
| 失败 | 失败 | `UNAVAILABLE` | 空结果 + 两条 warning |
| 空 keyword | — | `BLANK_QUERY` | 原 `selectVisibleList` 语义 |
| 复核失败 | — | `UNAVAILABLE` | 丢弃全部候选，绝不返回未复核数据 |

### 5.3 RRF

`rrf(d) = Σ w_i / (k + rank_i(d))`，`k=60`，rank 从 1 开始；同路重复 id 只计第一次；结果按分数降序，再按 `importance / updatedAt / id` 稳定排序。

### 5.4 回源复核（安全底线）

对融合后的 id 调用 `MemoryMapper.selectVisibleByIds(userId, spaceId, conceptId, ids)`，SQL 同时施加：

- `user_id = 当前用户`
- `status = 'ACTIVE'`
- 可见性（GLOBAL / SPACE / CONCEPT，与旧检索一致）

Qdrant 返回的跨用户、已归档、孤儿 id 全部被丢弃。向量命中携带的 `contentHash` 只用于识别索引落后并记录 `memory.hybrid.stale`；**正文始终取 MySQL 值**。

### 5.5 接口兼容

- 旧 `GET /api/memories/search`：仍返回 `List<MemoryResponse>`，内部改走混合检索。
- 新 `GET /api/memories/search/hybrid`：返回 `status / items / warnings / elapsedMs`，调用方可区分“没有结果”和“检索降级”。
- `maxLimit=200` 保留旧接口的历史安全阀。

---

## 6. 测试证据

### 6.1 默认模式（本机实际执行）

命令：

```powershell
cd backend/backend
mvn -B clean verify
```

结果（完整日志见 [`artifacts/maven-memory-phase3-default-20261107T2155.txt`](artifacts/maven-memory-phase3-default-20261107T2155.txt)）：

```
Tests run: 385, Failures: 0, Errors: 0, Skipped: 88     # Surefire
Tests run: 19,  Failures: 0, Errors: 0, Skipped: 19     # Failsafe（门控跳过）
BUILD SUCCESS
```

本轮新增单测 **39 个**，全部通过：

| 测试类 | 数量 |
| --- | --- |
| `RrfFuserTest` | 5 |
| `HybridSearchServiceTest` | 9 |
| `OutboxRelayWorkerTest` | 10 |
| `MemoryOutboxPublisherTest` | 3 |
| `MemoryOutboxPublishingTest` | 11 |
| `MemoryControllerTest` 新增 hybrid 用例 | 1 |

### 6.2 门控 MySQL IT

沿用项目既有门控约定：

```powershell
$env:AILS_MEMORY_IT='true'
$env:AILS_IT_MYSQL_PASSWORD='...'
mvn -B clean verify
```

IT 清单（共 19 个）：

- `OutboxEventMapperIT`：6 个（head-of-line、条件领取、退避到期、租约回收、fencing、清理）
- `MemoryFulltextSearchIT`：9 个（原 8 个 + F9 回源复核）
- `MemoryStateMachineConcurrencyIT`：4 个

**验证边界（如实记录）**：本次环境能连到本机 3306，但未持有 MySQL 密码；使用默认空密码与 `root` 均返回 `Access denied`，因此 gated IT 只验证了“默认模式正确跳过”，**未声称在真实 MySQL 上通过**。持有密码后按上面命令即可执行，无需改代码。

---

## 7. 异常处理汇总

| 场景 | 行为 |
| --- | --- |
| Memory 写入失败 / 乐观锁冲突 | 业务事务回滚，outbox 事件一起回滚；不产生孤儿事件 |
| payload 序列化失败 | 抛出 `IllegalStateException`，业务写入回滚 |
| Relay 单轮异常 | `relay()` 捕获并记录，调度线程继续下一轮 |
| Agent 4xx / 契约漂移 | `DEAD`，保留 payload 供人工重放 |
| Agent 5xx / 超时 / 连接失败 | 指数退避重试，超过上限转 `DEAD` |
| 多实例重复领取 | 条件 UPDATE 只有一个成功；fencing 防止旧租约覆盖 |
| 词法或向量单路失败 | 降级为另一路，响应带 `DEGRADED_*` 与 warning |
| 双路失败 | `UNAVAILABLE`，不伪装成空结果 |
| 回源复核失败 | `UNAVAILABLE`，丢弃候选 |
| Qdrant 返回越权/归档/孤儿数据 | SQL 复核过滤，永不返回给调用方 |
| 向量正文过期 | 只返回 MySQL 正文，并记录 `memory.hybrid.stale` |

---

## 8. 已知限制

1. **无指标/告警系统**：`DEAD`、降级、`stale` 目前只有结构化日志，未接 Prometheus/告警通道。
2. **Relay 单轮串行处理**：同一批内逐条调用 Agent；吞吐依赖 `batch-size` 与 Agent 延迟，后续可按聚合并行（需要保持同 id 顺序）。
3. **事件保留策略**：`DONE` 7 天、`DEAD` 永久；大量死信需要人工处理，尚无重放 CLI。
4. **检索线程池是进程内隔离**：多实例部署时每个实例各自有池，容量按实例配置。
5. **gated IT 未在本机执行**：见 §6.2，属于环境凭据限制，不是代码跳过。

---

## 9. 审查建议顺序

1. `MemoryService` 的 `update` / `archive` 事件规则（§4.1.1 对照）。
2. `OutboxEventMapper.selectClaimable` 的 head-of-line 与各 `mark*` 的 fencing 条件。
3. `OutboxRelayWorker.retryOrDead` 的 attempt 计数与退避边界。
4. `HybridSearchService` 的复核调用位置与降级分支。
5. `MemoryMapper.selectVisibleByIds` 的 SQL 过滤条件。

**未执行 `git add` / `git commit`，工作区保留全部改动等待审查。**
