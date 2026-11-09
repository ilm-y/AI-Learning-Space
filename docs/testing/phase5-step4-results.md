# Phase 5 Step 4：真实 MySQL + Qdrant + Embedding + LLM 全链路测试结果

- 执行时间：2026-11-10 00:37–00:45（本地时间）
- 测试戳：`20261110003720-72cb36`
- 执行脚本：`scripts/phase5/full/run_step4.py`
- 运行模式：真实 Spring 8081 → Agent 8001（Context Strategy C）→ 真实 MySQL 8.0.37 → 真实 Qdrant 1.20.0 → 真实 Embedding `text-embedding-v4`（1024 维）→ 真实 LLM `deepseek-flash`（max_tokens 4096，temperature 0）
- Tavily：0 次调用，未伪造
- 结论摘要：12 次业务请求、13 次 LLM 调用、30 次 Embedding HTTP 调用；显式失败判据 0 命中；跨 user/space 隔离 PASS；冻结一致性与清理 PASS。存在 1 项需求缺口（组合场景个人来源没有结构化 Citation）与 3 项行为/方法学问题，见第 6 节。

> 本报告只记录 Step 4 实测结果，不进入 Step 5，不修改生产代码，不 commit、不 push。

---

## 1. 前置条件确认

Setup 阶段 preflight 全部通过，环境按测试戳隔离：

| # | 前置条件 | 结果 | 证据 |
|---|---|---|---|
| 1 | 隔离 MySQL / Qdrant / Memory collection 可用，命名带 test stamp | PASS | MySQL `phase5_pilot_20261110003720_72cb36`；文档 collection `phase5-pilot-20261110003720-72cb36_text-embedding-v4_1024`；Memory collection `phase5-pilot-20261110003720-72cb36-memory_text-embedding-v4_1024`；默认 collection `ails_memories_text-embedding-v4_1024` 未被使用（测试前后均 404） |
| 2 | 内部凭据 401 已解决 | PASS | preflight `internalToken=set`；12 次 chat 均 HTTP 200；未出现 401 |
| 3 | 分析消费者关闭（`AILS_ANALYSIS_MODE=mq`，worker 未启动） | PASS（有观测说明） | Spring 子进程使用 `AILS_ANALYSIS_MODE=mq`，Agent analysis worker 未启动；request 队列 consumer=0，运行期间累积 12 条未消费消息；result 队列在 setup 时观察到 1 个 Spring listener、0 条消息，服务停止后 consumer=0；冻结表 hash 全程不变 |
| 4 | LLM max_tokens ≥ 4096 | PASS | `AILS_LLM_MAX_TOKENS=4096`，preflight detail=4096 |
| 5 | 文档索引走真实 Embedding，不写伪造向量 | PASS | 6 个文档均 `status=READY`；`embedding_model=text-embedding-v4`、`embedding_provider=openai-compatible`、`embedding_dimension=1024`、`embedding_fingerprint=v1:e7464a6c...`；Qdrant collection dimension=1024；setup 实际发生 11 次 Embedding HTTP 请求、18 次 Qdrant 写入；23 个文档 point + 4 个 Memory point |
| 6 | HISTORY_GUARD `internal_process` 对「想通」的误判已登记 known-issues（只登记，不修） | PASS | `docs/testing/phase5-known-issues.md` 已登记 KI-3，并补充 Step 4 run 15 的新证据；本批未修改 `agent/app/history_guard.py` |

### 1.1 成本

| 项目 | 用量 | 上限 | 结果 |
|---|---:|---:|---|
| LLM 调用 | 13（12 条记录 + 1 次守卫重生成） | 30 | PASS |
| Embedding HTTP 调用 | 30（setup 11 + run/guard 19） | 60 | PASS |
| Qdrant 查询请求 | 36（run/guard 期间） | — | — |
| Qdrant 写入请求 | 18（setup 文档/记忆索引） | — | — |
| Qdrant collection | 2（文档 + Memory，均带测试戳） | 1–2 | PASS |
| MySQL 隔离库 | 1 | 1 | PASS |
| Tavily | 0 | 0 | PASS |

---

## 2. 场景逐项记录

所有记录均包含四个观测字段：`historyGuardTriggered` / `memoryDegraded` + `memoryDegradedSource` / `quoteSources` / `understandingAsOriginal`。脚本内置判据结果并入各记录的 `problems`，12 条记录均为 `[]`。

### 2.1 场景 A：个性化学习问答链路

- 问题：`为什么 TCP 慢启动阶段拥塞窗口会指数增长？请说明依据。`
- run_id：**1**
- 结果：HTTP 200，`agent_run=COMPLETED`，`finishReason=stop`，`truncated=false`

**Manifest 关键字段**

- intent：`LEARNING_EXPLANATION`；policy：`d4-2-v1`
- sourceTypes：`evidence, history, knowledge, memory, session, understanding, user_input`
- sourceIds：`evidence:1, history:1, history:2, chunk:1, chunk:2, chunk:3, memory:1, session:1, understanding:1, message:current`
- budget：used 1611 / total 3000，effective 2700

**Citation / Usage**

- Citation：document 1（`s4a-slow-start.md`）chunks 1、2、3；`traceable=true`，`citationPayloadCheck.ok=true`
- Usage：`REAL`；prompt 2672 / completion 1843 / total 4515；cached 1408；reasoning 1141

**四个观测字段**

- `historyGuardTriggered=false`
- `memoryDegraded=false`，`memoryDegradedSource=[]`
- `quoteSources=["knowledge"]`；命中片段：`每收到一个 ACK`、`每个 RTT 内近似翻倍`
- `understandingAsOriginal=false`

**判定**：Manifest 含 history / understanding / memory / evidence；Citation 可追溯；AgentRun COMPLETED；Usage REAL —— 场景 A 的硬性判据全部通过。

**重要观测（D2）**：回答全文只引用文档，没有引用任何个人来源。Manifest 明明注入了 `understanding:1`、`memory:1`、`evidence:1`、`history:1/2`，但最终回答没有出现用户历史、理解、记忆或证据的片段。模型选择纯知识作答，不是 Schema/Builder 丢来源。

### 2.2 场景 B：Knowledge RAG 链路

#### B-1 用户 1 的文档检索

- 问题：`请根据我上传的资料，说明发送端在连接初期为什么会加速得越来越快。`
- run_id：**3**
- 结果：HTTP 200，`COMPLETED`，`finishReason=stop`，`truncated=false`

**Manifest 关键字段**

- intent：`KNOWLEDGE_QUERY`；policy：`d4-2-v1`
- sourceTypes / selectedTypes：`history, knowledge, session, user_input`
- sourceIds：`history:1, chunk:4, session:2, message:current`
- budget：used 501 / total 3000

**Citation / Usage / 检索计数**

- Citation：document 2（`s4b-window-growth.md`）chunk 4；`traceable=true`；payload owner `user_id=1, space_id=2, document_id=2`；`citationPayloadCheck.ok=true`
- Usage：`REAL`；prompt 1842 / completion 1544 / total 3386；cached 1408；reasoning 788
- Embedding：记录内 1 次调用、1 个文本；Qdrant 查询 2 次；collection dimension 1024

**四个观测字段**

- `historyGuardTriggered=false`
- `memoryDegraded=false`，`memoryDegradedSource=[]`
- `quoteSources=["knowledge"]`
- `understandingAsOriginal=false`

**判定**：真实上传 → 真实 Embedding → 隔离 Qdrant → 真实检索 → ContextBuilder → LLM → Citation 全链路通过；Citation 指向真实 chunk；无跨文档引用。

#### B-2 用户 2 / 独立 Space 的检索与隔离

- 问题：`请根据我上传的资料，说明页分裂时为什么只影响局部路径。`
- run_id：**5**
- 结果：HTTP 200，`COMPLETED`，`finishReason=stop`，`truncated=false`

**Manifest 关键字段**

- intent：`KNOWLEDGE_QUERY`；policy：`d4-2-v1`
- sourceIds：`history:1, chunk:5, session:3, message:current`
- budget：used 475 / total 3000

**Citation / Usage**

- Citation：document 3（`s4b2-page-split.md`）chunk 5；`traceable=true`；payload owner `user_id=2, space_id=3, document_id=3`；`citationPayloadCheck.ok=true`
- `foreignCitations=[]`
- Usage：`REAL`；prompt 1816 / completion 2196 / total 4012；cached 1408；reasoning 1305

**四个观测字段**

- `historyGuardTriggered=false`
- `memoryDegraded=false`，`memoryDegradedSource=[]`
- `quoteSources=["knowledge"]`
- `understandingAsOriginal=false`

**判定**：用户 2 只检索到自己的 chunk 5；用户 1 的 B-1 只检索到自己的 chunk 4；二者没有交叉引用。

### 2.3 组合场景：个人历史 + Knowledge

- 问题：`为什么 TCP 拥塞避免阶段窗口增长会变慢？请结合我已有的理解和阅读材料里的解释说明。`
- run_id：**7**
- 结果：HTTP 200，`COMPLETED`，`finishReason=stop`，`truncated=false`

**Manifest 关键字段**

- intent：`LEARNING_EXPLANATION`；policy：`d4-2-v1`
- sourceTypes：`evidence, history, knowledge, memory, session, understanding, user_input`
- sourceIds：`evidence:4, history:1, history:2, chunk:6, chunk:7, chunk:8, memory:4, session:4, understanding:4, message:current`
- budget：used 1559 / total 3000

**Citation / Usage**

- Citation：document 4（`s4c-congestion-avoidance.md`）chunks 6、7、8；`traceable=true`；payload owner `user_id=1, space_id=4, document_id=4`；`citationPayloadCheck.ok=true`
- `foreignCitations=[]`
- Usage：`REAL`；prompt 2601 / completion 1309 / total 3910；cached 1408；reasoning 598

**四个观测字段**

- `historyGuardTriggered=false`
- `memoryDegraded=false`，`memoryDegradedSource=[]`
- `quoteSources=["understanding", "memory", "knowledge"]`
  - understanding 片段：`慢启动翻倍、拥塞避免每轮加一`
  - memory 片段：`窗口随 RTT 变化的曲线`
  - knowledge 片段：`每个 RTT 只线性增加一个 MSS`、`ssthresh 是两种增长方式的分界`
- `understandingAsOriginal=false`
- `sourceConfusion=[]`

**判定**

- Manifest 中 personal 来源（`evidence/history/understanding/memory`）与 `knowledge` 的 type 可区分 —— PASS。
- 模型没有把用户历史/理解误称为文档知识，`sourceConfusion=[]` —— PASS。
- **D1：Citation 没有分别指向个人数据与文档。** `citations` 只有 document 4 的 chunks 6–8；个人来源只出现在 Manifest `sourceIds`（`evidence:4`、`history:1/2`、`memory:4`、`understanding:4`），没有结构化 Citation。产品 `Citation` Schema 本身只接受 `documentId/chunkId`（`agent/app/citations.py:82-113`、`214-245`），因此这不是本次检索丢数据，而是 Schema 能力边界。

### 2.4 异常场景

#### 2.4.1 Memory DEGRADED

- 问题：`为什么 TCP 慢启动阶段拥塞窗口会指数增长？请说明依据。`
- run_id：**17**
- 结果：HTTP 200，`agent_run=COMPLETED`，回答 1144 字符，非空

**构造与降级**

- 在同一 ACTIVE Memory 行的 `memory_type` 枚举列写入非法值 `CORRUPT_ENUM`（`SET SESSION sql_mode=''`），并在调用后恢复为 `LEARNING`；恢复影响 1 行。
- 直接写非法 `status` 不可行：`MemoryMapper` 的检索 SQL 始终过滤 `status='ACTIVE'`，非法 status 的行不会被选中，无法进入映射失败路径。这是对任务指定“写非法 status”的方法学偏差，见 D4。
- 调用前 `/api/memories/search/hybrid` 预检：HTTP 200，`status=UNAVAILABLE`，`items=[]`，warning 为 `词法检索失败：MyBatisSystemException`、`回源复核失败，已放弃本次检索结果`；Agent 将该状态映射为 `DEGRADED`。

**Manifest 关键字段**

- intent：`LEARNING_EXPLANATION`
- sourceTypes：`cognitive_status, evidence, history, knowledge, session, understanding, user_input`（没有 memory 内容）
- sourceIds：`understanding-status:SUCCESS, memory-status:DEGRADED, revision-status:NOT_QUERIED, evidence:1, history:1, history:2, chunk:1, chunk:2, chunk:3, session:1, understanding:1, message:current`
- 关键标记：`memory-status:DEGRADED`；不存在 `memory-status:EMPTY`

**Citation / Usage**

- Citation：document 1 chunks 1–3；`traceable=true`
- Usage：`REAL`；prompt 2609 / completion 1291 / total 3900；cached 1408；reasoning 607

**四个观测字段**

- `historyGuardTriggered=false`
- `memoryDegraded=true`，`memoryDegradedSource=["memory-status:DEGRADED"]`
- `quoteSources=["knowledge"]`
- `understandingAsOriginal=false`

**判定**：Manifest 保留 DEGRADED、不伪装 EMPTY；主链路 COMPLETED；回答非空。通过（方法学偏差见 D4）。

#### 2.4.2 Knowledge 空召回

- 问题：`请根据我上传的资料，说明中世纪庄园经济的税收方式。`
- run_id：**9**
- 结果：HTTP 200，`COMPLETED`，`finishReason=stop`，`truncated=false`

**Manifest / Citation**

- intent：`KNOWLEDGE_QUERY`
- knowledgeStatuses：`THRESHOLD_FILTERED`
- sourceIds：`history:1, knowledge-status:THRESHOLD_FILTERED, session:2, message:current`
- 没有注入任何 `knowledge` chunk；`citations=[]`

**Usage / 四字段**

- Usage：`REAL`；prompt 1774 / completion 1398 / total 3172；cached 1408；reasoning 843
- `historyGuardTriggered=false`
- `memoryDegraded=false`，`memoryDegradedSource=[]`
- `quoteSources=[]`
- `understandingAsOriginal=false`

**判定**：空召回语义正确。回答明确区分“候选片段低于阈值、不能当作文档事实”和“资料里没有这部分内容”，没有伪造 Citation。产品实际状态是 `THRESHOLD_FILTERED`（任务文本写作 NO_MATCH / EMPTY 语义），属于同一类空召回，但状态名不同，见 D5。

#### 2.4.3 Embedding 超时

- 问题：`请根据我上传的资料，说明发送端在连接初期为什么会加速得越来越快。`
- run_id：**19**
- 结果：HTTP 200，`COMPLETED`，`finishReason=stop`，`truncated=false`

**隔离配置**

- Agent 以 `AILS_EMBEDDING_TIMEOUT_SECONDS=0.001`、`AILS_EMBEDDING_MAX_RETRIES=0` 启动；Agent 日志出现 `知识检索失败：user=1 space=2 error=DocumentEmbeddingUnavailable`。

**Manifest / Citation / Usage**

- intent：`KNOWLEDGE_QUERY`
- knowledgeStatuses：`DEGRADED`
- sourceIds：`history:1, knowledge-status:DEGRADED, session:2, message:current`
- `citations=[]`
- Usage：`REAL`；prompt 1731 / completion 1557 / total 3288；cached 1408；reasoning 783

**四个观测字段**

- `historyGuardTriggered=false`
- `memoryDegraded=false`，`memoryDegradedSource=[]`
- `quoteSources=[]`
- `understandingAsOriginal=false`

**判定**：DEGRADED 不阻塞主链路；回答明确写明“本轮文档检索不可用（向量检索失败）”，后续内容全部标为通用知识，没有伪造文档 Citation。

#### 2.4.4 截断 / 预算裁剪

- 问题：`为什么文件系统要先提交日志再 checkpoint？请说明依据。`
- 文档：6512 字符，14 chunks
- run_id：**11**
- 结果：HTTP 200，`COMPLETED`，`finishReason=stop`，`truncated=false`

**Manifest / Citation / Usage**

- intent：`KNOWLEDGE_QUERY`
- knowledgeStatuses：`SUCCESS_PARTIAL`
- sourceIds：`history:1, chunk:12, chunk:21, knowledge-status:SUCCESS_PARTIAL, knowledge-retrieval:SUCCESS, session:6, message:current`
- knowledge 注入状态：`injection_status=PARTIAL`；knowledge used 834 / cap 1080；总 used 1314 / 3000
- Citation：document 6（`s4x-journal-checkpoint.md`）chunks 12、21；`traceable=true`；payload owner `user_id=1, space_id=6, document_id=6`
- Usage：`REAL`；prompt 2346 / completion 2435 / total 4781；cached 1408；reasoning 1673

**四个观测字段**

- `historyGuardTriggered=false`
- `memoryDegraded=false`，`memoryDegradedSource=[]`
- `quoteSources=["knowledge"]`
- `understandingAsOriginal=false`

**判定**：出现 `SUCCESS_PARTIAL`；回答引用真实 chunk，且全文没有出现「没有检索到 / 未检索到 / 资料中没有 / 文档中没有」等错误暗示；对资料未展开的部分明确标注为通用知识。通过。

#### 2.4.5 HISTORY_GUARD 触发

S4G 共执行 4 次尝试（attempt3 未执行：attempt2 已触发，run 阶段循环提前结束；后续 guard 阶段追加 attempt4/5）：

| key | run_id | 问题 | historyGuardTriggered | LLM 调用 | 结果 |
|---|---|---:|---|---:|---|
| S4G-attempt1 | 13 | `复盘一下：我当时是怎么一步步想通超时重传会缩小拥塞窗口的？请按记录说。` | false | 1 | 回答明确说记录里看不到“一步步”的过程，未编造 |
| S4G-attempt2 | 15 | `复盘一下：请描述我当时的思考过程，从「超时只是计时器到了」到「超时会缩小拥塞窗口」，我是怎么想通的。` | **true** | 2 | 首答命中 `internal_process`，重生成仍命中，最终走确定性回退 |
| S4G-attempt4 | 21 | 要求模拟第一人称心路独白、可补全记录没写的心理过程 | false | 1 | 模型拒绝编造，改为“推理示范” |
| S4G-attempt5 | 23 | 要求确认“意识到超时说明丢包”等句式 | false | 1 | 模型拒绝替用户确认未记录的心理过程 |

**run 15 产品 trace**

- `kind=internal_process`
- `excerpt=我如果给你补一段「你意识到重传意味着丢包，所以……」这样的叙述，那是我编的，不是发生过的事实`
- `reason=记录没有写出的过程断言：「意识到」`
- `regenerated=true`
- `remainingIssues` 仍为同类 `internal_process`（`「你意识到重传说明网络在丢包」`）
- `fallback=true`

**run 15 Manifest / Citation / Usage / 四字段**

- intent：`LEARNING_REVIEW`；sourceIds：`history:1, history:2, chunk:9, memory:5, session:5, revision:7, evidence:5, revision:8, understanding:5, message:current`
- Citation：`[]`（回退文本未带文档引用）
- Usage：`REAL`；prompt 2710 / completion 1360 / total 4070；cached 2304；reasoning 959
- `historyGuardTriggered=true`
- `memoryDegraded=false`，`memoryDegradedSource=[]`
- `quoteSources=["evidence"]`
- `understandingAsOriginal=false`

**判定**：守卫触发、重生成、trace、`historyGuardTriggered` 均记录 —— 机械验证通过。但这次触发是字面误判，不是真实幻觉：模型正在拒绝编造，句子里的「意识到」是在说明“我不会补这段”，仍被 `_INTERNAL_PROCESS_RE` 命中。其余 3 次尝试（attempt1/4/5）模型都没有产生可拦截的编造断言，因此未触发。因此“真实无依据断言 → 守卫拦截”的正向路径在本批没有复现，见 D3 / KI-3。

---

## 3. 跨 user / space 隔离验证

`isolation.ok=true`，`problems=[]`。

### 3.1 HTTP 越权访问

用户 1 访问用户 2 的 Space / 文档 / 对话：

| 检查 | HTTP | 结果 |
|---|---:|---|
| 用户 2 Space 详情 | 404 | 阻止，返回「学习空间不存在」 |
| 用户 2 Space 文档列表 | 404 | 阻止 |
| 用户 2 对话消息 | 404 | 阻止，返回「对话不存在」 |

### 3.2 Qdrant payload 归属

| 场景 | document_id | 期望 owner | point 数 | 错误 owner point |
|---|---:|---|---:|---:|
| S4A | 1 | user 1 / space 1 | 3 | 0 |
| S4B | 2 | user 1 / space 2 | 1 | 0 |
| S4B2 | 3 | user 2 / space 3 | 1 | 0 |
| S4C | 4 | user 1 / space 4 | 3 | 0 |
| S4G | 5 | user 1 / space 5 | 1 | 0 |
| S4X | 6 | user 1 / space 6 | 14 | 0 |

有 Citation 的记录中，`foreignCitations=[]`（S4A/S4B/S4B2/S4C/S4X/S4G 均为空）；Memory DEGRADED 记录走自定义异常路径，`foreignCitations=null`，但其 3 条 Citation 的 `citationPayloadCheck.ok=true`，payload 归属 user1/space1。用户 1 的回答没有引用 document 3，用户 2 的回答没有引用 document 1/2/4/5/6。隔离 PASS。

---

## 4. 异常场景降级行为与 D4-2A 契约

| 场景 | D4-2A / 任务契约 | 实测 | 判定 |
|---|---|---|---|
| Memory DEGRADED | Manifest 保留 DEGRADED，不伪装 EMPTY；`memoryDegraded=true`；主链路可继续 | `memory-status:DEGRADED` 注入；无 `memory-status:EMPTY`；`memoryDegraded=true`；`agent_run=COMPLETED`；回答非空 | PASS（方法学偏差 D4） |
| Knowledge 空召回 | NO_MATCH / EMPTY 语义正确，不注入 chunk，不产生 Citation | `THRESHOLD_FILTERED`；注入 `knowledge-status:THRESHOLD_FILTERED`；无 chunk；无 Citation；回答区分“低于阈值”与“资料没有” | PASS（状态名差异 D5） |
| Embedding 超时 | DEGRADED，不阻塞主链路；不得伪造文档内容 | `knowledge-status:DEGRADED`；无 Citation；`agent_run=COMPLETED`；回答明确说检索不可用，通用知识单独标注 | PASS |
| 截断 / 预算裁剪 | 出现 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`；不得误称“没有检索到” | `SUCCESS_PARTIAL`；knowledge 834/1080，`injection_status=PARTIAL`；2 条真实 Citation；全文无“没有检索到”类措辞 | PASS |
| HISTORY_GUARD | 守卫触发、重生成、记录 `historyGuardTriggered` | run 15 触发；2 次 LLM 调用；trace 完整；`fallback=true`；`historyGuardTriggered=true` | 机械 PASS；真实幻觉正向路径未复现（D3） |

补充：

- 没有出现 `DEGRADED` 被写成 `EMPTY` 的情况。
- 超时场景没有把通用知识伪装成文档知识；回答明确标注“通用知识”。
- 截断场景没有把“预算只保留部分 chunk”说成“没有检索到”。
- 空召回场景的“本轮没有检索到可确认的资料依据”是正确语义，不属于截断后的错误暗示。

---

## 5. 冻结一致性、分析消费者与清理

### 5.1 冻结一致性

12 条记录在调用前后各做一次快照检查：

- `freezeProblems`：全部 `[]`
- `qdrantProblems`：全部 `[]`
- `defaultMemoryProblems`：全部 `[]`
- 每条记录的 `preChecks.freezeProblems` / `postChecks.freezeProblems` 相同，冻结 hash 未变化
- Qdrant document / memory collection 的 combined hash 未变化
- 默认 Memory collection `ails_memories_text-embedding-v4_1024` 测试前后均不存在（404），没有写入默认 collection

分析消费者观测：

- request 队列 consumer=0；运行期间累积 12 条未消费消息；worker 未启动
- result 队列在 setup 时观察到 1 个 Spring listener、0 条消息；服务停止后 consumer=0
- 没有 analysis 消息被消费并写回冻结表；冻结表 hash 不变

### 5.2 清理

`--phase cleanup` 返回 `ok=true`，独立残留检查 `ok=true`：

- MySQL：`phase5_pilot_20261110003720_72cb36` 已删除，remaining=0
- Qdrant：文档 collection、Memory collection 均已删除（404）；无残留 `phase5-pilot-*` collection
- RabbitMQ：5 个队列、2 个 exchange 均 204；leftovers=[]
- 端口：8001/8081 均已关闭；无残留进程
- 默认 collection 未受影响（404）
- cleanup 的 `springStop` / `agentStop` 显示 PID not found，是因为服务在 cleanup 前已停止；端口检查为 closed，残留检查 PASS

**冻结与清理结论：PASS。**

测试覆盖限制：`full_freeze_specs` 的 cognitive / memory 冻结基线使用 user1；user2 的 cognitive / memory 行没有纳入该基线（user2 只出现在 spaces / conversations / messages 的覆盖中）。由于 analysis worker 未启动、request 队列未消费，风险低，但“user2 cognitive/memory 也完全未变”不是本批已证明的结论，见 D6。

---

## 6. 缺陷清单

### D1（中）：组合场景的个人来源没有结构化 Citation

- **现象**：S4C 的 `citations` 只有 document 4 chunks 6–8；`evidence:4`、`history:1/2`、`memory:4`、`understanding:4` 只存在于 Manifest `sourceIds`，没有 Citation 条目。
- **复现**：run 7；查看 `citations` 与 `manifestSummary.sourceIds`。
- **影响**：任务第三部分要求“Citation 分别指向个人数据与文档”，该验证项**未满足**。前端无法用与文档引用相同的结构化方式渲染个人来源，只能回退到 Manifest / 文本标注。
- **归因**：不是检索丢数据。`agent/app/citations.py` 的 `Citation` 只建模 `documentId/chunkId/chunkIndex`，`validate_citations` 只处理 knowledge chunk；个人来源本来就不在 Citation Schema 内。修复需要 Schema / Runtime 变更，超出 Step 4 边界。

### D2（中）：场景 A 模型忽略已注入的个人上下文

- **现象**：run 1 Manifest 注入了 `history:1/2`、`understanding:1`、`memory:1`、`evidence:1`，但最终回答 `quoteSources=["knowledge"]`，全文只引用 document 1，没有任何个人历史/理解/记忆/证据片段。
- **复现**：run 1；对比 `manifestSummary.sourceIds` 与 `answerFull` / `quoteSourceDetail`。
- **影响**：Manifest 层“个性化上下文已注入”成立，但回答层没有体现个性化。若 Step 5 的个性化学习体验以回答内容为验收对象，这一层会掉分。归因是模型选择/提示词，不是 Builder 丢来源。

### D3（低）：HISTORY_GUARD 只验证到误判触发，真实幻觉正向路径未复现

- **现象**：run 15 的 `internal_process` 触发发生在模型拒绝编造的句子里（“我如果给你补一段「你意识到……」……那是我编的”）；重生成仍被同一字面命中，最终回退。attempt1/4/5 模型均拒绝编造，未触发。
- **复现**：run 15 trace；attempt1/4/5 回答。
- **影响**：守卫的“触发 → 重生成 → 回退 → trace”管道已验证；但“真实无依据断言 → 守卫拦截”的正向能力没有在本批得到证据。KI-3 已登记，修复不在 Step 4 边界。

### D4（低）：Memory DEGRADED 的注入方式偏离任务指定的“非法 status”

- **现象**：任务要求“隔离库写非法 status 触发”。实际直接写非法 `status` 不可行，因为 `MemoryMapper` 的检索 SQL 始终过滤 `status='ACTIVE'`，非法 status 的行不会被选中；本批改为在同一 ACTIVE 行的 `memory_type` 枚举列写非法值，使 MyBatis 映射失败，词法路降级，后端返回 `UNAVAILABLE`，Agent 映射为 `DEGRADED`。
- **复现**：run 17 的 `fixtureMutation`；预检 `status=UNAVAILABLE`、`warnings` 含 `词法检索失败：MyBatisSystemException`。
- **影响**：Agent / Manifest 层的 DEGRADED 契约已验证；后端 `DEGRADED_VECTOR` / `DEGRADED_KEYWORD` 的独立路径没有被单独隔离验证。若要求精确复现“非法 status”，需要产品或测试夹具提供可选中但映射失败的行，当前 Schema / Mapper 下做不到。

### D5（信息）：空召回状态名与任务文本不一致

- **现象**：任务写“NO_MATCH / EMPTY 语义”；产品实际返回 `THRESHOLD_FILTERED`，并注入 `knowledge-status:THRESHOLD_FILTERED`。
- **影响**：语义正确（没有可用的 knowledge chunk，也没有 Citation），但状态枚举名与任务文本不同。若下游依赖 `NO_MATCH` / `EMPTY` 字面值，需要对齐；否则按 `THRESHOLD_FILTERED` 处理即可。

### D6（信息）：user2 的 cognitive / memory 行未纳入冻结基线

- **现象**：`full_freeze_specs` 的 cognitive / memory 表使用 `snapshot["userId"]`（user1）；user2 只在 spaces / conversations / messages 覆盖中。
- **影响**：本批“冻结表未变”的结论覆盖 user1 的 cognitive / memory 与全部 spaces / conversations / messages；user2 的 cognitive / memory 未证明。worker 未启动、队列未消费，实际风险低，但报告不应把覆盖范围说大。

### D7（信息）：Memory DEGRADED 记录缺少 `finishReason` / `truncated` / `latencyMs`

- **现象**：run 17 的 `finishReason=null`、`truncated=null`、`latencyMs=null`；但 HTTP 200、`agent_run=COMPLETED`、answer 非空、completion_tokens=1291 < 4096。
- **影响**：这是 harness 自定义异常记录路径的字段缺口，不是产品截断。四个必需观测字段仍完整。

---

## 7. Step 4 结论与 Step 5 就绪度

### 7.1 结论

**Step 4：PASS（带 1 项需求缺口）。**

按任务显式失败判据逐条检查：

| 失败判据 | 结果 |
|---|---|
| 任一场景 Citation 不可追溯 | 未命中；所有非空 Citation `traceable=true`，payload 归属正确 |
| Manifest 缺关键来源 | 未命中；A/B/C 均含场景预期来源 |
| DEGRADED 伪装成 EMPTY | 未命中；Memory 场景为 `memory-status:DEGRADED` |
| 截断后出现“没有检索到”类暗示 | 未命中；S4X 无该类措辞 |
| 跨 user/space 泄漏 | 未命中；HTTP 404 + Qdrant ownership 全 0 错误 |
| Usage 伪造 | 未命中；12 条均 `REAL` |
| 分析消费者写回冻结表 | 未命中；冻结 hash 不变，request 队列未消费 |
| error / timeout / 空输出 / 意外截断 | 未命中；12 条 `COMPLETED`，异常场景按预期降级 |
| 四个观测字段缺失 | 未命中；12 条全部记录 |

验收项：

- 四个场景全部执行 —— PASS
- 异常场景降级符合契约 —— PASS（D3/D4/D5 为边界说明）
- 四个观测字段全记录 —— PASS
- 跨 user/space 隔离验证通过 —— PASS
- 冻结与清理 PASS —— PASS（D6 为覆盖限制）
- 未修改禁止项 —— PASS
- 未 commit —— PASS

### 7.2 是否满足进 Step 5

链路能力上满足：真实 MySQL + Qdrant + Embedding + LLM、策略/Manifest/Citation/Usage/冻结/清理都已跑通，异常降级没有破坏主链路。**可以进入 Step 5 的准备工作。**

但进入 Step 5 前应先决策 D1：如果“组合场景 Citation 必须分别指向个人数据与文档”被视作硬门槛，则本项应判为未满足，Step 4 结论应改为条件性 FAIL；如果接受“文档引用用 Citation、个人来源用 Manifest sourceIds 追溯”的现有边界，则按 PASS 记录。D2 也需要产品侧确认：个性化回答是否要求实际引用个人上下文，而不只是 Manifest 注入。

---

## 8. 工作区状态

```
 M docs/testing/phase5-known-issues.md   # 新增 KI-3 与 Step 4 run 15 证据
?? scripts/phase5/full/run_step4.py      # 新增 Step 4 执行脚本
?? docs/testing/phase5-step4-results.md  # 本报告
```

- 未修改 Runtime / ContextBuilder / Policy / Schema / SSE / Spring / HISTORY_GUARD 实现 / Tavily / 其他场景数据。
- 未 commit、未 push。
- 隔离环境已清理：DB、2 个 Qdrant collection、5 个队列、2 个 exchange 均已删除；8001/8081 已关闭。
- 测试原始产物保留在临时目录：`C:\Users\yangsy\AppData\Local\Temp\phase5-full-qpyc7nxh\`（`state.json`、`step4-results.json`、`step4-snapshot.json`、日志）。该目录不属于仓库，不随 commit 提交。

---

## 9. 待审核决策点

1. **个人来源 Citation（D1）**：是否接受“文档用 Citation、个人来源用 Manifest 追溯”的现有 Schema 边界？若要求结构化个人 Citation，需要改 Schema / Runtime / 前端，超出 Step 4 边界。
2. **场景 A 的回答个性化（D2）**：Manifest 注入个人来源已通过，但模型回答只用了 knowledge。Step 5 是否要求“回答必须实际使用个人上下文”？若是，需要提示词/策略层工作。
3. **HISTORY_GUARD 正向验证（D3）**：当前只验证到 KI-3 误判触发；是否需要在 Step 5 用更强的人工构造/夹具来验证“真实幻觉 → 守卫拦截”，还是接受机械管道验证？
4. **Memory DEGRADED 注入方式（D4）**：是否接受用 `memory_type` 非法枚举触发 Agent 层 DEGRADED？若必须写非法 `status`，需要产品/夹具支持。
5. **空召回状态名（D5）**：下游是否严格要求 `NO_MATCH` / `EMPTY`？当前产品返回 `THRESHOLD_FILTERED`。
6. **冻结覆盖（D6）**：Step 5 是否要把 user2 的 cognitive / memory 行纳入冻结基线，补上本批未覆盖的范围。
