# 面试讲解提纲

> 这份提纲只讲**代码里真实存在的东西**。真实模型已完成 2026-10-30 单轮 5 场景
> Agent Runtime 复验（C-only 依赖 fallback）、Spring SSE 3 轮服务端 E2E（47/0），
> 以及 5.4 的 MQ 真实模型（19/0）、Qdrant 文档引用（28/0）、重复采样（3 轮 47/0）
> 和单机并发基线（6/12、12/24、24/24 均 10/0）；但仍不是完整产品验收：
> Flutter 未实现、没有人工评分 / 留存指标、没有多实例和持续压测，部分接口返回 501。
> 这些要主动说明，不能包装成“已完成”。

## 1. 30 秒版本

> 我做的是一个跨会话的学习状态系统：用户解释、批注、测试回答会被抽成
> append-only 的 Evidence；Spring 在事务里把它演进成当前 Understanding，
> 并写不可变 Revision；下一次对话只注入当前快照，只有用户显式复盘时才注入
> 历史轨迹。Agent 负责检索和生成，但不直接写库；数据库唯一约束保证重复分析
> 不会产生第二条证据。

一句话区分：

> **普通 Chatbot 记对话，RAG 记文档，这个项目记「用户现在怎么理解、过去怎么变」。**

## 2. 核心设计（按面试官最可能追问的顺序）

### 2.1 为什么把一致性放在 Spring，而不是 Agent？

- Agent 可能 retry，Worker 可能重复投递，同一消息可能被多次分析；
- 让 Agent 自己“记得不要重复”不可靠；
- Spring 统一做 conceptId / 枚举 / 权限校验，`learning_evidence.dedupe_key`
  唯一索引是最终兜底；
- Agent 只返回候选，不持有数据库写权限，边界清晰。

回答时可以说：**“应用层去重是优化，数据库唯一约束才是保证。”**

### 2.2 五种存储语义为什么必须分开？

| 语义 | 表 | 如果混在一起会怎样 |
| --- | --- | --- |
| 原始记录 | message / highlight / test_answer | 无法回跳，判断失去依据 |
| 追加事实 | learning_evidence | 重复分析会重复计数 |
| 当前快照 | learning_understanding | 历史被覆盖，无法复盘 |
| 不可变历史 | understanding_revision | 只能看到“现在”，看不到“怎么变成现在” |
| 物化投影 | learning_state | UI 状态与理解快照可能各说各话 |

误区“已解除”不是新增枚举，而是从当前快照的 `misconceptions` 数组移出；
旧 Revision 仍保留 `ACTIVE`。这样同一时刻能同时回答“当时错在哪”和“现在是否掌握”。

### 2.3 上下文怎么组装？

`ContextBuilder` 按优先级裁剪：

```text
user_input 100 > history 90 > session 85 > understanding 70
> trajectory 68 > memory 50 > knowledge_status 45 > knowledge 40 > evidence 20
```

两个关键取舍：

- **普通问题不注入 trajectory。** 否则模型会把历史误区当成当前状态，
  也会浪费预算；
- **显式复盘才注入 trajectory。** 并且用 Revision 的 `trigger_evidence_id`
  精确回查证据，而不是再塞一份“最近 3 条”，避免同一批证据出现两次。

### 2.4 文档检索与学习状态是两条线

```text
Qdrant → knowledge chunks + knowledge_status
Spring → Understanding / Memory / Evidence
```

检索失败不会被包装成成功：`NO_VALID_DOCUMENTS` / `NO_MATCH` /
`THRESHOLD_FILTERED` / `DEGRADED` 会进入上下文，`evidence_guard` 只在知识问句上
追加资料状态说明，不重写回答。

### 2.5 inline 与 MQ 的分工

- inline：回复结束后同步调用 Agent 分析，简单，但占用后端线程池；
- MQ：publish → Worker → result consumer；publisher confirm + mandatory，
  重复 taskId 幂等，失败进 retry TTL，最终 DLQ；
- Agent 永久失败时 run 是 `FAILED`，另有独立 `rules-v1` 兜底 run 写 `UNKNOWN`
  观察，不推进掌握状态——**失败不被伪装成成功**。

## 3. 关键缺陷与修复（面试重点）

### 3.1 SSE 完成顺序与异常边界

**问题：** 分析失败或客户端断开可能影响已经开始的回答流；分析在错误的位置执行，
会让“回答完成”和“学情写入”互相拖累。

**修复：** `ChatService` 把分析移到回答安静完成之后的 `finally` 路径，
只有存在 `assistantMessage` 时才分析；传输层异常在边界捕获；新增 `createEmitter()`
测试缝，单测覆盖顺序。

**可以讲：** 用户可见回答是第一优先级，学情分析是后置副作用，不能反过来。

### 3.2 Evidence 重复

**问题：** 同一消息被 retry / 重复投递时，会产生多条逻辑相同的证据。

**修复：** `dedupe_key = SHA-256(user_id, concept_id, evidence_type, primary_source,
normalized_observation)`，数据库 `UNIQUE(dedupe_key)`；重复时复用原 Evidence、
追加来源、更新 `last_seen_run_id`。

**验证：** MQ E2E 同一 taskId 投递两次，Evidence / Revision 都只有 1 条。

### 3.3 当前快照与历史互相污染

**问题：** 纠正后如果直接改旧记录，就无法复盘；如果只看旧 Revision，
又会把历史误区当成当前状态。

**修复：** 当前快照可更新；Revision 只插入；`persistIfChanged` 先归一化 JSON
再比较，内容没变不写；LearningState 只做单向投影。

### 3.4 非成功检索被“脑补”成成功

**问题：** 没有文档 / 未命中 / 被阈值过滤时，模型仍可能编造资料依据。

**修复（4.4）：** 检索状态显式进入上下文；`evidence_guard` 对四类非成功状态
追加“资料状态说明”，只追加、不重写；只对知识问句生效，闲聊不插入。

### 3.5 历史回答虚构未记录的过程（5.3 / 5.3.2）

**历史真实失败：** 5.3.1（2026-10-27）的 C 场景中，真实模型写出：

> 你很可能通过重读定义 + 对比缓存通用含义迁移了过来……而且没有外部提示或资料介入。

这些过程没有记录，属于编造。C 判定 `FAIL`，失败样本保留。

**2026-10-30 复验：** 用 `deepseek-flash` 重跑 A/B/C/C-only/D 五场景。C 不再编造
变化原因，明确写「记录里看不出原因」；C-only 的两次模型回答也都拒绝编造历史，
但被 `no_history_denial` 的否定句规则误判，最终由确定性 fallback 交付。
复验报告见 `docs/testing/phase53-real-model-20261030.md`。

**同日服务端 E2E：** 另起 Spring 8081 + Agent 8000，走 3 轮公开 API：
普通问题、错误解释、显式复盘，47 / 0 通过。三次聊天均
`message.start → delta → end`、`finish_reason=stop`、`message.truncated=0`；
inline 分析走真实模型并返回 `agent-v1`；显式复盘注入 3 个 Revision、
`history_guard issues=[]`、`fallback=false`。
证据见 `docs/testing/phase53-spring-e2e-20261030.md`。

这次 E2E 还抓到一个真实模型契约漂移：模型把 `verificationDecision.trigger`
写成自由文本，导致整轮分析 Pydantic 校验失败并退回 `rules-v1`。修复
`_coerce_verification_enums()` 后重跑通过。这个例子适合讲
「HTTP 200 不等于链路验收通过，必须核对分析器版本和 fallback」。

**修复思路：**

1. 复盘时注入 Revision 轨迹 + 精确关联 Evidence；
2. Prompt 要求只陈述可核对的原话 / 批注 / 测试回答 / 时间 / 状态；
3. `history_guard` 做句子级检查：未记录内省、外部帮助缺失、`PARTIAL` 升级、
   证据数量概括、显式批注未映射、无历史时的否认 / 反推；
4. 不合格时**最多一次**受限重生成；
5. 仍不合格则用 `build_history_fallback` 生成确定性回退文本，只来自记录，
   不含内部 Evidence ID；
6. 普通对话不缓冲；只有显式复盘才付这个延迟。

**必须主动说明的限制：**

- 2026-10-30 已完成真实模型 Agent Runtime 复验、Spring SSE 服务端 E2E、
  MQ 真实模型、Qdrant 文档引用、3 轮重复采样和 3 组并发基线；
  但 Runtime 只跑了**一轮 5 场景**、重复采样只覆盖**同一组 3 个场景**，
  没有人工评分、置信区间或留存指标；
- C-only 的模型回答本身正确，但被 `history_guard` 的 `no_history_denial`
  否定句规则误判，重生成后仍误判，最终交付的是 **fallback**，不是模型原文；
- 校验器是句子级启发式，不能覆盖所有同义改写；非常规措辞可能不触发复盘；
- 文档引用 28/0 只用了两篇各 1 个 chunk 的合成文档，证明标签可回跳，
  不证明事实正确率或多 chunk 排序；
- 并发只到 24 个短问题、单机单实例，没有 SLO、持续负载、限流或断线重连；
- MQ 轮保留了一次 `ConnectError` + 非法 JSON 的真实失败样本；失败由
  `rules-v1` 兜底，不能把 MQ 写成稳定通过；
- Flutter 前端仍未验证；`deepseek-flash` 在 `max_tokens=1024` 时会截断长回答，
  本轮已改为 4096；
- 因此可以写「已完成多链路真实模型验收和单机并发基线」，不能写
  「真实模型已稳定通过完整产品验收」。

这一段最能体现工程判断：**不把“改好了”说成“验证过了”，也不把“误报”藏起来。**

### 3.6 MQ 幂等与失败语义

**问题：** 至少一次投递、Worker 崩溃、坏消息可能造成重复写入或任务永久卡住。

**修复：** 重复 taskId 识别；结果消费二次校验；永久失败落 `FAILED` + DLQ；
兜底证据标记 `rules-v1` / `UNKNOWN`，不冒充 Agent 分析。

**测试环境教训：** 第一次 MQ E2E 有另一个常驻 Worker 争用队列，出现 9 个 FAIL；
停掉它并清空队列后重跑 27/0。这不是产品缺陷，但说明这个脚本要求**独占队列**。
讲的时候可以把它当成“如何区分环境问题与代码问题”的例子。

**5.4 真实模型 MQ 验收：** 另起 Spring 8082 + Agent 8001 + 独立 Phase54 拓扑，
脚本自行启停 Worker，走通 `publish → Worker → /agent/analyze → result consumer → DB`。
正式轮 **19 / 0**：run `SUCCEEDED`、Evidence `agent-v1`、来源回跳、
`MISCONCEPTION` + 1 Revision；同一 taskId 重复投递被 Worker 处理两次，但
Evidence / Revision 零新增，Result Consumer 识别并忽略重复结果。

同一脚本的上一轮保留了 **15 / 4** 的真实失败：首次 `ConnectError`（可重试），
重试 HTTP 200 但 JSON 非法（不可重试），run 落 `FAILED`，由 `rules-v1` 写
`UNKNOWN` 观察。这个样本很适合讲「失败语义」：系统没有把 fallback 冒充成
`agent-v1`，也没有推进掌握状态。

### 3.7 并发压测暴露的两个死锁与一个顺序竞态（5.4）

**这不是设计推演，是压测打出来的。**

#### 死锁一：消息序号的索引间隙锁

旧实现先在事务里锁 `conversation` 行，再执行：

```sql
SELECT sequence_no FROM message
WHERE conversation_id = ?
ORDER BY sequence_no DESC LIMIT 1 FOR UPDATE
```

`message(conversation_id, sequence_no)` 是普通唯一索引。空对话上的锁定范围读
会在索引区间上加**间隙锁**，相邻对话的区间互相覆盖；两个事务写不同对话却互相等待，
12 并发里 3 个请求直接 500：`Deadlock found when trying to get lock`。

修复：`conversation.last_message_seq` 计数器，分配改成
`UPDATE ... SET x = x + 1` 后在同一事务读回。只锁主键行，没有范围扫描。
实体字段标记 `updateStrategy = NEVER`，防止先读后改的 `updateById` 覆盖并发分配。
已有库执行 `schema-migration-phase54-message-seq.sql`。

#### 死锁二：首次 Session 归属的 S→X 锁升级

多条对话同时发出第一条学习消息时，每个事务都看到「没有可归属的 Session」，
各自 CREATE 出重复 Session；同时 `conversation.session_id` 的外键检查给 session 行
加共享锁，随后 `learning_session` 更新要升级为排他锁。两个事务各持 S 锁等 X 锁，
死锁回滚一个。

修复：首次归属先锁 `learning_space` 行，把同一空间的解析串行化；
`attach()` 固定「先 UPDATE session（X 锁），再写 conversation.session_id（FK 检查）」。
复测 12 个会话只有 1 个 Session，0 死锁。

#### 顺序竞态：`message.end` 早于 run 终态

重复采样第一轮 46/1：客户端收到 `message.end` 后立刻按 `runId` 查库，看到
`RUNNING`/空。修复：`ChatService` 先 `finishRun(...)` 写终态，再发 end；
写失败走 `message.error`。修复后连续 3 轮 47/0。

**可以讲：** 这三个都不是「代码写得丑」，而是并发/顺序假设被真实负载推翻；
修复分别选择了计数器、统一锁顺序、先提交后通知，而不是加重试掩盖。

## 4. 演示怎么讲（7 步，Mock）

先声明：**“以下 LLM 是 Mock，输出确定性；真实模型复验是独立的单轮实验，不在本演示中。”**

```bash
bash scripts/phase6-demo.sh
```

讲解顺序：

1. 用户暴露错误理解：“TLB 就是页表本身”；
2. 原始消息 + 显式批注 → Evidence，来源可回跳；
3. 当前 Understanding 进入 `MISCONCEPTION`，Revision 追加；
4. 纠正批注 + 测试回答 → 两条独立正确证据 → `DEMONSTRATED`，
   当前快照移除误区，历史 Revision 仍保留 `ACTIVE`；
5. 新会话普通问题：只注入当前 Understanding，没有 trajectory；
6. 显式复盘：才出现 trajectory，且 sourceIds 含 `revision:*`；
7. 生产 SSE：`message.start → delta → message.end`，回答按记录陈述历史与当前，
   不泄露内部 ID；Mock 下会走确定性回退，这正好演示“校验器不通过时不硬编”。

**不要展示成：** “真实模型已经能稳定遵守证据边界”。

## 5. 面试官可能追问

**Q：为什么不用向量库存 Memory？**
A：当前 Memory 是关键词 `LIKE`，`embedding_ref` 没接向量库，这是已知缺口；
文档切片才用 Qdrant。这样划分是为了先验证学习状态闭环，而不是把检索复杂度堆满。

**Q：为什么不做掌握度分数？**
A：第一版刻意用离散 status（UNKNOWN / EXPOSED / PARTIAL / DEMONSTRATED /
MISCONCEPTION），因为分数没有可解释的证据链；UI 需要排序时由 LearningState
派生复习时间 / 优先级。

**Q：为什么历史校验要缓冲？**
A：句子级校验需要完整回答；代价是显式复盘首字延迟从第一个 token 推迟到生成结束，
触发重生成时约等于两次生成。普通对话不缓冲。

**Q：如果真实模型一直不通过怎么办？**
A：保留失败样本，判定为已知限制；最低关闭条件是真实模型重跑 B / C / C-only，
分别记录 raw answer 与 production final answer，不能合并判定。2026-10-30 已按这个
方式补跑：C 通过，C-only 因守卫误判走 fallback；并补了 Spring SSE 服务端 E2E
（3 轮 / 47 检查）、MQ 真实模型、文档引用、3 轮重复采样和 3 组并发基线。
下一步是修复 `no_history_denial` 的否定句误报，补人工评分、持续压测和 Flutter。

**Q：系统最大的技术风险？**
A：单进程假设（SSE、线程池）、没有迁移框架、真实模型仍会连接失败或输出非法 JSON；
C-only 仍依赖 fallback，重复采样只覆盖 3 个场景；并发只到 24 个短问题，没有 SLO、
多实例或持续负载。5.4 已修掉两个数据库死锁和一个 `message.end` 顺序竞态，但
这些修复目前主要由 E2E / 并发脚本覆盖，没有专门的单元回归。
这些都在 `KNOWN_LIMITATIONS.md` 里，不藏在代码注释里。

## 6. 如果只能讲一个技术点

讲 **Evidence → Understanding → Revision → LearningState 的语义分层**：

> 原始记录是事实，Evidence 是追加依据，Understanding 是当前快照，
> Revision 是不可变历史，LearningState 是投影。
> 只要这五层不混，跨会话个性化才有可解释性；混了任何一层，
> 要么无法复盘，要么把历史当成现在。

## 7. 相关材料

- 架构：[ARCHITECTURE.md](ARCHITECTURE.md)
- 生命周期：[LEARNING_LIFECYCLE.md](LEARNING_LIFECYCLE.md)
- 演示：[DEMO.md](DEMO.md)
- 测试口径：[TEST_MATRIX.md](TEST_MATRIX.md)
- 5.4 真实验收：[../testing/phase54-real-model-20261030.md](../testing/phase54-real-model-20261030.md)
- 已知限制：[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)
