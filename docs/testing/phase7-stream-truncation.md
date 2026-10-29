# Phase 7：流式回答截断终态

- 运行日期：2026-10-29
- 代码基线：`6d05486`（`6d054869ebfb7b13fd89053530cf64bd4e45dd7f`，2026-10-29 13:31:00 +0800）
- 工作区状态：包含 Phase 6、Phase 7 尚未提交的修改
- 原始日志：`evaluation/reports/phase7-20261029/`

> 本文区分三件事：**已经修改并单测验证的契约**、**在真实 MySQL 上跑通的后端集成测试**、
> **没有验证的部分**。确定性测试通过不等于真实模型质量验证，也不等于真实前端已经适配。

## 1. 结论

修复前的行为是：模型因为 `finish_reason=length` 停止时，已经推送的部分文本会被
当作一次正常回答落库、把 `agent_run` 标记为 `COMPLETED`，并继续触发学习分析。
整条链路上没有任何一处保留「这次回答不完整」的事实。

修复后：

| 维度 | 正常完成 | Token 上限截断 |
| --- | --- | --- |
| Python `done` 事件 | `finishReason=stop`（或供应商原值），`truncated=false` | `finishReason=length`，`truncated=true` |
| Java Gateway 回调 | `onComplete(StreamCompletion(...))` | 同左，`truncated=true` |
| SSE `message.end` | `{messageId, runId}`（字段与旧版完全一致） | `{messageId, runId, truncated:true, finishReason:"length"}` |
| 助手消息 | 正常落库 | 部分内容照常落库，`message.truncated=1` |
| `agent_run.status` | `COMPLETED` | `TRUNCATED` |
| 后续学习分析 | 触发 | **不触发** |
| `GET /messages` | `truncated:false`（旧字段不变） | `truncated:true` |

正常完成路径的 SSE 字段、数据库状态和分析触发行为均未改变。

## 2. 修复前的调用链与证据

### 2.1 完整链路

```text
OpenAI 兼容 API 的 SSE chunk
  choice.finish_reason
    → OpenAICompatibleAdapter._stream_once 写入 StreamDiagnostics.finish_reason
      （agent/app/llm/openai_compatible.py，修复前已存在）
    → LLMAdapter.stream_chat 只 yield 文本增量，不暴露 diagnostics
      （agent/app/llm/base.py）
    → Tutor.stream 只消费字符串，不读取完成原因
      （agent/app/tutor/tutor.py）
    → AgentRuntime.stream 固定写 status="COMPLETED"，
      固定发 done {runId, detectedConcepts, estimatedTokens, elapsedMs}
      （agent/app/runtime.py）
    → HttpPythonAgentGateway 的 case "done" 直接调用 handler.onComplete()
      （backend/.../agent/HttpPythonAgentGateway.java）
    → ChatService.onComplete：buffer 落库 → message.end {messageId, runId}
      → agent_run 置 COMPLETED → analyzeQuietly(...)
      （backend/.../service/ChatService.java）
```

### 2.2 逐项审计结果

1. **Adapter 从哪里获取 `finish_reason`**
   `_stream_once` 解析每个 chunk 的 `choices[0].finish_reason`，写入本次请求的
   `StreamDiagnostics`（修复前即如此）。问题不在「拿不到」，而在「没有传给上层」。

2. **多个 delta 下如何保留最终完成原因**
   `StreamDiagnostics.finish_reason` 在同一个请求的 diagnostics 对象上被后到的
   chunk 覆盖，因此最终保留的是最后一个非空值。修复没有改变这一解析逻辑，只增加了
   「流正常耗尽后读取该值」的出口。

3. **修复前 SSE 事件格式**
   `meta`、`delta`、`done`、`error` 四种。`done` 只有
   `runId / detectedConcepts / estimatedTokens / elapsedMs`，
   没有 `finishReason` 或任何不完整标记。

4. **Java 后端如何解析、何时调用 `onComplete`**
   修复前 `case "done" -> finish(handler, terminated, true, 0, null)`，
   内部调用无参 `handler.onComplete()`。只要收到 `done`，无论上游为何停止，
   都会走正常完成分支。

5. **数据库、实体、枚举和 API 是否已有截断状态**
   没有。`agent_run.status` 只有 `COMPLETED / SUCCEEDED / FAILED` 等；
   `message` 没有截断列；`MessageResponse` 没有对应字段；`message.end` 也没有。

6. **前端能否区分正常完成和不完整回答**
   不能。修复前前端只能看到「流正常结束 + `message.end`」，没有任何可判断依据。

7. **现有测试可复用 / 未覆盖的边界**
   - 可复用：`HttpPythonAgentGatewayTest` 的假 Agent + 事件翻译框架；
     `ChatServiceTest` 的内联 executor + 可控 handler；
     `ChatSseIntegrationTest` 的真实 MySQL + 真实 SSE 链路；
     `test_llm_openai_compatible.py` 的假 OpenAI SSE 服务器。
   - 未覆盖：`finish_reason=length` 在 Runtime → SSE → Gateway → ChatService
     的整条终态传递；截断回答是否触发分析；截断状态的持久化和重新加载。

### 2.3 问题证据

- 修复前 `AgentRuntime.stream` 的 `RUN_END` 与 `done` 写死 `COMPLETED`：

  ```python
  trace.record(TraceEventType.RUN_END, status="COMPLETED", responseChars=...)
  yield StreamEvent("done", {"runId": ..., "detectedConcepts": ...,
                             "estimatedTokens": ..., "elapsedMs": ...})
  ```

- 修复前 `OpenAICompatibleAdapter` 已把 `finish_reason` 写进
  `StreamDiagnostics`，但 `stream_chat` 的返回类型是 `AsyncIterator[str]`，
  上层没有任何途径读取它。

- 修复前 `ChatService` 的 `onComplete` 无条件执行
  `appendAssistantMessage(...)`、`AgentRunStatus.COMPLETED`、
  `analyzeQuietly(...)`。

## 3. 产品语义

把模型调用的终态明确分成四类，互不混淆：

| 情况 | 判定依据 | 用户可见内容 | 终态 |
| --- | --- | --- | --- |
| 正常完成 | `finish_reason=stop` 或其他非 `length` 值 | 完整回答 | `COMPLETED` |
| Token 上限截断 | `finish_reason=length` | 已生成的部分内容 | `TRUNCATED` |
| 模型调用失败 | 适配器抛 `LLMError` / 上游 `error` 事件 | 已推送的部分文本 + 错误事件 | `FAILED`，SSE `message.error` |
| 空返回 | 流正常结束但没有任何非空 delta | 无回答 | 既有约定：`LLMError("模型没有返回任何内容")` → `FAILED` |

约束与取舍：

- **保留部分内容**：截断时已经生成的内容对用户仍有价值，不从中途删掉；
  但通过 `truncated` 明确标记为不完整。
- **不自动重试**：重试会产生重复计费、重复内容和不可预测的终态。截断就是截断，
  由产品层决定是否让用户手动重试。
- **不调大默认 `max_tokens`**：那只是把问题推迟，且会改变所有请求的成本和延迟，
  不属于本缺陷的修复范围。
- **不把截断回答作为完整学习证据**：不触发 `analyzeQuietly`，避免用半截回答
  推导用户理解、误区或记忆。
- **正常路径兼容**：正常完成的 `message.end` 字段保持原样；新增字段只在截断时出现，
  旧客户端忽略未知字段即可继续工作。

## 4. 修复方案

### 4.1 Python：终态从适配器传到 Runtime

| 文件 | 变更 |
| --- | --- |
| `agent/app/llm/base.py` | 新增 `StreamCompletion(finish_reason, metadata_available)` 与只读属性 `truncated`；新增 `CompletionAwareStream` 包装器，只在流**正常耗尽**时生成终态，中途异常或提前丢弃时 `completion` 保持 `None`；`LLMAdapter` 文档补充可选的 `stream_chat_with_completion` |
| `agent/app/llm/openai_compatible.py` | 新增 `stream_chat_with_completion(...)`，每次调用创建独立的 `StreamDiagnostics`，通过闭包把该次请求的 `finish_reason` 绑定到返回的流；原 `stream_chat` 改为消费该流并只 yield 文本。原流式主体下沉为 `_stream_chat(payload, diagnostics)`，避免并发请求共享 `last_diagnostics` 串号 |
| `agent/app/tutor/tutor.py` | `stream(..., completion: StreamCompletion \| None = None)`：优先调用适配器的 `stream_chat_with_completion`，流耗尽后把终态复制进调用方提供的槽；`produced=False` 时维持原有空返回报错 |
| `agent/app/runtime.py` | 每次请求创建独立 `StreamCompletion` 并传入 `generate`；`done` 增加 `finishReason` 与 `truncated`；`RUN_END` 的 status 改为 `TRUNCATED` / `COMPLETED`；历史复盘与受限重生成路径也传递终态（首次截断被接受 → 截断；重试成功 → 重试的终态；确定性兜底文本 → 正常完成） |
| `agent/app/main.py` | 内网 SSE 契约文档补充 `done` 的新字段 |

关键设计点：

- **不破坏旧适配器**：`stream_chat_with_completion` 是可选方法，Tutor 用 `getattr`
  探测。只实现 `stream_chat` 的适配器（包括现有测试替身）仍按正常完成处理，
  `finishReason=null`、`truncated=false`。这是明确的兼容语义，不是「猜」。
- **`metadata_available` 是显式事实**：适配器不提供终态时不能推断截断；只有
  真正读到 `finish_reason` 才可能置 `truncated=true`。
- **只有正常耗尽才算终态**：`CompletionAwareStream` 在异常/提前结束时不会伪造
  `completion`，因此模型调用失败不会被包装成一次「正常结束」。

### 4.2 Java：解析新终态并分化状态

| 文件 | 变更 |
| --- | --- |
| `backend/.../agent/StreamCompletion.java`（新增） | `record StreamCompletion(String finishReason, boolean truncated)`，附 `normal()` 静态工厂 |
| `backend/.../agent/AgentStreamHandler.java` | 新增默认方法 `onComplete(StreamCompletion completion)`，默认退回无参 `onComplete()`，旧实现无需改动 |
| `backend/.../agent/HttpPythonAgentGateway.java` | `done` 解析 `finishReason` / `truncated`；`finishReason="length"` 优先于上游的 `truncated` 字段，避免矛盾的载荷把截断当成正常完成；调用新的 `onComplete(StreamCompletion)` |
| `backend/.../enums/AgentRunStatus.java` | 新增 `TRUNCATED`，与同步对话的 `COMPLETED`、异步分析的 `SUCCEEDED` 区分 |
| `backend/.../service/ChatService.java` | 截断时用 `appendAssistantMessage(..., true)` 落库；`message.end` 追加 `truncated` / `finishReason`；run 置 `TRUNCATED`；跳过 `analyzeQuietly`。正常路径保持原字段与原状态 |
| `backend/.../service/MessageService.java` | 新增带 `truncated` 的重载；正常消息写 `null`，避免无谓地写入新列 |
| `backend/.../entity/Message.java` | 新增 `Boolean truncated` |
| `backend/.../dto/conversation/MessageResponse.java` | 新增 `boolean truncated` 组件，由 `Boolean.TRUE.equals(...)` 映射 |
| `backend/.../resources/schema.sql` | `message` 新增 `truncated TINYINT(1) NOT NULL DEFAULT 0`；`agent_run.status` 枚举加入 `TRUNCATED`，并提供幂等 `ALTER ... MODIFY COLUMN` |

兼容性对比：

```text
修复前 done:        {runId, detectedConcepts, estimatedTokens, elapsedMs}
修复后 done:        {..., finishReason, truncated}            # 旧客户端可忽略
修复前 message.end: {messageId, runId}
修复后正常:         {messageId, runId}                        # 不变
修复后截断:         {messageId, runId, truncated:true, finishReason:"length"}
```

### 4.3 数据库迁移边界

`schema.sql` 使用 `CREATE TABLE IF NOT EXISTS`，**不会**给已存在的库补列。
因此：

- 新库：上面的 `CREATE` 直接带上 `message.truncated`；
- 已有库：需手工执行文档中的 `ALTER TABLE message ADD COLUMN ...`，
  或按 README 用 `schema-dev-reset.sql` 清库重建。

`agent_run.status` 的枚举扩展由 `schema.sql` 里幂等的 `ALTER ... MODIFY COLUMN`
覆盖，可以在已有库上重复执行。

### 4.4 为什么没有改 RAG / Memory / Context Builder / 引用逻辑

本缺陷是终态传递问题，与上下文构建、检索、引用归属无关。修复只触碰
「适配器 → Tutor → Runtime → SSE → Gateway → ChatService → 持久化」这条链，
以及必要的枚举、实体、字段和测试。

## 5. 变更文件清单

### Python

- `agent/app/llm/base.py`
- `agent/app/llm/openai_compatible.py`
- `agent/app/tutor/tutor.py`
- `agent/app/runtime.py`
- `agent/app/main.py`
- `agent/tests/test_llm_openai_compatible.py`
- `agent/tests/test_stream_truncation.py`（新增）

### Java

- `backend/backend/src/main/java/com/ails/backend/agent/StreamCompletion.java`（新增）
- `backend/backend/src/main/java/com/ails/backend/agent/AgentStreamHandler.java`
- `backend/backend/src/main/java/com/ails/backend/agent/HttpPythonAgentGateway.java`
- `backend/backend/src/main/java/com/ails/backend/enums/AgentRunStatus.java`
- `backend/backend/src/main/java/com/ails/backend/service/ChatService.java`
- `backend/backend/src/main/java/com/ails/backend/service/MessageService.java`
- `backend/backend/src/main/java/com/ails/backend/entity/Message.java`
- `backend/backend/src/main/java/com/ails/backend/dto/conversation/MessageResponse.java`
- `backend/backend/src/main/resources/schema.sql`
- `backend/backend/src/test/java/com/ails/backend/agent/HttpPythonAgentGatewayTest.java`
- `backend/backend/src/test/java/com/ails/backend/service/ChatServiceTest.java`
- `backend/backend/src/test/java/com/ails/backend/service/ChatSseIntegrationTest.java`

### 文档 / 配置

- `backend/backend/README.md`（SSE 契约与状态说明）
- `agent/README.md`（内网 `/agent/chat` 的 `done` 契约）
- `backend/backend/POSTMAN_API_GUIDE.md`（同上；本地文档，被 `backend/.gitignore` 忽略，不进入提交）
- `.gitignore`（保留 Phase 7 原始日志）
- `docs/testing/phase7-stream-truncation.md`（本文）

## 6. 正常完成与截断的事件 / 状态对照

### 正常完成（`finish_reason=stop`）

```text
event: meta
data: {"runId":"...","detectedConcepts":[...],"estimatedTokens":...,"warnings":[...],...}

event: delta
data: {"delta":"TLB "}

event: delta
data: {"delta":"是页表项缓存。"}

event: done
data: {"runId":"...","detectedConcepts":[...],"estimatedTokens":...,
       "elapsedMs":...,"finishReason":"stop","truncated":false}

message.end   {messageId: 101, runId: 200}
message      : 正常落库，truncated = 0
agent_run    : COMPLETED
analysis     : 触发
GET /messages: {..., truncated: false}
```

### Token 上限截断（`finish_reason=length`）

```text
event: delta
data: {"delta":"TLB 缓存的是页表项，"}

event: done
data: {"runId":"...","detectedConcepts":[...],"estimatedTokens":...,
       "elapsedMs":...,"finishReason":"length","truncated":true}

message.end   {messageId: 101, runId: 200, truncated: true, finishReason: "length"}
message      : 部分内容落库，truncated = 1
agent_run    : TRUNCATED
analysis     : 不触发
GET /messages: {..., truncated: true}
```

### 模型失败 / 空返回

```text
data: {"event":"error","data":{"runId":"...","code":502,"message":"..."}}
message.error {runId, code, message}
agent_run     : FAILED
analysis      : 不触发
```

截断与失败的区别：截断仍然发出 `done`（内容可展示），失败发出 `error`。
两者都不会触发学习分析。

## 7. 测试与真实结果

所有命令均在当前工作区执行，日志保存在 `evaluation/reports/phase7-20261029/`。

### 7.1 Python 定向测试

```bash
cd agent
python -m pytest tests/test_stream_truncation.py tests/test_llm_openai_compatible.py -v
```

结果：**31 passed，0 failed，0 skipped，退出码 0**（3.73s）。
日志：`evaluation/reports/phase7-20261029/agent-targeted-tests.log`

新增用例：

| 测试 | 覆盖 |
| --- | --- |
| `test_normal_completion_stays_completed_and_untruncated` | 正常结束：文本完整、`truncated=false` |
| `test_partial_answer_before_length_is_kept_but_marked_truncated` | 部分 delta 后 `length`：文本保留、`truncated=true` |
| `test_model_failure_still_uses_error_path` | 中途模型失败：`error` 502，不产生 `done` |
| `test_empty_output_still_becomes_explicit_error` | 空返回：`error` 502「没有返回任何内容」 |
| `test_adapter_without_completion_metadata_keeps_legacy_behavior` | 旧适配器无终态信息：按正常完成兼容 |
| `test_stream_completion_exposes_final_finish_reason_for_truncation` | 真实适配器解析 `length` 并暴露终态 |
| `test_stream_completion_is_normal_after_stop` | 真实适配器在 `stop` 时 `truncated=false` |

### 7.2 Python 全量测试

```bash
cd agent
python -m pytest tests -q
```

结果：**365 passed，5 skipped，2 xfailed，0 failed，退出码 0**（10.64s）。
日志：`evaluation/reports/phase7-20261029/agent-full-tests.log`

### 7.3 Java 默认全量测试

```bash
cd backend/backend
./mvnw -B clean test
```

结果：**Tests run: 254, Failures: 0, Errors: 0, Skipped: 72，BUILD SUCCESS，退出码 0**。
日志：`evaluation/reports/phase7-20261029/backend-tests.log`

Surefire 的 `Tests run` **包含 skipped**，不能直接当成 passed。按 XML 口径拆开是：

| 总测试数 | 实际执行 | 通过 | 跳过 | 失败 | 错误 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 254 | 182 | 182 | 72 | 0 | 0 |

72 个 skip 是既有的环境门控用例（真实 MySQL / Docker / 环境变量），
**skip 不等于通过**。Phase 8 复核后的最新数字见
[`phase8-boundary-review.md`](phase8-boundary-review.md) §3。

新增 Java 用例：

| 测试 | 覆盖 |
| --- | --- |
| `HttpPythonAgentGatewayTest.translatesTruncatedDone` | 假 Agent 发 `done{finishReason:"length", truncated:true}`：delta 保留，`onComplete(StreamCompletion)` 一次，`truncated=true` |
| `HttpPythonAgentGatewayTest.infersTruncationFromFinishReason` | 上游只给 `finishReason:"length"` 时也能推断截断 |
| `HttpPythonAgentGatewayTest.finishReasonLengthOverridesContradictoryFlag` | 上游误发 `truncated:false` 但 `finishReason:"length"` 时，仍按截断处理 |
| `ChatServiceTest.truncatedStreamPersistsPartialButSkipsAnalysis` | 部分内容用 3 参重载落库、run=`TRUNCATED`、输出摘要含 `truncated=true`、分析服务无交互 |
| `ChatSseIntegrationTest.truncatedStreamPersistsPartialContentAndSkipsAnalysis` | 真实 MySQL + 真实 SSE：`message.end` 带 `truncated/finishReason`；`message.truncated=1`；run=`TRUNCATED`；无 Evidence；分析未派发；`GET /messages` 返回 `truncated=true` |

### 7.4 真实 MySQL SSE 集成测试

```bash
cd backend/backend
AILS_CHAT_SSE_IT=true AILS_IT_MYSQL_PASSWORD=... \
  ./mvnw -B test -Dtest=ChatSseIntegrationTest
```

结果：**Tests run: 8, Failures: 0, Errors: 0, Skipped: 0，BUILD SUCCESS，退出码 0**。
日志：`evaluation/reports/phase7-20261029/chat-sse-it.log`

该测试使用本机 MySQL 数据库 `ails_chat_sse_it`。Phase 7 首次运行时数据库不存在，
由 JDBC 参数的 `createDatabaseIfNotExist=true` 新建并执行当前 `schema.sql`，
因此验证的是**新库 + 新 schema**；最终这份日志是在同一个已存在库上的复跑。
这不能证明 Phase 7 之前创建的库已经执行过 `ALTER TABLE message ADD COLUMN`。

**复跑时发现并处理的既有竞态**：`normalStreamPersistsBeforeEndThenAnalyzes`
在 `message.end` 到达后立刻断言 `agent_run.status=COMPLETED`，但 `ChatService`
的发送顺序是「先 send `message.end`，再 `finishRun`」。连续 4 次复跑中 2 次
读到 `RUNNING`。这是该测试本身依赖瞬时状态，不是 Phase 7 引入的时序变化
（修复前后的发送顺序相同）。处理方式是把瞬时断言改为对最终状态的
`await("run 正常终态", ...)`，断言内容仍是 `COMPLETED`，没有放宽。
修改后连续 5 次运行全部通过。

### 7.5 离线评测

```bash
PYTHONPATH="$PWD:$PWD/agent" python -m evaluation -v
```

结果：**40 条，通过 40，失败 0，跳过 0，通过率 100.0%，退出码 0**。
日志：`evaluation/reports/phase7-20261029/offline-evaluation.log`

这是确定性契约回归，不涉及真实模型，也不覆盖 `finish_reason=length` 的真实供应商行为。

### 7.6 `-Pmq-integration` 结果（未通过，环境原因）

```bash
cd backend/backend
./mvnw -B clean test -Pmq-integration
```

结果：**Tests run: 255, Failures: 1, Errors: 0, Skipped: 72，BUILD FAILURE，退出码 1**。
日志：`evaluation/reports/phase7-20261029/backend-tests-mq.log`

唯一失败：

```text
AnalysisMqIntegrationTest.setUp:75
要求真实 RabbitMQ Broker（-Pmq-integration），但本机 Docker 不可用。
请启动 Docker 后重跑；直接跳过会把「没验证」伪装成「通过」。
```

这不是代码断言失败，而是本机 Docker 环境不可用导致 Testcontainers 无法启动。
**该 profile 下的 MQ 链路本轮未验证。**

Phase 8 只补做了不依赖 Testcontainers 的两段（详见
[`phase8-boundary-review.md`](phase8-boundary-review.md) §4）：真实 RabbitMQ 连接测试
`Phase2RabbitMqBrokerTest` 5/5 通过，真实 MySQL 上的 `Phase2MqProcessorIntegrationTest`
迁移后 5/5 通过。**Spring 监听器 + Testcontainers Broker 的完整 MQ E2E 仍未验证。**

**Phase 9 追加（2026-10-29）**：本轮新增来源过滤与回归测试，Python 全量变为
**382 passed / 5 skipped / 1 xfailed**；Java 协议与代码未改，Phase 8 复核后的
Java 口径（255 条 / 182 执行通过 / 73 跳过）和真实 MySQL SSE 8/8 仍然有效。
本文 §7.3 的 254 / 72 是 Phase 7 时点数字，Phase 8 新增 `ChatSseOrderingProbeTest`
后总数变为 255 / 73。离线评测重跑为 40/40 用例、124/124 断言。详见
[`phase9-evidence-boundary.md`](phase9-evidence-boundary.md)。

## 8. 尚未验证 / 已知局限

1. **真实模型只在 Python 侧验证到 `done` 事件。** Phase 8 用真实 DeepSeek
   （`deepseek-flash`，`max_tokens=1024`）跑通了
   `OpenAICompatibleAdapter → Tutor → AgentRuntime.stream`：`finish_reason=length`、
   `truncated=true`、已生成 972 字符。**没有**跑通「真实模型 → Java 后端 → 浏览器」整链，
   Java 侧仍由伪造 SSE 覆盖。
2. **没有真实前端消费者验证。** `message.end` 的新字段和 `GET /messages` 的
   `truncated` 目前只由后端测试断言；Flutter 端仍是默认计数器脚手架（122 行），
   没有 SSE 客户端，也没有截断提示。
3. **已有数据库的迁移脚本已补上并实跑。** Phase 7 当时缺脚本；Phase 8 增加了
   `schema-migration-phase7-truncation.sql`，在「Phase 7 之前的 schema + 旧数据」
   上验证通过（详见 `phase8-boundary-review.md` §2）。它**不会**被 Spring 自动执行，
   部署时仍需手工跑一次。
4. **`-Pmq-integration` 仍未通过**，原因是本机 Docker 不可用；只有不依赖
   Testcontainers 的两段补做了验证，完整 MQ E2E 仍无回归证据。
5. **历史复盘 / 受限重生成路径有既有测试，但没有专门的截断断言。**
   `test_history_guard_runtime.py` 覆盖了复盘回答的校验、一次重生成、确定性回退和
   正常 SSE 契约，但没有构造 `finish_reason=length` 来验证终态传播；
   本轮新增测试覆盖的是普通 `runtime.stream` 主路径。
6. **`finish_reason` 的其他取值没有细化。** 除 `length` 外一律按正常完成处理；
   供应商特有的截断原因（若存在）不会被识别。
7. **截断回答不触发分析**是当前产品决策，不是对「部分内容是否有学习价值」的证明。
   如果后续要利用部分回答，应单独设计证据置信度，而不是直接把 `truncated` 当作完整。

## 9. 简历候选描述

> 定位并修复 LLM 流式对话「Token 上限截断被误判为正常完成」的终态缺陷：在适配器与
> Runtime 之间增加规范化完成原因传递，扩展 SSE `done` 与 Java 后端状态机，新增
> `TRUNCATED` 终态与消息级 `truncated` 标记，并阻止不完整回答触发后续学习分析；
> 以可控假适配器、伪造 SSE 和真实 MySQL 集成测试覆盖正常完成、截断、模型失败、
> 空返回与旧适配器兼容五类路径（Python 365 passed / 5 skipped / 2 xfailed；
> Java 255 条（182 执行通过 / 73 跳过 / 0 失败）；真实 MySQL SSE IT 8/8；
> 离线评测 40/40；真实 DeepSeek 在 Python runtime 复现 `finish_reason=length`）。
> 局限：真实模型未跑通 Java 后端整链，未接入真实前端提示，
> Testcontainers MQ profile 因本机 Docker 不可用未验证。

**这条描述能证明的**：终态信息在 Python 与 Java 两侧的传递、状态分化和持久化契约，
以及正常路径没有回归。
**不能证明的**：真实供应商在 Token 上限下的行为、真实用户看到截断提示的体验、
已有生产库的迁移执行情况。
