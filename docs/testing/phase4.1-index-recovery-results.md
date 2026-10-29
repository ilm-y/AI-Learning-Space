# Phase 4.1 F2：中断索引的超时回收

- 日期：2026-10-28
- 范围：只处理「进程中断后文档永久停在 `PROCESSING`」；不新增文档状态、
  不改索引业务契约、不做静默自动重试
- 结论：用 `updated_at` 作为索引启动时间的近似值，增加可配置超时与定时
  条件回收；真实 MySQL 集成测试 15/15 通过（其中回收 6 条）

## 1. 修复前证据

Phase 4 隔离 E2E 中，后端在索引进行时被终止：

- 文档 3 状态：`PROCESSING`；
- `index_error`：`NULL`；
- `chunk_count`：0；
- MySQL 无 chunk，Qdrant 无 point；
- 不在 `selectReadyIndexedIds` 白名单中。

它不会污染检索，但用户会看到永久「处理中」，运维也无法区分「还在运行」
和「已经丢失」。

证据：[phase4-rag-index-results.md](phase4-rag-index-results.md) §2.3、
[phase4-failure-analysis.md](phase4-failure-analysis.md) §3。

## 2. 状态机审计

现有生命周期：

```text
UPLOADING --markProcessing--> PROCESSING --complete--> READY
                                    \------fail-----> FAILED
```

- 进入 `PROCESSING` 的条件更新会刷新 `updated_at`；
- 索引运行期间不再更新 `document` 行；
- 因此 `updated_at` 足以近似「本次索引启动时间」；
- 现有模型没有独立的 `processing_started_at`，但本轮不需要新增字段；
- 若未来出现「运行期间合法更新 document 行」的路径，才需要新增独立字段。

`updated_at` 来源：MyBatis-Plus 自动填充与 MySQL `ON UPDATE`。本轮没有改表结构。

## 3. 回收实现

新增 [DocumentIndexRecoveryService.java](../../backend/backend/src/main/java/com/ails/backend/service/DocumentIndexRecoveryService.java)：

1. `@Scheduled` 定时扫描，默认首次延迟 60 秒、之后每 5 分钟；
2. 查询条件：
   `status='PROCESSING' AND deleted_at IS NULL AND updated_at < cutoff`；
3. 对每条候选执行条件更新：
   `status='FAILED'`、写可诊断 `index_error`，且仍要求
   `status='PROCESSING' AND updated_at < cutoff`；
4. 更新 0 行表示任务已完成/已被其他实例回收，不覆盖、不重写错误；
5. **不自动重试**：只收敛到 `FAILED`，由现有业务契约允许用户重新提交索引；
6. 单条异常只记录日志，不阻断同批其他文档。

错误文本示例：

> 索引任务超过 15 分钟仍处于 PROCESSING，已判定为中断并标记 FAILED；可重新提交索引

配置（`ails.document.index.*`，均可用环境变量覆盖）：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `stale-processing-timeout-ms` | 900000（15 分钟） | 明显大于 parse 60s + index 120s 的最坏耗时 |
| `recovery-interval-ms` | 300000（5 分钟） | 扫描间隔 |
| `recovery-initial-delay-ms` | 60000（1 分钟） | 启动后首次扫描 |
| `recovery-batch-size` | 100 | 单次最多处理数 |

**不是启动时无条件清理**：启动后也要等首次延迟，并且只有超过时间阈值的行
才会被处理。

## 4. 真实 MySQL 集成测试

执行方式：

```text
AILS_DOCUMENT_INDEX_IT=true \
AILS_IT_MYSQL_URL='jdbc:mysql://127.0.0.1:3307/ails_document_index_it?...' \
AILS_IT_MYSQL_PASSWORD='' \
./mvnw -q -o -Dtest=DocumentIndexServiceIntegrationTest test
```

环境：

- 真实 MySQL 8.0.37 容器 `ails-mysql-it`，映射 `127.0.0.1:3307`；
- 独立数据库 `ails_document_index_it`；
- 本地假 Agent HTTP 服务：返回真实 JSON 契约，校验内部请求头；
- **不连接**真实 FastAPI、Qdrant、Embedding；这不是向量索引 E2E；
- 测试内将回收阈值设为 60 秒，并把定时扫描间隔/首次延迟设为 1 小时，
  只由测试显式调用 `recoverStale(...)`，避免后台线程干扰。

结果：`tests=15, failures=0, errors=0, skipped=0`，退出码 0。
运行日志：`evaluation/phase4_1/index-recovery-it.log`。
其中回收相关 6 条：

| 测试 | 覆盖点 |
| --- | --- |
| `freshProcessingIsNotRecovered` | 正常新任务不被回收，`index_error` 仍为 NULL |
| `staleProcessingIsFailedWithTraceableErrorAndExcludedFromReady` | 超时转 `FAILED`，错误含「超过/PROCESSING/中断/可重新提交索引」，且不在 ready-ids |
| `restartScanRecoversAnInterruptedDocumentToAnExplicitTerminalState` | 调用生产入口 `recoverStale()`（模拟重启后的扫描）能收敛到显式终态 |
| `recoveryIsIdempotentAndDoesNotRewriteTheError` | 第二次扫描更新 0 行，错误文本与 `index_attempts` 不变 |
| `recoveryRespectsTheTimeoutBoundary` | 60 秒阈值下，30 秒前启动的任务不回收；10 分钟前启动的才回收 |
| `lateCompletionCannotOverwriteARecoveredDocument` | 回收后原任务晚到的 `complete()` 失败，不能把 FAILED 改回 READY |

### 4.1 验证过程中发现并修复的测试缺陷

第一次在 `ails-mysql-it` 容器上重跑时，`recoveryRespectsTheTimeoutBoundary`
失败（期望 0、实际 1）。原因不是业务逻辑：

- 测试辅助方法用 MySQL `NOW() - INTERVAL 30 SECOND` 造旧时间；
- 容器会话时区是 UTC，JVM 是 Asia/Shanghai；
- 生产代码的 `updated_at` 与回收 cutoff 都来自 JVM，本来没有时区问题；
- 只有测试用数据库时钟，导致「30 秒前」被算成约 8 小时前。

修复只改测试辅助方法：用 JVM 的 `LocalDateTime.now().minus(...)` 写入
`updated_at`，不再依赖数据库会话时区。业务代码与断言预期未改。修复后
15/15 通过。

## 5. 回归与全量

- 真实 MySQL 集成：15/15，0 failures / 0 errors / 0 skipped；
- 后端全量（默认门控关闭）：249 tests，178 executed，71 skipped，
  0 failures / 0 errors；
- 集成测试默认仍由 `AILS_DOCUMENT_INDEX_IT=true` 门控，未把 skip 计为 PASS。

## 6. 边界与未覆盖

- 没有用真实 15 分钟挂钟等待验证定时器；测试通过把 `updated_at` 改旧来覆盖
  超时条件；
- 没有在测试中启动真实生产调度线程等待 5 分钟；`recoverStale()` 生产入口被
  直接调用，调度注解本身未被时间验证；
- 多实例并发只通过条件更新的数据库语义与重复扫描测试覆盖，没有启动两个
  Spring 进程做并发压测；
- 没有验证 MySQL 主从延迟、长事务锁等待下的回收表现；
- 没有验证「索引线程仍在合法运行但超过 15 分钟」的极端慢任务；默认阈值
  基于 parse/index 超时推算，不是生产耗时分布；
- 没有新增自动重试，也没有验证用户重新提交索引的完整 UI 流程。

## 7. 结论

超时 `PROCESSING` 可以在不误伤正常任务的前提下收敛到显式终态：

- 时间阈值可配置，默认 15 分钟；
- 使用现有 `updated_at`，未改表；
- 条件更新保证重复扫描/多实例幂等，不覆盖 READY 或已回收行；
- 回收写可诊断错误，不静默自动重试；
- 真实 MySQL 集成覆盖正常、超时、重启、幂等、边界、晚到完成六类场景。

这证明的是**本地隔离环境下的恢复语义**，不是生产长时间运行或并发调度验证。
