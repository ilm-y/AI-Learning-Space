# AI Learning Space

> 一个能够跨对话理解学习者认知过程，并基于历史理解持续调整教学方式的 AI Learning Agent。

---

## 1. 项目简介

AI Learning Space 不是传统的 AI 聊天工具，也不是简单的 RAG 学习助手。

它真正要解决的问题是：

> **AI 能不能逐渐理解“我这个人现在到底是怎么理解一个知识点的”，并在下一次对话中真正用上这种理解？**

普通 AI 主要保存：

```text
用户问了什么
AI 回答了什么
```

AI Learning Space 希望进一步形成：

```text
用户学过什么
用户现在知道什么
用户是怎么理解的
用户哪里理解错了
用户哪里还不确定
用户自己是怎么解释的
这些判断来自哪些真实证据
下一次应该从哪里继续讲
```

核心不是“记录聊天”，而是：

```text
对话
 ↓
用户表达
 ↓
Evidence
 ↓
用户理解模型
 ↓
长期 Memory
 ↓
下一次个性化回答
```

---

## 2. 核心场景

例如用户正在学习操作系统。

用户第一次问：

> TLB Miss 和 Page Fault 到底有什么关系？

Agent 会回答问题，同时从用户的表达中发现：

```text
Concept: TLB

已理解：
- TLB 是页表项缓存
- 知道 TLB Hit / Miss

曾经混淆：
- TLB Miss 与 Page Fault

仍不确定：
- Page Table 本身的位置
```

用户随后批注：

> 我的理解是：TLB Miss 只是缓存没命中，所以还要去查 Page Table。

系统将这段批注记录为高价值 Evidence。

几天后，用户又问：

> Page Table 到底在哪里？

Agent 检索到：

```text
历史 Understanding
+
相关 Memory
+
过去的 Evidence
```

因此不会重新解释一遍虚拟内存，而会从用户已有理解继续：

> 你前面已经理解 TLB Miss 之后会继续查 Page Table。现在我们只需要补上一个问题：Page Table 本身存在哪里，以及它和 Page Fault 的关系。

这就是系统的核心体验：

> **不是记住你问过什么，而是记住你怎么理解。**

---

## 3. 核心模型

系统由四个核心认知层组成。

### 3.1 Evidence

Evidence 是系统判断学习者状态的原始依据。

来源包括：

```text
Message
Highlight
Test Answer
```

例如：

```text
用户原话：
“TLB Miss 就是 TLB 里面没有，所以要查 Page Table。”
```

---

### 3.2 Learning Understanding

Learning Understanding 表示：

> 用户当前对某一个 Concept 的结构化理解。

例如：

```text
TLB

summary:
用户理解 TLB 是 Page Table Entry 的缓存。

known_parts:
- TLB Hit
- TLB Miss
- TLB Miss 后查询 Page Table

misconceptions:
- 曾混淆 TLB Miss 与 Page Fault

uncertain_parts:
- Page Table 本身的位置
```

它不是简单的分数，而是一个“用户认知快照”。

---

### 3.3 Memory

Memory 表示：

> 为了让未来的 Agent 更好地理解这个用户，需要长期保存的信息。

例如：

```text
用户倾向通过“硬件实际发生什么”理解概念。
用户更喜欢从 CPU → MMU → TLB → Page Table
这样的因果链理解知识。
```

Memory 不代替 Understanding。

```text
Understanding
= 用户对知识点的理解

Memory
= Agent 对用户的长期上下文
```

---

### 3.4 Learning State

Learning State 是学习状态的轻量表示。

第一版只使用：

```text
UNKNOWN
EXPOSED
PARTIAL
DEMONSTRATED
MISCONCEPTION
```

不追求：

```text
mastery = 0.76
confidence = 0.83
```

这些数值以后再扩展。

---

## 4. Evidence 驱动

系统不会直接写：

```text
用户 TLB 掌握度 = 80%
```

而是：

```text
Evidence #31
用户正确解释 TLB Miss

Evidence #32
用户测试中答错 Page Fault

Evidence #33
用户批注中给出了自己的解释

        ↓

Understanding Updater

        ↓

Learning Understanding
```

所有重要判断都可以追溯到原始来源。

用户可以查看：

```text
为什么 AI 判断我已经理解？
            ↓
Learning Understanding
            ↓
Evidence
            ↓
Message / Highlight / Test Answer
```

---

## 5. 用户主动批注

对话中支持：

```text
⭐ 重要
❓ 疑问
🧠 我的理解
⚠️ 我可能错了
🔗 关联
```

其中：

### 我的理解

是最重要的一类学习数据。

因为：

```text
AI → 用户
```

只能证明用户看到了内容。

而：

```text
用户 → 自己解释
```

才能真正帮助系统判断用户是否理解。

---

## 6. 主动验证

系统不会简单地：

```text
confidence 低
↓
出题
```

而是检测：

```text
新概念
概念混淆
多次矛盾证据
过度自信
长期未验证
```

然后经过：

```text
Verification Policy
```

判断：

> 现在是否值得打断用户？

只有高价值认知缺口才插入测试。

测试仍然嵌入当前对话，而不是单独建立一个考试页面。

---

## 7. 检索架构

每次回答不是只做 RAG。

系统会分别检索：

```text
                 Retrieval
                     │
       ┌─────────────┼─────────────┐
       ↓             ↓             ↓
Knowledge       Understanding    Evidence
Retrieval        Retrieval        Retrieval
       │             │             │
       └─────────────┼─────────────┘
                     ↓
              Context Builder
                     ↓
                  Tutor
```

### Knowledge Retrieval

回答：

> 我应该参考什么知识？

数据来源：

```text
Document
Document Chunk
```

### Understanding Retrieval

回答：

> 我面对这个用户应该怎么讲？

数据来源：

```text
Learning Understanding
Memory
```

### Evidence Retrieval

回答：

> 为什么系统会这样判断？

数据来源：

```text
Learning Evidence
Evidence Source
```

---

## 8. Agent 架构

第一版不拆多个互相调用的 Agent。

使用：

```text
Learning Agent
```

通过 Tools / Services 获得能力：

```text
retrieve_understanding()
retrieve_memory()
retrieve_evidence()
retrieve_knowledge()

answer_user()

extract_evidence()
update_understanding()

detect_evidence_gap()
create_test()

update_learning_state()
```

即：

> 一个大脑 + 多个能力。

Agent Runtime 参考 DeerFlow 的 Harness 思路，将 Memory、Tools、Context 管理作为运行时能力，而不是把业务拆成大量独立 Agent。

---

## 9. 技术架构

### MVP

```text
Flutter
   │
REST / SSE
   ↓
Spring Boot
   │
HTTP
   ↓
Python Learning Agent
   │
   ↓
MySQL
```

### 完整架构

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

RabbitMQ 在异步任务变多以后使用，例如：

```text
对话后 Evidence 提取
文档解析
历史对话导入
Embedding
笔记生成
思维导图生成
```

---

## 10. 技术栈

| 层级          | 技术                     | 用途                          |
| ------------- | ------------------------ | ----------------------------- |
| Client        | Flutter                  | 跨平台客户端                  |
| Backend       | Spring Boot 3.x          | API、认证、业务               |
| Database      | MySQL 8                  | 核心数据                      |
| Cache         | Redis                    | Session / Cache / Lock        |
| Agent         | Python                   | Learning Agent                |
| Agent Runtime | LangGraph / 自研 Harness | Agent Loop / 状态             |
| Vector        | pgvector / Qdrant        | Understanding / Document 检索 |
| LLM           | OpenAI-compatible API    | Tutor / Extraction            |
| Queue         | RabbitMQ                 | 异步任务                      |
| Storage       | MinIO / OSS              | 原始文档                      |

---

## 11. 核心数据流

```text
用户发送消息
      ↓
创建 Message
      ↓
Learning Agent
      ↓
Retrieval
 ├─ Understanding
 ├─ Memory
 ├─ Evidence
 └─ Knowledge
      ↓
Context Builder
      ↓
Tutor Response
      ↓
写入 AI Message

异步：

Message / Highlight / Test Answer
      ↓
Evidence Extractor
      ↓
Evidence
      ↓
Understanding Updater
      ↓
Learning Understanding Revision
      ↓
Memory Consolidator
      ↓
Memory
```

---

## 12. 项目亮点

### 1. 用户理解模型

不只是知道：

> 用户学过 TLB。

而是知道：

> 用户已经理解 TLB Miss，但曾经把 TLB Miss 和 Page Fault 混淆。

### 2. 多来源证据链

一个判断可以同时由：

```text
Message
Highlight
Test Answer
```

共同支撑。

### 3. 跨对话个性化

下一次对话不是从零开始，而是基于：

```text
历史 Understanding
+
长期 Memory
+
历史 Evidence
```

继续教学。

### 4. 自适应验证

不是简单“置信度低就考试”，而是检测有价值的认知缺口。

### 5. 用户主动参与认知建模

尤其是：

```text
我的理解
我可能错了
```

这些显式信号会显著提高系统判断用户认知的质量。

---

## 13. 参考项目

| 项目        | 主要借鉴                                                  |
| ----------- | --------------------------------------------------------- |
| LearningMAP | adaptive learning loop、student memory、跨 session recall |
| TutorAgent  | 错误追踪、重复错误、知识图谱、学习过程日志                |
| Open WebUI  | Memory type、path、metadata、Memory CRUD                  |
| Letta       | Stateful Agent、persistent memory、跨 conversation        |
| DeerFlow    | Agent Harness、context management、tools / memory         |
| RAGFlow     | Document → Chunk → Retrieval、citation / traceability     |

LearningMAP 当前公开仓库已经包含独立的 `student_memory`，并将 memory 用于跨 session 的个性化学习。

Open WebUI 当前 Memory 数据模型也独立保存 `type / path / content / meta`，并将其作为用户长期上下文能力。

Letta 的设计直接把 stateful agent 定义为具有持续 identity 与 long-term memory、能够跨 conversations 延续的 Agent。

---

## 14. 开发阶段

### M0：设计

```text
PRD
ER
API
Agent Context Schema
Evidence Schema
```

### M1：核心后端

```text
User
Space
Conversation
Message
Highlight
```

### M2：Learning Agent

```text
LLM Adapter
Agent Loop
Context Builder
Tool Calling
```

### M3：真正的跨对话理解

```text
Evidence
Understanding
Understanding Revision
Memory
```

### M4：个性化回答闭环

```text
Retrieve
→ Answer
→ Observe
→ Update
→ Retrieve Again
```

### M5：主动验证

```text
Evidence Gap
→ Verification Policy
→ Test
→ Evidence
→ Understanding
```

### M6：RAG

```text
Document
→ Chunk
→ Embedding
→ Retrieval
```

### M7：异步系统

```text
RabbitMQ
Import
Background Extraction
Embedding
```

### M8：完整客户端

```text
书桌
学习空间
对话
状态
知识图谱
批注
导出
```

---

## 15. MVP

第一版真正需要证明的不是“我做了多少页面”。

而是下面这个闭环真的成立：

```text
第一次对话
    ↓
用户表达
    ↓
系统形成 Understanding
    ↓
用户批注
    ↓
产生 Evidence
    ↓
跨对话
    ↓
Retriever 找回历史 Understanding
    ↓
AI 根据用户已有理解回答
    ↓
产生新的 Evidence
    ↓
Understanding 更新
```

如果这个闭环成立，AI Learning Space 就已经成立。

否则即使有 RAG、知识图谱、RabbitMQ 和漂亮 Flutter UI，也只是一个复杂的 AI 聊天应用。

---

## 16. License

MIT

## 文档
- [产品需求文档](docs/prd.md)
- [ER 图与数据模型](docs/er.md)
- [核心模型设计决策](docs/database&API_design.md)
- [数据库与 API 设计](docs/database&API_design.md)
- [Agent 架构](docs/agent-architecture.md)