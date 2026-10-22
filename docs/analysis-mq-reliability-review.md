# RabbitMQ 异步学习分析链路：可靠性问题复盘

> **文档用途**：项目技术复盘、后续维护、后端实习面试准备。  
> **项目**：AI Learning Space  
> **记录范围**：本轮 RabbitMQ 异步学习分析链路的设计、故障、修复、验证结果与遗留边界。  
> **状态基准**：以下测试结果与分支信息基于本轮交付报告；后续代码变更后，应重新核对 CI 和测试结果。

## 0. 结论先行

本轮工作的目标不是“把 RabbitMQ 接进项目”，而是验证异步链路在消息确认、不可路由、重复投递、消费者崩溃、重试和死信等情况下，能否保持可解释的行为，并避免静默丢失任务或重复产生业务副作用。

本轮主要结果：

- 修复了多个消息可靠性和状态处理问题，其中包括一个由真实 RabbitMQ 测试发现的 **mandatory return 与 publisher confirm 竞态**。
- Java 真实 RabbitMQ 集成测试 **10/10 通过**；Python 真实 Broker 测试 **2/2 通过**。
- Java 全量测试 **111 通过、0 失败、0 错误、1 跳过**；跳过项是既有、依赖本地 MySQL 初始化的 `BackendApplicationTests.contextLoads`。
- Python 全量测试 **67 通过、0 跳过**（运行时设置了 `AILS_TEST_RABBITMQ_URL`）。离线评测 **40/40 通过**。
- GitHub Actions 的 Backend、Agent 两个 Job 均成功。第一次 CI 运行暴露了一个测试时序问题，修复后重跑成功。

**准确的完成状态**：MQ 核心消息处理机制已经经过真实 Broker 测试与 CI 验证，可以作为当前 MVP 的阶段性成果并暂时冻结；但这不等于整个异步任务系统拥有自动恢复能力，也不等于生产环境下绝对不会丢消息或重复调用模型。

当前仍明确存在的边界：

1. `agent_run` 创建与 MQ 发布不是同一原子事务；进程可能在写入任务后、发布消息前退出。
2. `STALE` 只是标记疑似遗留任务，当前不会自动重投或恢复。
3. 在至少一次投递语义下，特定崩溃窗口可能导致同一任务再次调用模型；下游幂等只能降低重复业务写入，不能保证模型只调用一次。
4. 当前验证使用 RabbitMQ 3.13 测试 Broker，不代表已经验证生产环境中的集群、高可用、Quorum Queue 或网络分区行为。

---

## 1. 业务背景：为什么需要异步分析

AI Learning Space 不只是聊天机器人。用户对话结束后，系统还要从对话中提取学习相关证据，并更新用户的理解状态、Evidence、Understanding、Revision 和相关 Memory。分析可能调用模型，也可能访问 Agent 服务，耗时与失败概率都高于普通数据库写入。

如果把这些分析都放在聊天请求的主路径中，可能造成：

- 聊天响应延迟被学习分析拖慢；
- 模型或 Agent 服务短暂不可用时影响聊天链路；
- 分析失败与用户对话本身的成功混在一起；
- 缺少独立的重试、死信与故障排查边界。

因此项目引入 RabbitMQ，把聊天回复与学习分析解耦。这里的 MQ 不是为了“用上中间件”，而是为了把一个耗时、可能失败的副作用，从用户等待响应的主流程中拆出去。

## 2. 当前架构与数据流

### 2.1 主链路

```mermaid
flowchart TD
    A[用户发送消息] --> B[Spring Boot ChatService]
    B --> C[保存用户消息并生成助手回复]
    C --> D[SSE 对话完成]
    D --> E[AnalysisDispatchService]
    E --> F{AILS_ANALYSIS_MODE}
    F -->|inline| G[同步调用 Agent 分析]
    F -->|mq| H[创建 agent_run: ANALYSIS / PENDING]
    H --> I[发布 learning.analysis.request]
    I --> J[Python Worker]
    J --> K[调用 Agent 分析逻辑]
    K --> L[发布 learning.analysis.result]
    L --> M[Java AnalysisResultConsumer]
    M --> N[校验任务、用户、schema 与 trace]
    N --> O[条件抢占任务状态]
    O --> P[复用 AgentAnalysisWriteService]
    P --> Q[写 Evidence / Understanding / Revision / Memory 来源等]
    Q --> R[更新 agent_run 终态]
```

### 2.2 关键职责边界

| 组件 | 职责 | 不应承担的职责 |
|---|---|---|
| Spring Boot 业务服务 | 创建任务、校验结果、写入业务数据库、维护 `agent_run` 状态 | 不应假设消息 publish 返回就表示任务已被消费 |
| RabbitMQ | 缓冲请求与结果、路由消息、支撑确认与重试拓扑 | 不负责保证业务数据库副作用只执行一次 |
| Python Worker | 调用分析逻辑，生成结果消息；按明确的 ACK/重试规则处理请求 | 不直接写学习业务数据库 |
| Java Result Consumer | 校验结果、执行幂等/条件状态转换、写入业务数据并确认消息 | 不应把 ACK 异常误当作业务事务失败 |
| MySQL | 保存任务状态与学习业务数据 | 目前并未与 MQ publish 组成一个原子事务 |

### 2.3 消息方向

```text
请求方向：Java -> learning.analysis.request -> Python Worker
结果方向：Python Worker -> learning.analysis.result -> Java Result Consumer
```

系统使用持久化消息、publisher confirm、mandatory routing 检查、手动 ACK，以及有限重试和 DLQ 处理。具体队列与交换机名称、TTL 和路由配置以 `docs/analysis-mq.md` 和当前 `RabbitMqConfig` 为准。

### 2.4 为什么保留 inline 和 mq 两种模式

项目保留 `inline` / `mq` 两种模式，默认仍为 `inline`。这让开发和回归时可以沿用简单路径，同时让 MQ 实现可以被单独验证。启用 `mq` 模式需要 RabbitMQ 服务与 `AILS_INTERNAL_TOKEN` 等相应配置。

保留双模式不表示二者可以无限期分叉。关键的业务分析与写入逻辑应复用同一套实现，差异尽量限制在任务派发方式上。

---

## 3. 核心工程原则

### 3.1 Publisher confirm 不等于消息已被消费

Publisher confirm 说明 Broker 对发布进行了确认，但它本身不能证明消息已经被消费者处理，更不能证明业务数据库已经写入成功。

对于带 `mandatory` 的消息，还需要确认消息没有被 Broker 退回。**Broker ACK 和消息可路由是两个需要一起判断的维度。**

### 3.2 超时不一定等于失败

confirm 超时、连接中断或等待被中断，可能意味着发布失败，也可能意味着 Broker 已经接收消息、但确认没能被发布方观察到。

因此发布结果要区分：

- **确定失败（`DEFINITE_FAILURE`）**：例如明确 NACK、mandatory return 或连接建立失败；
- **结果未知（`UNKNOWN`）**：例如等待 confirm 超时或确认过程中连接异常；
- **确认成功**：Broker 接受发布，且没有发生 mandatory return。

把 `UNKNOWN` 当作“确定没发出去”并立即重新分析，可能造成同一任务重复调用模型。

### 3.3 至少一次投递不等于业务恰好执行一次

消息系统在消费者崩溃或 ACK 丢失时可能重新投递。合理的设计不是依靠 ACK 宣称“不会重复”，而是允许重复到达，并通过稳定任务标识、条件状态更新、终态检查及数据库约束，尽可能避免重复业务副作用。

即便数据库写入幂等，模型调用也可能再次发生：模型计算已经完成但结果尚未可靠发布时，Worker 崩溃后重试仍可能再次调用模型。这会产生额外成本，且不同次模型输出可能不完全相同。

### 3.4 数据库提交与 ACK 不是同一个原子事务

消费者通常需要先完成业务数据库事务，再 ACK MQ 消息。如果数据库已提交、但 ACK 之前进程崩溃，Broker 会重投消息。因此重复消费必须由业务状态和幂等机制兜底。

反过来，如果在业务数据提交之前先 ACK，那么进程随后崩溃就可能造成消息已确认、业务数据却未落库，风险更高。

### 3.5 “补偿收敛状态”不等于“自动恢复任务”

把任务标记为 `STALE` 可以表达“该任务等待太久，可能需要排查”，但如果系统没有保存原始任务快照，也没有安全的重投机制，就不能称为自动恢复。

---

## 4. 问题复盘：发现了什么、为什么、怎么修

下列问题按技术机制归类。早期审计中发现的问题有的已在后续轮次继续修正，不能只看最初的修复描述；应以本节“最终状态”和最终测试报告为准。

### 4.1 mandatory return 与 publisher confirm 的竞态：最关键的真实缺陷

#### 现象

发布端使用 `mandatory=true`。当 routing key 没有绑定到目标队列时，RabbitMQ 会退回消息。旧实现通过 returns callback 更新一个旁路 tracker，再在等待 confirm 后读取 tracker 来判断消息是否可路由。

真实 Broker 测试表明：Broker 可以确认发布，同时退回不可路由消息。Spring AMQP 对 returns callback 的执行使用了异步线程；发布线程等待的 confirm future 可能先完成，而自定义 callback 还没有更新旁路状态。

于是出现危险结果：

```text
发布请求
  -> RabbitMQ 判断无可用路由
  -> RabbitMQ 退回消息（NO_ROUTE）
  -> 发布确认同时成功
  -> confirm future 先完成
  -> 自定义 returns callback 尚未更新 tracker
  -> 发布方误判“发布成功”
  -> 任务保持 PENDING，实际没有消费者能收到消息
```

#### 为什么危险

发布端可能把一个从未进入请求队列的任务当作已投递。任务可能长期停留在 `PENDING`，而业务没有收到明确的发布失败信号。

#### 根因

不能假设“自定义 return callback 已经运行”与“confirm future 已完成”之间存在同步先后关系。仅凭 callback 更新的旁路布尔状态判定 return，会受异步调度时序影响。

#### 修复

- `AnalysisTaskPublisher` 和 `AnalysisResultConsumer.publishCopy` 都改为检查 `CorrelationData.getReturned()`。
- `AnalysisReturnTracker` 不再维护 messageId 到 returned 状态的旁路映射，只保留日志用途。
- 单元测试按真实协议顺序模拟 return 到达后再完成 confirm。
- 增加真实 Broker 用例，验证请求消息和结果重试副本在不可路由时都不能被误判成功。

#### 验证证据

真实 Broker 用例覆盖：

- `unroutableTaskIsRejectedEvenThoughBrokerAcks`
- `unroutableResultRetryCopyIsNotAckedEvenThoughBrokerAcks`

这是本轮最值得在面试中讲清楚的技术点：问题不是简单“少写一个异常判断”，而是发现了异步回调与确认 future 的竞态，使用协议关联对象上的返回信息消除旁路状态依赖，并通过真实 Broker 回归。

### 4.2 Worker 的 mandatory return 处理

#### 原问题

Python Worker 使用 `aio-pika` 发布结果时，如果使用 `mandatory=True`，但 channel 未启用 return 抛异常语义，不可路由消息可能只被记录而未让 `publish` 显式失败。Worker 可能因此 ACK 原请求，造成分析结果无法到达 Java。

#### 修复方向

Worker channel 使用 `on_return_raises=True`，并把“结果发布是否成功”作为决定能否 ACK 请求消息的重要条件。必须与实际使用的 aio-pika 版本和发布模式保持一致。

#### 验证重点

真实 Broker 环境下应验证不可路由发布确实被检测，而不只是对 `publish` 方法做 mock。

### 4.3 confirm 结果未知时立即 inline 兜底，会导致重复分析

#### 原问题

旧逻辑把所有发布异常都视为失败。如果 confirm 超时，但消息事实上已经进入队列并被 Worker 处理，Java 可能立即同步执行 inline 分析。同一用户输入就可能触发两次模型分析。

#### 修复

引入明确的发布结果分类：`CONFIRMED`、`FAILED`、`OUTCOME_UNKNOWN`。只有确定失败时才走 inline 兜底；结果未知时保留任务状态并记录诊断信息，不立即同步重做。

#### 仍然存在的边界

这避免了“把未知结果直接当失败”的一类重复调用，但并没有给所有未知状态提供自动恢复能力。由于当前任务快照未持久化，未知任务可能仍需后续排查。

### 4.4 ACK 异常与业务处理异常混淆

#### 原问题

如果业务事务提交成功，随后 `basicAck` 因 channel 关闭抛出 `IOException`，旧代码把 ACK 异常捕获为业务处理失败，进而主动发布一份 `result.retry`。

此时数据库已经提交，主动复制重试消息反而可能制造重复处理。

#### 修复

- 将 ACK 放在业务处理 `try/catch` 边界之外。
- 业务事务失败才进入业务重试副本路径。
- ACK 本身失败时让异常反映到消息通道；若 Broker 因未确认消息而重投，由任务状态检查和幂等逻辑安全处理。

#### 设计取舍

不能保证 ACK 永不失败。需要保证 ACK 失败不会被错误地解释成“数据库事务失败”，并让原消息重投时能够安全地再次进入消费者。

### 4.5 重试副本未确认就 ACK 原消息

#### 原问题

如果消费者处理失败后直接发送重试副本，发送函数返回就 ACK 原消息，副本却没有得到 Broker confirm，那么可能出现：原消息被确认删除，重试副本未可靠进入队列。

#### 修复

应用主动发布重试副本时：

1. 构造重试消息；
2. 使用 publisher confirm；
3. 检查 mandatory return；
4. 只有确认副本成功发布后，才 ACK 原消息。

如果副本发布失败，就不能先 ACK 原消息。系统应退避，并保留原消息以便恢复。

#### 注意

“应用主动重新发布”与“Broker 通过 TTL/DLX 自动转发”是不同路径。前者可以由应用等待 confirm 并检查 return；后者的可靠性语义取决于队列类型、死信配置和 Broker 行为，不能把两者一概而论。

### 4.6 重试耗尽时静默 ACK 或无限重入队

#### 原问题

早期失败收尾逻辑可能出现两种相反问题：

- 达到重试上限后，FAILED 结果和死信发布都失败，但仍 ACK 原消息，形成静默丢弃；
- 发布始终失败时无条件 requeue，导致快速、无界地反复消费。

#### 修复

Java 端：

- 常规重试副本和死信副本都等待 confirm 并检查 return。
- 重试耗尽时，先确认死信副本已发布，再把任务标记为永久失败，最后 ACK 原消息。
- 如果副本无法发布，则退避并重新入队，不能先把任务标记为终态，防止重投后命中终态分支而丢失合法结果。

Python 端：

- FAILED 结果与 DLQ 副本分别尝试发布。
- 任一条失败记录被确认可靠发布后，才允许 ACK 原请求。
- 两边都失败时退避并 requeue；连续达到配置的发布失败阈值后停止消费并以非零状态退出，避免无限快速循环。

#### 取舍

停止 Worker 会牺牲短期可用性，但比无限快速 requeue 更可控。部署系统需要能发现 Worker 退出并告警；否则“停止消费”本身可能让队列积压而没有人处理。

### 4.7 PENDING 状态不能证明任务仍在执行，也不能证明任务失败

#### 原问题

补偿器最初根据创建时间将超时的 `PENDING/RUNNING` 任务直接标记 `FAILED`。但任务可能只是排队较久，或 Worker 已经执行，只是结果尚未到达。结果晚到后，因为任务已是终态，消费者会将其当作重复或无效结果丢弃。

#### 修复

增加 `STALE` 状态。过期扫描只将符合条件的任务转为 `STALE`；结果消费者的条件抢占允许 `PENDING` 和 `STALE` 进入 `RUNNING`，因此合法的晚到结果仍然可以被处理。

状态示意：

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> RUNNING: 结果到达并成功抢占
    PENDING --> STALE: 等待超过阈值
    STALE --> RUNNING: 晚到结果到达并成功抢占
    RUNNING --> SUCCEEDED: 业务写入成功
    RUNNING --> FAILED: 确定的终态失败
```

注意：实际实现中还存在确定发布失败、inline 兜底及其他终态分支，状态图应以当前 `AgentRunStatus`、mapper 条件更新和消费者代码为准。

#### 尚未解决

`STALE` 只是“可能遗留”的标记，不会自动重新投递。当前 `agent_run.input_summary` 最多 255 字符，不足以重建包含原始文本、助手回答、历史窗口和 session context 的完整任务。因此不应在扫描时直接按当前会话重新拼装并重发，否则会用到后续变化的消息历史和 session 状态，重投的已经不是原始任务。

### 4.8 DuplicateKeyException 不能一概吞掉

#### 原问题

并发分析可能为同一条 Evidence 或 Memory 补挂相同来源关系。数据库唯一约束可以阻止重复关联，但如果服务层把所有 `DuplicateKeyException` 一律当作“来源已经存在”吞掉，其他唯一键冲突或数据问题也可能被掩盖。

#### 修复

- 插入前先查是否已经存在预期来源关联；
- 捕获冲突后再查询；
- 只有确认预期的关联行确实存在时才按幂等冲突处理；
- 如果无法证明是预期的重复来源，就重新抛出异常。

这体现了一个重要原则：幂等处理应该精确到业务约束，不能通过宽泛吞异常来伪装幂等。

### 4.9 非法消息、异常分支与消费循环稳定性

早期 Python Worker 还有契约错误路径中传参不匹配、非法 JSON 无法构造失败结果、单条消息异常可能逃出 `async for` 并终止消费循环等问题。

本轮修复方向包括：

- 每条消息有独立的异常边界，未预期异常不会轻易终止整个消费循环；
- 修正失败分支参数错误；
- 在 `task_id` 不存在时不再构造要求必填 `task_id` 的结果对象；
- 对重试次数及最终失败状态施加约束；
- 死信消息保留 `message_id`、`correlation_id` 和原消息标识，方便追踪。

### 4.10 重试计数重复累计

应用重新发布的重试副本本身也可能被 Broker 记录 `x-death`。如果只读 `x-death`，应用重发次数与 Broker 死信转发记录可能重复计数。

当前 Python 任务契约优先读取显式 `x-analysis-retry`，解析失败时才回退到 `x-death`。重试计数应有单一、清晰的语义，并由测试覆盖，否则很容易提前耗尽或超出上限。

---

## 5. 状态、确认与失败处理的参考时序

### 5.1 请求发布

```text
Java 创建 agent_run(PENDING)
  -> 发布 request（mandatory + publisher confirm）
      -> confirm 成功且未 return：等待结果
      -> 明确失败：按现有策略标失败并允许 inline 兜底
      -> 结果未知：保留 PENDING 与诊断信息，不立即 inline 重做
```

### 5.2 Python Worker 正常处理

```text
Worker 收到 request（尚未 ACK）
  -> 调用分析逻辑
  -> 发布 result（等待 confirm，并检查不可路由）
  -> result 确认发布成功
  -> ACK 原 request
```

如果结果发布失败，不应先 ACK 原请求。若持续无法发布，按配置退避；达到连续失败保护阈值后停止消费并保留未确认消息，由运行环境告警和恢复。

### 5.3 Java 结果消费

```text
Java 收到 result（手动 ACK）
  -> 校验 schema、trace、任务所有权等
  -> 条件抢占任务状态
  -> 执行业务事务并提交
  -> ACK result
```

如果数据库事务提交后 ACK 失败，原结果可能重新投递。此时任务终态检查应将其识别为重复结果并安全 ACK，而不主动再复制一份业务重试消息。

### 5.4 为什么重试副本必须先确认，再 ACK 原消息

```text
原消息仍处于未确认状态
  -> 发布重试副本
      -> 副本 confirm 成功且未 return：ACK 原消息
      -> 副本发布失败或结果未知：不得先 ACK 原消息
```

如果在副本发布尚未得到可靠确认时就 ACK 原消息，副本丢失就可能使任务没有任何可处理消息。

---

## 6. 为什么本轮没有引入 Transactional Outbox

### 6.1 当前原子性缺口

目前数据库写入 `agent_run(PENDING)` 与 RabbitMQ publish 不是一个原子事务。存在如下窗口：

```text
INSERT agent_run(PENDING) 成功并提交
  -> 进程退出
  -> MQ 消息尚未发布
```

因此数据库里可能存在任务，但队列里没有对应请求。当前补偿器只能将其标记为 `STALE`，不能自动恢复。

### 6.2 为什么不能靠重新查询当前会话恢复

任务载荷包含的上下文不止一个 conversation ID，通常还包含：原始用户文本、助手回答、最近一段历史、消息标识、session context 等。当前消息历史和 session 的 goal/currentFocus 会继续变化。

如果任务过期后重新查询最新会话并重建消息，得到的是一份新的上下文，而不是原任务快照。这样可能改变证据来源和分析结果，不能称作严格的原任务重放。

### 6.3 可行的后续方案

如果将来业务要求“遗留分析任务能自动恢复”，可考虑持久化完整任务快照，并基于稳定 taskId、状态条件更新、租约、尝试次数上限、退避与告警实现有界重投。

一种候选数据结构是独立的 `analysis_task_payload` 表，保存 `run_id`、`schema_version`、`payload_json`、创建时间；`agent_run` 增加投递尝试、下一次派发时间、租约截止时间、上次派发时间等字段。创建任务和快照应在同一数据库事务中完成，随后再发布消息。

这是一项**待评审的后续方案，不是当前已经实施的功能**。它会增加：

- 聊天内容重复存储及 PII 保留期限管理；
- 调度器扫描、条件抢占和多实例租约竞争；
- 重试风暴控制；
- schemaVersion 演进与旧任务兼容；
- 重复模型调用与非确定性输出；
- 旧任务没有快照、无法安全自动重放的问题。

是否引入 Outbox 或轻量任务快照方案，应由实际的可靠性需求决定，不应只因为“有一个理论上的窗口”就无条件扩建。

---

## 7. 本轮验证结果与测试证据

### 7.1 已执行测试

| 验证项 | 实际结果 | 说明 |
|---|---:|---|
| Java 真实 RabbitMQ 集成测试 | 10/10 通过 | Testcontainers 使用 `rabbitmq:3.13`；0 失败、0 跳过 |
| Java 全量测试 | 111 通过 | 0 失败、0 错误、1 跳过；跳过项是既有 MySQL 前置条件测试 |
| Python 真实 Broker 测试 | 2/2 通过 | 设置 `AILS_TEST_RABBITMQ_URL`，测试实际连接 RabbitMQ |
| Python 全量测试 | 67 通过 | 0 跳过 |
| 离线评测 | 40/40 通过 | 该结果只代表离线评测，不等同于全链路线上评测 |
| GitHub Actions | Backend、Agent 均成功 | 以最后一次成功运行记录为准 |

### 7.2 Java 真实 Broker 测试覆盖

报告列出的 10 个场景包括：

1. `taskMessageIsConfirmedAndPersisted`：publisher confirm 与消息持久化。
2. `unroutableTaskIsRejectedEvenThoughBrokerAcks`：Broker confirm 成功但消息被 mandatory return 时，应判定为发布失败。
3. `unackedRequestReturnsToQueueWhenWorkerCrashesAfterPublishingResult`：Worker 发布结果后、确认原请求前崩溃，未确认请求重新入队。
4. `duplicateResultDeliveryIsConsumedTwiceWithoutLeavingMessages`：重复结果能够消费与收敛。
5. `consumerCrashAfterCommitIsRetriedFromTtlQueueAndThenDeduplicated`：数据库提交后、ACK 前崩溃，消息重投后命中重复处理路径。
6. `retryExhaustionRoutesResultToDeadLetterQueue`：重试耗尽进入 DLQ，状态更新与 ACK 顺序正确。
7. `rejectedRequestTravelsThroughRetryQueueAndReturns`：请求通过重试/TTL 路径返回原请求队列。
8. `appRepublishedRetryCopyReturnsToRequestQueueWithCount`：应用主动重发副本后回到请求队列，计数头保留。
9. `publishFailsAfterBrokerGoesAway`：Broker 不可用时识别发布失败。
10. `unroutableResultRetryCopyIsNotAckedEvenThoughBrokerAcks`：结果重试副本不可路由时，不 ACK 原结果消息。

Python 的两个真实 Broker 测试覆盖：应用重发副本经过 TTL 返回请求队列且原消息按预期 ACK；以及结果和 DLQ 均无法发布时原消息不能被 ACK 丢弃。

### 7.3 CI 过程中的失败与修复

第一次 CI 运行失败，原因是测试在 `convertAndSend` 后立即 `basicGet`，较慢 Runner 上消息尚未完成路由，测试立即读到 `null`。修复方式是增加有 deadline 的轮询，最终断言仍要求消息真正到达，没有删除测试或降低断言。

另外，真实 Broker 测试先后暴露了测试与实现问题：

- 投递模式断言读取了发送端字段，而接收消息应检查 `receivedDeliveryMode`；
- 使用 `CachingConnectionFactory` 关闭缓存 channel 并没有模拟物理连接崩溃，因此改用原生 RabbitMQ 连接，在关闭整个连接时验证未 ACK 消息重新入队；
- mandatory return 与 confirm 的异步竞态是真实业务实现缺陷，最后通过 `CorrelationData.getReturned()` 检查和真实 Broker 回归测试修复；
- Python 测试误以为 `Queue.get(timeout=...)` 会等待未来消息。实际上队列为空时 `basic.get` 可以立刻返回空，因此改为带 deadline 的轮询。

这里重要的复盘方法是：每次失败都要区分**业务缺陷、测试实现错误、环境问题**，再决定修哪里。不能看到测试失败就一律改业务代码，也不能把失败都归结为测试不稳定。

### 7.4 复现命令

Java 全量测试与真实 MQ 集成：

```bash
cd backend/backend
./mvnw -B clean test -Pmq-integration
```

本地 Docker Engine 与 docker-java API 版本不兼容时，本轮测试使用过以下环境参数：

```bash
cd backend/backend
./mvnw -B test -Pmq-integration \
  -Dtest=AnalysisMqIntegrationTest \
  -DargLine='-Dapi.version=1.44'
```

Python 全量测试（真实 Broker 环境）：

```bash
cd agent
AILS_TEST_RABBITMQ_URL=amqp://guest:guest@localhost:5672/ python -m pytest -q
```

Python 真实 Broker 测试也可以单独运行：

```bash
cd agent
AILS_TEST_RABBITMQ_URL=amqp://guest:guest@localhost:5672/ \
  python -m pytest -q tests/test_worker_broker.py -rs
```

离线评测：

```bash
PYTHONPATH=.:agent python -m evaluation
```

实际项目中的运行方式和 profile 名称应以当前仓库配置为准；如果代码变更或 CI 配置变更，必须重跑相关命令，不能沿用历史测试结果代替当前验证。

---

## 8. 当前遗留风险清单

| 风险 | 当前处理状态 | 现在是否要继续做 |
|---|---|---|
| 创建任务后、发布请求前进程崩溃 | 可能遗留 `PENDING/STALE`；没有自动重投 | 暂不；等业务要求任务自动恢复时再评审 |
| confirm 结果未知 | 保留状态、不立即 inline 兜底；无完整自动恢复 | 接受已知限制，完善观测与人工处理流程 |
| Worker 崩溃造成重复模型调用 | 业务写入做幂等收敛，但模型调用可能重复 | 当前 MVP 可接受；若成本/一致性要求提高再加 task lease/claim |
| 相同内容的不同模型输出 | 内容去重不能识别所有语义重复 | 通过评估观察，不承诺完全消除 |
| PENDING 同时代表排队或执行中 | `STALE` 改善超时语义，但 Worker 不回报实时进度 | 当前不继续扩建；必要时增加 Worker 进度/租约协议 |
| 生产 Broker 集群、高可用与网络分区 | 本轮未验证 | 部署生产环境前按真实拓扑进行额外演练 |
| Java 全量测试中的 MySQL 前置条件测试 | 本轮环境跳过既有 `BackendApplicationTests.contextLoads` | 若要证明整套应用与真实 MySQL 兼容，需要额外的 MySQL 集成环境 |
| PR 合并状态 | 报告时 PR #1 为 open，CI 已成功 | 按项目代码审查流程合并，并以合并提交为开发基线 |

### 8.1 当前不应宣称的事情

不应该说：

- “MQ 保证消息绝不丢失”；
- “整个系统已经 exactly-once”；
- “重复模型调用已经完全消除”；
- “STALE 会自动恢复任务”；
- “已经验证生产 RabbitMQ 集群故障”；
- “所有数据库与消息发布操作都是原子事务”。

可以准确地说：

- 当前核心消息链路经过真实 RabbitMQ 集成测试和 CI 验证；
- 关键发布确认、不可路由、ACK、重试、DLQ 和重复投递场景有测试覆盖；
- 业务写入依靠条件状态更新与幂等规则降低重复副作用；
- 自动恢复与生产高可用仍有明确的未完成边界。

---

## 9. 后端实习：如何把这个问题讲清楚

### 9.1 一句话版本

我在 AI Learning Space 中将学习分析从聊天请求链路拆成 RabbitMQ 异步任务，并围绕 publisher confirm、mandatory return、ACK、重试、死信、状态竞争和幂等消费做可靠性审计；期间通过真实 Broker 测试发现并修复了一个 return 与 confirm 的竞态，最终让 Java 和 Python 的真实 MQ 集成测试以及 CI 通过。

### 9.2 STAR 结构

**S — Situation（背景）**  
聊天结束后系统需要调用学习分析逻辑，将对话转化为 Evidence，并更新理解状态与记忆。直接在聊天链路中完成所有分析会增加响应链路负担，也使模型服务故障影响聊天流程。

**T — Task（任务）**  
将分析改为异步处理，并保证请求发布失败、Worker 崩溃、结果重复投递、数据库提交后 ACK 失败、重试耗尽等情况下，系统不会轻易静默丢弃任务或重复写入业务数据。

**A — Action（行动）**  
- 使用 Java 创建分析任务并向 RabbitMQ 发布请求，由 Python Worker 执行分析，结果再交回 Java 校验和写库。
- 使用 publisher confirm 和 mandatory return 判断发布是否成功，并将确定失败与结果未知区分处理。
- 通过手动 ACK、重试副本确认、DLQ、有限重试与失败退避管理异常路径。
- 使用条件状态更新、任务终态检查、数据库唯一约束与来源关联校验处理重复投递及并发冲突。
- 使用 Testcontainers 运行真实 RabbitMQ 场景，在一次测试中发现并修复不可路由消息的真实确认竞态。
- 将测试接入 CI，并根据实际失败区分业务缺陷、测试代码问题和环境问题。

**R — Result（结果）**  
Java 真实 RabbitMQ 集成测试 10/10 通过；Python 真实 Broker 测试 2/2 通过；Java 全量测试 111 通过、1 个既有 MySQL 前置条件测试跳过；Python 全量测试 67 通过；CI 的 Backend、Agent Job 成功。任务快照持久化与 STALE 自动恢复仍未实现，因此没有将结果夸大为完整的自动故障恢复系统。

### 9.3 简历描述建议

**AI Learning Space｜异步学习分析与消息可靠性治理**  
技术栈：Spring Boot、RabbitMQ、FastAPI、Python、MySQL

- 基于 RabbitMQ 构建 Java 任务派发、Python Worker 分析、Java 结果校验与落库的异步链路，将学习分析从聊天主流程解耦，并保留 inline/MQ 双模式。
- 针对不可路由消息、Publisher Confirm 与 mandatory return 竞态、confirm 超时、重复投递及 ACK 异常等场景，完善发布确认、手动 ACK、有限重试和死信处理。
- 通过条件状态更新、任务终态检查与来源关联唯一约束降低重复消费带来的业务副作用；补充 `STALE` 任务标记与异常排查机制。
- 使用 Testcontainers 与真实 RabbitMQ 验证发布确认、不可路由、消费者崩溃、重试、DLQ 和重复投递；Java 集成测试 10/10 通过，Python Broker 测试 2/2 通过，并接入 CI。

简历上应保持边界诚实：不要写“消息绝不丢失”“Exactly Once”“实现任务自动恢复”，因为当前实现没有提供这些保证。

### 9.4 面试官可能追问的问题

#### Q1：为什么 Broker confirm 成功，消息还是可能没进入队列？

因为 publisher confirm 与消息路由不是完全相同的判断。带 mandatory 的消息可能因为不可路由被退回，而 Broker 仍然对发布进行确认。发布方需要同时检查 confirm 与 returned message。

#### Q2：你们是怎么发现 return/confirm 竞态的？

最初单元测试没有充分体现 Spring AMQP 的回调调度时序。真实 Broker 集成测试出现了“测试预期抛异常但没有抛”的失败，日志却能看到 `NO_ROUTE` return。进一步检查后发现自定义 returns callback 异步执行，可能晚于 confirm future 完成。最后改为在 confirm 完成后检查 `CorrelationData.getReturned()`，并用真实 Broker 回归验证。

#### Q3：为什么 confirm 超时不能直接 inline 重试？

超时表示调用方没有及时观察到确认结果，不证明 Broker 一定没有收到消息。如果原消息已入队，inline 重试可能导致同一输入执行两次分析。因此系统区分确定失败与结果未知；只有确定失败时才走 inline 兜底。

#### Q4：ACK 为什么要在数据库事务之后？

如果先 ACK 再写数据库，随后进程崩溃可能造成消息已从队列确认移除、业务数据却没有落库。先写库再 ACK 虽然可能在 ACK 失败后重复投递，但可以通过幂等消费处理；这是更容易恢复的失败方向。

#### Q5：数据库事务已提交，但 ACK 失败怎么办？

不能把 ACK 异常当作业务写入失败并主动复制一份重试消息。应该允许原消息因未确认而重新投递，再由任务状态与幂等检查识别重复结果，安全完成 ACK。

#### Q6：重试消息为什么要先 confirm 再 ACK 原消息？

因为如果原消息先 ACK，而重试副本没有可靠到达 Broker，就可能让任务失去任何可消费副本。只有在副本发布被确认且没有 return 后，才应该确认原消息。

#### Q7：你们实现了 exactly-once 吗？

没有。MQ 可能在消费者崩溃后重投；数据库状态检查和幂等约束主要是降低重复业务副作用。尤其是模型调用与消息发布不是同一个原子事务，模型调用仍可能重复。

#### Q8：为什么没有引入 Transactional Outbox？

当前目标是完成 MVP 的核心异步链路，并用测试验证主要可靠性边界。现阶段接受 `agent_run` 创建和 MQ 发布之间的窗口，以 `STALE` 标记异常任务，但不声称能够自动恢复。若业务开始要求遗留任务最终自动完成，再设计任务快照持久化、租约、有界重投和告警，并评估 Outbox 的收益与成本。

#### Q9：`STALE` 与 `FAILED` 有什么区别？

`STALE` 表示任务等待过久、可能遗留，不能仅根据时间判断任务实际失败；`FAILED` 是业务已经认定无法继续或进入明确终态。允许晚到结果从 `STALE` 继续被处理，可避免过期扫描抢先终结一个实际已执行完成的任务。

#### Q10：你怎么验证不是“单测绿了但系统实际有问题”？

使用 Testcontainers 在真实 RabbitMQ 上验证 confirm、return、TTL/DLX、重投和队列中的真实消息状态，并把集成测试接入 CI。与此同时，明确哪些情况仍没有覆盖，例如生产集群、高可用、真实 MySQL 事务竞争及自动恢复。

---

## 10. 推荐的后续行动

当前建议暂时冻结 MQ，不再为了理论上的完整性扩建队列机制。按下面顺序继续：

1. 确认可靠性修复 PR 已按项目流程审查和合并，之后的开发基于通过 CI 的提交。
2. 只有未来出现新的可复现失败，或业务确实要求遗留任务自动完成时，才重新打开 MQ 模块。
3. 将主要开发时间切回 AI Learning Space 的 RAG：文档导入、切块、向量索引、Space 隔离、Top-K 检索、来源引用和检索评估。
4. RAG 接入后，再评估 Context Builder 的上下文组合是否有效，避免同时修改消息系统和检索系统，导致故障来源难以定位。

### 阶段性验收标准

当前 MQ 阶段可以暂时结束，因为：

- 关键真实 Broker 场景已经执行，不是全被跳过；
- 关键 CI Job 已成功；
- 已知的自动恢复缺口已写明，未被伪装成已解决；
- 新增机制已经停止，后续可以把注意力转回产品和检索闭环。

---

## 附录 A：相关代码位置

以下路径根据本轮报告整理，若目录结构后续发生变化，以仓库当前文件为准。

### Java

- `backend/backend/src/main/java/com/ails/backend/mq/AnalysisTaskPublisher.java`：任务消息发布、confirm 与 return 检查。
- `backend/backend/src/main/java/com/ails/backend/mq/AnalysisTaskService.java`：任务创建及发布结果分类。
- `backend/backend/src/main/java/com/ails/backend/service/AnalysisDispatchService.java`：inline/MQ 派发入口。
- `backend/backend/src/main/java/com/ails/backend/mq/AnalysisResultConsumer.java`：结果消费、重试副本、DLQ 与 ACK 边界。
- `backend/backend/src/main/java/com/ails/backend/mq/AnalysisResultProcessor.java`：结果校验、条件抢占与业务写入调用。
- `backend/backend/src/main/java/com/ails/backend/mq/AnalysisStaleRunCompensator.java`：过期任务扫描及 `STALE` 标记。
- `backend/backend/src/main/java/com/ails/backend/enums/AgentRunStatus.java`：任务状态枚举。
- `backend/backend/src/main/java/com/ails/backend/mapper/AgentRunMapper.java`：条件状态更新与扫描 SQL。
- `backend/backend/src/main/java/com/ails/backend/service/EvidenceService.java`：Evidence 来源关联与幂等冲突处理。
- `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java`：Memory 来源关联与幂等冲突处理。
- `backend/backend/src/test/java/com/ails/backend/mq/AnalysisMqIntegrationTest.java`：Java 真实 RabbitMQ 集成测试。

### Python

- `agent/app/messaging/worker.py`：Worker 消费、结果发布、失败收尾和停滞保护。
- `agent/app/messaging/task.py`：消息契约与重试计数。
- `agent/app/worker.py`：Worker 启动入口。
- `agent/tests/test_worker_reliability.py`：Worker 可靠性单元测试。
- `agent/tests/test_worker_broker.py`：Python 真实 Broker 测试。

### 交付与文档

- `docs/analysis-mq.md`：当前 MQ 架构与运维说明。
- `.github/workflows/ci.yml`：CI 流程。
- `backend/backend/pom.xml`：Maven 集成测试 profile。
- 本轮报告记录的修复分支：`fix/rabbitmq-reliability-verification`。
- 本轮报告记录的提交：`05b7e55`、`2b715a1`。

> 提醒：提交 hash、PR 状态、测试数量可能随后续维护变化。面试或代码审查前，应以当前仓库的 `git log`、PR 页面与 CI 页面为准。
