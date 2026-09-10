# AI Learning Space — PRD

> Version: v3.0
> Status: Development Baseline
> Goal: 构建“能够持续理解学习者认知过程”的 AI Learning Agent
> Memory、Understanding Revision、Context Assembly、用户控制 Memory。

---

# 一、产品定位

## 1.1 一句话定位

> **一个能够跨对话理解学习者认知过程，并基于历史理解持续调整教学方式的 AI Learning Agent。**

---

# 二、核心问题

传统 AI 的问题不是“不会回答”。

而是：

> **它很难真正知道这个用户现在已经理解到了哪里，以及这个用户是怎么理解的。**

因此系统需要解决：

```text
记住用户问过什么
        ↓
升级为
记住用户理解过什么
        ↓
升级为
理解用户如何理解
        ↓
最终
基于这种理解继续教学
```

---

# 三、核心设计原则

## 3.1 Understanding > Score

核心不是：

```text
mastery = 72%
```

而是：

```text
已知什么
理解到哪里
哪里混淆
哪里不确定
自己如何解释
```

---

## 3.2 Evidence > Guess

任何重要的理解判断必须能够追溯：

```text
Understanding
     ↓
Evidence
     ↓
Message / Highlight / Test Answer
```

---

## 3.3 Current Understanding ≠ Long-term Memory

### Understanding

面向：

> 某个 Concept。

例如：

```text
用户对 TLB 的理解
```

### Memory

面向：

> 未来如何更好地服务这个用户。

例如：

```text
用户喜欢从硬件执行过程理解计算机系统。
```

二者必须分开。

---

# 四、用户场景

## 4.1 连续学习

用户：

> TLB 是什么？

AI：

> ……

系统分析：

```text
用户已经知道 Page Table
不知道 TLB 为什么存在
```

---

## 4.2 用户批注

用户选择回答：

> TLB 是 Page Table 的缓存。

点击：

```text
🧠 我的理解
```

系统产生：

```text
Evidence
```

并关联：

```text
Concept = TLB
```

---

## 4.3 跨对话继续

三天后：

> TLB Miss 到底发生什么？

Agent：

```text
Current conversation
+
Historical Understanding(TLB)
+
Relevant Memory
+
Relevant Evidence
```

然后回答。

---

## 4.4 用户纠正 AI

用户：

> 不是，我觉得你前面这里说错了，因为……

这本身也是重要 Evidence。

系统必须允许：

```text
AI observation
```

被：

```text
User feedback
```

修正。

所以 Understanding 不是不可逆的 AI 判断。

---

# 五、用户流程

```text
打开学习空间
      ↓
创建 / 继续对话
      ↓
用户提问
      ↓
Retriever
      ↓
Context Builder
      ↓
Learning Agent
      ↓
回答
      ↓
用户：
├─ 继续提问
├─ 标记重要
├─ 提出疑问
├─ 写我的理解
└─ 标记我可能错了
      ↓
Evidence Extraction
      ↓
Understanding Update
      ↓
Memory Consolidation
      ↓
下一次对话继续使用
```

---

# 六、核心功能

## 6.1 学习空间

用于隔离不同学习主题：

```text
操作系统
C++
Redis
数据库
Agent
```

---

## 6.2 对话

功能：

```text
普通提问
流式回答
历史消息
继续对话
```

---

## 6.3 批注

类型：

```text
IMPORTANT
QUESTION
MY_UNDERSTANDING
MISCONCEPTION_CANDIDATE
CONCEPT_LINK
```

---

## 6.4 Evidence

系统从：

```text
Message
Highlight
Test Answer
```

提取 Evidence。

Evidence 必须包含：

```text
observation
correctness
reasoning_quality
specificity
ai_confidence
```

第一版这些字段用于内部分析，不直接暴露给用户。

---

# 七、Learning Understanding

一个 User + Concept 原则上只有一个 current snapshot。

例如：

```text
TLB

summary:
用户知道 TLB 是页表缓存。

known_parts:
[
  "TLB Hit",
  "TLB Miss"
]

misconceptions:
[
  {
    "text": "把 TLB Miss 与 Page Fault 混淆",
    "status": "ACTIVE"
  }
]

（当前仍然成立的误区；出现反向证据后降为 PENDING，解除后从快照移除，
  历史通过 Revision 与 Evidence 追溯）

uncertain_parts:
[
  "Page Table 的位置"
]
```

---

# 八、Understanding Revision

每次 Understanding 更新都产生 revision。

```text
Understanding
      │
      ├── Revision 1
      ├── Revision 2
      ├── Revision 3
      └── Revision 4
```

这样可以回答：

> 用户什么时候开始理解这个概念？

以及：

> 为什么系统改变了判断？

---

# 九、Long-term Memory

Memory 分成：

```text
USER
LEARNING
PREFERENCE
```

### USER

例如：

```text
用户是一名 CS 学习者。
```

### LEARNING

例如：

```text
用户已经学过 TLB。
```

### PREFERENCE

例如：

```text
用户喜欢从硬件执行路径理解操作系统知识。
```

Open WebUI 当前的 Memory 模型也采用独立 Memory 数据，并区分 user/context 等类型，同时支持 path 和 metadata。

---

# 十、Memory 生命周期

Agent 不允许无条件写入 Memory。

流程：

```text
Conversation
     ↓
Candidate Memory
     ↓
Memory Reconciler
     ↓
┌────┼────┐
↓    ↓    ↓
ADD UPDATE IGNORE
```

对于用户明确写出的：

> “以后你解释计算机组成时都先告诉我实际硬件发生了什么。”

可以直接保存。

对于 AI 推断出的：

> “用户可能喜欢图示。”

默认只能成为 candidate，而不是永久 Memory。

---

# 十一、检索策略

## 11.1 当前上下文

永远优先：

```text
Current Conversation
```

---

## 11.2 Understanding Retrieval

查询：

```text
当前 Concept
相关 Concept
当前误区（ACTIVE / PENDING）
历史误区（从 Revision 与 Evidence 追溯）
最近 Revision
```

---

## 11.3 Memory Retrieval

查询：

```text
长期用户信息
学习偏好
相关学习记忆
```

---

## 11.4 Evidence Retrieval

只在：

```text
需要解释判断
需要验证
需要审计
```

的时候召回。

---

# 十二、Context Builder

最终发送给模型的 context：

```text
System Instruction
        +
Current Conversation
        +
Relevant Knowledge
        +
Relevant Understanding
        +
Relevant Memory
        +
Optional Evidence
```

不要直接：

```text
SELECT * FROM learning_evidence
```

全部塞进去。

---

# 十三、主动验证

触发条件：

```text
NEW_CONCEPT
CONCEPT_CONFLICT
REPEATED_ERROR
CONTRADICTORY_EVIDENCE
OVERCONFIDENCE
LONG_UNVERIFIED
```

↓

```text
Verification Policy
```

判断：

```text
是否值得打断？
```

↓

如果需要：

```text
Generate Test
```

---

# 十四、测试

第一版：

```text
单选
判断
简答
```

其中简答题不能简单依赖：

```text
answer == correct_answer
```

而应该由 Agent 做语义评价。

最终生成：

```text
Verification Evidence
```

---

# 十五、状态

状态：

```text
UNKNOWN
EXPOSED
PARTIAL
DEMONSTRATED
MISCONCEPTION
```

状态来自：

```text
Evidence
+
Understanding
```

不是由单次回答直接设置。

---

# 十六、笔记导出

导出的不是聊天记录。

而是：

```text
Concept
    ↓
Understanding
    ↓
Important Highlights
    ↓
Relevant Evidence
```

生成：

```text
结构化学习笔记
```

---

# 十七、思维导图

思维导图结构：

```text
Concept
  │
  ├── known parts
  ├── uncertain parts
  ├── misconceptions
  └── related concepts
```

用户可以选择：

```text
当前概念
当前学习空间
指定时间范围
```

生成脑图。

---

# 十八、证据链页面

用户点击：

> 为什么你认为我理解了 TLB？

显示：

```text
Understanding
      ↓
Evidence
 ├─ Message #10
 ├─ Highlight #21
 └─ Test Answer #7
```

点击 Evidence：

```text
跳回原始消息 / 批注 / 测试
```

---

# 十九、用户对 AI 判断拥有最终控制权

用户可以：

```text
删除 Memory
修改 Memory
标记 Understanding 为错误
撤销某条 Evidence
```

因为：

> Agent 的推断不一定正确。

系统必须允许用户反向修正认知模型。

---

# 二十、Agent 工作流

```text
User Message
     ↓
Retrieve Context
     ↓
Context Builder
     ↓
Tutor
     ↓
Response
     ↓
Async Evidence Extraction
     ↓
Evidence
     ↓
Understanding Updater
     ↓
Revision
     ↓
Memory Reconciler
     ↓
Memory
```

---

# 二十一、异常情况

## Agent 无法判断

不要强行更新。

```text
status = UNKNOWN
```

---

## Evidence 相互矛盾

保留 Evidence。

不要简单覆盖旧信息。

```text
Evidence A
Evidence B
     ↓
Conflict
```

再由后续测试或用户解释解决。

---

## Memory 与当前对话冲突

当前用户明确表达优先。

```text
Current User Input
        >
Historical Memory
```

---

# 二十二、MVP 边界

第一版只实现：

```text
User
Space
Conversation
Message
Highlight

Concept

Evidence
Understanding
Understanding Revision
Memory

Learning Agent

跨对话 Retrieval
```

暂时不做：

```text
复杂知识图谱
复杂学习计划
大规模 RAG
多 Agent
复杂推荐算法
RabbitMQ
```

核心目标只有一个：

> **证明 Agent 能真正跨对话利用“用户理解”。**

---

# 二十三、验收标准

### Case 1

第一次：

> 什么是 TLB？

用户解释：

> 它应该就是 Page Table 的缓存。

系统记录：

```text
Understanding(TLB)
```

---

### Case 2

第二次：

> TLB Miss 怎么办？

系统必须召回：

```text
用户已经知道 TLB 是缓存
```

回答不能再次从：

> TLB 是什么？

开始。

---

### Case 3

用户：

> 我觉得你这里讲错了。

系统：

```text
不覆盖旧 Evidence
新增 Evidence
创建新的 Revision
重新评估 Understanding
```

---

### Case 4

用户点击：

> 为什么你认为我理解 TLB？

必须能够回溯到：

```text
Message
Highlight
Test Answer
```

---

# 二十四、成功标准

MVP 不是：

```text
页面多
接口多
表多
Agent 多
```

而是：

```text
同一个用户
+
不同对话
+
不同时间
```

Agent 能够：

```text
知道用户以前怎么理解
↓
知道哪里有误区
↓
知道用户自己的解释
↓
避免重复教学
↓
接着用户已有认知继续讲
```

这才是 AI Learning Space 的核心价值。
