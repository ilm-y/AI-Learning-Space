# AI Learning Space

> 一个能够跨对话理解学习者认知过程，并基于历史理解持续调整教学方式的 AI Learning Agent。

**Version:** 3.0  
**Status:** Development Baseline

---

## 1. 项目是什么

AI Learning Space 不是简单的 AI Chat，也不是把聊天记录做成 RAG。

它要解决的是：

> **AI 能不能逐渐理解“这个用户现在是怎么理解一个知识点的”，并在后续对话中真正用上这种理解。**

核心闭环：

```text
用户表达
   ↓
Evidence
   ↓
Learning Understanding
   ↓
Understanding Revision
   ↓
Long-term Memory
   ↓
下一次对话 Context
   ↓
个性化回答
   ↓
新的 Evidence
```

其中：

- **Conversation**：发生了什么
- **Evidence**：为什么可以这样判断
- **Learning Understanding**：用户目前怎么理解某个 Concept
- **Understanding Revision**：这种理解如何变化
- **Memory**：未来服务这个用户时值得长期记住什么
- **Learning State**：面向 UI 的轻量状态
- **Learning Session**：一次学习活动，不承担长期认知

---

## 2. 一个典型例子

第一次学习 TLB：

```text
用户：
“我理解 TLB 就是 Page Table 的缓存。”
```

系统不会只记录一条聊天消息。

它可能产生：

```text
Evidence
  observation:
  用户知道 TLB 与 Page Table 的缓存关系

Understanding(TLB)
  known_parts:
    - TLB 是 Page Table Entry 的缓存
  uncertain_parts:
    - TLB Miss 与 Page Fault 的关系
```

几天后：

```text
用户：
“Page Table 到底在哪里？”
```

Agent 检索：

```text
当前对话
+
TLB Understanding
+
相关 Memory
+
必要的 Knowledge
```

然后继续用户已有的认知，而不是重新从“什么是虚拟内存”讲起。

---

## 3. 核心设计原则

### 3.1 Understanding > Score

第一版不追求：

```text
mastery = 78%
confidence = 0.83
```

而记录：

```text
已理解
仍不确定
曾经混淆
用户自己的解释
```

---

### 3.2 Evidence > Guess

所有重要的学习判断都必须可以回到：

```text
Understanding
   ↓
Evidence
   ↓
Message / Highlight / Test Answer
```

---

### 3.3 Understanding ≠ Memory

```text
Understanding
= 用户对某个 Concept 的当前认知快照

Memory
= Agent 为未来服务这个用户而长期保存的信息
```

例如：

```text
Understanding:
用户已经知道 TLB 是页表缓存。

Memory:
用户偏好通过“CPU → MMU → TLB → Page Table”的执行路径理解 OS。
```

---

### 3.4 原始数据不能被学习分析覆盖

Evidence、Message、Highlight、Test Answer 是原始事实。

Understanding 和 Memory 都是可以修正的派生结果。

因此：

```text
删除 / 更新 Memory
≠
删除 Evidence

新的 Understanding
≠
覆盖历史 Revision
```

---

## 4. Agent 架构

```text
Flutter
   │
REST / SSE
   ↓
Spring Boot
   │
   ├── Business API
   ├── Context Assembly
   ├── Persistence
   └── Agent Gateway
            │
            ↓
      Python Learning Agent
            │
      ┌─────┼───────────┐
      ↓     ↓           ↓
    Tutor Retrieval  Analysis
                  ↓
        Evidence / Understanding
```

原则：

> **Spring Boot 管业务数据和数据库一致性，Python Agent 管推理。**

Agent 不直接修改 MySQL。

---

## 5. 为什么要把数据库一致性放在 Backend

尤其是 Evidence。

一个 Message 可能因为：

```text
重复消息
Agent retry
Worker retry
同一个历史消息再次分析
```

被多个 Agent Run 处理。

因此不能只依靠 Agent 自己“记得不要重复”。

必须：

```text
Agent
 ↓
返回 Evidence Candidate
 ↓
Spring Boot
 ↓
DB Unique Constraint
 ↓
INSERT / UPDATE
```

数据库是最终兜底。

---

## 6. Evidence 去重

每条 Evidence 有一个确定的：

```text
dedupe_key
```

由：

```text
user_id
concept_id
evidence_type
primary_source
normalized_observation
```

计算 SHA-256。

数据库：

```text
UNIQUE(dedupe_key)
```

因此：

```text
Run A
   ↓
Evidence X
   ↓
INSERT

Run B
   ↓
同一 Evidence X
   ↓
same dedupe_key
   ↓
数据库拒绝重复
   ↓
转为 upsert / attach source
```

应用层的 dedupe 只是优化。

**数据库唯一约束才是最终保证。**

---

## 7. Memory Scope

Memory 必须明确作用域。

支持：

```text
GLOBAL
SPACE
CONCEPT
```

例如：

```text
GLOBAL
用户喜欢详细解释。

SPACE: 操作系统
用户喜欢从硬件执行路径理解 OS。

CONCEPT: TLB
用户已经理解 TLB Hit / Miss。
```

这样不会再出现：

```text
learning_space_id = NULL
```

却因为 Agent 只查询当前空间而永远取不到的问题。

检索规则：

```text
当前用户
AND
(
  GLOBAL
  OR 当前 SPACE
  OR 当前 Concept
)
```

---

## 8. Learning Session

Session 只是学习活动容器：

```text
Session
 ├── Conversation A
 ├── Conversation B
 └── Test
```

长期认知不存 Session。

真正长期数据：

```text
Understanding
Memory
Evidence
```

---

## 9. 目录建议

```text
ai-space-learning/
├── README.md
├── PRD.md
├── DATABASE_AND_API.md
│
├── backend/
│   ├── user/
│   ├── space/
│   ├── conversation/
│   ├── learning/
│   └── agent/
│
├── agent/
│   ├── harness/
│   ├── context/
│   ├── tutor/
│   ├── analysis/
│   └── adapter/
│
├── evaluation/
│   ├── cases/
│   ├── bad_cases/
│   └── reports/
│
└── docs/
    ├── agent/
    ├── database/
    └── decisions/
```

---

## 10. MVP

第一版只验证：

```text
Conversation
   ↓
Agent
   ↓
Answer
   ↓
Evidence
   ↓
Understanding Revision
   ↓
Memory
   ↓
Cross-session Retrieval
```

先不要把：

```text
RabbitMQ
复杂知识图谱
复杂学习计划
完整 Import
完整 Export
多 Agent
```

放进第一条闭环。

---

## 11. MVP 验收

### Case 1：首次理解

用户：

> TLB 是什么？

形成：

```text
Understanding(TLB)
```

### Case 2：跨对话

新 Conversation 再问 TLB。

Agent 能找到历史 Understanding。

### Case 3：批注

用户：

> 我的理解是……

产生 Evidence。

### Case 4：重复分析

同一个 Message 被两个 Agent Run 分析。

数据库只产生一个逻辑 Evidence。

### Case 5：Memory Scope

GLOBAL Memory 必须在任意学习空间的新对话中可检索。

SPACE Memory 只能在对应 Space 中检索。

### Case 6：理解冲突

新证据与历史理解冲突：

```text
旧 Evidence 保留
新增 Evidence
新增 Revision
```

而不是覆盖旧数据。

---

## 12. 参考项目

重点研究顺序：

1. **LearningMAP**：学习状态、跨 session memory、adaptive loop
2. **LangGraph 官方文档**：thread/checkpoint/store、stateful workflow
3. **TutorAgent**：错误追踪、验证、学习行为测试
4. **Open WebUI**：Memory scope、CRUD、memory retrieval
5. **Letta**：stateful agent、长期 memory
6. **DeerFlow**：Harness / runtime / context management
7. **RAGFlow**：Document → Chunk → Retrieval / citation

参考这些项目的设计思想，不直接复制其完整架构。

---

## 13. 技术栈

| 层 | 技术 | 用途 |
|---|---|---|
| Client | Flutter | UI |
| Backend | Spring Boot 3.x | API / 数据 / 权限 |
| Agent | Python | Learning Agent |
| Database | MySQL 8 | 持久化 |
| Cache | Redis | Session / Cache / Lock |
| Agent Runtime | LangGraph / 自研 Harness | Agent execution |
| Vector DB | pgvector / Qdrant | Memory / Document retrieval |
| LLM | OpenAI-compatible API | Tutor / Analysis |
| MQ | RabbitMQ | 第二阶段异步化 |

---

## 14. 当前实现与验证

仓库里的代码已经跑通上面这条闭环（文档是设计基线，落地差异以各模块 README 为准）：

```text
backend/    Spring Boot REST / SSE / 事务写入 / Session / Memory 作用域
agent/      Python Chat（检索 + 上下文 + Tutor）与 Analyze（学习分析候选）
evaluation/ 离线 + 在线回归评测、Bad Case 数据集、报告
flutter/    客户端
scripts/    闭环回归脚本
docs/       PRD、架构分析、数据库/API 设计、Session 设计
```

```bash
# Agent 单测（无需数据库 / 网络 / API Key）
cd agent && python -m pytest -q

# 后端单测（无需 MySQL）
cd backend/backend && ./mvnw test

# Agent 回归评测：offline 不需要外部服务；all 需要 MySQL + 两个服务
python -m evaluation
python -m evaluation --mode all

# 真 MySQL 端到端闭环
bash scripts/regress-closed-loop.sh
```

当前结果：Agent 42 个单测、后端 54 个执行用例、评测 39 offline + 9 live 全绿。

---

## 15. 最重要的一句话

> **当前回答依赖 Conversation，个性化依赖 Understanding，长期连续性依赖 Memory，所有学习判断依赖 Evidence。**
