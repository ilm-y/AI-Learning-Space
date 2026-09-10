# AI Learning Space — Agent Architecture

> Version: 1.0
> Status: Development Baseline
> Purpose: 定义 Learning Agent 的运行模型、Context、Memory、Evidence、Understanding、Tools、生命周期与数据流。

---

# 一、设计目标

AI Learning Space 的 Agent 不是普通 Chatbot。

普通 Chatbot：

```text
User Message
     ↓
LLM
     ↓
Answer
```

AI Learning Space：

```text
User Message
     ↓
Context Retrieval
     ↓
理解用户当前认知
     ↓
组织回答
     ↓
产生新的 Evidence
     ↓
更新 Understanding
     ↓
更新 Memory
     ↓
影响未来对话
```

核心目标：

> **让 Agent 能够跨对话持续理解“用户现在是怎么理解知识的”，而不是只记住用户聊过什么。**

---

# 二、Agent 的边界

整个系统分成：

```text
                    Application
                         │
            ┌────────────┴────────────┐
            ↓                         ↓
       Spring Boot                Learning Agent
       Business API               AI Runtime
            │                         │
            ↓                         ↓
        MySQL / Redis          Tools / Retrieval
```

Spring Boot 负责：

```text
用户
学习空间
对话
消息
批注
测试
文件
权限
```

Learning Agent 负责：

```text
Context Assembly
Reasoning
Tutor Response
Evidence Extraction
Understanding Update
Memory Management
Verification Decision
```

原则：

> **业务数据由 Backend 管理，认知判断由 Agent 管理。**

Agent 不直接绕过 Backend 随意修改数据库。

Agent 应通过 Service / Tool 调用业务能力。

---

# 三、Agent 不等于 LLM

一个重要原则：

```text
LLM ≠ Agent
```

LLM 只负责：

```text
理解输入
生成文本
结构化判断
```

Agent 负责：

```text
什么时候检索
检索什么
什么时候调用工具
什么时候更新状态
什么时候需要验证
什么时候结束
```

因此：

```text
                 Learning Agent
                       │
          ┌────────────┼────────────┐
          ↓            ↓            ↓
       Context        LLM         Tools
       Builder                    /Services
```

---

# 四、Agent 的核心模型

Learning Agent 处理的不是一个简单 Conversation。

每次运行都包含：

```text
Current Context
+
Learning Context
+
User Memory
+
Knowledge
+
Evidence
```

最终形成：

```text
Agent Context
```

---

# 五、四类核心数据

## 5.1 Conversation

短期交互：

```text
当前正在发生什么？
```

来源：

```text
Message
```

特点：

* 高频
* 有序
* 当前上下文优先
* 可以被截断 / summary

---

## 5.2 Understanding

用户对 Concept 当前的认知：

```text
用户现在怎么理解这个东西？
```

例如：

```text
TLB

known:
- TLB 是页表缓存
- TLB Miss 后查询 Page Table

misconception:
- 曾经混淆 TLB Miss 与 Page Fault

uncertain:
- Page Table 本身的位置
```

这是 Agent 判断“应该怎么讲”的核心数据。

---

## 5.3 Memory

长期用户信息：

```text
未来是否值得记住？
```

例如：

```text
用户更喜欢从硬件执行路径理解 OS。
```

Memory 不等于 Understanding。

---

## 5.4 Evidence

为什么系统这样判断？

```text
Understanding
      ↑
   Evidence
      ↑
Message / Highlight / Test Answer
```

Evidence 是认知判断的依据。

---

# 六、Agent Context 层次

最终 Context：

```text
┌───────────────────────────────────┐
│ System Instructions               │
├───────────────────────────────────┤
│ Current Conversation              │
├───────────────────────────────────┤
│ Current User Input                │
├───────────────────────────────────┤
│ Relevant Understanding            │
├───────────────────────────────────┤
│ Relevant Memory                   │
├───────────────────────────────────┤
│ Relevant Knowledge                │
├───────────────────────────────────┤
│ Optional Evidence                 │
└───────────────────────────────────┘
```

优先级：

```text
Current User Input
      >
Current Conversation
      >
Current Understanding
      >
Relevant Memory
      >
Knowledge
      >
Old Evidence
```

---

# 七、为什么需要 Context Builder

不能：

```text
SELECT *
FROM message

SELECT *
FROM evidence

SELECT *
FROM memory
```

然后全部塞进 Prompt。

必须先：

```text
User Input
    ↓
Context Retriever
    ↓
Context Ranking
    ↓
Context Builder
    ↓
LLM
```

Context Builder 负责：

```text
1. 去重
2. 排优先级
3. 控制 Token
4. 处理冲突
5. 组织结构
```

---

# 八、Retrieval Architecture

系统拥有三个主要 Retrieval：

```text
                     Retrieval
                         │
          ┌──────────────┼──────────────┐
          ↓              ↓              ↓
     Knowledge       Understanding    Memory
     Retrieval        Retrieval       Retrieval
          │              │              │
          └──────────────┼──────────────┘
                         ↓
                    Evidence
                    Retrieval
                         ↓
                    Context Builder
```

---

# 九、Knowledge Retrieval

回答：

> “我需要参考什么知识？”

来源：

```text
Document
Document Chunk
Concept
```

典型方法：

```text
Semantic Search
Keyword Search
Concept Filter
Hybrid Retrieval
```

第一版：

```text
Vector Search + Concept Filter
```

即可。

---

# 十、Understanding Retrieval

回答：

> “这个用户以前是怎么理解这个概念的？”

输入：

```text
current_user_message
current_space
detected_concepts
```

检索：

```text
LearningUnderstanding
UnderstandingRevision
```

优先返回：

```text
当前 Concept
相关 Concept
最近 Revision
当前 misconceptions
当前 uncertain_parts
```

---

# 十一、Memory Retrieval

回答：

> “这个用户有什么长期信息值得影响当前回答？”

例如：

```text
学习偏好
长期背景
已经学过的主题
用户明确要求长期记住的信息
```

Memory 不应该全部注入。

必须：

```text
semantic retrieval
+
importance
+
scope
```

进行筛选。

LangGraph 官方目前也是类似地把 thread-level state 和跨 thread 的 long-term store 分开：前者负责当前交互，后者负责跨会话信息。

---

# 十二、Evidence Retrieval

通常不是每次回答都需要大量 Evidence。

只在：

```text
需要解释状态
需要判断是否矛盾
需要生成复习
需要主动验证
用户质疑 AI 判断
```

时召回。

例如：

```text
为什么认为用户已经理解 TLB？
```

Retrieval：

```text
Evidence #31
  ├─ Message #10
  ├─ Highlight #21
  └─ TestAnswer #7
```

---

# 十三、Agent Main Loop

核心 Agent Loop：

```text
User Message
     ↓
Create Message
     ↓
Detect Intent / Concepts
     ↓
Retrieve Context
     ↓
Build Context
     ↓
Generate Response
     ↓
Persist Assistant Message
     ↓
Schedule Learning Analysis
```

注意：

> **回答用户和学习分析不一定必须在同一个同步链路完成。**

这样用户不会因为 Evidence Extraction 慢而等待。

---

# 十四、同步路径

用户发送消息之后，必须立即完成：

```text
1. 保存 User Message
2. Context Retrieval
3. Tutor Response
4. 保存 Assistant Message
5. SSE 返回
```

路径：

```text
Flutter
   ↓
Spring Boot
   ↓
Learning Agent
   ↓
Retriever
   ↓
LLM
   ↓
Spring Boot
   ↓
SSE
   ↓
Flutter
```

---

# 十五、异步学习路径

回答完成之后：

```text
Assistant Message
       ↓
Learning Analysis Job
       ↓
Evidence Extraction
       ↓
Understanding Update
       ↓
Revision
       ↓
Memory Consolidation
       ↓
Learning State Update
```

第一版可以直接：

```text
Spring Boot
      ↓ HTTP
Python Agent
```

完成。

后续任务量增加后：

```text
Spring Boot
      ↓
RabbitMQ
      ↓
Python Worker
```

---

# 十六、为什么 Evidence Extraction 放到回答之后

因为：

> Tutor Response 的首要目标是帮助用户。

不是：

> 先花时间分析用户，再回答。

因此默认：

```text
Answer first
Analysis second
```

但如果当前对话触发了高价值验证需求，也可以在当前 Agent Loop 中同步决策。

---

# 十七、Evidence Extraction

输入：

```text
Current User Message
Recent Conversation
Relevant Understanding
```

输出结构：

```json
{
  "concept": "TLB",
  "observation": "用户能够解释 TLB Miss 后需要查询 Page Table",
  "correctness": "CORRECT",
  "reasoning_quality": 0.85,
  "specificity": 0.92,
  "ai_confidence": 0.88
}
```

注意：

这些分值是内部分析信号。

不要直接向用户展示。

---

# 十八、Understanding Updater

输入：

```text
Current Understanding
+
New Evidence
```

输出：

```text
No Change
Update
Conflict
```

### No Change

用户表达没有改变系统认知。

### Update

例如：

```text
unknown
→
known
```

### Conflict

例如：

```text
历史：
用户理解 TLB Miss ≠ Page Fault

当前：
“TLB Miss 就是 Page Fault”
```

不能直接覆盖。

必须：

```text
Conflict Evidence
      ↓
Further Verification
```

---

# 十九、Understanding Revision

每次真正改变 Understanding 时：

```text
Current Understanding
        ↓
Revision
        ↓
New Understanding
```

例如：

```text
Revision 1
status = EXPOSED

Revision 2
known = TLB cache

Revision 3
misconception = TLB Miss ≠ Page Fault

Revision 4
misconception resolved
```

这样可以追踪：

```text
用户是怎么学会的？
什么时候纠正错误？
为什么状态改变？
```

---

# 二十、Memory Consolidation

Evidence 不等于 Memory。

例如：

```text
用户今天说：
“我觉得用图比较好理解。”
```

这只是 Evidence。

不能立刻写：

```text
Memory:
用户永远喜欢图。
```

应：

```text
Candidate Memory
      ↓
Memory Reconciler
      ↓
ADD / UPDATE / IGNORE
```

---

# 二十一、Memory 类型

第一版：

```text
USER
LEARNING
PREFERENCE
```

### USER

长期事实。

### LEARNING

长期学习状态相关事实。

### PREFERENCE

用户明确或稳定表现出的偏好。

---

# 二十二、Memory Scope

Memory 可以属于：

```text
Global
Learning Space
Concept
```

例如：

```text
Global:
用户喜欢详细解释。

OS:
用户喜欢先看硬件执行路径。

TLB:
用户已经理解 TLB Hit / Miss。
```

---

# 二十三、Memory 冲突

永远遵循：

```text
当前用户明确表达
        >
历史 Memory
```

例如 Memory：

```text
用户不喜欢代码。
```

当前用户：

> “这次给我一份 C++ 实现。”

当前请求覆盖历史偏好。

不是删除 Memory。

只是：

```text
Current Context > Memory
```

---

# 二十四、用户修改 Memory

用户应该可以：

```text
查看
编辑
删除
```

Memory。

但：

```text
删除 Memory
```

不应该删除：

```text
Evidence
Conversation
Understanding
```

Memory 只是一个可修改的长期摘要。

---

# 二十五、主动验证

Verification 不是：

```text
confidence < X
```

就触发。

而是：

```text
Evidence Gap
      ↓
Verification Policy
```

判断：

```text
是否存在高价值认知缺口？
```

---

# 二十六、Evidence Gap

检测：

```text
NEW_CONCEPT
CONCEPT_CONFLICT
REPEATED_ERROR
CONTRADICTORY_EVIDENCE
OVERCONFIDENCE
LONG_UNVERIFIED
```

---

# 二十七、Verification Policy

输入：

```text
Gap Type
Current Conversation
User Experience
Recent Test
Learning State
```

输出：

```text
SKIP
ASK_NOW
ASK_LATER
```

不要默认打断用户。

---

# 二十八、Test Generation

如果：

```text
ASK_NOW
```

则：

```text
Generate Test
      ↓
Insert Test Event
      ↓
User Answer
      ↓
Evaluate
      ↓
Evidence
```

测试嵌入当前消息流。

---

# 二十九、Test Evaluation

### 客观题

```text
deterministic evaluation
```

### 简答题

```text
LLM evaluation
+
rubric
```

输出：

```text
CORRECT
PARTIAL
INCORRECT
```

---

# 三十、Test → Evidence

测试回答本身必须成为 Evidence。

例如：

```text
Test Answer #7
    ↓
Evidence #33
    ↓
Understanding
```

这会比普通聊天观察具有更高的验证价值。

但是：

> 不应该简单规定“所有测试证据权重永远高于所有对话证据”。

实际权重应该由 Evidence 类型、质量、具体程度和任务设计共同决定。

---

# 三十一、用户批注 → Evidence

用户主动写：

> 我的理解是……

应该成为强信号：

```text
Highlight
   ↓
Evidence
   ↓
Understanding
```

因为这是用户主动进行的知识重构。

---

# 三十二、用户纠错 → Evidence

如果用户说：

> 你这里说错了。

流程：

```text
User Correction
      ↓
Correction Evidence
      ↓
Re-evaluate
      ↓
Understanding Revision
```

Agent 必须允许：

```text
AI 判断错误
```

而不是强行维持旧状态。

---

# 三十三、Agent Tools

Learning Agent 需要以下工具：

```text
retrieve_understanding
retrieve_memory
retrieve_evidence
retrieve_knowledge

create_evidence

update_understanding
create_understanding_revision

create_memory_candidate
update_memory
archive_memory

detect_evidence_gap

create_test
evaluate_test_answer

update_learning_state
```

---

# 三十四、Tool 分层

```text
Read Tools
├── retrieve_understanding
├── retrieve_memory
├── retrieve_evidence
└── retrieve_knowledge

Write Tools
├── create_evidence
├── update_understanding
├── create_revision
└── update_learning_state

Memory Tools
├── create_memory_candidate
├── update_memory
└── archive_memory

Learning Tools
├── detect_gap
├── create_test
└── evaluate_answer
```

这样可以避免 Agent 无限制修改系统数据。

---

# 三十五、Agent 权限

Agent 不应该拥有：

```text
DROP
DELETE ALL
```

之类的能力。

建议：

```text
Create
Read
Update
Soft Archive
```

默认不做物理删除。

---

# 三十六、Agent Run

每次 Agent 执行都有：

```text
agent_run
```

例如：

```text
run #1024

type:
CHAT

status:
COMPLETED

duration:
4280ms
```

用于：

```text
Debug
Performance
Tracing
Evaluation
```

---

# 三十七、Agent Run 不等于 Reasoning Log

不要保存：

```text
LLM 内部 Chain of Thought
```

只保存：

```text
Input Summary
Output Summary
Tool Calls
Status
Duration
Error
```

如果需要调试，保存结构化事件：

```text
RETRIEVE_UNDERSTANDING
RETRIEVE_MEMORY
LLM_CALL
CREATE_EVIDENCE
UPDATE_UNDERSTANDING
```

而不是内部思维链。

---

# 三十八、推荐的 Runtime 状态

```text
AgentState

{
  user_id,
  space_id,
  conversation_id,
  session_id,

  user_message,

  detected_concepts,

  recent_messages,

  understandings,

  memories,

  evidences,

  knowledge,

  response,

  pending_tasks
}
```

注意：

`AgentState` 是运行时状态。

不是数据库里的长期实体。

---

# 三十九、Short-term State vs Long-term Memory

```text
AgentState / Checkpoint
        ↓
当前 Thread
        ↓
短期连续性

Memory Store
        ↓
跨 Thread
        ↓
长期连续性
```

LangGraph 官方当前也明确采用这种区分：checkpointer 保存 thread-scoped state，而 store 用于跨 thread 的持久信息。

因此：

```text
Learning Session
```

更接近：

```text
Thread / Activity
```

而不是：

```text
Long-term Memory
```

---

# 四十、Learning Session

Session 是：

> 一次学习活动的生命周期。

例如：

```text
Session #18
Topic = Virtual Memory
```

包含：

```text
Conversation
Test
Highlight
Agent Run
```

Session 完成后：

```text
Evidence
Understanding
Memory
```

留下长期结果。

---

# 四十一、完整运行链路

```text
                    USER
                      │
                      ↓
                 User Message
                      │
                      ↓
                  Backend API
                      │
                      ↓
               Learning Agent
                      │
             ┌────────┴────────┐
             ↓                 ↓
        Concept Detect     Session State
             │
             ↓
        Context Retriever
             │
     ┌───────┼────────┐
     ↓       ↓        ↓
Understanding Memory Knowledge
     │       │        │
     └───────┼────────┘
             ↓
       Context Builder
             ↓
            LLM
             ↓
         Tutor Reply
             ↓
        Assistant Message
             │
             ├──────────────→ SSE → User
             │
             ↓
      Async Learning Analysis
             ↓
       Evidence Extractor
             ↓
           Evidence
             ↓
    Understanding Updater
             ↓
          Revision
             ↓
     Memory Reconciliation
             ↓
      Learning State Update
```

---

# 四十二、为什么要异步分析

用户等待：

```text
Answer
```

应该尽可能短。

但：

```text
Evidence Extraction
Understanding Update
Memory Consolidation
```

不一定需要阻塞用户。

因此：

```text
Chat Path
```

与：

```text
Learning Analysis Path
```

逻辑解耦。

---

# 四十三、但不能让异步导致“下一条消息看不到刚刚的学习结果”

例如：

```text
用户：
解释 TLB

AI：
……

用户立刻：
那 TLB Miss 呢？
```

如果上一条 Evidence 尚未分析完成，第二条请求可能检索不到新 Understanding。

所以需要：

```text
Current Conversation
        >
Recent Local Observation
        >
Persistent Understanding
```

当前 Agent 可以读取：

```text
最近消息
```

弥补异步分析延迟。

---

# 四十四、Concurrency

同一个用户可能同时：

```text
Conversation A
Conversation B
```

进行学习。

因此 Understanding Update 必须避免：

```text
Revision A 覆盖 Revision B
```

建议：

```text
optimistic locking
```

或：

```text
version number
```

例如：

```text
understanding.version = 7
```

更新时：

```text
UPDATE ...
WHERE id = ?
AND version = 7
```

成功后：

```text
version = 8
```

---

# 四十五、Memory 更新也需要版本控制

Memory：

```text
version
```

避免：

```text
Agent A:
更新 preference

Agent B:
同时更新 preference
```

造成覆盖。

---

# 四十六、错误恢复

Agent 运行：

```text
LLM
 ↓
Tool A
 ↓
Tool B
 ↓
失败
```

不能让整个系统数据进入半完成状态。

建议：

```text
Agent Run
  ↓
Event
  ↓
Idempotent Write
```

尤其：

```text
create_evidence
update_understanding
create_revision
```

必须具备幂等性。

---

# 四十七、Idempotency

例如同一个：

```text
message_id = 100
```

不能因为 Worker 重试导致：

```text
Evidence #1
Evidence #2
Evidence #3
```

全部重复。

可以使用：

```text
source_type + source_id + observation_hash
```

作为去重依据。

---

# 四十八、Agent Evaluation

你的项目不能只测试：

> “能不能回答？”

必须测试：

### Memory Retrieval

```text
用户第二次问同一 Concept
→ 是否找到历史 Understanding？
```

### Personalization

```text
用户已经掌握 A
→ 是否跳过重复教学？
```

### Evidence

```text
用户批注
→ 是否形成 Evidence？
```

### Understanding

```text
用户纠正错误
→ 是否产生 Revision？
```

### Conflict

```text
新旧理解冲突
→ 是否保留两个 Evidence？
```

### Verification

```text
存在高价值缺口
→ 是否合理触发 Test？
```

---

# 四十九、最小 Agent Evaluation Set

建议至少准备：

```text
Case 01
首次学习

Case 02
跨对话召回

Case 03
用户正确解释

Case 04
用户错误解释

Case 05
用户主动批注

Case 06
用户纠正 AI

Case 07
历史与当前理解冲突

Case 08
重复错误

Case 09
长期未验证

Case 10
Memory 应新增

Case 11
Memory 应忽略

Case 12
Memory 与当前用户输入冲突
```

---

# 五十、第一版 Agent 不需要

不要一开始做：

```text
Multi-Agent
Self-Reflection 无限循环
自动 Skill Learning
复杂 Planning
自主长期任务
复杂 Subagent
```

第一版核心：

```text
Retrieve
→ Answer
→ Observe
→ Evidence
→ Understanding
→ Memory
```

---

# 五十一、参考架构来源

## LearningMAP

主要参考：

```text
student_memory
cross-session recall
adaptive learning loop
```

LearningMAP 当前仓库明确提供独立 `student_memory/` 服务，包括 retrieve / ingest 等能力，并把 Memory 用于跨 session 个性化。仓库还将完整工程结构、API 和架构说明放入单独 `ARCHITECTURE.md`。

---

## TutorAgent

主要参考：

```text
error tracking
repeated errors
knowledge graph
evaluation
session persistence
```

该项目当前还把学习分析、错误分类和行为测试作为独立工程能力。

---

## Open WebUI

主要参考：

```text
Memory
Memory types
Memory paths
Manual CRUD
Model-managed memory
```

当前 Open WebUI 的 Memory 支持 user/context 类型和 path/meta，并允许用户手动增删改；同时也支持模型通过工具主动管理 Memory。

---

## Letta

主要参考：

```text
Stateful Agent
Persistent Memory
Cross-conversation experience
Recall
```

Letta Code 当前明确把 agent identity、memory、experience 和 conversation history 分层，并通过 recall 访问历史经验。

---

## DeerFlow

主要参考：

```text
Harness
Runtime
Memory
Context Management
Tool Boundary
```

DeerFlow 2.0 当前将 Harness 定义为 Agent Runtime 层，并把 memory、tools、skills、context management 等作为运行环境的一部分；其后端还明确区分 Harness 和 App 的依赖边界。

---

# 五十二、最终原则

整个 Agent 只需要记住一句话：

> **回答依赖当前上下文，个性化依赖 Understanding，长期连续性依赖 Memory，判断必须依赖 Evidence。**

完整关系：

```text
Conversation
    ↓
Evidence
    ↓
Understanding
    ↓
Memory
    ↓
Future Context
    ↓
Future Answer
```

这就是 AI Learning Space 的 Agent Architecture。