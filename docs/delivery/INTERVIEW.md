# 面试讲解提纲

> 这份提纲只讲**代码里真实存在的东西**。真实模型验收未完成、Flutter 未实现、
> 部分接口返回 501，这些要主动说明，不能包装成“已完成”。

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

**真实失败：** 5.3.1 的 C 场景中，真实模型写出：

> 你很可能通过重读定义 + 对比缓存通用含义迁移了过来……而且没有外部提示或资料介入。

这些过程没有记录，属于编造。C 判定 `FAIL`，失败样本保留。

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

- 5.3.2 真实模型调用 **0 次**（本地没有 Key，按约束停止重试）；
- 离线回放和 Mock SSE 只能证明校验器能拦截**已知失败**，不能证明真实模型下次合规；
- 校验器是句子级启发式，不能覆盖所有同义改写；非常规措辞可能不触发复盘；
- 这是**可接受的已知限制，但不是已通过验收**。

这一段最能体现工程判断：**没有证据时，不把“改好了”说成“验证过了”。**

### 3.6 MQ 幂等与失败语义

**问题：** 至少一次投递、Worker 崩溃、坏消息可能造成重复写入或任务永久卡住。

**修复：** 重复 taskId 识别；结果消费二次校验；永久失败落 `FAILED` + DLQ；
兜底证据标记 `rules-v1` / `UNKNOWN`，不冒充 Agent 分析。

**测试环境教训：** 第一次 MQ E2E 有另一个常驻 Worker 争用队列，出现 9 个 FAIL；
停掉它并清空队列后重跑 27/0。这不是产品缺陷，但说明这个脚本要求**独占队列**。
讲的时候可以把它当成“如何区分环境问题与代码问题”的例子。

## 4. 演示怎么讲（7 步，Mock）

先声明：**“以下 LLM 是 Mock，输出确定性；真实模型验收未完成。”**

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
分别记录 raw answer 与 production final answer，不能合并判定。在没有 Key 时继续
改 Prompt 或扩大架构无法验证收益。

**Q：系统最大的技术风险？**
A：单进程假设（SSE、线程池）、没有迁移框架、真实模型合规性未验证。
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
- 已知限制：[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)
