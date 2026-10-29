# Phase 8：Phase 7 交付边界复核

本轮不新增业务功能，只复核 Phase 7 遗留的三块边界：事件顺序、旧库迁移、测试口径，
并补做当前环境能完成的验证。

所有命令都在本工作区实际执行，日志在 `evaluation/reports/phase8-20261029/`。
**没有为了通过而修改生产代码。**

---

## 1. `message.end` 与 `agent_run` 终态的真实顺序

### 1.1 代码事实

`ChatService` 的正常完成路径（`backend/backend/src/main/java/com/ails/backend/service/ChatService.java`）：

```text
271-273  messageService.appendAssistantMessage(...)   // 助手消息落库
284      send(emitter, "message.end", end)            // 推送结束事件
286-294  finishRun(run, COMPLETED / TRUNCATED, ...)   // 写 agent_run 终态
298      completeQuietly(emitter)
307-309  analyzeQuietly(...)                          // 仅正常完成、且已落库
```

也就是说，**`message.end` 在 `finishRun` 之前发出**。两者之间隔一次数据库 UPDATE。

用真实 Spring 上下文 + 真实 MySQL 做了可复现的探针
（`ChatSseOrderingProbeTest`，默认 skip，不影响日常测试）：

```bash
cd backend/backend
AILS_CHAT_SSE_IT=true AILS_P8_ORDER_PROBE=true AILS_IT_MYSQL_PASSWORD=... \
  ./mvnw -B test -Dtest=ChatSseOrderingProbeTest
```

结果：

```text
P8_ORDER_PROBE rounds=20 observed_at_message_end={RUNNING=20}
```

**20/20 次在收到 `message.end` 的同一瞬间读到 `RUNNING`。**
日志：`evaluation/reports/phase8-20261029/ordering-probe.log`

这不是偶发竞态，而是当前实现的稳定结果：客户端收到事件后做一次 DB 查询，
服务端线程随后才执行 `finishRun`，查询几乎必然先完成。

### 1.2 现有协议承诺了什么

| 来源 | 承诺 |
| --- | --- |
| `backend/backend/README.md:424-436` | `message.end { messageId, runId, truncated?, finishReason? }`；`runId` 是 `agent_run` 主键，状态走 `RUNNING → COMPLETED / TRUNCATED / FAILED`。**没有说事件发出时 run 已终态。** |
| `ChatService` 类注释 48-49 行 | 「buffer 整段存成助手消息 → 推 `message.end` → agent_run 置 COMPLETED」——明确承认 end 在终态之前 |
| Controller / DTO | 没有任何接口返回 `agent_run.status`（`grep AgentRun controller/ dto/` 无结果），外部 HTTP 客户端读不到该状态 |
| `docs/testing/phase2.1-integration-results.md:38` | 声称「end 到达时 … run=COMPLETED」——**与当前代码不一致的历史口径** |

### 1.3 结论

1. **`message.end` 的产品语义**应定义为「助手消息已持久化，本轮文本流结束」，
   而不是「`agent_run` 已终态」。现有 README 没有越过这个边界。
2. **事件发出后，消费者可以读到仍为 RUNNING 的 `agent_run`**：20/20。
   这个窗口是外部可观察的（直接查库、或未来新增的运维接口），不是测试内部的幻觉。
3. **现有公开接口没有承诺「事件发出时任务已终态」**。因此严格按现有 HTTP 契约，
   不需要为了兼容客户端而改生产代码。
4. **但 Phase 7 把测试改成 `await` 确实掩盖了一个稳定存在的状态竞争**。
   `await` 让测试通过，却让「end 时 run 仍 RUNNING」这一事实从回归信号里消失；
   Phase 2.1 的历史报告仍写着相反结论，说明这个不一致已经误导过文档。
5. **是否需要生产修复**：属于「内部一致性缺陷」而非「公开契约破坏」。
   建议修，但按本轮要求不擅自改生产代码。

### 1.4 建议的最小修复（未实施，待审核）

只交换两行，不改变消息落库、SSE 字段、分析触发条件：

```diff
-                        send(emitter, "message.end", end);
                         finishRun(
                                 run,
                                 truncated ? AgentRunStatus.TRUNCATED : AgentRunStatus.COMPLETED,
                                 truncated
                                         ? "chars=" + buffer.length() + ",truncated=true"
                                         : "chars=" + buffer.length(),
                                 null,
                                 startedAt);
+                        send(emitter, "message.end", end);
```

修复后不变式变成：**收到 `message.end` ⇒ 助手消息已落库且 `agent_run` 已终态**。

附带收益：当前顺序下，如果 `finishRun` 抛异常，客户端会先收到 `message.end`、
再收到 `message.error`（两个终局事件）。调整后 `finishRun` 失败会直接走 catch，
不会出现「end 之后又 error」。

建议的回归测试：

- 把 `ChatSseIntegrationTest.normalStreamPersistsBeforeEndThenAnalyzes` 里的
  `await("run 正常终态", ...)` 改回 `assertThat(runStatus()).isEqualTo("COMPLETED")`，
  并连续跑 20 次确认不再读到 RUNNING；
- 或加一个 Mockito `InOrder` 单测，断言 `agentRunMapper.updateById(终态)` 先于
  `emitter.send("message.end", ...)`。

注意：**同样的倒置也存在于失败路径**。`onError`（约 330-334 行）以及两个 catch 分支
都是「先 `send(message.error)`，再 `finishRun(FAILED)`」。如果采纳
「终局事件发出时 run 已终态」的不变式，这几处需要一起调整；只改成功路径会留下
「end 有保证、error 没有」的半套语义。

风险与取舍：`finishRun` 失败时用户会收到 `message.error`，而助手消息其实已落库。
这与「落库失败 → error」不同，需要在失败文案上区分；如果不想引入这个分支，
另一种选择是保留当前顺序，并在 README 明确写「`message.end` / `message.error`
只表示流结束，不保证 `agent_run` 已终态」。**两条路都需要产品/协议层面的确认，
本轮不替用户做决定。**

---

## 2. 旧数据库升级

### 2.1 结构事实

| 结构 | 新库（`schema.sql`） | 老库启动新版本后 |
| --- | --- | --- |
| `message.truncated` | `TINYINT(1) NOT NULL DEFAULT 0`（`schema.sql:154`） | **不存在**：`CREATE TABLE IF NOT EXISTS` 不会补列 |
| `agent_run.status` | 枚举含 `TRUNCATED`（`schema.sql:514-517`） | 会被启动时执行的幂等 `ALTER TABLE ... MODIFY COLUMN` 补上 |

Phase 7 只在 `schema.sql` 里留了一段「手工执行 ALTER」的注释，
**仓库里没有对应的真实迁移脚本**。已有的迁移脚本只有
`schema-migration-document-rag.sql`。

### 2.2 复现缺失（不是推测）

用 Phase 7 之前的 `schema.sql`（`git show HEAD:...`）建库、写入旧数据，
再执行当前 `schema.sql`（模拟 Spring 启动）：

```text
message.truncated 缺失（schema.sql 的 CREATE IF NOT EXISTS 不会补列）
agent_run.status 已由 schema.sql 末尾的幂等 ALTER 补上 TRUNCATED
复现 ERROR 1054: Unknown column 'truncated' in 'field list'
```

更强的证据来自一个**真实存在的旧测试库** `ails_phase2_it`（Phase 7 之前创建）。
迁移前直接跑 `Phase2MqProcessorIntegrationTest`：

```text
Tests run: 5, Failures: 1, Errors: 2, Skipped: 0
Cause: java.sql.SQLSyntaxErrorException: Unknown column 'truncated' in 'field list'
```

日志：`evaluation/reports/phase8-20261029/phase2-mq-processor-it.log`

这不是测试写得不对，而是 MyBatis 按新实体查询 `message.truncated`，
老库没有这一列——**Phase 7 的功能在老库上会直接失败**。

### 2.3 补上的迁移脚本

新增 `backend/backend/src/main/resources/schema-migration-phase7-truncation.sql`：

- 先查 `information_schema`，可重复执行；
- `message.truncated`：`TINYINT(1) NOT NULL DEFAULT 0`，位置与 `schema.sql` 一致
  （`AFTER tokens`）；历史行由默认值补 0，不改写内容；
- `agent_run.status`：先校验没有未知值，再加 `TRUNCATED`；
- 不会被 `spring.sql.init` 自动执行，部署时必须手工跑一次；
- 末尾带只读核对查询。

### 2.4 可复现验证

脚本：`evaluation/phase8_migration/verify-upgrade.sh`
旧 schema 快照：`evaluation/phase8_migration/schema-before-phase7.sql`
日志：`evaluation/reports/phase8-20261029/migration-verification.log`

```bash
cd evaluation/phase8_migration
MYSQL_PASSWORD=*** bash verify-upgrade.sh
```

实际输出（节选）：

```text
[1] 旧消息仍可读取: 2 / 2
[2] 旧消息 truncated 默认 0: 2 / 2
[3] 正常新消息持久化: ok（truncated=0）
[4] 截断消息持久化: ok（truncated=1）
[5] agent_run.status 接受 TRUNCATED: ok
[5b] 旧 agent_run 状态保持 COMPLETED: ok
[5c] 未知状态仍被数据库约束拒绝: ok
ALL MIGRATION CHECKS PASSED
```

迁移脚本连续执行两次，结构不变（幂等）。

迁移后，在同一个 `ails_phase2_it` 旧库上重跑 MyBatis 集成测试：

```text
Tests run: 5, Failures: 0, Errors: 0, Skipped: 0
```

日志：`evaluation/reports/phase8-20261029/phase2-mq-processor-it-after-migration.log`

这证明迁移不仅让 SQL 能插入，也让**真实实体/映射层**能在老库上正常工作。

### 2.5 边界

- 只在 MySQL 8.0.37 上验证；
- 没有触碰任何生产库，也没有把「本机旧库」说成「线上已升级」；
- 默认值 0 的语义是「功能上线前的历史回复都视为完整」，不是从数据推断出来的；
- 脚本仍需部署时手工执行，Spring 不会替你做。

---

## 3. 测试数字（Maven Surefire 原始报告）

### 3.1 口径

Surefire XML 的 `tests` 属性**包含 skipped**，控制台 `Tests run` 同样包含 skipped。
因此不能把 `Tests run: 255` 写成「255 passed」。

按 `target/surefire-reports/TEST-*.xml` 聚合（脚本输出见
`evaluation/reports/phase8-20261029/surefire-reconciliation-final.txt`）：

| 总测试数 | 实际执行 | 通过 | 跳过 | 失败 | 错误 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 255 | 182 | 182 | 73 | 0 | 0 |

- 39 个 report 文件；
- 73 个 skip 全部来自环境门控类（真实 MySQL / Docker / 环境变量），
  例如 `ChatSseIntegrationTest` 8、`SecurityContextIntegrationTest` 19、
  `DocumentIndexServiceIntegrationTest` 15；
- Phase 8 新增的 `ChatSseOrderingProbeTest` 默认 skip 1 条，
  所以总数从 Phase 7 的 254 变成 255，**实际执行数仍是 182**。

### 3.2 其他套件

| 套件 | 结果 | 日志 |
| --- | --- | --- |
| Python 全量 | 365 passed / 5 skipped / 2 xfailed / 0 failed | `agent-full-tests.log` |
| Python 定向（Phase 7） | 31 passed | Phase 7 日志 |
| 真实 MySQL SSE IT | 8/8 passed | Phase 7 日志 |
| 离线评测 | 40/40 | Phase 7 日志 |
| 真实 RabbitMQ Broker | 5/5 passed | `phase2-rabbitmq-broker-it.log` |
| MQ 结果处理器（迁移后） | 5/5 passed | `phase2-mq-processor-it-after-migration.log` |

Phase 7 报告中的「Java 254 passed / 72 skipped」已更正为
「254 条（182 执行通过 / 72 跳过）」，简历段同步改为 255 条口径。

---

## 4. 环境验证记录

### 4.1 MQ profile：仍未完成（Docker 不可用）

重试命令与结果：

```text
./mvnw -B test -Pmq-integration -Dtest=AnalysisMqIntegrationTest
Tests run: 1, Failures: 1, Errors: 0, Skipped: 0
要求真实 RabbitMQ Broker（-Pmq-integration），但本机 Docker 不可用。
```

日志：`evaluation/reports/phase8-20261029/mq-integration-retry.log`

**结论：Spring 监听器 + Testcontainers Broker 的完整 MQ E2E 未验证。**
不把「本机有一个 RabbitMQ 容器」等同于 Testcontainers 路径已通过。

能补的两段（不依赖 Testcontainers）：

- `Phase2RabbitMqBrokerTest`（外部真实 Broker，`127.0.0.1:5672`）：
  **5/5 passed**；
- `Phase2MqProcessorIntegrationTest`（真实 MySQL，MQ 消费者写库逻辑）：
  迁移前 1 failure + 2 errors，迁移后 **5/5 passed**。

### 4.2 真实模型的 `finish_reason=length`：Python 侧已验证

探针：`evaluation/phase8_migration/real_model_length_probe.py`
（真实 `deepseek-flash`，直接读 `agent/.env`，不打印密钥）

| `max_tokens` | 结果 |
| ---: | --- |
| 16 / 64 / 128 / 256 | `LLMError: 模型返回空流（reason=reasoning_only, status=200）`——推理占满预算、没有可见正文，走错误路径 |
| 1024 | `finish_reason='length'`，`truncated=True`，972 字符，`deltas=590` |

进一步用真实 `AgentRuntime.stream`（检索源固定为 FixtureRetriever，模型与
tutor/runtime 均为生产实现）：

```text
events=['meta', 'delta', ..., 'done']
done.finishReason='length'
done.truncated=True
RESULT: PASS
```

日志：`evaluation/reports/phase8-20261029/real-model-length-probe.log`
与 `real-model-runtime-probe.log`

**结论：真实供应商确实会返回 `length`，适配器与 runtime 的 `done` 事件能正确传递。**
范围仅到 Python 侧；**没有**跑通「真实模型 → Java 后端 → 浏览器」整链，
Java 侧仍由伪造 SSE 的单测覆盖。

### 4.3 真实前端：未验证

`flutter/ails_flutter/lib/main.dart` 仍是 122 行的默认计数器应用，
`lib/` 与 `test/` 下搜不到 `text/event-stream`、`message.end`、`truncated`。
**截断提示的展示无法验证——前端根本没有接入对话流。**
这条保持「未验证」，不算通过。

---

## 5. 尚未关闭的事项

1. **是否把 `finishRun` 移到 `message.end` 之前**，需要审核确认（§1.4）。
   未确认前不改生产代码。
2. `docs/testing/phase2.1-integration-results.md:38` 的「end 时 run=COMPLETED」
   与当前实现不符。本轮只在新报告里指出，没有改写历史报告。
3. 部署到已有库时**必须手工执行** `schema-migration-phase7-truncation.sql`；
   本轮只在本机旧库上验证，没有升级任何真实部署。
4. 真实模型的完整跨进程链路（Java → Python → 模型 → Java SSE）未验证。
5. 真实前端截断提示未验证（前端未实现）。
6. Testcontainers MQ E2E 仍未通过，原因是本机 Docker 不可用。
7. `message.end` 之后 `finishRun` 失败会产生 end→error 双终局；调整顺序后
   会变成「已落库但客户端收到 error」，两种都需要在协议层明确取舍。

### 5.1 Phase 9 追加（2026-10-29）

- 本文 §3.2 的「Python 365 passed / 5 skipped / 2 xfailed」是 Phase 8 时点数字。
  Phase 9 新增来源过滤后实跑为 **382 passed / 5 skipped / 1 xfailed**；
- 两条预算 XFAIL 中，越界引用交付已关闭（转 PASS），「没有检索到的误述」仍以
  strict XFAIL 记录为架构边界；
- Java 代码与协议未变，本文的 Java 255 条口径继续有效；
- 详细变更与证据见 [`phase9-evidence-boundary.md`](phase9-evidence-boundary.md)。
