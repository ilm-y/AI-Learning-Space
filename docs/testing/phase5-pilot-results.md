# Phase 5 Step 3 Pilot 结果（A/B/C 小规模真实调用）

- 执行日期：2026-11-09
- 执行窗口：18:33:42 – 18:35:12（Asia/Shanghai）
- 范围：3 次真实 LLM 调用、4 次真实 Embedding 调用、Tavily 0 次、1 个隔离 Qdrant collection、1 个隔离 MySQL 库、1 个 Pilot 直接写库事务。
- 结论：3/3 调用成功，未出现 Mock/Fake/Fixture 替代，未出现 401/403，未写生产 Qdrant/MySQL。发现 2 个需要人工决策的问题：
  1. A 基线并非严格“只有 system prompt + 问题”，ContextBuilder 额外注入了 `knowledge_status`（144 tokens），A 的回答也以“本轮没有可用的上传资料”开头。
  2. B 的 Manifest 没有单独的 `evidence` source，但回答引用了 Evidence 的原话；代码与日志确认 Evidence 被折进 `trajectory` 区块，Manifest 只记录 `source_ids[0] = revision:1`。这是 Manifest 忠实度问题，不是静态推断。
- 未进入 Step 3 全量（90 次调用），未进入 Step 4，未调用 Tavily，未修改生产代码/配置/Schema/Policy/ContextBuilder/Manifest/评测标准，未 commit/push。

---

## 1. 前置条件确认

| 前置条件 | 结果 | 证据 |
|---|---|---|
| Agent `.env` 与 Spring `application-local.yml` 的 `AILS_INTERNAL_TOKEN` 相同且非空 | PASS | 两处均存在 64 字符 token；`start_services.py` 启动前做等值校验，未打印 token。 |
| 服务未重启，Agent 内部入口已启用，Spring→Agent 最小请求返回 200 而非 401 | PASS（隔离进程） | 生产 8000/8080 未重启；本次启动隔离 Agent 8001 + Spring 8081。Agent 启动日志 `internal_auth_configured=True`、`internal_identity_configured=True`。3 次 `/agent/chat` 均为 200；Agent/Spring 日志中 401=0、403=0。 |
| 隔离 Qdrant collection `phase5-pilot-*` 由脚本创建，带时间戳/随机后缀 | PASS | `phase5-pilot-20261109183337-f844f7_text-embedding-v4_1024`，创建 HTTP 200，起始 points=0，结束时 points=1。 |
| 不写默认/生产 Qdrant collection | PASS（有 1 个只读检查告警） | 知识检索只使用上述隔离 collection；默认知识 collection 未被创建/写入。记忆检索只对默认名 `ails_memories_text-embedding-v4_1024` 做 `/exists` 检查，返回“memory collection 未建立”warning，未创建、未写入。 |
| LLM `max_tokens >= 4096` | PASS | 实际 `max_tokens=4096`，`temperature=0.6`。 |
| 记录 `finish_reason` 与 token usage | PASS | 3 次均 `finish_reason=stop`；usage 由测试专用只读诊断路由读取，未修改生产代码。 |
| Tavily 已排除 | PASS / SKIPPED | 日志中 `tavily` 出现 0 次；未发起任何 Tavily 调用。 |
| 隔离 MySQL 库 `phase5_pilot_*` | PASS | `phase5_pilot_20261109183337_f844f7`；Pilot 直接写库事务 1 个；Spring 业务写入全部落在该库。 |
| 不输出密钥/token/连接串密码 | PASS | 本文档不含任何密钥；日志已做布尔检查，`AILS_INTERNAL_TOKEN`、`AILS_LLM_API_KEY`、`AILS_EMBEDDING_API_KEY` 均未出现在日志中。 |

补充说明：

- `agent/.env` 中没有 `AILS_INTERNAL_IDENTITY_SECRET` / `AILS_INTERNAL_DELEGATION_SECRET`。`start_services.py` 为隔离 Agent/Spring 生成了临时 identity/delegation secret，仅注入子进程环境，未写任何配置文件；Spring 的 `InternalSecurityConfigValidator` 校验通过。
- 旧 Spring jar 早于最新提交，已用当前源码执行 `mvnw -o -DskipTests package` 重建；未改源码。
- 本次验证的是隔离进程，不是生产 8000/8080 进程。生产进程保持原状，符合“服务尚未重启”的前提，但也意味着生产进程的旧 token 状态没有被本次 Pilot 修复。

---

## 2. 最小数据集

| 对象 | ID / 值 |
|---|---|
| 测试用户 | id=1，`phase5pilot_20261109183337_f844f7` |
| 测试 Space | id=1，`Phase 5 Pilot 20261109183337-f844f7` |
| Concept | id=1，`TCP 三次握手` |
| Session | id=1，goal=`真正理解 TCP 三次握手，尤其是第三次 ACK 的必要性`；currentFocus=`说不清第三次握手为什么不能省` |
| Conversation | A=1，B=2，C=3 |
| 历史消息 | B: id=3/4；C: id=5/6（同一轮用户原话与助手回复） |
| Understanding | id=1，PARTIAL |
| Revision | id=1 |
| Evidence | id=1，`USER_EXPLANATION`，PARTIAL |
| Memory | id=1，ACTIVE，CONCEPT scope |
| 文档 | id=1，`phase5-pilot-tcp.md`，912 bytes，380 chars，READY，chunkCount=1，indexVersion=1 |
| Qdrant | 隔离 collection 中 1 个 point，1024 维，Cosine |

文档内容为合成的 TCP 三次握手说明，覆盖第三次 ACK 的“服务端确认客户端收到 SYN-ACK、确认 server_isn、避免历史重复 SYN”三个要点，长度约 380 字符，恰好 1 个 chunk。

---

## 3. 调用顺序与问题

实际顺序为 **A → B → C**，不是理想的 A → C → B。原因：B 必须在文档上传前运行，否则 B 会检索到知识文档，破坏“B 无 Knowledge”的对照。C 在文档 READY 后运行。这个顺序是有意为之，但会在全量 Step 3 中作为设计约束保留或改用独立 Space 解决。

| 策略 | 问题 | 设计意图 |
|---|---|---|
| A | `TCP 三次握手中，为什么第三次握手是必要的？` | 无个人上下文基线；Knowledge 不可用；无 Session/Understanding/Memory/Evidence/History。 |
| B | `帮我回顾一下我目前对 TCP 三次握手为什么需要第三次握手的理解；请引用我之前说过的原话作为证据，指出我卡在哪里。` | D4 个人上下文（Session + Understanding + Memory + Evidence + History）；Knowledge 不可用；无测试文档。 |
| C | `请结合我刚上传的资料，解释 TCP 三次握手为什么需要第三次握手，并引用证据说明我当前理解里还缺什么。` | 与 B 相同的个人上下文 + 隔离 collection 中的 Knowledge chunk；验证检索/注入/Citation。 |

注意：A/B/C 使用了不同的提问。这符合三种策略的语义，但也意味着三者不是“同一问题的三种上下文”严格对照；全量 Step 3 若要比较策略效果，应改为同一 case 同一问题、只切换上下文策略。

---

## 4. 逐次调用记录

| 策略 | conversation | request_id | run_id | provider / model | max_tokens | temperature | finish_reason | prompt tokens | completion tokens | total tokens | reasoning tokens | cached tokens | HTTP elapsed | AgentRun duration | upstream LLM latency | first delta | answer chars | truncated | citations |
|---|---|---|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| A | 1 | `d035a2258b7140ac80ecea68c75903c8` | 1 | openai / deepseek-flash | 4096 | 0.6 | stop | 1645 | 2424 | 4069 | 1644 | 0 | 20016 ms | 19910 ms | 19743 ms | 14188 ms | 1517 | false | 0 |
| B | 2 | `67c14ec1e53841c2a8b7ffa93b2080fc` | 3 | openai / deepseek-flash | 4096 | 0.6 | stop | 2592 | 1561 | 4153 | 955 | 1280 | 13468 ms | 13417 ms | 12887 ms | 13436 ms | 1069 | false | 0 |
| C | 3 | `b1bda099188f4ea4b333086935e8fe61` | 5 | openai / deepseek-flash | 4096 | 0.6 | stop | 2547 | 2895 | 5442 | 2136 | 1792 | 20234 ms | 20194 ms | 19723 ms | 15750 ms | 1427 | false | 1 |
| 合计 | — | — | — | — | — | — | 3/3 stop | 6784 | 6880 | 13664 | 4735 | 3072 | — | — | — | — | 4013 | 0 truncated | 1 |

补充：

- `attempt=1`，`maxAttempts=2`，`attemptHistory` 只有 1 条；**无重试**。
- `emptyChoiceCount=0`，三次均有非空 content。
- AgentRun 状态：run 1/3/5 均为 `COMPLETED`，`error_message=null`；assistant message id 分别为 2/8/10。
- 另有 3 条 ANALYSIS 行（id=2/4/6）状态 `PENDING`，对应 MQ 请求队列中 3 条未被消费的消息；`/agent/analyze` 调用 0 次，确认没有额外的分析 LLM 调用。
- Spring 日志显示 3 次 `分析任务已投递, taskId=2/4/6`；请求队列 `messages_ready=3, consumers=0`。这是 `AILS_ANALYSIS_MODE=mq` 的预期行为，不是失败。

---

## 5. Manifest 与 Policy

### 5.1 Manifest 摘要

| 策略 | intent | policy_version | budget used / total | sources（type:source_id / tokens / injection_status） |
|---|---|---|---:|---|
| A | `KNOWLEDGE_QUERY` | `d4-2-v1` | 215 / 3000 | `knowledge_status:knowledge-status:NO_VALID_DOCUMENTS` 144 SELECTED；`user_input:message:current` 20 SELECTED |
| B | `LEARNING_REVIEW` | `d4-2-v1` | 1600 / 3000 | `history:1` 35；`history:2` 24；`knowledge_status` 144；`memory:1` 100；`session:1` 50；`trajectory:revision:1` 177；`understanding:1` 333；`user_input` 54。**无 `evidence` source。** |
| C | `KNOWLEDGE_QUERY` | `d4-2-v1` | 1567 / 3000 | `evidence:1` 53；`history:1` 35；`history:2` 24；`knowledge:chunk:1` 302；`memory:1` 100；`session:1` 50；`understanding:1` 333；`user_input` 48 |

### 5.2 Policy 合规

| 策略 | Policy 期望 | 实际 | 判定 |
|---|---|---|---|
| A | KNOWLEDGE_QUERY：Knowledge REQUIRED；无 concept anchor 时 U/M/E/Revision 强制 SKIP。 | Knowledge 无有效文档 → `NO_VALID_DOCUMENTS`；U/M/E/Revision 均未注入；只注入 `knowledge_status` + `user_input`。 | 符合 Policy；但 A 不是严格“只有 system + 问题”的基线（见 5.3）。 |
| B | LEARNING_REVIEW：U/M/Revision REQUIRED；显式证据请求时 Evidence REQUIRED；Knowledge OPTIONAL。 | U/M/Revision 均 SELECTED；Evidence 被折进 `trajectory` 区块（`GET /api/evidence/1` 200，回答引用其 observation）；Knowledge 无文档 → `NO_VALID_DOCUMENTS`。 | 行为上满足 Evidence 要求；Manifest 未单列 Evidence，严格按 Manifest 审计会误判为 Evidence 缺失。 |
| C | KNOWLEDGE_QUERY：Knowledge REQUIRED；U/M OPTIONAL；显式证据请求时 Evidence REQUIRED；Revision SKIP。 | Knowledge chunk SELECTED；Evidence SELECTED；U/M SELECTED；Revision 未注入。 | 符合 Policy。 |

### 5.3 两个 Manifest/基线发现

#### 发现 1：B 的 Evidence 未在 Manifest 中单列（确认，非静态推断）

证据链：

1. B 的回答原文引用：`「客户端先发 SYN，服务端回 SYN-ACK，客户端再回 ACK，但不清楚为什么还要第三次。」`
2. 这句话只存在于 seeded Evidence 的 `observation`，不在历史消息或 Revision 的 `user_explanation` 中。
3. Agent 日志：`GET http://127.0.0.1:8081/api/evidence/1 "HTTP/1.1 200"`，发生在 B 的 run 3 期间（18:34:37）。
4. B 的 Manifest 只有 `type=trajectory, source_id=revision:1, estimated_tokens=177`；`per_type` 中也没有 `evidence`。
5. 代码：`agent/app/context/builder.py:651-653` 把 trajectory 区块的 `source_ids` 设为 `revision:{id}` + `evidence:{id}`；而 `agent/app/context/builder.py:1254`（以及 `:1151-1152` 的 exclusion 路径）只取 `section.source_ids[0]` 写入 Manifest。

结论：Evidence 确实被注入（在 trajectory 区块内），但 Manifest 只记录 `revision:1`，导致：

- 按 source 粒度审计时，Evidence 的 provenance 丢失；
- Evidence 的 token 被计入 `trajectory`（177），`per_type` 没有 evidence 条目；
- 如果全量 Step 3 用 Manifest 统计“Evidence 是否注入/占多少预算”，B 会被误判。

建议（未在本次实施）：Manifest 对多 source section 输出 `source_ids` 数组，或为每个 source 生成独立 entry；`per_type` 对折叠进 trajectory 的 evidence 也保留 allocation/used。该修改属于生产代码变更，需单独立项。

#### 发现 2：A 基线包含 `knowledge_status`

A 的 Manifest 只有 `knowledge_status` + `user_input`，没有 Session/Understanding/Memory/Evidence/History，这部分符合“无个人上下文”。但 ContextBuilder 在无有效文档时仍注入 `knowledge_status:NO_VALID_DOCUMENTS`（144 tokens），且 A 的回答以“本轮没有可用的上传资料”开头。

这意味着：

- A 是“无个人上下文 + 无 Knowledge chunk + 有 knowledge 状态提示”的基线；
- 不是字面意义上的“只有 system prompt + 问题”；
- 该提示可能让模型倾向于说明“没有资料”，从而影响 A 与 B/C 的可比性。

建议：全量 Step 3 要么显式接受这个基线定义，要么增加一个真正的 `baseline_no_context_no_status` 开关/策略；不要把它当作“纯 system+question”基线来解读。

---

## 6. 成功与失败模式

### 成功

- 3/3 真实 LLM 调用 HTTP 200，SSE `message.start` → `message.delta` → `message.end` 正常；`finish_reason=stop`；无空回答；无重试。
- 3/3 AgentRun `COMPLETED`，assistant message 正确落库，`duration_ms` 与 HTTP elapsed 一致。
- C 的 Knowledge 检索真实走通：隔离 collection `points/query` 200，`candidates=1, aboveThreshold=1, valid=1`，注入 302 tokens，生成 Citation。
- Citation 闭环：SSE `done.citations` → `message.citations` → `GET /messages` 的 `available=true`。
- Spring→Agent 内部鉴权走通：无 401/403；`X-Internal-Token` / `X-Internal-Identity` 生效。
- Tavily 0 次；未出现 Mock/Fake/Fixture 替代（`retrieverMode=http`，provider 为真实 `openai`/DashScope）。
- 清理成功：隔离 DB、Qdrant collection、RabbitMQ 队列/交换机全部删除，端口关闭。

### 失败/降级/告警

- 无调用级失败。
- 记忆检索告警 2 次：`Agent 记忆向量检索返回 warning：[memory collection 未建立]`。原因是记忆检索使用默认 collection 名 `ails_memories_text-embedding-v4_1024`，该 collection 不存在；未创建、未写入。Memory:1 仍由 Spring hybrid search 返回，并被 ContextBuilder 选中。
- ANALYSIS 任务 3 条 `PENDING`、MQ 请求队列 3 条未消费：MQ 模式预期，但意味着分析链路（Understanding/Revision 更新）未在本次 Pilot 中验证。
- AgentRun 表没有 model/token usage 列；SSE `done` 也没有 usage 字段。本次 usage 来自测试专用只读诊断路由，不是生产可观测能力。

---

## 7. 成本与资源

| 项目 | 实际 | 上限 | 说明 |
|---|---:|---:|---|
| LLM 调用 | 3 | 6 | A/B/C 各 1 次，无重试。 |
| LLM tokens | 13664（prompt 6784 / completion 6880 / reasoning 4735 / cached 3072） | — | 平均 4555 tokens/次。 |
| Embedding 调用 | 4 | 15 | 文档索引 1、B memory search 1、C memory search 1、C knowledge query 1。 |
| Tavily 调用 | 0 | 0 | 已排除。 |
| Qdrant collection | 1 个隔离 collection | 1 | 1 个 point；6 次 payload index PUT 属于 collection 元数据，不是数据点。 |
| MySQL 库 | 1 个隔离库 | 1 | `phase5_pilot_...`；Pilot 直接写库事务 1 个。 |
| 重试 | 0 | 1 | 所有调用 attempt=1。 |
| 运行时长 | 约 57 秒（18:34:15–18:35:12） | — | 不含服务启动/清理；单次 13–20 秒。 |

**成本口径风险**：本次把“写事务 ≤2”理解为 Pilot 脚本直接写库的事务数（1 个）。Spring 正常业务链路在同一隔离库内产生了多次写入（用户、Space、Conversation、Message、AgentRun、Document、Chunk 等）。如果该上限的意图是“整个 Pilot 的 MySQL 事务总数 ≤2”，则本次**不满足**，需要在全量 Step 3 前明确口径。本文不把应用正常写入伪装成 1 个事务。

---

## 8. Citation 可追溯性

C 的闭环完整：

1. Agent `done.citations`：
   ```json
   [{"documentId":1,"chunkId":1,"chunkIndex":0,"sourceTitle":"phase5-pilot-tcp.md","available":true,"isHistorical":false}]
   ```
2. Spring `message.citations` 持久化：
   ```json
   [{"chunkId":1,"chunkIndex":0,"documentId":1,"sourceTitle":"phase5-pilot-tcp.md","indexVersion":1}]
   ```
3. `GET /messages` 计算 `available`：
   ```json
   [{"documentId":1,"chunkId":1,"chunkIndex":0,"sourceTitle":"phase5-pilot-tcp.md","available":true,"isHistorical":false}]
   ```
4. 回答正文引用 `[资料《phase5-pilot-tcp.md》#0]`，对应 document id=1、chunk index=0、indexVersion=1。
5. 文档为 READY，chunkCount=1，Qdrant 隔离 collection 中 points=1。

A/B 的 `citations=[]`，与“无 Knowledge 文档”一致。B 引用的 Evidence 原话属于个人证据，不是文档 citation；它不应产生文档 citation，但应在 Manifest 中体现 evidence source（见 5.3）。

---

## 9. D4-2A 语义错误检查

样本：B、C 两次个人上下文调用。

- B：正确区分“用户已掌握的三步顺序”与“用户不确定的第三次握手必要性”。B 说“这个方向值得单独拿出来说”“你卡在哪里”，把 seeded uncertain part 当作不确定点，而不是已确认的误区；没有把“用户没说过的错误”强加给用户。未观察到 D4-2A 语义错误。
- C：指出“你的疑问在这里方向反了”，并用文档证据解释“第三次 ACK 是让服务端确认客户端收到了 SYN-ACK”，与 seeded Understanding 的 uncertainty 对齐；对文档未展开的历史重复 SYN 机制明确标注为“通用背景”，没有伪造成资料内容。未观察到 D4-2A 语义错误。

限制：

- 只有 2 个样本，不能据此宣称 D4-2A 没有缺陷；
- 本次未覆盖“用户明确说错”“用户否定句”“多概念冲突”“历史纠正”等边界；
- 未复现的缺陷不登记为已复现缺陷；静态推断不作为缺陷结论。

---

## 10. 全量 Step 3 建议与预算

### 10.1 进入全量前的决策项

1. **B Manifest Evidence**：先决定是否修复 `source_ids[0]` 的 Manifest 表达，或在全量分析中接受“Evidence 折进 trajectory”的口径。若不修复，所有 B 类样本的 evidence 统计都会偏低。
2. **A 基线定义**：接受 `knowledge_status` 提示，还是增加真正的无上下文/无状态基线。两者不能混称。
3. **usage 可观测性**：在生产 AgentRun 中增加 model/token usage 字段，或明确全量 Step 3 继续使用测试专用诊断路由。后者不适合作为长期方案。
4. **同题对照**：全量应使用同一 case 的同一问题，只切换 A/B/C 上下文策略；否则策略效果与问题难度混杂。
5. **顺序与隔离**：若同一 Space 内先跑 C 再跑 B，B 会检索到文档。建议每个 case 使用独立 Space/Conversation，或严格按 A→B（上传前）→C（上传后）执行。
6. **写事务口径**：明确“写事务 ≤2”是否只统计 Pilot 直接 SQL，还是统计整个应用链路。

### 10.2 建议预算

按 30 个 case × 3 策略 = 90 次 LLM 调用估算：

| 项目 | 建议预算 | 依据 |
|---|---:|---|
| LLM 调用 | 90（+最多 9 次重试） | 3 策略 × 30 case；Pilot 重试率 0。 |
| LLM tokens | 约 410k（建议硬上限 500k） | Pilot 平均 4555 tokens/次 × 90 = 409,950；prompt 约 204k、completion 约 206k、reasoning 约 142k。 |
| Embedding 调用 | 120–150（建议上限 200） | 30 个文档 chunk + 60 次 B/C memory search + 30 次 C knowledge query；Pilot 每次 memory/knowledge 各 1 次。 |
| Tavily | 0 | 已排除。 |
| Qdrant | 1 个隔离 collection，约 30–60 points | 30 个文档 chunk；若每 case 独立 collection 则需额外隔离与清理。 |
| MySQL | 1 个隔离库 | 与 Pilot 一致。 |
| 运行时长 | 约 30–45 分钟（顺序执行） | Pilot 3 次调用约 57 秒；全量含索引、等待 READY、清理。 |
| 重试/失败预算 | 每调用最多 1 次重试；失败即记录 | 与本次约束一致。 |

### 10.3 建议停止条件

- 任何请求返回 401/403；
- 任何知识/记忆写入默认或生产 Qdrant collection；
- 任何写入生产 MySQL；
- 连续空回答或 `finish_reason=length` 超阈值；
- 单调用重试超过 1 次；
- 出现 Tavily 调用；
- 隔离资源无法清理。

---

## 11. 未完成与风险

1. **样本量极小**：3 次调用，不能代表 90 次全量；语义质量、Citation 稳定性、失败率都无统计意义。
2. **A/B/C 问题不同**：策略效果与问题差异混杂；全量需同题对照。
3. **顺序 A→B→C**：不是理想的 A→C→B；B 在文档上传前运行是刻意隔离，但全量需明确同一 case 的执行顺序。
4. **A 基线不纯**：`knowledge_status` 注入，A 不是严格 system+question 基线。
5. **B Manifest 缺 evidence**：已确认，需修复或明确口径。
6. **记忆检索默认 collection 告警**：`ails_memories_text-embedding-v4_1024` 不存在，仅 `/exists` 检查；未写入，但说明记忆检索路径没有随 Pilot 前缀隔离。
7. **ANALYSIS 未执行**：3 条 PENDING、MQ 3 条未消费；Understanding/Revision 更新链路未验证。
8. **AgentRun 缺 model/token 字段**：usage 只能靠测试专用诊断路由；生产可观测性不足。
9. **单文档/单 chunk**：未覆盖多 chunk、低分过滤、空检索、Citation 去重、历史 citation、文档删除后的 `available=false` 等边界。
10. **未覆盖失败路径**：超时、429、5xx、空 SSE、`finish_reason=length`、重试均未触发。
11. **Tavily 未验证**：按范围排除，不是缺陷。
12. **生产服务未重启**：隔离进程验证通过，但生产 8000/8080 仍是旧进程/旧 token 状态；后续若要上生产，需要单独的重启与鉴权验证。
13. **日志保留**：临时目录 `%TEMP%\phase5-pilot-_o9d2e7k` 保留 `agent.log`、`spring.log`、`pilot-results.json`、`cleanup-result.json`。日志含 DEBUG SQL 参数与测试用户内容，不含密钥；是否保留由人工决定。

---

## 12. 清理结果

`python scripts/phase5/pilot/cleanup.py <state.json>` 返回 `ok=true`：

| 资源 | 结果 |
|---|---|
| Agent 8001 | 已停止（PID 33248） |
| Spring 8081 | 已停止（PID 55868） |
| 端口 8001/8081 | 已关闭 |
| 隔离 MySQL 库 `phase5_pilot_20261109183337_f844f7` | 已 DROP，剩余 0 |
| 隔离 Qdrant collection | DELETE 200，复查 404，`remainingPhase5Pilot=[]` |
| RabbitMQ Pilot 队列（5 个） | 全部 DELETE 204 |
| RabbitMQ Pilot 交换机（2 个） | 全部 DELETE 204 |
| RabbitMQ leftovers | `[]` |

独立复查：`SHOW DATABASES LIKE 'phase5_pilot_%'` 返回空；Qdrant 中无 `phase5-pilot-*`；RabbitMQ 中无 `phase5.pilot.*`；8001/8081 均不可连接。

---

## 13. Git 状态

`git status --short`：

```text
?? docs/testing/phase5-connectivity-check.md
?? docs/testing/phase5-preflight-audit.md
?? scripts/phase5/
```

- 无 tracked 文件修改（`git diff --name-only` 为空）。
- 本次新增：`docs/testing/phase5-pilot-results.md`、`scripts/phase5/pilot/`（common.py、run_agent.py、start_services.py、pilot.py、cleanup.py、fixture_tcp.md）。
- 之前 Step 1/2 的两个 docs 与 `scripts/phase5/` 仍是 untracked。
- 未 commit、未 push。
- Spring jar 在 `backend/backend/target/` 重建，target 为构建产物，未纳入 tracked 变更。

---

## 14. 人工决策

1. 是否接受 A 的 `knowledge_status` 注入作为基线定义？若不接受，全量前增加真正的无上下文基线。
2. 是否在全量 Step 3 前修复 B 的 Manifest evidence 表达（`source_ids` 数组或独立 entry）？建议修复，否则 evidence 统计不可信。
3. 是否把 model/token usage 持久化到 AgentRun，或继续使用测试专用诊断路由？建议至少为全量 Step 3 保留诊断路由并明确其非生产属性。
4. 全量 Step 3 是否采用“同一 case 同一问题、独立 Space/Conversation”的严格对照设计？
5. “写事务 ≤2”的口径是否只统计 Pilot 直接 SQL？若统计整个应用链路，本次已超限，需要重新定义。
6. 生产 8000/8080 的旧 token/旧进程是否在 Phase 5 后续单独重启验证？本次未重启。
7. Tavily 继续排除，不纳入 Phase 5。
