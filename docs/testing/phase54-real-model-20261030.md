# 阶段 5.4：MQ 真实模型、文档引用、重复采样与并发压测（2026-10-30）

> **结论先行：** 在独立端口上补跑了 5.3 未覆盖的四类真实验收：
>
> | 项目 | 结果 | 性质 |
> | --- | --- | --- |
> | Qdrant 文档引用 E2E | **28 PASS / 0 FAIL** | 真实 Qdrant + 真实 Embedding + 真实 `deepseek-flash` |
> | MQ 异步分析（真实模型） | **19 PASS / 0 FAIL** | Spring MQ → RabbitMQ → Worker → Agent → Result Consumer → MySQL |
> | 重复采样 | **3 轮 × 47 PASS / 0 FAIL** | 同一服务端 E2E 连续重跑；不含人工评分 |
> | 并发压测 | **3 组 × 10 PASS / 0 FAIL** | 6/12、12/24、24/24；单实例、单机基线 |
>
> 过程中发现并修复了 3 个真实并发/顺序缺陷：
>
> 1. `SELECT MAX(message.sequence_no) ... FOR UPDATE` 的间隙锁导致**跨对话死锁**；
> 2. 并发首次 Session 归属产生**重复 Session + 外键锁升级死锁**；
> 3. `message.end` 先于 `agent_run` 终态提交，客户端收到结束后立刻查库会看到 `RUNNING`。
>
> 同时保留了一次真实模型失败样本：MQ Worker 首轮 `ConnectError`、重试拿到 HTTP 200 但
> JSON 非法，run 落 `FAILED`，由 `rules-v1` 兜底写 `UNKNOWN` 观察；这次**没有**把失败
> 包装成通过。
>
> **仍然不是完整产品验收：** Flutter 未实现；没有人工评分、留存或教学效果指标；
> 并发只到 24 个短问题、单机单实例；MQ 只有本地 broker；文档引用只用了两篇合成文档。

---

## 1. 环境与边界

| 组件 | 本轮实例 | 说明 |
| --- | --- | --- |
| Spring Boot | 8081（inline）、8082（MQ） | 不占用用户原有的 8080 |
| Python Agent | 8000（inline）、8001（MQ） | 真实 `deepseek-flash` |
| MySQL | 本机 8.x | 测试库 `ails_phase53` / `ails_phase54_rag` / `ails_phase54_mq` |
| Qdrant | `127.0.0.1:6333` | collection `ails_phase54_text-embedding-v4_1024` |
| RabbitMQ | 本机 compose | 独占测试拓扑 `learning.analysis.phase54.*` |
| Embedding | `text-embedding-v4`，1024 维 | 真实向量，不是 `hash` |
| LLM | `deepseek-flash` | `temperature=0.6`、`max_tokens=4096` |

密钥只从本地 gitignored 配置 / 环境变量注入，本文不记录任何 key、密码或内部 token 值。
Agent 进程**不持有** `AILS_INTERNAL_DELEGATION_SECRET`；需要后端委托身份的脚本由外层
环境签发，Worker 只原样转发消息里的 `backendIdentity`。

---

## 2. Qdrant 文档引用 E2E

脚本：[`scripts/phase54-rag-citation-e2e.py`](../../scripts/phase54-rag-citation-e2e.py)

### 2.1 场景

通过公开 API 注册新用户、建空间、建会话，上传两篇合成 Markdown：

| 文档 | 内容 | 用途 |
| --- | --- | --- |
| `orbit_current_<stamp>.md` | 当前轨道周期 **17 秒**，标记 `P4-TARGET-7K2` | 现行值 |
| `orbit_legacy_<stamp>.md` | 历史轨道周期 **12 秒**，标记 `P4-DISTRACTOR-3M8` | 旧值 / 干扰项 |

等待两篇文档 `READY` 后，脚本只读核对：

- Qdrant collection 存在，维度 1024 与 Embedding 配置一致；
- 每篇文档的 point 数等于 `chunkCount`；
- payload 的 `user_id` / `space_id` / `document_id` 归属正确，且带正文。

然后用后端委托身份调用 `/agent/context/preview`，确认：

- `knowledgeStatus=SUCCESS`；
- 注入片段 ID 为 `[3, 4]`，sections 含 `knowledge:3`、`knowledge:4`；
- 现行文档标记确实进入上下文。

最后跑 3 轮真实模型聊天：

1. 当前轨道周期是多少？
2. 历史资料里的轨道周期是多少？
3. 与文档无关的问题（镓的熔点）。

### 2.2 结果

正式轮产物：`backend/backend/target/phase54-rag-e2e/20261030-213447/summary.json`

**28 PASS / 0 FAIL**，关键证据：

| 检查 | 结果 |
| --- | --- |
| collection / 维度 | `ails_phase54_text-embedding-v4_1024`，1024 vs 1024 |
| point 数 vs `chunkCount` | target 1/1，legacy 1/1 |
| preview 知识状态 | `SUCCESS` |
| preview 注入片段 | `ids=[3, 4]` |
| 当前值回答 | 365 字，含 `17 秒`，引用标签全部能回跳到 Qdrant point |
| 历史值回答 | 348 字，含 `12 秒`，引用标签全部能回跳 |
| 无关问题 | 332 字，没有 `资料《…》#n` 标签，明确说明资料不足 |
| SSE | 3 轮均 `200`，`message.start → delta → end`，无 `message.error` |

引用标签正则与 Qdrant 核对使用 `资料《文件名》#序号`；本轮每篇文档只有 1 个 chunk，
所以序号都是 `#0`。这验证的是**标签可回跳**，不是事实正确性。

### 2.3 首轮两个失败是测试脚本问题

首轮产物：`.../20261030-213359/summary.json`（**26 PASS / 2 FAIL**）

| 失败 | 原因 | 修复 |
| --- | --- | --- |
| `preview 注入现行文档标记` | 脚本读 section 的 `content`，但 preview 契约字段是 `preview` | 改读 `s.get("preview")` |
| `当前值未把旧值 12 秒当成现行值` | 模型正确地把 12 秒标成历史资料，脚本只要出现 12 秒就判失败 | 允许 12 秒出现，但要求同时出现 legacy / 历史等限定语 |

这两个失败不是产品缺陷，也没有被删除；正式轮重跑后保留两份产物。

### 2.4 边界

- 只有两篇文档、每篇 1 个 chunk，没有多 chunk 排序、Reranker、Hybrid Search；
- 引用校验仍是格式 + 上下文标签 + Qdrant point 映射，不是语义事实核查；
- 没有验证文档删除 / 重新索引后的向量清理；
- 没有人工评分，不能把 28/0 写成「回答正确率 100%」。

---

## 3. 并发压测

脚本：[`scripts/phase54-concurrency-e2e.py`](../../scripts/phase54-concurrency-e2e.py)

### 3.1 方法

- 每个请求使用**独立会话**，避免同一会话的 `sequence_no` 互相竞争；
- 问题是同一句短问题：「请用一句话解释 TLB 缓存的是什么。」；
- `ThreadPoolExecutor` 同时发起 N 个 SSE 请求，记录 HTTP 状态、事件序列、
  首字延迟、总耗时和回答；
- MySQL 只读核对：`agent_run` 终态、助手 TEXT 消息内容与 SSE 拼接逐字一致、
  `message.truncated=0`、没有 `ANALYSIS FAILED`；
- 不声称 SLO：单机、单实例、默认线程池（`chatStreamExecutor` core 16 / queue 128）。

### 3.2 失败与根因

#### 运行 A：6 并发 / 12 总请求（`20261030-213516`，4 PASS / 6 FAIL）

- HTTP 200 只有 9/12，3 个请求 **500**，在 327–359 ms 内结束、没有任何 SSE 事件；
- 另外 2 个请求 HTTP 200，但在约 **79.6 s / 79.7 s** 以 `message.error` 结束；
- Spring 日志：`DeadlockLoserDataAccessException`，SQL 是
  `INSERT INTO message (... sequence_no ...)`。

根因：旧实现先在事务里锁 `conversation` 行，再执行

```sql
SELECT sequence_no FROM message
WHERE conversation_id = ?
ORDER BY sequence_no DESC LIMIT 1 FOR UPDATE
```

`message(conversation_id, sequence_no)` 是普通唯一索引。空对话上的
`ORDER BY ... LIMIT 1 FOR UPDATE` 会在索引区间上加**间隙锁**，相邻对话的锁定区间
互相覆盖；并发写不同对话时互相等待，形成死锁。

修复：

- `conversation` 增加 `last_message_seq INT NOT NULL DEFAULT 0`；
- 分配序号改为 `UPDATE conversation SET last_message_seq = last_message_seq + 1`
  后在同一事务读回；
- 只锁 conversation 主键行，没有范围扫描 / 间隙锁；
- `Conversation.lastMessageSeq` 标记 `updateStrategy = NEVER`，防止先读后改的
  `updateById` 把并发分配出去的序号覆盖回去；
- 旧库执行 `schema-migration-phase54-message-seq.sql`，按 `MAX(message.sequence_no)`
  回填计数器。

#### 运行 B：12 并发 / 24 总请求（`20261030-214545`，9 PASS / 1 FAIL）

HTTP 和 SSE 全部正常；唯一失败是「落库内容与 SSE 拼接逐字一致」11/12。
原因是脚本取「最新一条 ASSISTANT 消息」时取到了本轮测试卡
（`message_type=TEST`），不是回答文本。修复：SQL 过滤 `message_type='TEXT'`。
这是测试脚本缺陷，不是产品缺陷。

#### 运行 C：6 并发 / 12 总请求（`20261030-214619`，4 PASS / 6 FAIL）

- 1 个请求 500，其余 11 个正常；
- Spring 日志：`DeadlockLoserDataAccessException`，SQL 是
  `UPDATE learning_session SET ... WHERE id=?`。

根因：同一空间下多条对话同时发出第一条学习消息时，每个事务都看到「没有可归属的
Session」而各自 CREATE 出重复 Session；同时 `conversation.session_id` 的外键检查
给 session 行加共享锁，随后 `sessionMapper.updateById` 要升级为排他锁。两个事务
各持 S 锁等 X 锁，死锁回滚一个。

修复：

- `LearningSpaceMapper.lockById()` 在首次归属判定前锁空间行，把同一空间的
  「第一次 Session 解析」串行化；不同空间仍可并发；
- `SessionService.attach()` 固定顺序：**先** `UPDATE learning_session`（X 锁），
  **再** 写 `conversation.session_id`（触发 FK 检查），避免 S → X 升级。

### 3.3 修复后结果

| 产物 | 并发 / 总数 | 结果 | 首字 p50 / p95 / max | 总耗时 p50 / p95 / max | 墙钟 |
| --- | --- | --- | --- | --- | --- |
| `20261030-215226` | 6 / 12 | **10 PASS / 0 FAIL** | 2264 / 2764 / 3344 ms | 2921 / 3391 / 3688 ms | 6014 ms |
| `20261030-215256` | 12 / 24 | **10 PASS / 0 FAIL** | 3484 / 5202 / 5530 ms | 3875 / 5843 / 6233 ms | 11390 ms |
| `20261030-215327` | 24 / 24 | **10 PASS / 0 FAIL** | 4438 / 9092 / 9827 ms | 4641 / 10265 / 11093 ms | 11140 ms |

三组都核对：

- HTTP 200 全部成功，SSE 契约全部正常，回答全部非空；
- 每个会话都有 `ANALYSIS` run 且到终态，没有 `ANALYSIS FAILED`；
- 落库 TEXT 内容与 SSE 拼接逐字一致，`message.truncated` 全为 0。

修复后对三组另做只读 SQL 核对：

| 组 | 会话数 | `learning_session` | 助手 TEXT 消息 | 测试卡 |
| --- | --- | --- | --- | --- |
| 6 / 12 | 12 | 1 | 12 | 0 |
| 12 / 24 | 24 | 1 | 24 | 0 |
| 24 / 24 | 24 | 1 | 24 | 0 |

即并发首次解析没有重复创建 Session，也没有把测试卡误当成回答。

### 3.4 边界

- 24 个短问题不是持续压测，也不是容量测试；
- 所有请求共享同一台机器、同一个 MySQL / Agent / 模型供应商；
- 首字延迟主要受模型供应商影响，不能当成服务端处理能力指标；
- 没有多实例、分布式锁、断线重连、限流或降级验证；
- 上游 `RemoteProtocolError` 仍可能让单个请求在约 80 s 后以 `message.error` 结束，
  这不是本轮修复的数据库死锁，属于供应商连接不稳定。

---

## 4. MQ 真实模型 E2E

脚本：[`scripts/phase54-mq-real-e2e.py`](../../scripts/phase54-mq-real-e2e.py)

### 4.1 链路

```text
Spring 8082 (AILS_ANALYSIS_MODE=mq)
  → publish learning.analysis.phase54.request
  → RabbitMQ
  → Worker（脚本自行启动 / 停止）
  → Agent 8001 POST /agent/analyze（真实 deepseek-flash）
  → publish learning.analysis.phase54.result
  → Spring Result Consumer
  → MySQL（agent_run / learning_evidence / understanding / revision）
```

测试拓扑使用独立 exchange / queue / retry / DLQ，避免与常驻 Worker 争用：

```text
learning.analysis.phase54.exchange
learning.analysis.phase54.request
learning.analysis.phase54.result
learning.analysis.phase54.retry
learning.analysis.phase54.dlq
learning.analysis.phase54.result.retry
```

测试库：`ails_phase54_mq`。

### 4.2 正式轮（`20261030-220957`，19 PASS / 0 FAIL）

| 检查 | 证据 |
| --- | --- |
| 聊天 SSE | HTTP 200 / 683 events，正常 `message.start → delta → end` |
| Worker 未启动时 | ANALYSIS run `PENDING`，没有 Evidence |
| Worker 启动后 | run `SUCCEEDED` |
| 真实模型调用 | Worker 日志含 `LLM_CALL adapter openai-compatible:deepseek-flash` |
| Evidence | 1 条，`analyzer_version=agent-v1` |
| 来源回跳 | `source_type=MESSAGE`，指向本轮用户消息 |
| Understanding / Revision | `MISCONCEPTION`，1 条 Revision |
| 重复 taskId | Worker 处理 2 次（`count=2`），Evidence / Revision 均未增加 |
| Result Consumer | 日志识别「分析结果重复投递，已忽略」 |

这条链路证明：MQ 模式下真实模型分析、结果消费、Evidence 落库和重复投递幂等都能走通。
它不是 Testcontainers 集成测试，broker 是本地单实例。

### 4.3 保留的真实失败样本（`20261030-220047`，15 PASS / 4 FAIL）

同一条链路在另一轮遇到供应商不稳定：

1. 第一次调用 `模型连接失败：ConnectError`，分类 `LLM_UNAVAILABLE`、可重试；
2. 重试拿到 HTTP 200，但响应不是合法 JSON：
   `Expecting ',' delimiter`，分类 `ANALYSIS_INVALID`、不可重试；
3. ANALYSIS run 落 `FAILED`，耗时约 83 s；
4. Spring 用独立 `rules-v1` 兜底 run 写了 1 条 `UNKNOWN` 观察，**没有**冒充
   `agent-v1` 分析，也没有推进掌握状态；
5. 之后脚本补投的同一 taskId 被 Worker 成功分析（`count=1` 只统计到这次，
   因为前两次都失败了），但 Result Consumer 看到 run 已经是终态 `FAILED`，
   按重复结果忽略。

因此这一轮的 4 个 FAIL（run 未 `SUCCEEDED`、Evidence 非 `agent-v1`、
Worker 真实模型日志断言、重复任务计数）都是**真实失败的表现**，不是脚本 bug。
产物和 Worker 日志保留：

```text
backend/backend/target/phase54-mq-real-e2e/20261030-220047/
├── summary.json
└── worker.log
```

这轮说明「失败语义」是成立的；也说明真实模型 JSON 输出仍可能非法，不能把
MQ 链路写成「稳定通过」。

### 4.4 一次断言修正

更早一轮 `20261030-215815` 是 18 PASS / 1 FAIL：脚本断言 Worker 日志里出现
`adapter=openai-compatible:deepseek-flash`，实际日志是 Python dict 形式
`'adapter': 'openai-compatible:deepseek-flash'`。改成只断言
`openai-compatible:deepseek-flash` 子串后重跑；这不是产品缺陷。

---

## 5. 重复采样

脚本：[`scripts/phase53-spring-e2e.py`](../../scripts/phase53-spring-e2e.py)
（3 轮：普通问题 / 错误解释 / 显式复盘，真实 `deepseek-flash`，47 项检查）

### 5.1 修复前：46 PASS / 1 FAIL（`20261030-221130`）

失败检查：`复盘 CHAT run=COMPLETED`，读到 `[]`。

SSE 的 `message.end` 带 `runId=200`；客户端收到 end 后立刻查库，`agent_run` 200
当时还是 `RUNNING` / 尚未提交；稍后同一行变成 `COMPLETED`。原因是
`ChatService` 先发 `message.end`，再调用 `finishRun(...)` 写终态。

这是一个真实的**顺序竞态**：客户端按契约收到 end 后查库，可能看到未完成状态。
修复：先 `finishRun(...)` 把 run 写到 `COMPLETED` / `TRUNCATED` / `FAILED`，
再发 `message.end`；终态写失败会走 catch，客户端收到 `message.error`，不会只看到
一个干净的 end。`ChatServiceTest` 5/5 通过。

### 5.2 修复后：连续 3 轮 47 PASS / 0 FAIL

| 产物 | 普通问题 | 错误解释 | 显式复盘 | 检查 |
| --- | --- | --- | --- | --- |
| `20261030-221439` | 2077 ms / 141 字 | 11608 ms / 1286 字 | 9641 ms / 729 字 | **47 / 0** |
| `20261030-221521` | 2875 ms / 112 字 | 13295 ms / 1490 字 | 9157 ms / 744 字 | **47 / 0** |
| `20261030-221612` | 2608 ms / 155 字 | 11204 ms / 1392 字 | 22702 ms / 1118 字 | **47 / 0** |

三轮都核对：SSE 首尾事件、`message.start` 的 `runId`、消息与 run 落库、
`agent-v1` 分析、显式复盘轨迹注入、普通问题不注入轨迹。三次都没有
`message.error`，`message.truncated=0`。

### 5.3 边界

- 3 轮不是统计意义上的采样，不能给出置信区间或失败率；
- 同一组问题、同一模型参数，只覆盖 3 个场景；
- 判定由脚本完成，没有人工评分 / 双人复核；
- 显式复盘一轮 22.7 s，说明供应商延迟波动很大，不能把单轮耗时当基线。

---

## 6. 代码与迁移

### 6.1 代码变更

| 文件 | 变更 |
| --- | --- |
| `entity/Conversation.java` | 新增 `lastMessageSeq`，`updateStrategy = NEVER` |
| `mapper/ConversationMapper.java` | `incrementLastMessageSeq` / `selectLastMessageSeq` |
| `mapper/MessageMapper.java` | 删除 `selectMaxSequenceNoForUpdate`，恢复为纯 `BaseMapper` |
| `service/MessageService.java` | 序号分配改为 conversation 计数器原子自增 |
| `mapper/LearningSpaceMapper.java` | 新增 `lockById`（`FOR UPDATE`） |
| `service/SessionService.java` | 首次归属锁空间行；`attach()` 先写 session 再写 conversation |
| `service/ChatService.java` | `finishRun` 先于 `message.end` |
| `resources/schema.sql` | `conversation.last_message_seq` |
| `scripts/phase53-spring-e2e.py` | 支持 `MYSQL_DB`，用于独立测试库 |
| `scripts/phase54-*.py` | 新增 RAG / MQ / 并发三个 E2E |

### 6.2 已有库必须执行迁移

`schema.sql` 的 `CREATE TABLE IF NOT EXISTS` **不会**给已有 `conversation` 表加列。
升级已有库必须执行：

```bash
mysql -h 127.0.0.1 -u <user> -p < backend/backend/src/main/resources/schema-migration-phase54-message-seq.sql
```

脚本是幂等的：列不存在才 `ALTER TABLE`，然后按
`MAX(message.sequence_no)` 回填 `last_message_seq`；已经有列时只做只读核对。
漏跑时新代码会报 `Unknown column 'last_message_seq'`。

### 6.3 测试

- Spring：`./mvnw test` → **255 条（182 执行通过 / 73 skipped / 0 failed）**，BUILD SUCCESS；
- Agent 未改动，沿用 **383 passed / 5 skipped / 1 xfailed**；
- 序号分配与 Session 归属目前由 E2E / 并发脚本覆盖，**没有**新增专门的单元测试；
  这是测试覆盖缺口，不能写成「已有回归测试保护」。

---

## 7. 综合结论与仍未覆盖

### 可以声称

- 已完成 **MQ → Worker → 真实模型 → Result Consumer → MySQL** 的一次通过；
- 已完成 **真实 Qdrant + 真实 Embedding + 真实模型** 的文档引用链路；
- 已完成同一服务端场景的 **3 次连续重复采样**；
- 已完成 **6/12、12/24、24/24** 三组单机并发基线，并修复了压测暴露的
  两个数据库死锁和一个 SSE/DB 顺序竞态。

### 不能声称

- 不能声称真实模型「稳定」：MQ 轮已出现 `ConnectError` + 非法 JSON，
  并发轮也出现上游 `RemoteProtocolError`；
- 不能声称引用「正确」：只验证了标签可回跳，没有事实核查 / 人工评分；
- 不能声称并发「通过压测」：没有 SLO、没有持续负载、没有多实例；
- 不能声称 MQ 生产可用：本地单 broker、无 HA / 监控 / 告警；
- 不能声称 Flutter 可用：前端仍是脚手架；
- 不能把 28/0、19/0、47/0、10/0 相加成总体通过率，它们是不同链路、不同断言。

### 下一步最低验收

1. 给序号分配与 Session 首次归属补单元 / 集成回归测试；
2. 对引用回答做人工评分，覆盖多 chunk、干扰片段、无依据问题；
3. 扩大并发到持续负载，记录错误率、P95/P99、连接池和线程池饱和点；
4. 用 Testcontainers 把 MQ 链路纳入 CI，而不是依赖手工环境；
5. 启动 Flutter，验证 SSE 消费、断线重连与渲染；
6. 修复 `history_guard` 对否定句的误报，再确认 C-only 不依赖 fallback。

---

## 8. 产物索引

| 内容 | 路径 |
| --- | --- |
| RAG 正式轮 | `backend/backend/target/phase54-rag-e2e/20261030-213447/` |
| RAG 首轮失败 | `backend/backend/target/phase54-rag-e2e/20261030-213359/` |
| 并发失败样本 | `backend/backend/target/phase54-concurrency/20261030-213516/`、`.../20261030-214619/` |
| 并发正式轮 | `.../20261030-215226/`、`.../20261030-215256/`、`.../20261030-215327/` |
| MQ 正式轮 | `backend/backend/target/phase54-mq-real-e2e/20261030-220957/` |
| MQ 失败样本 | `backend/backend/target/phase54-mq-real-e2e/20261030-220047/` |
| 重复采样修复前 | `backend/backend/target/phase53-spring-e2e/20261030-221130/` |
| 重复采样修复后 | `.../20261030-221439/`、`.../20261030-221521/`、`.../20261030-221612/` |
| 服务日志 | `evaluation/reports/phase54-real-20261030/` |

相关文档：

- 5.3 服务端 E2E：[`phase53-spring-e2e-20261030.md`](phase53-spring-e2e-20261030.md)
- 5.3 Runtime 复验：[`phase53-real-model-20261030.md`](phase53-real-model-20261030.md)
- 测试矩阵：[`../delivery/TEST_MATRIX.md`](../delivery/TEST_MATRIX.md)
- 已知限制：[`../delivery/KNOWN_LIMITATIONS.md`](../delivery/KNOWN_LIMITATIONS.md)
