# 学习分析异步化（RabbitMQ）

把「回答结束后的学习分析」从聊天请求线程里挪出去。聊天接口与 SSE 不变，写入权仍然只在
Spring Boot：Python Worker 只产出**候选结果**，Java 校验、去重、落库。

默认模式是 `inline`（原同步链路）。只有显式设置 `AILS_ANALYSIS_MODE=mq` 才会连接 RabbitMQ，
所以新克隆的仓库不装 RabbitMQ 也能启动、跑测试。

---

## 1. 链路

```text
ChatService（SSE 回复已完成）
   │  AnalysisDispatchService
   │    mq 模式：创建 agent_run(ANALYSIS, PENDING) → 投递任务
   ↓
learning.analysis.request
   │  Python Worker：分析用户表达，生成 Evidence 候选
   ↓
learning.analysis.result
   │  AnalysisResultConsumer → AnalysisResultProcessor
   │    校验 → 抢占任务 → 从数据库重建上下文 → 写入 → 收尾
   ↓
Evidence / Understanding / Revision / agent_run 状态
```

涉及类（都在 `backend/.../mq/` 与 `agent/app/messaging/`）：

| 职责 | Java | Python |
| --- | --- | --- |
| 建任务、投递 | `AnalysisTaskService` / `AnalysisTaskPublisher` | — |
| 消费任务、分析 | — | `AnalysisWorker`（`python -m app.worker`） |
| 校验、落库 | `AnalysisResultProcessor` | — |
| ACK/NACK | `AnalysisResultConsumer` | `AnalysisWorker._handle` |

`TurnAnalysisService`、`AgentAnalysisWriteService`、`UnderstandingService`、`analyzer.py`
保持原职责，没有为 MQ 复制一套业务逻辑。

---

## 2. 队列拓扑

```text
learning.analysis.exchange (direct, durable)
 ├─ analysis.request       → learning.analysis.request
 │     Worker 主动把重试副本发布到 DLX(analysis.retry) → learning.analysis.retry
 │         (TTL 15s) → analysis.request → 回到请求队列
 │     永久错误 / 重试耗尽 → learning.analysis.dlx → learning.analysis.dlq
 └─ analysis.result        → learning.analysis.result
       暂时性处理失败 → Java 主动发布副本到 result.retry → (TTL) → 回到结果队列
       无法解析 / 重试耗尽 → Java 主动发布副本到 learning.analysis.dlx → learning.analysis.dlq
```

所有队列、交换机、消息都是 durable / persistent。重试没有用消费者 `sleep`：
消息进入 TTL 队列后进程立刻空出来，到期由 broker 重新投递。

**重试由应用负责搬运，而不是依赖 broker 的 `nack` + DLX 转发。** 两者在正常情况下等价，
但 classic 队列在目标队列或绑定缺失时会静默丢弃消息：应用侧看不到任何错误。
现在的做法是先发布一份副本，等 publisher confirm 且确认未被 mandatory return，才 ACK 原消息；
副本发不出去就保留原消息。broker 自己的 DLX 仍然保留，用于「监听器根本进不来」的消息
（例如反序列化失败）。迁移到 Quorum Queue 可以解决 classic 队列的这个限制，但会改变
所有队列的存储与资源模型，本项目当前没有必要；这里选择改动更小的应用侧重投。

拓扑由 Java 与 Python **双方幂等声明**。两边参数必须完全一致（队列名、TTL、DLX、routing key），
否则 `PRECONDITION_FAILED` 会让后启动的一方直接失败。修改 TTL 或参数时，先删除旧队列
（或换一个队列名），不要指望 RabbitMQ 自动改队列。

---

## 3. 消息契约

`schemaVersion` 是契约版本。消费者遇到不认识的版本会拒绝并转入死信，不会「猜字段」。

### 3.1 任务消息（Spring Boot → Worker）

```json
{
  "schemaVersion": 1,
  "taskId": 4201,
  "runId": 4201,
  "traceId": "b1f0c7d2e9a34c56",
  "userId": 7,
  "conversationId": 9,
  "learningSpaceId": 3,
  "sessionId": 5,
  "messageId": 1001,
  "assistantMessageId": 1002,
  "userMessage": "我的理解是 TLB 缓存的是页表项……",
  "assistantMessage": "差不多，不过还要注意……",
  "history": [
    {"role": "USER", "content": "TLB 是什么？"},
    {"role": "ASSISTANT", "content": "TLB 是……"}
  ],
  "session": {"id": 5, "title": "分页", "goal": "理解 TLB", "currentFocus": null}
}
```

- `taskId` 等于 `agent_run.id`，也是结果消息的关联键；`runId` 保留给 Python 直接构造
  `AnalyzeRequest`，两者当前相同。
- `history` 已排除本轮的用户/助手消息（它们由独立字段携带），最多 20 条。
- **没有 `authToken`。** 用户 JWT 会过期，而消息可能在队列里停留任意长时间。

Worker 需要的跨对话上下文由它自己回访后端只读接口获取，使用服务端凭据而非用户 JWT：

```text
X-Internal-Token:   <AILS_INTERNAL_TOKEN>
X-Internal-User-Id: <task.userId>
```

后端 `InternalServiceAuthenticationFilter` 校验共享密钥后建立与该用户等价的 principal，
原有 Controller / 空间归属校验照常执行，没有为 Worker 开旁路接口。
这是「服务代用户读」的凭据：只能部署在内网，不能下发给客户端。

### 3.2 结果消息（Worker → Spring Boot）

成功：

```json
{
  "schemaVersion": 1,
  "taskId": 4201,
  "traceId": "b1f0c7d2e9a34c56",
  "messageId": 1001,
  "assistantMessageId": 1002,
  "status": "SUCCEEDED",
  "result": { "evidenceCandidates": [], "analyzerVersion": "agent-v1" },
  "errorCode": null,
  "errorMessage": null,
  "retryable": null
}
```

失败：

```json
{
  "schemaVersion": 1,
  "taskId": 4201,
  "traceId": "b1f0c7d2e9a34c56",
  "messageId": 1001,
  "status": "FAILED",
  "result": null,
  "errorCode": "RETRIEVAL_UNAVAILABLE",
  "errorMessage": "backend read API returned 503",
  "retryable": false
}
```

**结果只是候选。** Java 会重新校验：schemaVersion、taskId 对应的 run 类型与状态、traceId、
status/result 是否齐全、conversation 与 message 的归属；然后从数据库重建
`TurnContext`（userId / spaceId / messageId / runId），再走原有的
`AgentAnalysisWriteService.apply`（Evidence dedupe_key、concept 空间归属、Understanding
生命周期）。Worker 无法通过伪造结果消息把数据写进别人的空间。

---

## 4. 状态与失败处理

`agent_run` 的分析任务生命周期：

```text
PENDING ──(结果到达后由 Java 抢占；Worker 处理期间仍是 PENDING)──→ RUNNING
   │  ↑                                                           │
   │  └──── 补偿扫描超时：PENDING/RUNNING → STALE ────────────────┤
   │                                    （晚到结果仍可回到 RUNNING）│
   │                                                              ├─→ SUCCEEDED
   └────────────────────→ FAILED ←────────────────────────────────┘
```

`STALE` 不是终态，只表示「`created_at` 已超过阈值但仍未看到结果」。任务可能还在队列里，
也可能正在调用模型；如果结果随后到达，消费者仍会把 `STALE → RUNNING` 并正常落库。
把它直接判成 FAILED 会让晚到的合法结果被当成 `DUPLICATE` 丢掉。

| 场景 | Worker | Java | 任务状态 |
| --- | --- | --- | --- |
| 分析成功 | 发布 SUCCEEDED 并通过 confirm 后 ACK | 抢占、写入、置 SUCCEEDED | SUCCEEDED |
| 检索/模型暂时不可用 | 发布带 `x-analysis-retry` 的副本到 retry 队列，confirm 后 ACK | 无消息，保持 PENDING | PENDING |
| 重试耗尽 / 永久错误 | 发布 FAILED 结果与死信副本，任一被 confirm 后 ACK | 置 FAILED，随后 rules-v1 兜底 | FAILED |
| 投递请求确定失败（连接失败 / NACK / 不可路由） | — | 把 run 置 FAILED，退回同步分析 | FAILED |
| 投递请求结果未知（confirm 超时 / 连接中断） | — | 保留 PENDING，只写诊断信息，**不**退回同步分析 | PENDING |
| 结果重复投递 | — | 已终态 → `DUPLICATE`，直接 ACK | 不变 |
| 结果处理遇数据库闪断 | — | 复制到 result.retry，TTL 后重试 | 保持 PENDING/RUNNING |
| 结果重试耗尽 | — | 先发布死信副本（等 confirm）、再置 FAILED、最后 ACK | FAILED（数据库可用时） |
| 长时间无终态（投递前崩溃、Worker 永久下线等） | — | 补偿扫描按 `created_at` 置 STALE | STALE（非终态） |

失败分类（Worker）：

| errorCode | 可重试 | 含义 |
| --- | --- | --- |
| `MESSAGE_INVALID` | 否 | 任务消息不符合契约 |
| `ANALYSIS_INVALID` | 否 | 模型输出不可解析 / 不符合契约 |
| `RETRIEVAL_UNAVAILABLE` | 是 | 后端只读接口不可用 |
| `LLM_UNAVAILABLE` | 是 | 模型上游失败 |
| `TIMEOUT` | 是 | 分析超时 |
| `RETRY_EXHAUSTED` | — | 已到达尝试次数上限 |
| `INTERNAL` | 是 | 未分类异常 |

`RETRY_EXHAUSTED` 与永久错误会同时：回一条 `FAILED` 结果 + 把原任务消息投到
`learning.analysis.dlq`。前者让 Spring 把任务落到终态，后者保留原始消息供排查。

rules-v1 兜底会由 `LearningAnalysisService` 自己再建一条 ANALYSIS run，因此失败任务在轨迹上
会看到两条记录：FAILED 的 Agent 任务 + COMPLETED 的规则任务。前者说明 Agent 这一跳失败，
后者说明学习闭环仍然往前走了一步。

---

## 5. 可靠性机制

- **手动 ACK（Java 结果端）**：业务事务提交之后才 ACK，并且 ACK 放在业务 `try` 之外。
  事务已提交、ACK 却失败时，broker 会重投；重投的消息在 `PENDING → RUNNING` 条件更新处
  变成 `DUPLICATE` 并再次 ACK。不能把「ACK 失败」误判成「处理失败」再复制一份重试消息。
- **手动 ACK（Python 任务端）**：只有结果已发布并通过 publisher confirm 才 ACK 请求。
  结果没发出去时请求重新入队，而不是两条都丢。
- **mandatory return**：两端的发布都带 `mandatory=true`。Python 侧 channel 必须
  `on_return_raises=True` —— 默认值 `False` 会只记一条日志，publish 照常返回，
  请求随后被 ACK，结果静默丢失、任务永远 PENDING。Java 侧判断「是否被退回」必须读
  `CorrelationData.getReturned()`：Spring AMQP 在客户端 I/O 线程处理 `basic.return`
  时就写入它，broker 保证 return 帧先于随后的 ack 帧，因此 confirm future 完成时该值
  已经可靠可见。**不要**读 `RabbitTemplate.setReturnsCallback` 里自定义的标记：
  callback 在 deferred 线程池执行，可能晚于 confirm future 完成（真实 broker 测试
  可复现这个竞态）。
- **发布确认分三种结果**：Java 投递请求后等待 broker 的 `basic.ack`。
  - **确定失败**：连接异常、NACK、mandatory return（不可路由）。此时消息一定没进队列，
    把 run 置 FAILED 并退回同步分析。
  - **确定成功**：拿到 `basic.ack`，不做任何兜底。
  - **结果未知**：confirm 超时、连接在确认前中断、确认 future 抛错。消息**可能**已经在
    队列里、`可能`已被消费。这时既不能退回同步分析（会分析两遍、重复写 Evidence），
    也不能置 FAILED（任务其实还会完成）。当前处理是保留 PENDING、把「投递结果未知」
    写进 `error_message` 供排障。这不是「解决」，而是把不确定性留在可见的位置；
    真正的解法需要 Outbox，本轮不引入（见下文残余风险）。
- **结果重试副本也等确认**：复制到 `result.retry` 的消息同样等待 publisher confirm
  并通过同一个 `CorrelationData.getReturned()` 判断是否被退回；只有副本被 broker 收下
  才 ACK 原消息，否则先按 `retry-ttl-ms` 退避，再 `nack(requeue=true)`。
- **有界重试，但绝不静默丢弃**：任务消息与结果消息都限制尝试次数（`max-retries`，默认 3，
  包含首次尝试），延迟由 TTL 队列提供。Python 的 FAILED 结果与死信副本是两个独立的投递：
  任一被 confirm 即 ACK；**两边都失败时不 ACK**。此时先退避
  （`AILS_ANALYSIS_PUBLISH_FAILURE_BACKOFF_SECONDS`，默认 15 秒）再
  `nack(requeue=true)`，让 broker 稍后重投；连续失败达到
  `MAX_CONSECUTIVE_PUBLISH_FAILURES`（10 次）后停止消费并让进程非零退出，
  消息留在 broker 队列里等人工处理。用「停止消费」而不是「无限快速重投」，
  避免一个发不出去消息的 Worker 把 CPU 和队列日志打满。
- **死信**：请求与结果都无法处理时进入 `learning.analysis.dlq`。死信消息保留原
  `messageId` / `correlationId`，并带 `x-error-code`、`x-original-queue`、
  `x-original-message-id`、`x-retry-count` 与 broker 的 `x-death`。
  结果端在重试耗尽时**先**发布死信副本并等待 confirm，再置 FAILED，最后 ACK；
  顺序反过来会出现「副本还没落地、原消息已 ACK、数据库又恰好不可用」的丢结果窗口。
- **并发补挂来源**：`evidence_source` / `memory_source` 的唯一键冲突**只在确认是本轮
  预期的重复来源时**才吞掉（插入前先查、插入后再查一次确认行已存在）。
  其他唯一键/外键冲突照常抛出。两轮分析同时补挂同一个来源时，抛出去会回滚整份分析，
  连带丢掉本轮已经写好的 Evidence / Understanding，所以这一处必须容忍；
  但容忍范围不能扩大到「任何 `DuplicateKeyException`」。
- **补偿扫描只标记 STALE**：`AnalysisStaleRunCompensator` 定期把超过阈值仍未完成的
  `run_type=ANALYSIS AND status IN ('PENDING','RUNNING')` 置为 STALE。条件 UPDATE 与
  结果消费者的抢占是同一条行锁上的竞争，输的一方自然让位；扫描**不自动重投**，
  也不判 FAILED——`created_at` 只能说明「等得久了」，不能说明「一定没执行」。

### 已知窗口与残余风险

数据库写入与 MQ 发布**不是原子的**。当前实现是：

1. 先 `INSERT agent_run(PENDING)`（独立事务已提交），再投递；
2. 投递**确定失败** → 条件更新置 FAILED 并退回同步分析；
3. 投递**结果未知** → 保留 PENDING 并写诊断信息，不重试、不兜底；
4. 进程恰好在「插入之后、投递之前」崩溃 → 留下 PENDING 任务，由补偿扫描置 STALE。

**这部分边界是真实的**：状态机只能把「确认没送到」和「确认送到了」分开，
中间那一块（confirm 超时/连接中断）在没有 Outbox 的情况下无法由应用自己判定。
把超时调大不会消除这个窗口，只会改变它出现的概率。

**补偿扫描的代价**：阈值必须大于「排队 + 分析 + 结果重试」的最坏耗时。若 Worker 停机时间
超过阈值，仍在队列里的任务会被置 STALE；这**不会**让随后到达的结果失效——消费者仍接受
`STALE → RUNNING`，结果照常落库。代价是这段时间里状态显示为「可能遗留」，
需要人工或告警来判断。调大阈值会推迟发现，调小会让更多正常任务短暂显示 STALE。
默认 60 分钟、每 10 分钟扫一次。

**仍未解决**：

- **没有自动重投**：`agent_run` 没有保存完整的任务消息载荷（只有 userId / conversation /
  message 等外键），补偿扫描能标记 STALE，却不能凭空重建一条合法的任务消息。
  从外键重建看似可行，但会话历史可能已经变了，重投会基于另一份上下文再分析一次。
  因此这里选择「标记 + 人工」，而不是「自动重发」。真要做自动恢复，需要先把任务载荷
  持久化（或引入 Outbox），这超出本轮范围。
- `RUNNING` 对 Java 侧外部不可观测：Worker 开始分析时不会通知 Java，
  `PENDING` 同时覆盖「排队中」和「分析中」。没有 `started_at` / `attempt_count` /
  `last_error` 列，排障只能看 `created_at`、`error_message` 与 broker 的 `x-death`。
  要补这些字段，需要 Worker → Java 增加一条进度协议（额外的消息/接口），当前未做。
- 幂等键覆盖的是「同一条观察」：Evidence 的 `dedupe_key` 能挡住重复的相同观察，
  但模型两次输出不同措辞时仍会各写一条 Evidence。这是内容层去重的固有限制，
  不是消息系统的重试重复。
- **「结果未知」期间的重投可能带来第二次模型调用**：如果消息其实已经入队，Java 又因为
  别的原因（例如人工重新投递）再发一次，Worker 会再分析一遍。业务写入端的幂等
  （`PENDING/STALE → RUNNING` 的条件更新、Evidence 的 `dedupe_key`）保证不会写出重复数据，
  但模型调用本身不便宜。这是 at-least-once 的固有代价，不能声称「至少一次 = 业务不会重复执行」。
- 彻底消除「插入与投递之间」的窗口需要 Transactional Outbox。当前没有引入：
  compensation 已能收敛状态，而 Outbox 会新增 outbox 表、轮询/CDC 发布器与清理任务，
  实现成本明显高于当前窗口的实际损失（仅影响状态可见性，不丢已提交业务数据）。
  如果将来要求「任务状态与消息投递严格同生共死」，再单独评审 Outbox。

---

## 6. 本地运行

```bash
# 1) RabbitMQ（管理界面 http://127.0.0.1:15672，guest/guest）
cd docker && docker compose up -d

# 2) 后端：打开 mq 模式，并给 Worker 配置内部凭据
export AILS_ANALYSIS_MODE=mq
export AILS_INTERNAL_TOKEN=dev-internal-token
cd backend/backend && ./mvnw spring-boot:run

# 3) Python Worker（不需要启动 FastAPI）
cd agent
export AILS_INTERNAL_TOKEN=dev-internal-token
export AILS_RETRIEVER_MODE=http      # 分析链路不允许静默退回 Fixture
python -m app.worker
```

聊天与 SSE 仍由 `python -m app.main` 提供；Worker 是另一个进程，可以独立扩容、重启。
只想跑聊天时不需要 RabbitMQ，也不需要 Worker。

### 配置对照

| 后端（Spring） | Python（Worker） | 说明 |
| --- | --- | --- |
| `AILS_ANALYSIS_MODE=mq` | — | 打开异步分析；默认 `inline` |
| `AILS_INTERNAL_TOKEN` | `AILS_INTERNAL_TOKEN` | 必须一致；为空时 Worker 检索被拒 |
| `RABBITMQ_HOST/PORT/USER/PASSWORD/VHOST` | `AILS_RABBITMQ_URL` | 指向同一个 broker |
| `AILS_ANALYSIS_*_QUEUE` / `_ROUTING_KEY` | `AILS_ANALYSIS_*_QUEUE` / `_ROUTING_KEY` | 默认一致；改动必须两边同步 |
| `AILS_ANALYSIS_RETRY_TTL_MS` | `AILS_ANALYSIS_RETRY_TTL_MS` | 默认 15000 |
| `AILS_ANALYSIS_MAX_RETRIES` | `AILS_ANALYSIS_MAX_RETRIES` | 默认 3（含首次尝试） |
| — | `AILS_ANALYSIS_PUBLISH_FAILURE_BACKOFF_SECONDS` | 副本发不出去时 `nack(requeue=true)` 前的退避，默认 15 秒 |
| `AILS_ANALYSIS_COMPENSATION_*` | — | 补偿扫描；仅 `mq` 模式装配，默认开启，阈值 60 分钟，置 STALE |

Worker 的 `AILS_RETRIEVER_MODE` 建议显式设为 `http`。`auto` 在没有凭据时会退回 Fixture，
而分析结果会写进真实用户的长期认知，样例数据不能出现在这里；分析链路本身也传了
`allow_degradation=False`，无凭据会直接报错。

---

## 7. 测试

```bash
# Java：单元测试（无 Docker 也能跑）
cd backend/backend
./mvnw test

# Java：要求真实 RabbitMQ（Testcontainers 自己拉起 broker）
./mvnw test -Pmq-integration

# Python：消息契约 + Worker 可靠性分支（Fake 对象，不连 broker）
cd agent
python -m pytest -q

# Python：真实 broker（未设置变量时整组 skip，跳过 ≠ 通过）
AILS_TEST_RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m pytest -q
```

`AnalysisMqIntegrationTest` 用 Testcontainers 启动真实 RabbitMQ，当前覆盖 10 个场景：

| 用例 | 验证的行为 |
| --- | --- |
| `taskMessageIsConfirmedAndPersisted` | 发布拿到 confirm，消息持久化 |
| `unroutableTaskIsRejectedEvenThoughBrokerAcks` | mandatory return 把「broker 已 ack」纠正为确定失败 |
| `unroutableResultRetryCopyIsNotAckedEvenThoughBrokerAcks` | 结果重试副本不可路由时不 ACK 原消息，而是 `nack(requeue=true)` |
| `unackedRequestReturnsToQueueWhenWorkerCrashesAfterPublishingResult` | 结果已发、请求未 ACK 即崩溃 → 请求重新入队 |
| `duplicateResultDeliveryIsConsumedTwiceWithoutLeavingMessages` | 重复结果不会留下堆积 |
| `consumerCrashAfterCommitIsRetriedFromTtlQueueAndThenDeduplicated` | 业务已提交但 ACK 前崩溃 → TTL 重投 → `DUPLICATE` |
| `retryExhaustionRoutesResultToDeadLetterQueue` | 重试耗尽：先死信、再 `markFailedPermanently`、最后 ACK |
| `rejectedRequestTravelsThroughRetryQueueAndReturns` | `nack(requeue=false)` 经 broker DLX + TTL 回到请求队列 |
| `appRepublishedRetryCopyReturnsToRequestQueueWithCount` | 应用主动重投的副本经 TTL 回到请求队列，计数头保留 |
| `publishFailsAfterBrokerGoesAway` | broker 停机后投递是确定失败 |

Python 的 `tests/test_worker_broker.py` 在真实 broker 上验证两件 Fake 对象回答不了的事：

- `test_retry_copy_returns_to_request_queue_and_original_is_acked`：重试副本真的经 TTL
  回到请求队列，并且原消息确实已被 ACK；
- `test_when_no_copy_can_be_published_the_message_stays_in_the_queue`：FAILED 结果与死信
  都发不出去时，消息仍然回到队列，而不是被 ACK 丢掉。

**跳过语义**：没有 Docker 时 `AnalysisMqIntegrationTest` 会 abort（Surefire 里显示 skipped），
默认构建不会失败。CI 使用 `-Pmq-integration` 把 `ails.it.require-docker` 设为 `true`，
此时 Docker 缺失会直接让构建失败，而不是把「没跑」伪装成「通过」。
Python 的 broker 测试在未设置 `AILS_TEST_RABBITMQ_URL` 时 skip，CI 的 `agent` job 起了
`rabbitmq:3.13` service 并设置该变量。**报告时必须如实区分 passed / skipped。**

数据库事务、幂等与兜底判定由单元测试覆盖：
`AnalysisResultConsumerTest`、`AnalysisResultProcessorTest`、`AnalysisTaskServiceTest`、
`AnalysisTaskPublisherTest`、`AnalysisStaleRunCompensatorTest`、`EvidenceConcurrencyTest`、
`MemorySourceConcurrencyTest`，以及 Python 的 `tests/test_worker_reliability.py`。
CI 配置见 `.github/workflows/ci.yml`。

---

## 8. 排障

管理界面 `Queues` 页：

- `learning.analysis.request` 堆积：Worker 没起、prefetch 太小、或分析整体变慢；
- Worker 进程非零退出且日志里有 `WorkerStalledError`：连续 10 次连重试/死信副本都发布失败，
  Worker 主动停止消费以避免空转。消息仍在队列里，修好 broker/网络后重启 Worker 继续消费；
- `learning.analysis.retry` 有消息：检索/模型暂时不可用，15 秒后自动回主队列；
- `learning.analysis.dlq` 有消息：查看消息头 `x-error-code`、`x-original-queue`，
  以及 `x-death` 里的 `reason` / `count`；修复后可按需重新发布到请求队列；
- `agent_run.error_message` 是另一条线索：投递失败、校验失败、rules-v1 兜底都会留下文字。

本地改过 TTL / 队列参数后启动报 `PRECONDITION_FAILED`：删除对应队列再启动。

Docker Engine 29+ 上 Testcontainers 报 HTTP 400 / 「Could not pull image」（docker-java 3.3.6
默认协商的 API 版本低于引擎最低版本）时，给测试 JVM 指定引擎支持的版本即可，不需要改业务代码：

```bash
./mvnw -B clean test -Pmq-integration -DargLine='-Dapi.version=1.44'
```
