# 阶段 3：Citation 最小闭环落地报告

- 项目：`D:\learningMatiarials\AI-Learning-Space`
- 分支：`main`
- 完成日期：2026-11-07
- 本轮性质：**实现 + 测试 + 文档**。未提交 Git，未进入下一阶段。
- 核心保证：**引用不再只存在于模型生成的文本标签里；它从 Agent 的结构化终态出发，经 FastAPI → Spring → SSE → MySQL 落库，并能在历史消息中恢复。**

> 事实边界：本文件的“通过”只代表对应命令在本机真实执行并返回成功。
> 需要 MySQL 的测试若未执行，会明确标注为“未执行”；不把跳过的测试写成通过。

> **测试数字模式说明（2026-11-07 补记）**：本报告中的后端数字分两种模式，不可混用。
> **默认模式** = 不设置任何 `AILS_*_IT`，执行 `mvn -B test`，Failsafe 不执行；
> **门控全量模式** = 打开全部 `AILS_*_IT`，执行 `mvn clean verify`。
> 最新实测、产物与逐项差异见 [`TEST_EVIDENCE_MODES.md`](TEST_EVIDENCE_MODES.md)。
> 文中未标注模式的历史数字只作基线。

---

## 0. 结论摘要

1. 定位到引用丢失点：Agent 侧 `validate_citations()` 已经产出 `CitationReport`，但 `runtime.stream()` 只把它写进 trace，`done` 事件里没有任何结构化字段；Spring 侧 `StreamCompletion`、`Message`、`MessageResponse` 也完全没有引用概念。因此文本里的 `[资料《…》#…]` 是唯一的“引用”，客户端只能靠正则猜。
2. 最小 DTO 采用 4 个字段：`documentId`、`chunkId`、`chunkIndex`、`sourceTitle`。`chunkIndex` 用于把正文里的 `#片段序号` 映射回结构化来源；`sourceTitle` 是展示快照。没有新增本轮没有业务用途的字段。
3. 闭环链路：`CitationReport` → `done.citations` → `StreamCompletion.citations` → `CitationMetadata` → `message.citations`（JSON）→ `GET /messages` 的 `citations`。SSE 的 `message.end` 也同步携带同一份引用，实时与历史看到的是同一份事实。
4. **只存身份与标题，不存 chunk 正文。** 历史引用是否可用在读取时重新计算：文档存在、属于当前用户、chunk 存在且属于该文档。任一不满足即 `available=false`，客户端显示“来源已不可用”，不会留下会 404 的有效链接。
5. 权限边界：引用只随“属于当前用户的对话”的消息列表返回；没有新增“按引用 id 直接读 chunk”的接口，文档原文仍走原有按用户鉴权的文档接口。
6. 本机真实执行结果（2026-11-07 最终单次全量，**门控全量模式**，全部门控开启）：
   - 后端 `./mvnw.cmd -B clean verify`：Surefire **335 tests, 0 failures, 0 errors, 11 skipped**；
     Failsafe **4 tests, 0 failures, 0 errors, 0 skipped**。
   - Citation 定向测试：`ChatServiceTest` 6/6、`HttpPythonAgentGatewayTest` 15/15、`CitationServiceTest` 8/8。
   - SSE + MySQL 集成测试：`ChatSseIntegrationTest` **9/9**（含新增引用闭环用例）。
   - 迁移脚本升级路径：在专用库 `ails_phase3_migration_it` 上从“缺 `message.citations` 的旧 schema”升级，schema 与全新建库一致，集成测试 **9/9**，重复执行幂等。
   - Agent 完整套件：`python -m pytest -q` → **415 passed, 0 skipped, 1 xfailed**
     （历史文档声明；本轮未保留该次 pytest 原始产物）。
   - 默认模式对照（2026-11-07 19:56 实测）：Surefire **335 / 0 / 0 / 88**（247 passed），
     Failsafe 不执行；Agent 默认 pytest 为 **408 passed / 7 skipped / 1 xfailed**
     （原始产物见 [`artifacts/`](artifacts/)）。

---

## 1. 引用丢失点定位

修改前逐跳核对：

| 环节 | 修改前状态 | 结论 |
| --- | --- | --- |
| Retriever | 返回 `KnowledgeChunk`，含 `document_id` / `chunk_id` / `chunk_index` / 文件名 | 信息完整 |
| Agent Runtime | `validate_citations()` 产出 `CitationReport`，但只写 trace | **结构化信息在此被丢弃** |
| FastAPI `done` | 只含 `finishReason` / `truncated` / 统计字段 | 无引用 |
| Spring `StreamCompletion` | 只有 `finishReason`、`truncated` | 无引用 |
| `message` 表 | 无引用列 | 无持久化 |
| `MessageResponse` | 只有文本消息字段 | 无引用 |
| SSE `message.end` | 只有 `messageId` / `runId`（截断时加标记） | 无引用 |

因此修改前唯一能称为“引用”的，是模型写在正文里的 `[资料《文件名》#序号]` 文本标签。它既没有数据库身份，也无法在文档删除后判断是否仍可用。

---

## 2. 最小 Citation DTO

### 2.1 字段

```json
{
  "documentId": 10,
  "chunkId": 1,
  "chunkIndex": 0,
  "sourceTitle": "zenframe.md"
}
```

| 字段 | 必要性 | 说明 |
| --- | --- | --- |
| `documentId` | 必需 | 打开原文、校验文档归属的唯一身份 |
| `chunkId` | 必需 | 判断切片是否仍存在、是否仍属于该文档 |
| `chunkIndex` | 必需 | 正文标签是 `#片段序号`，没有它就无法把文本标签映射回结构化来源 |
| `sourceTitle` | 必需 | 展示快照；文档改名或删除后仍能告诉用户“当时引用的是什么” |

设计取舍：

- **不存 `chunk.content`**：正文属于文档，重新索引会变化；复制一份快照会制造两份互相矛盾的事实。
- **不新增 `indexVersion`**：Agent 的 `CitationReport` 本轮没有携带它；`chunkId` 已经是切片身份。若未来需要表达“引用的是哪一版索引”，应从 `document.index_version` 或引用表单独读取，而不是现在补一个没有业务消费方的字段。
- **不新增评分字段**：`lexical_support` / `numeric_conflict` 属于离线评估信号，不是来源身份，本轮不进入客户端契约。

### 2.2 持久化 DTO 与读取 DTO

- `CitationMetadata`：落库形态，4 个字段，不含 `available`。
- `CitationResponse`：读取形态，多一个 `available`，由 `CitationService` 在读取时计算。

实时 SSE 与历史读取共用同一套字段，区别只在 `available` 的来源：实时链路刚通过检索白名单，直接为 `true`；历史链路按当前数据库状态重新计算。

---

## 3. 修改文件

### 3.1 Agent（Python）

| 文件 | 变更 |
| --- | --- |
| `agent/app/citations.py` | 新增 `structured_citations(report)`：只输出 `valid` 且 `document_id` / `chunk_id` 完整的引用，按 `(documentId, chunkId)` 去重，转 camelCase |
| `agent/app/runtime.py` | 保存 `validate_citations()` 的返回值；`done` 事件新增 `citations` |
| `agent/app/main.py` | 更新 SSE 契约注释，`done` 增加 `citations` |
| `agent/tests/test_runtime_citations.py` | 新增 3 个测试：`done` 携带结构化引用、无标签时为空、去重并跳过无法解析的标签 |

### 3.2 Spring Boot（Java）

| 文件 | 变更 |
| --- | --- |
| `dto/agent/AgentCitation.java` | 新增：Agent 终态引用的接收 DTO |
| `agent/StreamCompletion.java` | 新增 `List<AgentCitation> citations`；保留 2 参数兼容构造器与 `normal()` |
| `agent/HttpPythonAgentGateway.java` | 解析 `done.citations`；畸形条目逐条丢弃；按 `documentId:chunkId` 去重 |
| `dto/conversation/CitationMetadata.java` | 新增：持久化快照 |
| `dto/conversation/CitationResponse.java` | 新增：客户端读取形态，含 `available` |
| `entity/Message.java` | 新增 `String citations`，映射 JSON 列 |
| `service/CitationService.java` | 新增：序列化 / 解析 / 批量可用性判定 |
| `service/MessageService.java` | 助手消息写入时保存引用；消息列表按消息批量解析并计算可用性 |
| `dto/conversation/MessageResponse.java` | 新增 `List<CitationResponse> citations`（追加在末尾） |
| `service/ChatService.java` | Agent 终态引用转 `CitationMetadata` 后落库；`message.end` 下发 `CitationResponse` |
| `resources/schema.sql` | `message` 新增 `citations JSON DEFAULT NULL` |
| `resources/schema-migration-phase3-citations.sql` | 新增：已有库的幂等迁移脚本 |

### 3.3 测试

| 文件 | 变更 |
| --- | --- |
| `ChatServiceTest.java` | 适配 4 参数 `appendAssistantMessage`；新增“引用落库并出现在 `message.end`” |
| `HttpPythonAgentGatewayTest.java` | 新增 `done.citations` 解析（畸形丢弃、去重）与缺失字段兼容 |
| `CitationServiceTest.java` | 新增：序列化往返、损坏 JSON、可用 / 文档删除 / chunk 缺失 / 他人文档 / chunk 归属不符 / 无引用不查库 |
| `ChatSseIntegrationTest.java` | 新增真实 HTTP + MySQL 的引用闭环用例；适配 4 参数 stub |

---

## 4. 完整数据流

```
Retriever
  └─ KnowledgeChunk(document_id, chunk_id, chunk_index, document_name)
       │
Agent Runtime
  ├─ 模型正文：……[资料《zenframe.md》#0]……
  ├─ StreamingCitationFilter：丢弃非法/畸形标签
  └─ validate_citations() → CitationReport
       └─ structured_citations() → done.citations
            │
FastAPI SSE
  event: done
  data: { finishReason, truncated, citations: [...] }
       │
HttpPythonAgentGateway
  ├─ parseCitations：只接受正数 documentId/chunkId + 非空 sourceTitle
  ├─ 畸形条目逐条丢弃
  └─ (documentId, chunkId) 去重
       │
StreamCompletion.citations
       │
ChatService.onComplete(StreamCompletion)
  ├─ CitationMetadata.fromAgent(...)
  ├─ MessageService.appendAssistantMessage(..., citations)
  │     └─ CitationService.serialize → message.citations JSON
  └─ message.end { messageId, runId, citations: [{..., available: true}] }
       │
GET /api/conversations/{id}/messages
  ├─ 校验对话归属当前用户
  ├─ CitationService.parse(message.citations)
  ├─ 批量加载 document / document_chunk
  ├─ 计算 available
  └─ MessageResponse.citations
```

关键点：

- 落库发生在 `message.end` 之前。客户端看到 `message.end` 时，历史接口已经能读到同一条消息和同一份引用。
- `message.end` 的 `available` 固定为 `true`，因为这一轮引用刚通过检索白名单；历史读取会按届时的文档状态重新计算。
- Agent 的 `done` 是“终态元数据”，不依赖正文标签是否存在。即使模型没有写标签，`citations` 也是空数组。

---

## 5. 接口与 SSE 示例

### 5.1 SSE `message.end`

```text
event: message.end
data: {
  "messageId": 101,
  "runId": 200,
  "citations": [
    {
      "documentId": 10,
      "chunkId": 1,
      "chunkIndex": 0,
      "sourceTitle": "zenframe.md",
      "available": true
    }
  ]
}
```

没有引用时：

```json
{"messageId": 101, "runId": 200, "citations": []}
```

截断时仍在原字段基础上追加 `truncated` / `finishReason`，引用字段不受影响。

### 5.2 `GET /api/conversations/{id}/messages`

```json
{
  "code": 200,
  "message": "success",
  "data": [
    {
      "id": 100,
      "role": "USER",
      "content": "TLB 缓存的是什么？",
      "citations": []
    },
    {
      "id": 101,
      "role": "ASSISTANT",
      "content": "TLB 缓存的是页表项。[资料《zenframe.md》#0]",
      "citations": [
        {
          "documentId": 10,
          "chunkId": 1,
          "chunkIndex": 0,
          "sourceTitle": "zenframe.md",
          "available": true
        }
      ]
    }
  ]
}
```

文档被删除后，同一条消息返回：

```json
{
  "documentId": 10,
  "chunkId": 1,
  "chunkIndex": 0,
  "sourceTitle": "zenframe.md",
  "available": false
}
```

标题仍是引用发生时的快照；`available=false` 明确表示来源已不可用。

---

## 6. 数据库变更

### 6.1 新库

`schema.sql` 的 `message` 表新增：

```sql
`citations` JSON DEFAULT NULL
    COMMENT '助手回复的结构化来源 [{documentId,chunkId,chunkIndex,sourceTitle}]；不存 chunk 正文'
```

不新增表、不新增索引。

### 6.2 已有库

`schema.sql` 全部是 `CREATE TABLE IF NOT EXISTS`，不会给已存在的表补列。已有库必须手工执行：

```bash
mysql -uroot -p ails < backend/backend/src/main/resources/schema-migration-phase3-citations.sql
```

脚本特性：

- 先查 `information_schema.columns`，列已存在则跳过，可重复执行；
- MySQL 8.0 不支持 `ADD COLUMN IF NOT EXISTS`，因此用存储过程实现幂等；
- 不新增、不改写任何原有业务数据；
- 迁移后输出只读核对结果。

本机已对 `ails` 与 `ails_chat_sse_it` 执行该脚本，两库均得到 `citations json YES NULL`。此外，已在专用验证库 `ails_phase3_migration_it` 上完整验证“缺列旧库 → 迁移 → 集成测试通过”的升级路径，详见 §7.4。

### 6.3 旧数据

功能上线前产生的消息 `citations` 为 `NULL`。读取时解析为空数组，不伪造引用。

---

## 7. 测试结果

### 7.1 实际执行

| 命令 | 模式 | 结果 |
| --- | --- | --- |
| `cd agent && python -m pytest -q`（全门控） | 门控全量（历史） | **415 passed, 0 skipped, 1 xfailed**（历史声明，无本轮原始产物） |
| `cd backend/backend && ./mvnw.cmd -B clean verify`（全门控） | 门控全量（历史） | Surefire **335 tests, 0 failures, 0 errors, 11 skipped**；Failsafe **4 / 0 / 0 / 0** |
| `mvn -B test`（不设置 `AILS_*_IT`） | 默认（2026-11-07 19:56 实测） | Surefire **335 / 0 / 0 / 88**（247 passed）；Failsafe 不执行 |
| `cd agent && python -m pytest -q -rs` | 默认（2026-11-07 19:54 实测） | **408 passed / 7 skipped / 1 xfailed** |
| `./mvnw -o -q -Dtest=ChatServiceTest,HttpPythonAgentGatewayTest,CitationServiceTest test` | 定向 | **6 + 15 + 8，全部通过** |
| `AILS_CHAT_SSE_IT=true AILS_IT_MYSQL_PASSWORD=*** ./mvnw -o -q -Dtest=ChatSseIntegrationTest test` | 门控定向 | **9/9 通过**（重建后的 `ails_chat_sse_it`） |
| 同上，但 `AILS_CHAT_SSE_IT_MYSQL_URL` 指向 `ails_phase3_migration_it` | 门控定向 | **9/9 通过**（迁移后的旧 schema，见 §7.4） |

> 计数说明：Phase 3 收尾时无门控的默认套件曾为 **321 / 0 / 0 / 74**，那是历史基线。
> 2026-11-07 门控全量显式开启 MySQL/Qdrant/RabbitMQ/Agent 门控后，Surefire 为
> **335 / 0 / 0 / 11**，Failsafe 为 **4 / 0 / 0 / 0**；Agent 为 **415 / 0 / 1**（历史声明）。
> 同日默认模式实测为 Surefire **335 / 0 / 0 / 88**（247 passed），Failsafe 不执行；
> Agent 为 **408 / 7 / 1**（有原始产物）。两套数字不可混用。11 条 skip 的逐项原因见
> [`INTEGRATION_VERIFICATION.md`](INTEGRATION_VERIFICATION.md) §3.3。

集成测试环境说明：`ails_chat_sse_it` 是旧 schema 遗留库，缺少 `conversation.last_message_seq` 与 `document_chunk.index_version`，首次运行时所有 HTTP 用例 500。该库是专用集成测试库（每个用例开始都会清空业务表），已删除并由启动时的 `schema.sql` 重建后复跑通过。这个问题与本轮代码无关，但记录在此，避免把环境问题误判成通过。

### 7.2 覆盖映射

| 任务要求 | 覆盖位置 |
| --- | --- |
| 1. Agent 返回结构化引用 | `test_runtime_citations.py::test_stream_done_carries_structured_citations` |
| 2. Spring 正确接收 | `HttpPythonAgentGatewayTest::parsesStructuredCitations` |
| 3. SSE 正确携带引用 | `ChatServiceTest::structuredCitationsArePersistedAndSentInEndEvent`；`ChatSseIntegrationTest` |
| 4. DB 正确持久化 | `ChatSseIntegrationTest::citationsRoundTripAndReflectDocumentAvailability` 直接读 `message.citations` |
| 5. 历史消息恢复引用 | 同一集成用例的 `GET /messages` 断言 |
| 6. 无引用时兼容 | `test_stream_done_citations_is_empty_without_labels`、`missingCitationsIsCompatible`、`messagesWithoutCitationsSkipLookups` |
| 7. 不同用户不能读取无权限来源 | `CitationServiceTest::otherUsersDocumentIsUnavailable`；集成用例断言他人 token 读对话返回 404 |
| 8. 文档删除后不变成错误有效链接 | `CitationServiceTest::deletedDocumentIsUnavailable`；集成用例软删文档后断言 `available=false` |

### 7.3 skip 基线变化（历史默认模式，73 → 74）

#### 轮前基线（Phase 2 结束时）

来源：Phase 2 收尾时的历史全量报告，后端未开启门控套件 **309 run / 0 fail / 0 error / 73 skipped**。
该数字仅用于解释 Phase 3 的 skip 增量，最终单次全量见本节末尾。

| 测试类 | skip 条数 | 门控 |
| --- | ---: | --- |
| `DocumentServiceIntegrationTest` | 7 | `AILS_DOCUMENT_IT=true` |
| `DocumentIndexServiceIntegrationTest` | 15 | `AILS_DOCUMENT_INDEX_IT=true` |
| `DocumentIndexE2EIntegrationTest` | 1 | `AILS_DOCUMENT_E2E_IT=true` |
| `Phase2RealStateIntegrationTest` | 6 | `AILS_PHASE2_IT=true` |
| `SecurityContextIntegrationTest` | 19 | `AILS_SECURITY_IT=true` |
| `InternalIdentityHttpIntegrationTest` | 5 | `AILS_SECURITY_IT=true` |
| `ChatSseIntegrationTest` | 8 | `AILS_CHAT_SSE_IT=true` |
| `ChatSseOrderingProbeTest` | 1 | `AILS_CHAT_SSE_IT=true` + `AILS_P8_ORDER_PROBE=true` |
| `Phase2MqProcessorIntegrationTest` | 5 | `AILS_PHASE2_IT=true` |
| `Phase2RabbitMqBrokerTest` | 5 | `AILS_PHASE2_MQ_IT=true` |
| `BackendApplicationTests` | 1 | 类上显式 `@Disabled` |
| **合计** | **73** | — |

#### 历史基线（Phase 3 收尾，未开启门控；**非最终全量**）

**321 run / 0 fail / 0 error / 74 skipped**。此表只用于说明 Phase 3 新增用例对 skip 数的影响，
不是最终单次全量证据。逐类对照：

| 测试类 | 轮前 | 轮后 | 变化 |
| --- | ---: | ---: | ---: |
| `DocumentServiceIntegrationTest` | 7 | 7 | 0 |
| `DocumentIndexServiceIntegrationTest` | 15 | 15 | 0 |
| `DocumentIndexE2EIntegrationTest` | 1 | 1 | 0 |
| `Phase2RealStateIntegrationTest` | 6 | 6 | 0 |
| `SecurityContextIntegrationTest` | 19 | 19 | 0 |
| `InternalIdentityHttpIntegrationTest` | 5 | 5 | 0 |
| `ChatSseIntegrationTest` | 8 | 9 | **+1** |
| `ChatSseOrderingProbeTest` | 1 | 1 | 0 |
| `Phase2MqProcessorIntegrationTest` | 5 | 5 | 0 |
| `Phase2RabbitMqBrokerTest` | 5 | 5 | 0 |
| `BackendApplicationTests` | 1 | 1 | 0 |
| **合计** | **73** | **74** | **+1** |

#### 新增的 1 条

| 项 | 内容 |
| --- | --- |
| 类 / 方法 | `com.ails.backend.service.ChatSseIntegrationTest#citationsRoundTripAndReflectDocumentAvailability` |
| skip 原因 | 类级 `@EnabledIfEnvironmentVariable(named = "AILS_CHAT_SSE_IT", matches = "true")`；普通 `mvn test` 不设置该变量。Surefire 消息：`Environment variable [AILS_CHAT_SSE_IT] does not exist` |
| 是否 Phase 3 引入 | **是**。它是本轮为引用闭环新增的集成用例，继承了该类既有的环境门控；不是环境波动导致旧测试新进入 skip |
| 是否可解锁 | **可**。设置 `AILS_CHAT_SSE_IT=true` 并指向真实 MySQL 后执行 |
| 本轮实际执行 | **已执行并通过**：在重建后的 `ails_chat_sse_it` 上 9/9；在迁移验证库 `ails_phase3_migration_it` 上同样 9/9 |
| 对 Phase 3 结论的影响 | 无。无环境变量时它只是未执行；带门控运行结果是 PASS，不把 skip 当作通过 |

数量核对（历史）：Phase 2 的 309 条 + 本轮新增 12 条 = 321 条（`ChatServiceTest` +1、`HttpPythonAgentGatewayTest` +2、`CitationServiceTest` +8、`ChatSseIntegrationTest` +1）。其中 11 条在普通套件中执行，1 条（上述集成用例）被门控跳过，因此 skip 从 73 增至 74。

#### 最终门控全量（2026-11-07 19:20，`./mvnw.cmd -B clean verify`）

| 套件 | tests | failures | errors | skipped |
| --- | ---: | ---: | ---: | ---: |
| Surefire | 335 | 0 | 0 | 11 |
| Failsafe | 4 | 0 | 0 | 0 |

`ChatSseIntegrationTest` 在本次门控全量中为 **9 / 0 skipped / 0 failures**，真实执行；
`ChatSseOrderingProbeTest` 为 **1 / 0 / 0 / 0**，仍是诊断探针，不计入契约结论。
11 条 skip 只有 `BackendApplicationTests` 1 条与 `AnalysisMqIntegrationTest` 10 条。
Agent 同轮门控全量为 **415 passed / 0 skipped / 1 xfailed**（历史声明，无本轮原始产物）。

**默认模式对照（2026-11-07 19:56 实测）**：Surefire **335 / 0 / 0 / 88**（247 passed），
Failsafe 不执行；`ChatSseIntegrationTest` 的 9 条在默认模式下计入 skip；
Agent 默认模式为 **408 / 7 / 1**（原始产物见 [`artifacts/`](artifacts/)）。

### 7.4 迁移脚本升级路径验证

#### 验证目标

`schema-migration-phase3-citations.sql` 面向“已有库”，而 `schema.sql` 的 `CREATE TABLE IF NOT EXISTS` 不会补列。本轮在真实 MySQL 上验证：**缺少 `message.citations` 的旧库 → 执行迁移 → schema 与全新建库一致 → 集成测试通过 → 重复执行幂等。**

#### 验证库与旧 schema 构造

- 专用库：`ails_phase3_migration_it`（新建；未复用 `ails_chat_sse_it`，未改动 `ails`）。
- 步骤：
  1. `CREATE DATABASE ails_phase3_migration_it`；
  2. 执行 `schema.sql` 建出完整结构，`message.citations` 存在（information_schema 计数 = 1）；
  3. 执行 `ALTER TABLE message DROP COLUMN citations`，计数 = 0，得到“Phase 3 之前”的 schema；
  4. 记录 DROP 前后的 `SHOW CREATE TABLE message`。
- 为什么这个旧 schema 的构造是准确的：Phase 3 对 `schema.sql` 的变更只有 `message.citations` 这一列，没有新增表或索引。因此“当前 schema 减去该列”就是本阶段要升级的实际差异。

DROP 前（节选）：

```sql
`citations` json DEFAULT NULL COMMENT '助手回复的结构化来源 [{documentId,chunkId,chunkIndex,sourceTitle}]；不存 chunk 正文'
```

DROP 后该行消失，其余列定义不变。

#### 迁移执行结果

```text
COLUMN_NAME   COLUMN_TYPE   IS_NULLABLE   COLUMN_DEFAULT
citations     json          YES           NULL
```

列定义核对：

| 项 | 期望 | 实际 |
| --- | --- | --- |
| 列存在 | 是 | 是 |
| 类型 | `json` | `json` |
| 可空 | `YES` | `YES` |
| 默认值 | `NULL` | `NULL` |
| 注释 | 与脚本一致 | 一致 |

#### schema 对比

对“`schema.sql` 全新建库后的 `SHOW CREATE TABLE message`”与“迁移后的 `SHOW CREATE TABLE message`”执行 `diff`：

```text
$ diff schema_built.sql migrated.sql
（无输出）
IDENTICAL
```

即迁移后的 `message` 表与全新建库路径逐字一致，包括列顺序（`AFTER truncated`）与注释。

#### 集成测试

```bash
AILS_CHAT_SSE_IT=true \
AILS_IT_MYSQL_PASSWORD=*** \
AILS_CHAT_SSE_IT_MYSQL_URL='jdbc:mysql://127.0.0.1:3306/ails_phase3_migration_it?...' \
./mvnw -o -q -Dtest=ChatSseIntegrationTest test
```

结果：`Tests run: 9, Failures: 0, Errors: 0, Skipped: 0`，退出码 0。

测试确实使用该库：运行结束后库内仍有 `message = 2`、`agent_run = 2` 行（用例在每个测试前清空业务表，最后一个用例留下的数据即证据）。

#### 幂等验证

在同一库上再次执行迁移脚本：

- 退出码 0，无报错；
- 输出仍是同一列定义（`citations json YES NULL`）；
- 第二次执行后的 `SHOW CREATE TABLE message` 与执行前一致（仅归一化 `AUTO_INCREMENT=n` 后 `diff` 无差异）；
- `citations` 列计数仍为 1。

#### 清理

验证完成后已 `DROP DATABASE ails_phase3_migration_it`；`SHOW DATABASES LIKE 'ails_phase3_migration_it'` 返回空。未保留验证库。

#### 结论与边界

- **升级路径已验证**：缺列旧库执行迁移后与全新建库 schema 一致，集成测试 9/9 通过，重复执行幂等。
- 验证覆盖的是“本阶段实际差异”（仅缺 `message.citations`）。它不能证明任意历史版本、任意未知偏差的 schema 都能被本脚本修复；脚本职责也仅限于补这一列。
- 该验证是手工执行，尚未纳入 CI。

---

## 8. 兼容性

| 场景 | 行为 |
| --- | --- |
| 旧 Agent 不返回 `done.citations` | `parseCitations` 返回空列表，`message.end.citations` 为 `[]` |
| 新 Agent 被旧后端消费 | 旧后端忽略未知的 `done.citations` 字段，不受影响 |
| 旧客户端不认识 `message.end.citations` | SSE 追加字段，旧客户端忽略即可 |
| 旧客户端不认识 `MessageResponse.citations` | JSON 追加字段，旧客户端忽略即可 |
| 功能上线前的历史消息 | `citations` 为 `NULL`，读取为空数组 |
| 引用 JSON 损坏 | 该消息按无引用处理并记录 warning，不导致整个消息列表 500 |
| 截断回答 | 引用照常保存和下发；截断标记与引用互不影响 |
| 畸形引用条目 | 单条丢弃，不因为一条坏数据丢掉整轮终态 |

`StreamCompletion` 保留 `(finishReason, truncated)` 兼容构造器；`MessageService` 保留 2/3 参数 `appendAssistantMessage` 重载，内部委托到 4 参数版本。

---

## 9. 可用性与权限

### 9.1 `available` 判定

读取历史消息时，对每条引用检查：

1. 文档仍存在（MyBatis-Plus `@TableLogic` 自动排除软删）；
2. 文档 `user_id` 等于当前用户；
3. `chunk` 仍存在；
4. `chunk.document_id` 等于引用中的 `documentId`。

任一失败 → `available=false`。

### 9.2 为什么不在写入时固化

文档可能在消息产生后被删除或重新索引，chunk 也可能在版本切换后被清理。写入时的 `true` 到读取时可能已经失真。因此 `message.citations` 只保存身份快照，`available` 始终由读取时计算。

### 9.3 权限边界

- 引用只随消息列表返回，而消息列表本身要求对话归属当前用户。
- 没有新增“按 `chunkId` 直接读 chunk 正文”的接口。
- 打开原文仍走原有文档接口，该接口按用户鉴权。
- 即使客户端拿到了一条 `available=false` 的引用，也没有新的越权读取入口。

---

## 10. 本轮未实现（明确边界）

1. **chunk 正文快照**：不保存引用时的正文；重新索引后原文变化不会反映到历史引用。
2. **引用评分 / 支持度展示**：`lexical_support`、`numeric_conflict` 仍只用于离线评估，不进入客户端契约。
3. **复杂权限系统**：没有实现学习空间共享、协作者、按引用粒度授权；当前只按文档 owner 判断。
4. **重新索引后的引用重映射**：chunk 被清理后旧引用直接变为 `available=false`，不会自动指向新版本的相似切片。
5. **前端渲染**：本轮只保证数据闭环；客户端如何展示角标、跳转与“来源已不可用”不在本轮范围。
6. **复盘 / 其他 Agent 路径**：结构化引用只接入普通对话的 `stream()` 终态；其他会返回引用文本的路径未扩展。
7. **引用去重的业务语义**：按 `(documentId, chunkId)` 去重，不做“同一文档多片段折叠”。
8. **迁移的 CI 覆盖**：迁移脚本的升级路径已在本轮手工验证（§7.4），但尚未纳入 CI；验证范围也仅限于“缺少 `message.citations`”这一本阶段实际差异，不代表能自动修复任意历史 schema 偏差。

---

## 11. 复现命令

```bash
# Agent
cd agent
python -m pytest -q

# 后端单测（清理旧报告，得到权威计数）
cd backend/backend
./mvnw -o clean test

# Citation 定向测试
./mvnw -o -q -Dtest=ChatServiceTest,HttpPythonAgentGatewayTest,CitationServiceTest test

# SSE + MySQL 集成测试（需要本地 MySQL）
AILS_CHAT_SSE_IT=true AILS_IT_MYSQL_PASSWORD=*** \
  ./mvnw -o -q -Dtest=ChatSseIntegrationTest test

# 已有库迁移（幂等）
mysql -uroot -p ails < src/main/resources/schema-migration-phase3-citations.sql

# 迁移升级路径验证（专用库，验证后删除）
mysql -uroot -p -e "CREATE DATABASE ails_phase3_migration_it"
mysql -uroot -p ails_phase3_migration_it < src/main/resources/schema.sql
mysql -uroot -p ails_phase3_migration_it -e "ALTER TABLE message DROP COLUMN citations"
mysql -uroot -p ails_phase3_migration_it < src/main/resources/schema-migration-phase3-citations.sql
AILS_CHAT_SSE_IT=true AILS_IT_MYSQL_PASSWORD=*** \
AILS_CHAT_SSE_IT_MYSQL_URL='jdbc:mysql://127.0.0.1:3306/ails_phase3_migration_it?...' \
  ./mvnw -o -q -Dtest=ChatSseIntegrationTest test
mysql -uroot -p ails_phase3_migration_it < src/main/resources/schema-migration-phase3-citations.sql  # 幂等复跑
mysql -uroot -p -e "DROP DATABASE ails_phase3_migration_it"
```
