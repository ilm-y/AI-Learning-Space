# Phase 3.3 消息升级策略评审：schema v1 → v2

- 日期：2026-10-28
- 范围：现有 `learning.analysis.request` / `retry` / `dlq` 契约与升级路径
- 方式：源码对照 + 已提交版本（`git HEAD`）对照 + Phase 3.2 真实 E2E 证据
- 结论：**当前不存在安全的滚动升级窗口，必须做协调切换（停生产者 → 排空队列 →
  升消费者 → 一次性升生产者 → 恢复流量）**。当前仓库没有部署流水线或队列门禁，
  因此这是一份**已评审的部署要求**，不是已验证的生产行为。

## 1. 结论摘要

| 问题 | 结论 | 依据 |
| --- | --- | --- |
| 旧生产者 + 新 Worker 同时运行会怎样？ | **旧 v1 任务全部被新 Worker 拒绝**：`TASK_SCHEMA_UNSUPPORTED`，`FAILED` + 死信；分析不可用 | 当前 Worker 要求 `schemaVersion == 2`；v1 没有 `backendIdentity` |
| 新生产者 + 旧 Worker 同时运行会怎样？ | **新 v2 任务全部被旧 Worker 拒绝**：旧 Worker 只认 `schemaVersion == 1` | `git HEAD` 的 `TASK_SCHEMA_VERSION = 1` |
| 旧消息能否留在主队列 / retry / DLQ？ | **能**。三个队列都是 durable；retry 有 15s TTL 后回主队列；DLQ 无 TTL，可长期保留 | `RabbitMqConfig`、`application.yml` |
| 升级期间会不会被拒绝并死信？ | **只要出现混合版本窗口，就会**。两种混合方向都不兼容；协调切换且排空队列时可以避免 | 版本矩阵见第 3 节 |
| 当前部署流程是否真正阻止？ | **没有证据**。仓库没有部署流水线、蓝绿/金丝雀配置、队列深度门禁或版本握手 | `.github/workflows/ci.yml`、`docker/docker-compose.yml` |
| 最小改动方案 | **协调切换**，不做 v1/v2 兼容层 | 见第 5 节 |

## 2. 当前契约与版本矩阵

| 侧 | 当前工作树 | `git HEAD`（已提交的旧版本） |
| --- | --- | --- |
| Spring 生产者 | `AnalysisTaskMessage.SCHEMA_VERSION = 2`；`AnalysisTaskService` 用 `delegation-secret` 签发 `backendIdentity` 后发布 | `SCHEMA_VERSION = 1`；`AnalysisTaskService` 不签发 `backendIdentity` |
| Python Worker | `TASK_SCHEMA_VERSION = 2`；`schemaVersion >= 2` 且 `backendIdentity` 为空即 `MESSAGE_INVALID` | `TASK_SCHEMA_VERSION = 1`；只接受 `schemaVersion == 1` |
| 结果消息 | `RESULT_SCHEMA_VERSION = 1` | `RESULT_SCHEMA_VERSION = 1`（未升级） |
| 后端结果消费者 | `AnalysisResultMessage.SCHEMA_VERSION = 1` | 同左 |

任务消息是**严格版本匹配**，不是向后兼容解析：

- 新 Worker 对 `schemaVersion != 2` 的任务直接 `TASK_SCHEMA_UNSUPPORTED`；
- 旧 Worker 对 `schemaVersion != 1` 的任务直接 `TASK_SCHEMA_UNSUPPORTED`；
- v1 任务没有 `backendIdentity`，新 Worker 即使愿意“兼容”也不能安全地代表用户访问后端。

四种组合：

| 生产者 | 消费者 | 任务链路 |
| --- | --- | --- |
| v1 旧 | v1 旧 | 可用（旧行为） |
| v1 旧 | v2 新 | **全部拒绝**：`TASK_SCHEMA_UNSUPPORTED` |
| v2 新 | v1 旧 | **全部拒绝**：`TASK_SCHEMA_UNSUPPORTED` |
| v2 新 | v2 新 | 可用（Phase 3.2 已实测 109 PASS / 0 FAIL） |

因此“先升级消费者、再升级生产者”只能保证**新消费者已经就绪**，不能保证
消费者升级完成到生产者升级完成之间的窗口安全：只要旧生产者还在发 v1，新
Worker 就会拒绝。反过来先升生产者更糟，旧 Worker 会拒绝所有 v2。

## 3. 混合版本窗口的实际后果

以“先升 Worker、后升 Spring”为例：

1. 旧 Spring 仍在发 `schemaVersion=1`、无 `backendIdentity` 的任务；
2. 新 Worker 在入口校验版本，直接返回 `TASK_SCHEMA_UNSUPPORTED`；
3. Worker 发布 `FAILED` 结果并投死信；Runtime 调用为 0；
4. 后端收到失败结果后把该 ANALYSIS run 置为 `FAILED`，并走现有 rules-v1 兜底，
   生成一条独立的 `COMPLETED` 分析（`OBSERVATION/UNKNOWN/rules-v1`）；
5. 用户侧不会得到 Agent 分析，但也不会写成 Agent 成功。

这不是“少部分失败”，而是**窗口内所有新分析任务都失败**。Phase 3.2 的旧版本
场景已经用真实 Worker 验证了同样的入口拒绝与死信路径（单条任务 `FAILED`、
死信 +1、retry +0、无 Agent 学情写入）。

## 4. 旧版本消息在队列 / retry / DLQ 中的状态

| 位置 | 持久化 | TTL / 回流 | 升级时的影响 |
| --- | --- | --- | --- |
| request queue（`learning.analysis.request`） | durable | 无 TTL；被消费后 ACK | 升级前未消费的 v1 会被新 Worker 拒绝；必须先排空 |
| retry queue（`learning.analysis.retry`） | durable | `x-message-ttl=15000`，到期 DLX 回 request | 排空等待必须覆盖 TTL；否则 retry 中的 v1 会在升级后回流并被拒绝 |
| DLQ（`learning.analysis.dlq`） | durable | 无 TTL；不自动重放 | v1/v2/非法消息可长期保留；不能把 DLQ 当作“已排空” |
| result queue（`learning.analysis.result`） | durable | 结果 schema 仍为 1 | 旧 Worker 的结果仍可被新后端接受；本轮升级不涉及结果契约 |

**旧消息可以留在这些队列里**，这是 durable 队列的正常行为，不是异常。关键点是：
升级动作本身不会清除它们；如果不清空 request/retry，新消费者会在启动后继续
遇到旧 v1 并逐条死信。DLQ 中的旧消息通常应归档审计，不应直接重放到 v2 消费者。

**结论：** “旧消息能留在队列里”与“升级安全”是两件事。必须把队列深度作为
升级门禁，而不是假设升级后消费者会自然兼容。

## 5. 推荐的部署契约（最小改动）

当前架构没有 v1→v2 转换器，也没有可信的“v1 用户身份”来源。最小改动不是写兼容层，
而是**协调切换**。建议把以下步骤写成发布 runbook，并在每次升级时留证据。

### 5.1 前置条件

- 选定维护窗口；停止所有会产生分析任务的流量（停止/隔离所有 Spring 实例，
  或在入口层阻断聊天请求）。当前没有“只暂停分析派发”的开关：
  `AILS_ANALYSIS_MODE` 只有 `inline` / `mq`，切到 `inline` 会改变执行路径且可能
  依赖用户 JWT，**不能当作安全的暂停开关**。
- 记录五个队列的深度与消费者数量；确认没有第二个旧 Worker 实例在消费。
- 备份/导出 DLQ（只导出，不重放）；记录当前 DLQ 基线。

### 5.2 排空旧任务

1. 保持旧 Worker 运行，让 request queue 与 retry queue 继续被消费；
2. 轮询两个队列深度直到 **0**；
3. 再等待至少 `retry-ttl-ms`（默认 15s）加上一条任务的最长处理时间，确认没有
   retry 副本回流；
4. 若仍有毒消息反复回流，让旧 Worker 按现有策略耗尽重试并进入 DLQ；不要为了
   排空而直接 purge 掉未处理消息；
5. 再次确认 request/retry 深度为 0，DLQ 只包含已归档的失败消息。

### 5.3 升级消费者

1. 停止旧 Worker（确认没有在途消息，或等在途处理完成）；
2. 部署并启动 v2 Worker；
3. 验证它声明的是同一组 durable 拓扑，日志显示 `retries<=3`；
4. 此时队列应为空；可选用测试签名器发一条 v2 冒烟任务，确认成功后再进入下一步。

### 5.4 一次性升级生产者

- 把所有 Spring 实例**同批**升级到 v2；不要让旧、新生产者长期并存。
- 如果必须分批，先停掉旧实例再启动新实例，且每批之间确认没有旧格式任务进入队列；
  但更简单的做法是维护窗口内一次性替换。
- 恢复流量后检查第一条真实任务：`schemaVersion=2`、`backendIdentity` 非空、
  run 最终 `SUCCEEDED`，DLQ 没有新增 `TASK_SCHEMA_UNSUPPORTED`。

### 5.5 回滚

回滚是反向操作，不能直接“把 Worker 换回 v1”：

1. 再次停止生产者；
2. 排空或隔离已经产生的 v2 任务（旧 Worker 不认识 v2）；
3. 回滚消费者到 v1；
4. 回滚生产者到 v1；
5. 恢复流量。

如果 v2 任务还在 request/retry 中就直接回滚消费者，它们会被旧 Worker 拒绝并
死信；这与正向升级的混合窗口对称。

### 5.6 需要自动化的门禁

发布脚本/流水线至少应检查：

- request、retry 深度为 0（不是只看 DLQ）；
- 消费者实例数与预期一致，且没有旧版本进程；
- 生产者实例全部报告 v2（或全部停止）；
- 升级后 DLQ 的 `TASK_SCHEMA_UNSUPPORTED` / `MESSAGE_INVALID` 增量为 0；
- 首条真实任务成功，且结果 schema 仍为 1。

当前仓库**没有**这些门禁，也没有部署流水线。以上是评审后的要求，不是已运行过的
生产流程。

## 6. 为什么不选“兼容升级”或“滚动升级”

| 方案 | 可行性 | 原因 |
| --- | --- | --- |
| 新 Worker 同时接受 v1 | **不可接受** | v1 没有 `backendIdentity`；要么无身份访问后端（安全回退），要么让 Agent 自签（已明确禁止）。没有可信的用户身份来源 |
| 旧 Worker 接受 v2 | **需要改旧代码** | 旧版本已经发布，无法在不升级的情况下获得新逻辑；且旧 Worker 不会使用 `backendIdentity` |
| 双写/双消费 v1+v2 | **超出最小改动** | 需要额外队列、转换器、去重与回滚设计；当前没有这些基础设施 |
| 先升消费者再升生产者 | **不足以保证安全** | 消费者就绪不等于旧生产者停止；窗口内 v1 仍会被拒绝 |
| 协调切换（停生产者 + 排空 + 同批升级） | **推荐** | 不需要兼容层，不引入新依赖，能把混合窗口压到 0 |

## 7. 当前部署流程是否真正阻止

**没有证据表明当前流程能阻止混合版本窗口。** 具体事实：

- `.github/workflows/ci.yml` 只跑测试与离线评测，没有部署、蓝绿、金丝雀或
  队列深度检查；
- `docker/docker-compose.yml` 只定义本地 MySQL/RabbitMQ/Qdrant 基础设施，
  没有后端/Agent 的生产编排；
- 仓库没有 Dockerfile、Kubernetes manifest、发布脚本或环境门禁；
- Phase 3.2 的 E2E 是手工脚本，不是发布流水线。

因此本报告的部署契约应标记为 **“已评审的部署要求（reviewed requirement）”**，
不能写成“生产滚动升级已验证”或“当前发布流程已保证兼容”。

## 8. 证据与限制

**已核对：**

- 当前工作树与 `git HEAD` 的 producer/consumer 版本常量；
- `AnalysisTaskService` 的签名时机（`createRun` 后、`publish` 前）；
- `RabbitMqConfig` / `application.yml` 的 durable、TTL、DLX 配置；
- Phase 3.2 真实 E2E 的 v1/v3 拒绝与死信结果；
- CI 与 docker-compose 的实际内容。

**未验证：**

- 没有在真实多实例生产环境执行过协调切换；
- 没有队列深度门禁的自动化实现；
- 没有 RabbitMQ 集群/镜像队列/高可用行为证据；
- 没有生产流量下的升级耗时、回滚耗时或故障注入数据。

## 9. 决定

1. 不实现 v1/v2 兼容层；不做滚动升级。
2. 采用第 5 节的协调切换 runbook；把它作为发布前置要求。
3. 在 CI/发布流程中加入队列深度与版本一致性门禁之前，不对外宣称“可安全升级”。
4. 下一阶段如果要降低升级风险，优先做**部署门禁脚本**，而不是改消息协议。
