# 核心模型设计决策 — Understanding、Memory 与渐进式架构

> 文档类型：架构决策记录 / 核心模型设计说明  
> 版本：v1.0  
> 状态：已采纳  
> 适用范围：AI Learning Space  
> 关联文档：README.md、PRD.md、ER.md

---

## 1. 背景

AI Learning Space 的 v2 已经建立了：

```text
Learning Understanding
Learning State
Learning Evidence
Evidence Source
Session
```

方向正确，但存在五个结构性问题：

1. `Learning Understanding` 和 `Long-term Memory` 没有分离
2. `learning_understanding` 只保存当前快照，历史理解过程会丢失
3. `evidence_source(source_id)` 是多态引用，缺少工程约束
4. `session.agent_notes` 容易承担长期记忆职责
5. 技术栈过早工程化，RabbitMQ 在 MVP 阶段不是刚需

本决策用于在进入 M1 之前，把核心模型固定下来，避免后面一边写代码一边推翻数据模型。

---

## 2. 决策总览

本次共采纳五项核心决策：

| 编号 | 决策                                                       |
| :--- | :--------------------------------------------------------- |
| D1   | `Learning Understanding` 与 `Long-term Memory` 分离        |
| D2   | `Learning Understanding` 必须保留 `Understanding Revision` |
| D3   | `Evidence Source` 使用显式多态引用，并增加唯一约束         |
| D4   | `Session.agent_notes` 只做临时上下文，不承担长期记忆       |
| D5   | MVP 技术栈渐进式，RabbitMQ 后置                            |

---

## 3. D1：Understanding 与 Memory 分离

### 3.1 问题

v2 中有 `Learning Understanding`，但缺少独立的 `Long-term Memory`。

这会导致 Agent 后面退化成：

```text
从 Learning Understanding 查历史
```

而不是真正拥有跨对话长期记忆。

### 3.2 定义

```text
Learning Understanding
= 用户当前对某个 Concept 怎么理解

Long-term Memory
= 未来回答这个用户时，Agent 应该记住什么
```

### 3.3 示例

Understanding：

```text
TLB
用户知道 TLB 是页表缓存；
曾经混淆 TLB Miss 与 Page Fault。
```

Memory：

```text
在讲操作系统时：
用户喜欢从“硬件实际发生了什么”理解概念；
不喜欢直接背定义；
已经理解 TLB → Page Table 的基本关系。
```

前者是**知识点认知模型**，后者是**Agent 的长期用户上下文**。

### 3.4 参考依据

- LearningMAP：独立 `student_memory`
- Open WebUI：独立 memory 表，支持 type / path / metadata
- Letta：persistent memory 作为 stateful agent 核心能力

### 3.5 数据模型影响

新增：

```text
memory
memory_source
```

`memory` 建议字段：

```text
id
user_id
learning_space_id nullable
concept_id nullable
memory_type        USER / LEARNING / PREFERENCE
path
content
status             CANDIDATE / ACTIVE / ARCHIVED
importance
embedding_ref
created_at
updated_at
```

---

## 4. D2：Understanding 必须保留 Revision

### 4.1 问题

用户的理解会不断变化。

例如：

```text
9月1日：
“TLB Miss 就是 Page Fault”

9月5日：
“TLB Miss 不一定 Page Fault”

9月10日：
“TLB Miss 是查 TLB 失败，之后访问 Page Table；
如果页表项表明页不在内存，才可能 Page Fault。”
```

如果只 update 当前那一行，历史理解过程就丢了。

而产品恰恰要回答：

> 为什么你现在判断我已经理解了？

### 4.2 设计

```text
Learning Understanding
       ↓
当前快照

Understanding Revision
       ↓
历史变化
```

### 4.3 数据模型影响

新增：

```text
understanding_revision
```

`learning_understanding` 保留当前快照：

```text
id
user_id
concept_id
summary
known_parts
misconceptions
uncertain_parts
latest_user_explanation
status
current_revision_id
updated_at
```

`understanding_revision` 保存历史：

```text
id
understanding_id
revision_no
summary
known_parts
misconceptions
uncertain_parts
user_explanation
status
trigger_evidence_id
created_at
```

规则：

```text
UNIQUE(understanding_id, revision_no)
```

---

## 5. D3：Evidence Source 多态引用规范

### 5.1 问题

当前：

```text
source_type
source_id
```

这种 polymorphic reference 很灵活，但数据库无法保证：

```text
source_id = 123
```

到底真的是：

```text
message.id
highlight.id
test_answer.id
```

之一。

### 5.2 决策

第一版不拆成：

```text
evidence_message
evidence_highlight
evidence_test_answer
```

而是保留统一表，但增加应用层校验和数据库唯一约束：

```text
UNIQUE(evidence_id, source_type, source_id)
```

字段：

```text
id
evidence_id
source_type    MESSAGE / HIGHLIGHT / TEST_ANSWER
source_id
created_at
```

说明：

```text
source_id 由应用层验证真实对象存在。
```

如果以后要特别强调数据库完整性，再拆表。

---

## 6. D4：Session 只做活动容器

### 6.1 问题

v2 已经把 Session 降成活动容器，这是对的。

但：

```text
session.agent_notes
```

仍然很容易慢慢变成：

> AI 把用户记住的东西全部写这里。

这是错误的。

### 6.2 职责划分

```text
Session
 ↓
临时上下文

Understanding
 ↓
知识点认知

Memory
 ↓
长期用户上下文
```

### 6.3 规则

- `agent_notes` 只记录当前 Session 的临时观察
- 长期信息必须进入 `Memory`
- 知识点认知必须进入 `Learning Understanding`
- Session 完成后，有价值的信息应被提取到 Understanding / Memory

参考 DeerFlow Harness：

> context management / memory 是 runtime 能力，而不是普通 thread note。

---

## 7. D5：MVP 技术栈渐进式

### 7.1 问题

完整架构可以保留：

```text
Spring Boot
MySQL
Redis
RabbitMQ
Python
Vector DB
Flutter
```

但 MVP 不应全部一开始上。

尤其：

```text
RabbitMQ
```

在只有一个人开发、只有一个 Python Agent 服务时，不是刚需。

### 7.2 MVP 架构

```text
Spring Boot
     ↓ HTTP
Python Agent
     ↓
MySQL
```

### 7.3 何时引入 RabbitMQ

当以下任务开始变成异步耗时任务后：

```text
Evidence extraction
Document parsing
Import
Embedding
笔记生成
思维导图生成
```

再引入：

```text
Java → RabbitMQ → Python
```

### 7.4 完整架构

```text
Flutter
   │
REST / SSE
   ↓
Spring Boot
   │
   ├────────── Redis
   │
   ├────────── MySQL
   │
   └────────── RabbitMQ
                   │
                   ↓
           Python Agent Runtime
                   │
       ┌───────────┼────────────┐
       ↓           ↓            ↓
    Memory      Evidence      RAG
    Service     Service       Service
```

---

## 8. 目标架构

本次决策后的核心模型：

```text
                 Conversation
                       │
                       ↓
                  Evidence
                       │
             ┌─────────┴─────────┐
             ↓                   ↓
      Understanding           Memory
       当前认知模型          长期用户上下文
             │                   │
             └─────────┬─────────┘
                       ↓
                Context Builder
                       ↓
                   AI Tutor
                       ↓
                  新的 Evidence
```

再加：

```text
Concept
   ↓
Understanding
   ↓
Learning State
```

其中：

```text
Learning State
= 派生状态 / 轻量状态
```

不是整个系统的中心。

---

## 9. 对数据模型的影响

新增 / 调整的表：

```text
learning_understanding      当前理解快照
understanding_revision      理解历史版本
memory                      Agent 长期记忆
memory_source               Memory 来源
evidence_source             Evidence 来源
learning_session            活动容器，不承担长期记忆
```

核心关系：

```text
MESSAGE
HIGHLIGHT
TEST_ANSWER
       │
       ↓
    EVIDENCE
       │
       ↓
┌──────┴────────┐
↓               ↓
UNDERSTANDING   MEMORY
↓               ↓
REVISION       MEMORY_SOURCE

UNDERSTANDING
      ↓
LEARNING_STATE
```

---

## 10. 对现有文档的影响

### README

定位更新为：

> 一个能够跨对话理解学习者认知过程，并基于历史理解持续调整教学方式的 AI Learning Agent。

### PRD

需要增加：

```text
Memory
Understanding Revision
Context Assembly
用户控制 Memory
Memory 生命周期
```

### ER 图

需要新增：

```text
understanding_revision
memory
memory_source
```

并调整：

```text
learning_understanding
learning_state
evidence_source
learning_session
```

---

## 11. 实施顺序

M1 的第一个重点不是把所有表一次建完。

而是先跑通最小闭环：

```text
Conversation
   ↓
Evidence
   ↓
Understanding Revision
   ↓
Memory
   ↓
下一次 Context Retrieval
```

验收标准：

### Case 1

第一次：

> 什么是 TLB？

用户解释：

> 它应该就是 Page Table 的缓存。

系统记录：

```text
Understanding(TLB)
```

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

## 12. 结论

本决策的核心是：

```text
不是继续给 PRD 加功能，
而是把模型固定在：

Conversation
→ Evidence
→ Understanding / Memory
→ Context Builder
→ Learning Agent
→ 新 Evidence
```

只要这条最小闭环成立，AI Learning Space 就成立。

否则即使有 RAG、知识图谱、RabbitMQ 和漂亮 Flutter UI，也只是一个复杂的 AI 聊天应用。

下一步：

```text
停止扩写 PRD
进入 M1
先跑通跨对话理解闭环
```

