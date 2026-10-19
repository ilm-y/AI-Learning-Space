# AI Learning Space — PRD

**Version:** 3.0  
**Status:** Development Baseline  
**定位：** Stateful Learning Agent

---

# 1. 产品定位

## 1.1 一句话

> 一个能够跨对话理解学习者认知过程，并基于历史理解持续调整教学方式的 AI Learning Agent。

## 1.2 核心目标

不是：

> 记住用户聊过什么。

而是：

> **理解用户目前是如何理解知识的，并把这种理解用于下一次教学。**

---

# 2. 核心对象

```text
Conversation
    ↓
Message
    ↓
Evidence
    ↓
Learning Understanding
    ↓
Understanding Revision
    ↓
Memory
    ↓
Future Context
```

辅助对象：

```text
Concept
Learning State
Test
Document
```

`Learning Session` **不在这两类里**，它是第三种角色：既不是认知对象，也不是辅助对象，
而是**活动容器** —— 见下面 2.1。

---

## 2.1 Session 设计规则

以下六条是 Space / Session / Conversation 三者关系的**正式约束**，由
`session-design.md` 最终审查确定。新增功能时若与之冲突，**改功能，不改这六条**：

> **1. Space 是长期学习领域。**
> **2. Session 是围绕一个明确学习目标的一段连续学习活动。**
> **3. Conversation 是具体聊天线程，一个 Session 可以包含多个 Conversation。**
> **4. Session 主要负责学习目标、当前焦点、组织多个 Conversation，以及提供短期 Agent 工作上下文。**
> **5. Session 不承载长期 Understanding、Evidence 或 Memory。**
> **6. Session 采用懒创建：Conversation 创建时可以 `session_id = NULL`，用户产生第一条有效学习消息后，由 Session Resolver 决定加入现有 Session 或创建新 Session。**

为什么 Session 不是「辅助对象」：辅助对象是**为闭环服务的配角**（Concept 提供锚点、
Learning State 提供投影），而 Session 是**用户和 Agent 共同工作的那个空间本身**。
把它降级成配角，会直接导致第 4、5 条被违反 —— 要么开始往里塞学习成果，要么干脆不做。

第 5 条最容易违反，也最难事后纠正。**Session 结束不等于认知消失**：

```text
Session        这段学习活动在解决什么      ── 会结束、会暂停、会被放弃
Understanding  这个人现在怎么理解这个点    ── 不随 Session 结束而消失
```

用户在这个 Session 里形成的 Understanding、攒下的 Evidence、收敛出的 Memory
全部留在 Space 与 Concept 上，换一个 Session 依然检索得到。这正是
「记忆」这件事在本产品里的含义：**记忆挂在知识点上，不挂在某次对话上。**

第 6 条的完整判定规则（Resolver 输入契约、优先级、`ATTACH / CREATE / NONE` 三档输出）
见 `session-design.md` §五。

---

# 3. 用户核心体验

## 3.1 第一次学习

用户：

> TLB Miss 和 Page Fault 有什么关系？

Agent 结合当前空间知识和已有理解回答。

回答结束后进行学习分析。

---

## 3.2 用户表达自己的理解

用户选中：

> TLB Miss 只是缓存没命中……

标记：

```text
🧠 我的理解
```

系统产生：

```text
Highlight
   ↓
Evidence
   ↓
Understanding Update
```

---

## 3.3 跨对话

用户几天后新建对话：

> Page Table 到底在哪里？

Agent 检索：

```text
Current Conversation
Relevant Understanding
Relevant Memory
Relevant Evidence
Relevant Knowledge
```

如果发现：

```text
已理解：
TLB 是 Page Table 的缓存

不确定：
Page Table 的实际存储位置
```

回答从“不确定部分”继续，而不是重新解释整章虚拟内存。

---

# 4. 页面信息架构

## 4.1 底部导航

只保留：

```text
书桌 | 状态
```

“自测”不做一级 Tab。

Test 是 Agent 根据认知缺口插入当前对话的事件。

---

# 5. 书桌

展示：

```text
继续学习
书架
```

继续学习显示：

```text
最近 Conversation
+
最近 Learning Session
+
current_focus
```

重点不是：

> 最近打开了哪个页面。

而是：

> **上次学到哪里。**

---

# 6. Learning Space

每个 Space：

```text
操作系统
```

包含：

```text
对话
文档
足迹
图谱
```

---

# 7. 对话

对话页面支持：

```text
普通提问
流式回答
继续对话
批注
主动验证
理解更新
```

---

# 8. 批注

支持：

```text
IMPORTANT
QUESTION
MY_UNDERSTANDING
MISCONCEPTION_CANDIDATE
CONCEPT_LINK
```

推荐文案：

```text
⭐ 重要
❓ 存疑
🧠 我的理解
⚠️ 我可能错了
🔗 关联
```

---

# 9. Understanding

用户不直接看到 JSON。

对用户显示：

```text
你已经理解
……
```

以及：

```text
还需要留意
……
```

当用户主动查看时，可以展开：

```text
为什么这么判断？
```

进入：

```text
Understanding
 ↓
Evidence
 ↓
原始消息 / 批注 / 测试
```

---

# 10. Evidence

Evidence 是系统对用户认知做判断的依据。

来源：

```text
Message
Highlight
TestAnswer
```

Evidence 永不因 Understanding 更新而删除。

---

# 11. Understanding Revision

当前理解是 Snapshot。

每次发生有效变化：

```text
Current Snapshot
      ↓
New Evidence
      ↓
New Revision
      ↓
New Snapshot
```

这样能够回答：

```text
什么时候开始理解？
什么时候出现错误？
什么时候纠正？
为什么状态发生变化？
```

---

# 12. Memory

Memory 是长期用户上下文。

Scope：

```text
GLOBAL
SPACE
CONCEPT
```

示例：

```text
GLOBAL:
用户喜欢详细解释。

SPACE:
用户学习 OS 时喜欢先理解真实硬件执行路径。

CONCEPT:
用户已经理解 TLB Hit / Miss。
```

---

# 13. Memory 生命周期

Agent 不允许直接把任何推断永久写入 Memory。

流程：

```text
Observation
   ↓
Memory Candidate
   ↓
Reconciler
   ↓
ADD / UPDATE / IGNORE
```

用户可以：

```text
查看
修改
删除
```

Memory。

删除 Memory 不删除 Evidence。

---

# 14. 状态

第一版：

```text
UNKNOWN
EXPOSED
PARTIAL
DEMONSTRATED
MISCONCEPTION
```

UI 不显示：

```text
mastery = 76%
confidence = 82%
```

状态页主要显示：

```text
最近学习话题分布
需要留意的地方
学习计划
```

---

# 15. 需要留意的地方

来源不是“考试错题”单独一种。

来源可以包括：

```text
概念混淆
重复错误
矛盾证据
长期未验证
用户主动存疑
测试错误
```

示例：

```text
Page Fault

你已经理解基本触发条件，
但最近几次讨论中容易把它
与 TLB Miss 混淆。

[继续复盘]
```

---

# 16. 主动验证

触发条件：

```text
NEW_CONCEPT
CONCEPT_CONFLICT
REPEATED_ERROR
CONTRADICTORY_EVIDENCE
OVERCONFIDENCE
LONG_UNVERIFIED
```

Policy：

```text
SKIP
ASK_NOW
ASK_LATER
```

只有值得打断时才插入 Test Card。

---

# 17. Test

Test 属于 Conversation Event。

不是一级导航。

流程：

```text
Evidence Gap
   ↓
Verification Policy
   ↓
Test Task
   ↓
User Answer
   ↓
Evaluation
   ↓
Evidence
   ↓
Understanding Revision
```

---

# 18. 笔记导出

第一阶段可以先不做。

以后：

```text
Concept
+
Understanding
+
Important Highlight
+
Verified Evidence
```

生成结构化 Markdown。

不是简单总结整个聊天。

---

# 19. 思维导图

来源：

```text
Concept
   ├── Known Parts
   ├── Uncertain Parts
   ├── Misconceptions
   └── Related Concepts
```

Highlight 和 Evidence 作为补充信息。

---

# 20. 证据链

用户点击：

```text
为什么这样判断？
```

显示：

```text
Understanding
     ↓
Evidence #31
     ↓
Message #10
Highlight #21
TestAnswer #7
```

点击来源可以跳回原始位置。

---

# 21. Agent 主流程

```text
User Message
    ↓
Create Message
    ↓
Create / Load Session        （Session Resolver：ATTACH / CREATE / NONE）
    ↓
Context Retrieval
    ↓
Context Builder
    ↓
Learning Agent
    ↓
Tutor Response
    ↓
Persist Assistant Message
    ↓
SSE → User
```

第三步 **Create / Load Session 是三选一的判定，不是「没有就建一个」**：

| 判定     | 何时                       | 结果                                        |
| -------- | -------------------------- | ------------------------------------------- |
| `ATTACH` | 已有匹配的学习活动         | 挂到那个 Session 上                         |
| `CREATE` | 有学习内容但没有匹配活动   | 新建 Session，`goal` 由首个命中的 Concept 生成 |
| `NONE`   | 本轮没提到任何已知 Concept | **不创建 Session**，`session_id` 保持 NULL  |

`NONE` 是一档正经输出，不是「还没想好」：用户在闲聊、在问一个全新知识点，
都不该产生学习活动。所以 `conversation.session_id` 为 NULL 有两种合法含义
（还没到判定时机 / 判定结果是 NONE），都不能当异常处理 —— 见 2.1 规则 6。

位置也是硬的：**必须在 Context Retrieval 之前**。最需要继承上个 Session 目标的那一轮，
恰恰是「新建对话接着学同一个知识点」，而此刻归属还没定。放到生成之后再解析，
这一轮就什么都带不上。

随后：

```text
Learning Analysis
    ↓
Evidence Extraction
    ↓
Evidence Upsert
    ↓
Understanding Update
    ↓
Revision
    ↓
Memory Reconciliation
    ↓
Learning State Update
```

---

# 22. 数据一致性原则

## 22.1 Evidence

必须由数据库唯一约束兜底。

```text
dedupe_key UNIQUE
```

Agent retry 不得产生逻辑重复。

---

## 22.2 Memory

必须有明确 scope。

```text
GLOBAL
SPACE
CONCEPT
```

GLOBAL Memory 在任意 Space 都可被当前用户检索。

SPACE Memory 只能在对应 Space。

CONCEPT Memory 必须在对应 Concept。

---

## 22.3 Understanding

一个：

```text
user + concept
```

只有一个当前 Snapshot。

但可以拥有多个 Revision。

---

# 23. Agent 与 Backend 边界

## Spring Boot

负责：

```text
Authentication
Authorization
CRUD
Persistence
Transaction
Idempotency
Database Constraint
SSE Gateway
```

## Python Agent

负责：

```text
Intent / Concept Detection
Context Ranking
Tutor
Evidence Extraction
Understanding Proposal
Memory Candidate
Verification Decision
```

Agent 不直接修改 MySQL。

---

# 24. MVP 接口目标

第一阶段只需要：

### Client API

```text
Auth × 2
Space × 2
Conversation × 3
Highlight × 1
Learning × 2
Test × 1
```

总计：

**11 个公开接口。**

### Agent Internal API

```text
Chat × 1
Analyze × 1
```

总计：

**2 个 Agent Gateway 接口。**

因此：

> **13 个接口可以完整跑通第一条学习闭环。**

---

# 25. MVP 不做

先不做：

```text
复杂学习计划排期
复杂知识图谱推理
完整多平台导入
复杂导出
RabbitMQ
多 Agent
复杂推荐算法
```

这些都建立在核心闭环成立以后。

---

# 26. MVP 验收

必须通过：

```text
首次学习
↓
Understanding 创建
↓
第二个 Conversation
↓
Understanding 被召回
↓
个性化回答
↓
用户批注
↓
Evidence 创建
↓
重复 Agent Run
↓
Evidence 不重复
↓
新增 Evidence
↓
Revision
↓
Memory 被正确检索
```

这条链跑通以后，项目的核心就成立。

---

# 27. 非功能要求

## 数据一致性

```text
Evidence dedupe_key UNIQUE
Memory version
Understanding version
```

## 可观测性

每个 Agent Run：

```text
run_id
trace_id
duration
status
error
```

不保存模型内部 Chain of Thought。

## 可测试性

至少建立：

```text
cross_session
evidence_dedup
memory_scope
understanding_revision
memory_conflict
verification
```

六类 Agent regression case。
