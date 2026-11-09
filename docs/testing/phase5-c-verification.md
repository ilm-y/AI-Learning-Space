# Phase 5 pre-Step3 批次 3：C 策略真实链路验证记录

- 执行日期：2026-11-09
- 分支：`phase5/pre-step3-batch3`（未 commit、未 push）
- 范围：只跑 C，不跑 A/B，不进入 90 次全量，不实现 Tavily
- 结论：**首次执行 FAIL（run 1 命中判据设计问题，runs 2/3 未执行）；
  修正两个阻塞点后重跑 run 1 = PASS（见 §11）**

> 本文只记录 hash、id、状态、计数与必要的回答摘录；不包含密钥、token、
> 连接串密码或完整 prompt。

---

## 1. 执行结论

| 项目 | 结果 |
|---|---|
| 4 项决策补丁 | 已落实，离线测试与真实 restore 均通过 |
| 隔离环境启动 | PASS（固定 `AILS_CONTEXT_STRATEGY=C`） |
| 统一快照 + Knowledge 索引 | PASS（2 个 chunk，READY） |
| MySQL/Qdrant 基线 | PASS |
| C 策略真实运行 | 仅执行 run 1；run 1 的 Manifest / Usage / Citation / Context Hash / 冻结全部 PASS |
| 失败判据 | **命中：回答未包含预置历史唯一标记** |
| runs 2/3 | **未执行**（失败判据要求立即停止） |
| 资源清理 | 完成：DB / 两个 collection / 队列 / 进程均无残留 |
| commit / push | 未执行 |

因此，不能把本批记为“3 次 C 验证通过”。本报告保留 run 1 的完整证据，
并给出两个需要决策的阻塞点（见 §10）。

---

## 2. 四项决策的落实与验证

### 决策 1：单会话 + 每组前恢复，恢复后立即冻结

`scripts/phase5/pre-step3/restore_snapshot.py --apply` 现在在同一个事务内执行：

1. 读取基线并计算当前状态；不一致则不开始删除；
2. 严格删除运行期追加消息；
3. 执行恢复 upsert；
4. 再次计算并比对基线；不一致则回滚；
5. commit 后再独立计算一次并与基线比对；不一致则非零退出；
6. 校验删除范围内无残留消息。

真实执行结果（run 1 前后各一次）：

```text
beforeCombined   = 395ae6449695e08742ed07f6acef0117a8bb973f0e03219dea22ded75a1a2f5f
afterCombined    = 395ae6449695e08742ed07f6acef0117a8bb973f0e03219dea22ded75a1a2f5f
postCommitCombined = 395ae6449695e08742ed07f6acef0117a8bb973f0e03219dea22ded75a1a2f5f
prunedMessages   = 2
residualScopedMessages = 0
freezeCheck      = PASS
```

### 决策 2：`--prune-run-messages` 的三条硬前提

删除 SQL 固定为：

```sql
DELETE FROM message
WHERE conversation_id=%s AND created_at > %s AND id NOT IN (%s,%s)
```

真实执行时，run 1 写出的 2 条消息（user + assistant）恰好全部命中范围并被删除，
快照历史消息 id 1/2 保留；删除前后与 commit 后的冻结哈希均一致。

### 决策 3：collection 前缀严格相等

`agent/app/config.py` 已把 `startswith` 改为逐字相等：

- `AILS_QDRANT_COLLECTION_PREFIX == AILS_TEST_ENV_ID`；
- `AILS_MEMORY_COLLECTION_PREFIX == AILS_TEST_ENV_ID + "-memory"`。

`agent/tests/test_context_strategy.py` 新增了边界拒绝用例
（`<env>-other`、`<env>0`、`<env>_suffix`、memory 的 `<env>`、
`<env>-memory-extra` 等），并保留了正确前缀对的接受用例。

### 决策 4：Qdrant 只读冻结

新增 `scripts/phase5/pre-step3/qdrant_freeze_check.py`：

- `--write-baseline`：记录 collection、point 总数、每个 point id + payload
  规范化 SHA-256、整体哈希；
- `--check`：只读重算并比对，计数 / id 集合 / payload 任一变化即失败；
- 只发送 GET 与 scroll POST，不调用任何写接口；
- 自检会扫描自身源码，确认不存在写接口调用。

真实结果：

```text
collection = phase5-pilot-20261109203052-afbed9_text-embedding-v4_1024
pointCount = 2
combined   = ae48e3edb20d6748bca20d29ff7f968420f773a181e6a95b30ec049ffcdf6e40
run 前后与 restore 后均为 PASS
```

### 离线测试

```text
python -m pytest agent/tests/test_context_strategy.py \
  agent/tests/test_phase5_prestep3_scripts.py -q
=> 32 passed
```

三个脚本的 `--self-test` 均为 `problems: []`。

---

## 3. 前置条件与隔离环境

| 检查项 | 结果 |
|---|---|
| Agent 8001 / Spring 8081 | 启动成功，运行后已停止 |
| `contextStrategy` | `C`（`/phase5/prestep3-context-hashes` 与 Agent 启动参数一致） |
| LLM | `openai` / `deepseek-flash`，`maxTokens=4096` |
| Retriever | `http` |
| 文档 collection 前缀 | `phase5-pilot-20261109203052-afbed9` |
| Memory collection 前缀 | `phase5-pilot-20261109203052-afbed9-memory` |
| 默认 Memory collection | `ails_memories_text-embedding-v4_1024`，运行前后均为 **404（不存在）** |
| 分析 worker 消费端 | `.request` / `.retry` 消费者均为 0；Spring 自身监听 `.result`（正常链路） |
| 分析请求队列 | 运行后 1 条 request 消息，无消费者，未发生写回 |
| 隔离 DB | `phase5_pilot_20261109203052_afbed9`，运行后已删除 |
| Embedding 计数 | 文档索引 1 次调用 / 2 个文本；run 1 为 2 次调用 / 2 个文本 |

> 注意：C 策略在 `KNOWLEDGE_QUERY` 下同时触发 Knowledge 与 Memory 检索，
> 因此每次运行有 **2 次 Embedding 调用**（Knowledge query + Memory query），
> 不是 1 次。详见 §10 的成本结论。

---

## 4. 执行流程

1. `start_prestep3_services.py` 启动隔离 Agent + Spring（固定 C）；
2. `setup_snapshot.py --apply --index-knowledge` 建立统一快照并索引 2 个 chunk；
3. `freeze_check.py --write-baseline --assert-generation` 写 MySQL 基线；
4. `qdrant_freeze_check.py --write-baseline` 写 Qdrant 基线；
5. run 1：
   - `restore_snapshot.py --apply --prune-run-messages`；
   - MySQL / Qdrant 冻结检查；
   - 同一 conversation（id=1）发送 `NEUTRAL_QUESTION`；
   - 从 SSE、Spring `agent_run`、Agent 只读诊断与新增的 Context Hash 路由采集证据；
   - 运行后 restore + 两组冻结检查；
6. 命中“回答未包含唯一标记”后停止，未执行 runs 2/3；
7. `cleanup.py` 清理隔离资源并复核残留。

执行中修复了一个脚本缺陷：`setup_snapshot.py` 的 `api()` 不接受 `token`
关键字，首次调用在发请求前就抛出 `TypeError`。已删除两处无效的
`token=""` 参数（仅影响本目录脚本，不涉及生产代码），随后快照正常建立。

---

## 5. C 策略运行结果

### run 1（唯一实际执行的一次）

| 字段 | 值 |
|---|---|
| run_id | `1` |
| clientRequestId | `1ffd56f9-c9e1-4a11-b75e-5ccdf6d31bc0` |
| provider request UUID | `db691d25592141c28df93713f04b0b5e` |
| providerRequestId | `null`（provider 未返回） |
| finish_reason | `stop` |
| truncated | `false` |
| usageStatus | `REAL` |
| prompt / completion / total tokens | `2549 / 1747 / 4296` |
| cached / reasoning tokens | `1536 / 1056` |
| 客户端 SSE 耗时 | `13063 ms` |
| 诊断记录耗时 | `12474 ms` |
| contextStrategy | `C` |
| agent_run.status | `COMPLETED` |
| assistant_message_id | `4`（运行后已由 restore 删除） |
| manifest.intent | `KNOWLEDGE_QUERY` |
| 回答字符数 | `1280` |
| 回答含唯一标记 | **否** |
| Embedding（本次） | 2 次调用 / 2 个文本 |

回答前 200 字（仅模型输出，不含 prompt）：

```text
## 先纠正一个导致你不确定的点

你把第三次握手理解成「确认服务端收到 ACK」。按这种理解，第三次握手确实显得多余：如果 ACK 只是告诉对方「我收到了你的 ACK」，那就变成每次确认都要再确认一次，无限递归，永远结束不了。

但你那句描述里，被确认的对象错位了。第三次握手中，客户端发出的 ACK 所确认的，是**服务端之前发出的 SYN-ACK 已经抵达客户端**。这一点对服务端来说是你尚未
```

回答明确引用了预置的 Understanding/历史卡点（“你把第三次握手理解成……”“导致你不确定的点”），
但没有逐字输出内部标记字符串。

---

## 6. Manifest 与 token 拆分

run 1 的 `context_manifest` 已由 Spring 落库到 `agent_run.context_manifest`。
Manifest 顶层字段：`schema_version`、`policy_version`、`intent`、`sources`、`budget`。

`sources`（全部 `SELECTED`、`within_budget`）：

| type | source_id | estimated_tokens |
|---|---|---|
| evidence | `evidence:1` | 53 |
| history | `history:1` | 43 |
| history | `history:2` | 32 |
| knowledge | `chunk:1` | 95 |
| knowledge | `chunk:2` | 97 |
| memory | `memory:1` | 108 |
| session | `session:1` | 50 |
| understanding | `understanding:1` | 333 |
| user_input | `message:current` | 40 |

token 守恒核对（Manifest `budget.per_type.used` 对选中 entry 求和）：

| type | per_type.used | entry 求和 | 结果 |
|---|---:|---:|---|
| evidence | 53 | 53 | PASS |
| history | 75 | 43 + 32 | PASS |
| knowledge | 192 | 95 + 97 | PASS |
| memory | 108 | 108 | PASS |
| session | 50 | 50 | PASS |
| understanding | 333 | 333 | PASS |
| user_input | 40 | 40 | PASS |

说明：

- 本次 intent 为 `KNOWLEDGE_QUERY`，Policy 对 `revision` 的决策是 **SKIP**，
  因此 Manifest 中没有 `trajectory` / `revision:*` entry；`trajectory_source`
  的多 source 均分拆分检查在本轮不适用。
- 这正是预置历史“能看到、但没有被当作复盘轨迹注入”的表现：history 与
  understanding 进入上下文，revision 轨迹没有进入。
- Evidence 是 `REQUIRED`，`evidence:1` entry 存在并指向快照的 `evidenceId=1`。
- 本报告不把“缺少 revision entry”记为失败，因为该 intent 下按策略就不应出现；
  若后续要求 revision entry，必须改用 `LEARNING_REVIEW` 问题，而不是改 Manifest。

---

## 7. Citation 可追溯性

`message.end.citations` 返回 2 条，均可在隔离 DB 中追溯到
`document_chunk` 与 `document`：

| documentId | chunkId | chunkIndex | sourceTitle | available |
|---:|---:|---:|---|---|
| 1 | 1 | 0 | `phase5-prestep3-tcp.md` | true |
| 1 | 2 | 1 | `phase5-prestep3-tcp.md` | true |

校验结果：chunk 存在、属于当前 `spaceId=1`、`document_id` 一致；
Citation 检查 `problems: []`。没有编造来源。

---

## 8. Context Hash、Usage 与 run_id 关联

### Context Hash

新增的只读路由 `GET /phase5/prestep3-context-hashes` 在
`OpenAICompatibleAdapter.stream_chat_with_completion` 入口对**实际送入模型**
的 messages 计算规范化 SHA-256：

```json
{
  "seq": 1,
  "recordedAt": "2026-11-09T20:32:33",
  "contextSha256": "2d4ed4ec9338a2bd4019ff33435e78653a9bd5454eb11e46c386356349840c7e",
  "messageCount": 4,
  "roles": ["system", "user", "assistant", "user"],
  "chars": 4436,
  "systemSha256": "9965f83251374740d783362308dd50e3af50bd9c06c4f1a3c296bd32da9d54c2",
  "userSha256": "8cc2344824d1162bf5a6b8e6c311b5d085bf57c4cd8bda5975617ce84218c210",
  "fixtureMarkerOccurrences": 6
}
```

`fixtureMarkerOccurrences=6` 证明预置标记确实进入了模型上下文（history 2 条、
memory、evidence、2 个文档 chunk）。这与回答未逐字输出标记并不矛盾：标记在
上下文里，但 system instruction 明确要求不要复述内部记录结构。

### Usage

`usageStatus=REAL`，provider 返回了完整的 prompt/completion/total 与缓存、
推理 token；没有本地估算或伪造。`finish_reason=stop`，`truncated=false`。

### run_id 关联

SSE `message.start` / `message.end` 的 `runId=1` 与 Spring `agent_run.id=1`
一致；该行的 `status=COMPLETED`、`trace_id=1bf9b97003e4ee2fa701d760a91dc372`。
`agent_run.assistant_message_id=4` 与本次 assistant 消息一致（该消息随后被
restore 删除）。

限制：Agent 的 `/phase5/llm-diagnostics` 只保留最近一次调用，诊断本身不带
Spring run_id，`runIdAssociable` 固定为 `"false"`。本批是串行单请求，run 1 的
usage 通过时间顺序与 SSE runId 人工对应；这不是程序化关联。若后续要求
“usage 与 run_id 程序化绑定”，需要在 Agent 诊断里记录 run_id，属于新改动。

---

## 9. 冻结、写回与清理

### 运行前 / 运行后

| 检查 | 运行前 | run 后 + restore 后 |
|---|---|---|
| MySQL 15 表 combined | `395ae644...a1a2f5f` | 同值 PASS |
| Qdrant pointCount | 2 | 2 PASS |
| Qdrant combined | `ae48e3ed...cdf6e40` | 同值 PASS |
| 默认 Memory collection | 404（不存在） | 404（不存在） |
| 隔离 Memory collection | 404（未创建） | 404（未创建） |

### 分析写回

- `.request` / `.retry` 队列消费者为 0，未启动 `app.worker`；
- 运行后 `.request` 队列有 1 条待处理消息，无消费者，未产生结果；
- 冻结表在运行前后一致，说明 Understanding / Evidence / Memory 没有被写回；
- Spring 自身监听 `.result` 是正常链路，但没有 worker 就不会有 result 消息。

### 清理

`cleanup.py` 结果：

- Agent / Spring 进程已停止，8001/8081 已关闭；
- 隔离 DB 已 DROP，复查 `SHOW DATABASES LIKE ...` 为空；
- 文档 collection 与 Memory collection 均已删除，复查 404；
- RabbitMQ 隔离队列与 exchange 已删除，残留列表为空；
- `phase5-pilot-*` 残留 collection 列表为空。

清理脚本的 `ok=false` 是检查顺序造成的：Memory collection 清理时，
文档 collection 尚未删除，所以那一项的 `remainingPhase5Pilot` 暂时列出了文档
collection；随后文档 collection 也被删除，最终残留为空。资源状态以最终复查为准。

---

## 10. 失败判据逐条判定与阻塞点

| 判据 | 判定 | 证据 |
|---|---|---|
| Manifest 缺少 revision / evidence entry | **部分 N/A** | intent=`KNOWLEDGE_QUERY`，revision 按策略 SKIP；`evidence:1` 存在 |
| token 拆分之和与 section 估算不符 | PASS | §6 的 7 类 per_type 守恒 |
| 回答未包含预置历史唯一标记 | **FAIL** | `answerContainsFixtureMarker=false` |
| Citation 不可追溯 | PASS | 2 条 citation 均可回查 |
| Usage 伪造/估算 | PASS | `REAL`，provider 原始 usage |
| run_id 无法关联 | PASS（有串行关联限制） | SSE runId=1 = agent_run.id=1 |
| freeze_check / qdrant_freeze_check 不一致 | PASS | 前后哈希一致 |
| 分析消费者写回 | PASS | worker 未启动，冻结表未变 |
| 任何 error / 超时 / 空输出 / 截断 | PASS | `stop`、非空、未截断、无 error |
| 默认 Memory collection 被读写 | PASS | 前后均 404 |

### 阻塞点 A：回答唯一标记判据与当前问题/系统指令冲突

- `NEUTRAL_QUESTION` 是知识问答，intent=`KNOWLEDGE_QUERY`，不是 `LEARNING_REVIEW`；
- system instruction 明确要求不要复述内部记录结构；
- 标记 `PHASE5-PRESTEP3-SNAPSHOT` 是内部 fixture 标签，不是用户可见内容；
- 实际上历史已被使用：Manifest 有 `history:1/2`、`understanding:1`、
  `memory:1`，Context Hash 中标记出现 6 次，回答也直接引用了用户的卡点。

因此“回答必须逐字包含标记”在本问题下不是一个有效的“历史被使用”代理指标。
建议三选一：

1. 改用显式要求引用用户原话的问题（会改变 A/B/C 的问题口径）；
2. 用“Context Hash marker 出现次数 + Manifest history/understanding entry +
   回答语义引用”替代逐字标记；
3. 把标记改成用户可见、且系统指令允许模型复述的短语。

### 阻塞点 B：C 策略的 Embedding 预算与“≤5 次”冲突

本次实际停止在 run 1，Embedding 总计：

- 文档索引：1 次调用 / 2 个文本；
- run 1：2 次调用 / 2 个文本（Knowledge query + Memory query）；
- **合计 3 次调用 / 4 个文本**，仍在 ≤5 内。

如果继续 runs 2/3，按每次 2 次调用计算：

- 3 次运行合计：1 + 3×2 = **7 次调用**（文本约 8 个），超过“1 次文档 +
  问题向量 ≤5”的预算。

这是 C 策略的结构性行为：`KNOWLEDGE_QUERY` 下 memory 是 OPTIONAL 但
`triggered=true`，Runtime 会并行发起 Knowledge 与 Memory 两路检索，两路各自
Embedding 一次。建议在跑 3 次全量前明确选择：

1. 把 C 的 Embedding 预算调到 ≥7 次调用；
2. 为本批显式关闭 Memory 检索（需要改 Policy/配置，超出本批允许范围）；
3. 接受“每次运行 2 次”并把预算口径改为“按调用次数而非问题数”。

### 后续建议

1. 先解决阻塞点 A 的判据定义，再重跑 C；不要为了让标记出现而改系统 prompt。
2. 同步确认阻塞点 B 的预算口径，否则即使标记判据通过，run 3 也会超预算。
3. 重跑时保留本报告的 §5–§9 采集字段；`run_c_verification.py` 已可复用。
4. 本批未 commit / push；工作区改动见 `git status`。

---

## 附录：证据文件与改动清单

- 运行结果 JSON：
  `C:\Users\yangsy\AppData\Local\Temp\phase5-pilot-z83n4m_d\c-verification-results.json`
  （temp 目录，cleanup 不删除；如系统清理临时目录则失效）
- 本批修改/新增：
  - `agent/app/config.py`（前缀严格相等）
  - `agent/tests/test_context_strategy.py`（边界拒绝用例）
  - `agent/tests/test_phase5_prestep3_scripts.py`（restore / qdrant 纯函数回归）
  - `scripts/phase5/pre-step3/restore_snapshot.py`（决策 1/2）
  - `scripts/phase5/pre-step3/qdrant_freeze_check.py`（决策 4）
  - `scripts/phase5/pre-step3/run_agent_prestep3.py`（只读 Context Hash / Embedding 计数）
  - `scripts/phase5/pre-step3/start_prestep3_services.py`（固定 C 启动）
  - `scripts/phase5/pre-step3/run_c_verification.py`（C 执行器）
  - `scripts/phase5/pre-step3/setup_snapshot.py`（修复无效 `token` 参数）
  - `scripts/phase5/pre-step3/README.md`
  - `docs/testing/phase5-c-verification.md`（本文件）
- 未修改 Runtime / ContextBuilder / Policy / Schema / SSE / Spring 生产代码。
- 未执行 A/B、90 次全量、Tavily；未 commit、未 push。

---

## 11. 批次 3 修正：两个阻塞点决策与 run 1 重跑（2026-11-09）

### 11.1 阻塞点 1 决策：方案 A（改用复盘问题）

**决策：方案 A。** `NEUTRAL_QUESTION` 改为 `REVIEW_QUESTION`，使 Policy 判定
`intent=LEARNING_REVIEW`：

```text
请回顾我之前对 TCP 三次握手的理解：逐字引用我那条消息的原文
（包括开头的方括号标记），再指出我卡在哪里、为什么卡住。
```

理由：

1. 原判据「回答必须逐字包含 `PHASE5-PRESTEP3-SNAPSHOT`」在 `KNOWLEDGE_QUERY`
   下与系统指令直接冲突：`BASE_SYSTEM_INSTRUCTION` 要求「不要复述它的结构」，
   标记又是内部 fixture 标签，模型不复述是正确行为。
2. `LEARNING_REVIEW` 是「验证历史被使用」最直接的场景：Revision 为 REQUIRED，
   trajectory 区块进入上下文；系统指令在复盘场景下明确允许「引用用户当时的原话」。
3. 问题显式要求「逐字引用……包括开头的方括号标记」，把标记判据从事后猜测变成
   问题本身的明确要求；问题没有直接写出标记值，不构成泄题。
4. 方案 B（保留知识问答 + 语义判定）需要引入关键词/语义阈值，判定口径更软，
   且无法证明读取的是**本快照**而不是通用 TCP 知识。

实际改动：

- `snapshot_common.py`：`NEUTRAL_QUESTION` → `REVIEW_QUESTION`，并更新注释；
- `setup_snapshot.py` / `run_c_verification.py`：引用与快照清单同步；
- `run_c_verification.py`：新增 `intent != LEARNING_REVIEW` 失败判据；
- 测试新增：问题必须被 Policy 判为 `LEARNING_REVIEW`、revision/evidence/memory
  均为 REQUIRED，且问题要求方括号标记但不泄露标记值。

### 11.2 阻塞点 2 决策：方案 A（预算按单次运行口径）

**决策：方案 A。** 预算拆成两个独立口径：

| 口径 | 上限 | 依据 |
|---|---:|---|
| 单次运行 | ≤3 次 Embedding | C 策略在 `LEARNING_REVIEW` 下 Knowledge + Memory 各 1 次，实际 2 次/run |
| 整个批次 | ≤10 次 Embedding | 文档索引 1 次 + 3 次运行 ×2 = 7 次 |

理由：预算本来就应按「单次运行」定义；整批上限单独设置，避免把「一次运行的成本」
和「一批实验的总成本」混为一个数字。方案 B（关闭 C 的 Memory 检索）会改变 C 的
语义（C 的定义包含 Memory），不可取；只验证 run 1 则放弃 runs 2/3 的覆盖。

实际改动：`run_c_verification.py` 新增 `EMBEDDING_MAX_PER_RUN=3` /
`EMBEDDING_MAX_BATCH=10`，每次运行后校验单次预算，批次结束记录
`embeddingBudget` 并校验总预算；README 同步口径。

### 11.3 修正后的失败判据

| 判据 | 修正后口径 |
|---|---|
| Manifest 缺 revision / evidence entry | `LEARNING_REVIEW` 下 revision 与 evidence 都必须存在，且指向快照 id |
| token 拆分之和 != section 估算 | 按 Manifest **精确 type** 对齐 `budget.per_type[type].used`；trajectory 与 trajectory_source 分开校验，同一 section 多 source 仍检查 divmod 均分 |
| 回答未使用预置历史 | 回答逐字包含 `PHASE5-PRESTEP3-SNAPSHOT`（问题显式要求引用含标记的原话） |
| Citation 无法追溯 | 每条 citation 回查 `document_chunk` + `document`，必须属于当前 space |
| Usage 伪造或估算 | 只接受 provider 原始 usage 的 `REAL`（或明确 `UNKNOWN`） |
| run_id 无法关联 | SSE runId 必须对应 `agent_run.id` 且状态为 COMPLETED/TRUNCATED |
| freeze / qdrant 前后不一致 | 运行前后与 restore 后均必须 PASS |
| 分析消费者写回 | `.request` / `.retry` worker 消费者必须为 0，冻结表不变 |
| 任何 error / timeout / 空输出 / 截断 | `message.error` 为空、回答非空、`finishReason != length` |
| 默认 Memory collection 被读写 | 运行前后只读比对必须一致（本批均为 404） |

### 11.4 前置条件逐项确认

| 前置 | 结果 |
|---|---|
| 4 项决策补丁已提交 | PASS（`ea13db0`；文档 `a7b0811`） |
| 分析消费者关闭 | PASS：`.request` / `.retry` 消费者均为 0 |
| 内部凭据 401 已解决 | PASS：Agent 启动日志 `internal_auth_configured=True`、`internal_identity_configured=True`；SSE 全链路无 401/403 |
| Memory 隔离补丁生效 | PASS：`memoryIsolationChecked=true`，默认 Memory collection 运行前后均 404 |
| 修正后的判据已写入脚本 | PASS：`run_c_verification.py --self-test` = `problems: []` |
| LLM max_tokens ≥4096 | PASS：诊断 `maxTokens=4096` |
| 离线回归 | PASS：`pytest agent/tests/test_phase5_prestep3_scripts.py agent/tests/test_context_strategy.py -q` → **37 passed** |

### 11.5 run 1 重跑记录

| 字段 | 值 |
|---|---|
| run_id | `1` |
| clientRequestId | `3aa758a4-c4af-4913-85aa-972e87f1d00c` |
| provider request UUID | `026f65822f6c49e4a9d53a688a332bf2` |
| providerRequestId | `null`（provider 未返回） |
| finish_reason | `stop` |
| truncated | `false` |
| usageStatus | `REAL` |
| prompt / completion / total tokens | `2763 / 1899 / 4662` |
| cached / reasoning tokens | `1536 / 1366` |
| 客户端 SSE 耗时 | `15578 ms` |
| 诊断记录耗时 | `14592 ms` |
| contextStrategy | `C` |
| agent_run.status | `COMPLETED` |
| manifest.intent | `LEARNING_REVIEW` |
| manifest.policy_version | `d4-2-v1` |
| 回答字符数 | `981` |
| 回答含唯一标记 | **是** |
| Embedding（本次） | 2 次调用 / 2 个文本（Knowledge + Memory） |
| Embedding（批次） | 文档 1 + 查询 2 = **3 次**（≤10） |

回答开头（仅模型输出，不含 prompt）：

```text
## 你的原话

> 【PHASE5-PRESTEP3-SNAPSHOT】我先说说我的理解：TCP 三次握手是客户端发 SYN，
> 服务端回 SYN-ACK，客户端再回 ACK。

## 这句话说对了什么，又停在哪
...
## 卡点的位置
...
```

### 11.6 Manifest 与 token 拆分

`sources`（10 条，全部 `SELECTED` / `within_budget`）：

| type | source_id | estimated_tokens |
|---|---|---:|
| history | `history:1` | 43 |
| history | `history:2` | 32 |
| knowledge | `chunk:1` | 95 |
| knowledge | `chunk:2` | 97 |
| memory | `memory:1` | 108 |
| session | `session:1` | 50 |
| trajectory | `revision:1` | 89 |
| trajectory_source | `evidence:1` | 88 |
| understanding | `understanding:1` | 333 |
| user_input | `message:current` | 57 |

token 守恒（精确 type）：

| type | per_type.used | entry 求和 | 结果 |
|---|---:|---:|---|
| history | 75 | 43 + 32 | PASS |
| knowledge | 192 | 95 + 97 | PASS |
| memory | 108 | 108 | PASS |
| session | 50 | 50 | PASS |
| trajectory | 89 | 89 | PASS |
| trajectory_source | 88 | 88 | PASS |
| understanding | 333 | 333 | PASS |
| user_input | 57 | 57 | PASS |

`manifestCheck.problems = []`；`revisionEntry=true`、`evidenceEntry=true`；
trajectory 两个 source 的 token 相差 1（89 / 88），符合 divmod 均分。

> 本次修正同时修复了一个执行器缺陷：原 `validate_manifest` 按去掉 `_source`
> 的基类型分组，会把 trajectory 的 revision + evidence 两个 entry 相加后只与
> `per_type["trajectory"]` 比较，在真实复盘 Manifest 上必然假失败。现改为按
> 精确 type 对齐，并保留同一 section 的均分与状态一致性检查。

### 11.7 Citation、Context Hash 与 Usage

- Citation：2 条（`documentId=1`、`chunkId=1/2`、`sourceTitle=phase5-prestep3-tcp.md`），
  均可回查且属于当前 space；`citationCheck.problems = []`、`traceable=true`。
- Context Hash：

```json
{
  "seq": 1,
  "recordedAt": "2026-11-09T20:45:34",
  "contextSha256": "0ba8a1c123f0c4bd507714f1294aed4ca4e6369036d8a2ff89723fbfc6329cee",
  "messageCount": 4,
  "roles": ["system", "user", "assistant", "user"],
  "chars": 4775,
  "systemSha256": "3ea4a2f3fca45a50f43936f878204bd1d33b5e6959190442a2fce20b5a871edb",
  "userSha256": "072686dba83997877689c831593e0be4b46b509ffa9d980a934e7d7ee45c0786",
  "fixtureMarkerOccurrences": 6
}
```

- Usage：`REAL`，provider 返回完整 usage；`finish_reason=stop`、未截断、无 error。
- run_id：SSE `runId=1` 与 `agent_run.id=1` 一致，状态 `COMPLETED`。
  与上一轮相同的串行关联限制仍存在（诊断不带 run_id），本批是单请求串行。

### 11.8 冻结一致性与清理

| 检查 | 基线 / 运行前 | run 后 + restore 后 |
|---|---|---|
| MySQL 15 表 combined | `ee6e5b3f...33f8abd3` | 同值 PASS |
| Qdrant pointCount | 2 | 2 PASS |
| Qdrant combined | `ae48e3ed...cdf6e40` | 同值 PASS |
| 默认 Memory collection | 404 | 404 |
| restore 原子性 | 删除前 / 删除后 / commit 后一致 | PASS（run1 删除 2 条，残留 0） |

清理（`cleanup.py` 实际执行）：

- Agent / Spring 进程停止，8001/8081 已关闭；
- 隔离 DB 已 DROP，独立复查残留为空；
- 文档 collection 与 Memory collection 已删除，独立复查残留为空；
- RabbitMQ 隔离队列 / exchange 已删除，复查 leftovers 为空；
- 默认 Memory collection 复查 404。

`cleanup.py` 的 `ok=false` 仍是检查顺序导致：Memory collection 清理时文档
collection 尚未删除；随后文档 collection 也被删除，最终 `residualPhase5Collections=[]`。
以独立复查为准。本次还修复了执行器二次写盘的一个缺陷：`run_script()` 把
`Path` 放进结果字典，cleanup 后写 JSON 失败；现统一转为 `str`。

### 11.9 修正后失败判据逐条判定：**PASS**

| 判据 | 判定 | 证据 |
|---|---|---|
| Manifest 缺 revision / evidence entry | PASS | `revision:1` + `evidence:1` 均在，`manifestCheck.problems=[]` |
| token 拆分不守恒 | PASS | §11.6 按精确 type 全部守恒 |
| 回答未使用预置历史 | PASS | `answerContainsFixtureMarker=true`，回答逐字引用含标记原话 |
| Citation 无法追溯 | PASS | 2 条 citation 均可回查 |
| Usage 伪造/估算 | PASS | `REAL`，provider 原始 usage |
| run_id 无法关联 | PASS（串行关联限制） | SSE runId=1 = agent_run.id=1 |
| freeze / qdrant 前后不一致 | PASS | 前后与 restore 后哈希一致 |
| 分析消费者写回 | PASS | worker 消费者 0，冻结表未变 |
| error / timeout / 空输出 / 截断 | PASS | `stop`、非空、未截断、无 error |
| 默认 Memory collection 被读写 | PASS | 前后均 404 |
| Embedding 预算 | PASS | 单次 2≤3；批次 3≤10 |

### 11.10 结论

- **run 1 重跑 PASS**，两个阻塞点均按方案 A 解决；
- 本轮只执行 run 1，没有进入 runs 2/3，没有跑 A/B 或 90 次全量；
- 冻结一致性与隔离资源清理均通过；
- 未 commit、未 push。

进入 runs 2/3 的条件：按修正后判据，run 1 已满足；后续只需在**同一判断口径**下
补跑 run 2/3（每次 2 次 Embedding，批次总上限 10 次仍有余量），不再需要改动
Runtime / Policy / Schema。

### 11.11 需要人工审核的决策点

1. 复盘问题显式要求「包括开头的方括号标记」是否可接受：这是为了让逐字标记成为
   可判定证据；若认为问题过度引导，可退回到「只要求引用原话」，但需接受标记
   判据变为概率性。
2. 批次 Embedding 上限取 10（而非任务示例的 7）：本批实际 3 次，3 次全量预计 7 次；
   10 给未来重试留了 3 次余量。若要求严格等于 7，需要把上限改为 7。
3. run 1 的 Usage 与 run_id 仍是串行人工对应（Agent 诊断不带 run_id）；若要求程序化
   绑定，需要新增 Agent 诊断字段，属于新改动，未在本批实施。
