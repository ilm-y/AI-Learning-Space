# AI Learning Space —— 个性化 AI 学习与长期学习状态管理平台

## 产品需求文档（PRD）v1.0


## 一、项目简介

### 1.1 项目名称
**AI Learning Space**

### 1.2 一句话定位
> **一个以"学习状态"为核心、连接 AI 对话、知识库与长期学习过程的个性化 AI 学习系统。**

### 1.3 核心理念
市面上已有 ChatGPT、Claude、Gemini 等通用 AI 助手，它们擅长回答问题，但**不擅长记住"你学到什么程度了"**。

AI Learning Space 要解决的核心问题是：
> **把一次次零散的 AI 对话，沉淀为持续更新的个人学习状态。**

系统不仅保存"你和 AI 聊过什么"，还要逐渐形成：

> 我学过什么 → 我理解到什么程度 → 哪些地方理解错了 → 为什么判断我理解错了 → 下一步应该学什么

### 1.4 与竞品的本质区别

|            | ChatGPT / Claude         | AI Learning Space                     |
| :--------- | :----------------------- | :------------------------------------ |
| 核心资产   | 聊天记录（Conversation） | 学习状态（Learning State）            |
| 记忆方式   | 上下文窗口（临时）       | 结构化知识图谱 + 证据链（永久）       |
| 学习方式   | 被动回答                 | 主动验证 + 自适应推荐                 |
| 可解释性   | 黑盒                     | 证据可追溯（"为什么认为我掌握了80%"） |
| 跨模型迁移 | 不支持                   | 支持导入 ChatGPT/Gemini/Claude 历史   |


## 二、目标用户

### 2.1 主要用户画像
- **大学生**：学习计算机基础课程（操作系统、组成原理、数据结构）
- **程序员/自学者**：学习 Java、Redis、Kafka、Agent、C++ 等技术栈
- **长期备考者**：需要持续跟踪知识掌握情况

### 2.2 典型使用场景
1. 用户在学习"操作系统"时遇到 TLB 的问题，向 AI 提问
2. AI 回答后，系统自动分析用户的理解程度
3. 三天后，系统发现用户对"TLB Miss vs Page Fault"存在混淆
4. 系统主动推送一道验证题："TLB Miss 一定会触发 Page Fault 吗？"
5. 用户答错 → 系统标记 Misconception → 安排复习
6. 用户答对 → 系统更新 Learning State → 推荐下一个知识点

### 2.3 用户痛点
| 痛点                              | 解决方案                            |
| :-------------------------------- | :---------------------------------- |
| 学习过程碎片化，知识点无法串联    | 知识图谱 + 概念关系建模             |
| AI 不知道用户会到什么程度         | Learning State + Evidence 证据链    |
| AI 容易重复教学（每次都从零开始） | 基于状态的自适应内容推荐            |
| 多 AI 平台之间学习状态无法迁移    | 导入 ChatGPT/Gemini/Claude 历史对话 |


## 三、技术架构

### 3.1 总体架构图

```
┌─────────────────────────────────────────────────────────────┐
│                         Flutter                             │
│                    (iOS / Android / Web)                    │
└──────────────────────────┬──────────────────────────────────┘
                           │ REST / SSE
┌──────────────────────────▼──────────────────────────────────┐
│                      Spring Boot                            │
│  ┌────────────┬────────────┬────────────┬────────────────┐ │
│  │   Auth     │   User     │   Space    │  Conversation  │ │
│  │  JWT/BCrypt│  Profile   │   CRUD     │    Message     │ │
│  └────────────┴────────────┴────────────┴────────────────┘ │
│  ┌────────────┬────────────┬────────────┬────────────────┐ │
│  │  Document  │  Concept   │  Learning  │  Verification  │ │
│  │   / RAG    │   Graph    │   State    │                │ │
│  └────────────┴────────────┴────────────┴────────────────┘ │
└───────┬───────────────────────────────────────┬────────────┘
        │                                       │
┌───────▼───────────┐               ┌───────────▼────────────┐
│      MySQL        │               │         Redis          │
│  持久化数据存储    │               │  会话 / 缓存 / 分布式锁 │
└───────────────────┘               └────────────────────────┘
        │
┌───────▼───────────┐
│    RabbitMQ       │
│  异步任务队列      │
└───────┬───────────┘
        │
┌───────▼──────────────────────────────────────────────────────┐
│                 Python Agent Runtime                        │
│  ┌────────────────────────────────────────────────────────┐ │
│  │                    Supervisor                         │ │
│  │         ┌──────────┼──────────┐                     │ │
│  │         ▼          ▼          ▼                     │ │
│  │    Tutor      RAG Agent   Learning State            │ │
│  │    Agent                  Agent                     │ │
│  │                              │                      │ │
│  │                              ▼                      │ │
│  │                        Review Agent                 │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                           │
│  ┌──────────────────────────────────────────────────────┐ │
│  │              Harness / Agent Loop                    │ │
│  │  LLM Adapter │ Tool Registry │ Context Manager      │ │
│  │  Retry/Timeout │ Tracing │ Memory                   │ │
│  └──────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────┘
```

### 3.2 技术选型明细

| 层级           | 技术                            | 用途                                        |
| :------------- | :------------------------------ | :------------------------------------------ |
| **客户端**     | Flutter                         | 跨平台 UI（iOS/Android/Web）                |
| **后端框架**   | Spring Boot 3.x                 | 业务 API、认证、ORM                         |
| **数据库**     | MySQL 8.0                       | 持久化存储（User/Space/Conversation/State） |
| **缓存/会话**  | Redis                           | Session 缓存、分布式锁、Agent 临时状态      |
| **消息队列**   | RabbitMQ                        | 异步任务（证据分析、文档解析、导入处理）    |
| **AI Agent**   | Python (FastAPI) + LangGraph    | Agent Runtime、RAG、Learning State 分析     |
| **向量数据库** | pgvector / Milvus（待定）       | 文档 Embedding 存储与检索                   |
| **LLM**        | OpenAI API / 本地模型（可插拔） | 对话生成、证据提取、验证出题                |
| **对象存储**   | 本地文件系统 → MinIO/OSS        | 文档原始文件存储                            |
| **可观测性**   | 自研 Agent Trace                | 执行轨迹追踪、调试                          |

### 3.3 为什么用 RabbitMQ 而不是 Kafka？

| 对比维度       | Kafka                        | RabbitMQ（选用）                |
| :------------- | :--------------------------- | :------------------------------ |
| 设计哲学       | 日志存储，消费者拉取，可重放 | 任务分发，推模式，ACK 确认      |
| 适用场景       | 事件溯源、大数据流处理       | **业务解耦、异步任务派发**      |
| 运维复杂度     | 依赖 ZK/KRaft，调优复杂      | Docker 一键启动，管理 UI 友好   |
| Python 客户端  | kafka-python 性能一般        | pika 极其稳定成熟               |
| 与本项目匹配度 | 不需要重放历史事件           | 完美匹配"Java 派活给 Python 干" |

> 本项目的异步场景：对话分析、文档解析、导入处理 → 都是"派活"而非"事件溯源"，RabbitMQ 是更自然的选择。


## 四、核心功能模块

### 4.1 功能全景图

```
┌─────────────────────────────────────────────────────────────┐
│                    AI Learning Space                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │   首页       │  │  学习空间    │  │   AI 对话           │ │
│  │ 继续学习     │  │  知识图谱    │  │   普通提问           │ │
│  │ 空间列表     │  │  文档管理    │  │   标记重要/疑问      │ │
│  │ 全局进度     │  │  对话列表    │  │   我的理解           │ │
│  └─────────────┘  └─────────────┘  │   主动验证题         │ │
│                                     └─────────────────────┘ │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │  我的学习    │  │  导入对话    │  │  系统后台           │ │
│  │ 掌握度看板   │  │ ChatGPT     │  │  证据链追溯         │ │
│  │ 证据时间线   │  │ Gemini      │  │  学习状态聚合        │ │
│  │ 薄弱知识点   │  │ Claude      │  │  自适应策略引擎      │ │
│  │ 复习计划     │  │ Markdown    │  │  Agent 执行追踪      │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 详细功能说明

#### 模块一：首页仪表盘
> **目标**：让用户一眼看到"我该学什么"。

| 功能点       | 说明                                                         |
| :----------- | :----------------------------------------------------------- |
| 继续学习     | 系统根据 Learning State 推荐最高优先级的知识点，一键进入学习 |
| 学习空间列表 | 展示用户所有 Space（操作系统/Java/Kafka），带待复习数量角标  |
| 今日概览     | "今日已学 X 分钟"、"已掌握 X 个知识点"、"有 X 个误解待纠正"  |

#### 模块二：学习空间（Learning Space）
> **目标**：按科目/主题组织所有学习资产。

| 功能点       | 说明                                                         |
| :----------- | :----------------------------------------------------------- |
| 对话管理     | 在 Space 下创建/查看对话（一个 Space 可有多个 Conversation） |
| 文档管理     | 上传 PDF/Markdown/TXT，自动解析、切片、索引进入 RAG          |
| 知识图谱     | 自动提取 Concept 及关系，可视化展示（D3.js / ECharts）       |
| 学习状态总览 | 该 Space 下所有 Concept 的掌握度分布                         |

#### 模块三：AI 对话（核心交互）
> **目标**：不仅"聊"，还要"学"。

| 功能点     | 说明                                                         |
| :--------- | :----------------------------------------------------------- |
| 普通提问   | 用户提问，Tutor Agent 回答（流式 SSE）                       |
| 标记重要   | 选中 AI 回复中的文字 → 标记为 Important（存入 Highlight）    |
| 标记疑问   | 选中文字 → 标记为 Question（触发后续澄清）                   |
| 我的理解   | 选中文字 → 弹出输入框 → 用户用自己的话解释（**高权重证据**） |
| 主动验证   | 系统 Confidence 不足时，在对话流中插入验证题卡片（选择/判断） |
| 掌握度通知 | 验证完成后，系统在聊天流中插入"掌握度更新"消息               |

#### 模块四：我的学习（学习状态看板）
> **目标**：让学习状态"可见、可解释、可追溯"。

| 功能点     | 说明                                                        |
| :--------- | :---------------------------------------------------------- |
| 掌握度总览 | 全局雷达图/进度条，展示所有知识点掌握度                     |
| 知识点详情 | 点击任一 Concept → 显示 mastery/confidence/status 详情      |
| 证据时间线 | 按时间倒序展示所有 Evidence（含来源、正确性、推理质量）     |
| 薄弱知识点 | 按 priority 排序，列出待复习/待纠正的知识点                 |
| 学习计划   | 系统根据 Learning State 生成的长期学习计划（7天掌握 Kafka） |

#### 模块五：导入对话
> **目标**：打破 AI 平台壁垒，导入历史学习数据。

| 功能点   | 说明                                                         |
| :------- | :----------------------------------------------------------- |
| 支持格式 | ChatGPT Export JSON、Gemini Export、Claude Export、Markdown、TXT |
| 异步处理 | 上传后进入 RabbitMQ 队列，后台解析、提取概念、生成 Evidence、更新 State |
| 进度可见 | 导入任务状态：PENDING → PROCESSING → SUCCESS/FAILED          |


## 五、核心数据模型（15 张核心表）

> 完整 ER 图详见设计文档，此处列出核心实体及关系。

### 5.1 实体关系总览

```
User ─── owns ─── Learning Space
                         │
        ┌────────────────┼────────────────┐
        ↓                ↓                ↓
   Conversation      Document          Concept
        │                │                │
        ↓                ↓                ↓
     Message      Document_Chunk   Concept_Relation
        │                │                │
        └────────────────┼────────────────┘
                         ↓
                  Learning State
                         ↑
                         │
                  Learning Evidence
                   ↙         ↘
            Message     Verification_Answer
                         ↑
                  Verification_Task
```

### 5.2 核心实体说明

| 实体                    | 说明                     | 关键字段                                                     |
| :---------------------- | :----------------------- | :----------------------------------------------------------- |
| **User**                | 用户                     | id, username, password_hash, email                           |
| **Learning Space**      | 学习空间（原知识库）     | id, user_id, name, description                               |
| **Conversation**        | 对话                     | id, user_id, learning_space_id, title                        |
| **Message**             | 消息（一问一答）         | id, conversation_id, role, content                           |
| **Document**            | 文档元数据               | id, learning_space_id, name, storage_uri, status             |
| **Document Chunk**      | 文档切片                 | id, document_id, chunk_index, content                        |
| **Concept**             | 知识点（全局共享）       | id, learning_space_id, canonical_name, description           |
| **Concept Relation**    | 概念关系                 | id, source_concept_id, target_concept_id, relation_type      |
| **Learning State**      | 用户×知识点 掌握状态     | user_id, concept_id, mastery, confidence, status, next_review_at |
| **Learning Evidence**   | 证据链（可追溯）         | id, user_id, concept_id, source_type, observation, correctness, reasoning_quality |
| **Verification Task**   | 主动验证题               | id, user_id, concept_id, question_text, correct_answer, status |
| **Verification Answer** | 验证回答                 | id, task_id, user_answer, is_correct                         |
| **Highlight**           | 高亮标记                 | id, user_id, message_id, selected_text, type                 |
| **Import Task**         | 导入任务                 | id, user_id, learning_space_id, status, progress             |
| **Agent Run**           | Agent 执行轨迹（可观测） | id, user_id, agent_type, status, duration_ms                 |

### 5.3 证据权重设计（核心亮点）

> **不写死权重**，而是记录多维度属性，由 Evidence Aggregator 动态计算。

| 证据属性            | 说明                | 取值示例                                      |
| :------------------ | :------------------ | :-------------------------------------------- |
| `source_type`       | 来源类型            | MESSAGE / VERIFICATION / HIGHLIGHT / IMPORTED |
| `correctness`       | 正确性              | CORRECT / PARTIAL / INCORRECT                 |
| `reasoning_quality` | 推理质量（AI 评估） | 0.0 ~ 1.0（用户自由解释时通常更高）           |
| `specificity`       | 具体程度            | 0.0 ~ 1.0（泛泛而谈 vs 精准表述）             |
| `ai_confidence`     | AI 对这条观察的信心 | 0.0 ~ 1.0                                     |

**聚合策略**：`Evidence Aggregator` 综合以上属性，结合时间衰减、稳定性等因素，动态计算对 `mastery` 和 `confidence` 的贡献。


## 六、核心业务流程

### 6.1 学习闭环总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        学习闭环                                 │
│                                                                 │
│   ┌───────────┐                                                │
│   │  用户学习  │                                                │
│   └─────┬─────┘                                                │
│         │                                                      │
│         ▼                                                      │
│   ┌───────────┐     ┌───────────┐     ┌───────────┐          │
│   │ AI 对话   │────▶│  标记/理解  │────▶│   Evidence │          │
│   └───────────┘     └───────────┘     └─────┬─────┘          │
│                                               │                │
│   ┌───────────┐     ┌───────────┐     ┌─────▼─────┐          │
│   │ 继续学习  │◀────│  复习计划  │◀────│ Learning  │          │
│   └───────────┘     └───────────┘     │   State   │          │
│                                         └─────┬─────┘          │
│                                               │                │
│                                    ┌──────────▼──────────┐   │
│                                    │  Confidence 不足？   │   │
│                                    └──────────┬──────────┘   │
│                                        是     │     否       │
│                                    ┌─────────┘     └───────┐│
│                                    ▼                       ▼│
│                              ┌───────────┐       ┌─────────┐│
│                              │ 主动验证  │       │ 静默更新 ││
│                              │ (出题)    │       └─────────┘│
│                              └─────┬─────┘                  │
│                                    ▼                        │
│                              ┌───────────┐                  │
│                              │ 用户回答  │──────────────────┘│
│                              └───────────┘                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 主动验证流程（系统核心创新）

```
用户自由对话
      │
      ▼
Evidence Extractor 提取观察
      │
      ▼
Learning State Aggregator 尝试更新
      │
      ▼
┌─────────────────────────────────────┐
│  Confidence < 0.65 且 Mastery < 0.8 │ ← 策略引擎（可配置）
└─────────────────────────────────────┘
      │ 是
      ▼
生成 Verification_Task（选择题/判断题）
      │
      ▼
推送到 Flutter（聊天流中展示卡片）
      │
      ▼
用户回答
      │
      ▼
生成高权重 Verification_Evidence（权重动态计算）
      │
      ▼
强制更新 Learning State
      │
      ├── 答对 → Mastery ↑, Confidence ↑, next_review = 3天后
      │
      └── 答错 → 标记 Misconception, next_review = 明天
```


## 七、开发路线图（Milestone）

| 阶段                   | 周期 | 目标                                    | 产物                 |
| :--------------------- | :--- | :-------------------------------------- | :------------------- |
| **M0：设计**           | 1 周 | 完成 PRD、ER 图、API 设计、Agent 架构   | 设计文档             |
| **M1：Java 核心**      | 2 周 | User/Space/Conversation/Message CRUD    | Postman 可调通的 API |
| **M2：Agent Runtime**  | 2 周 | LLM Adapter、Agent Loop、Tool Registry  | Python Agent 骨架    |
| **M3：AI 对话**        | 1 周 | 打通 Flutter→Java→Python→LLM 链路       | 可对话的 MVP         |
| **M4：Learning State** | 2 周 | Evidence 提取、State 聚合、Concept 管理 | 学习状态闭环         |
| **M5：主动验证**       | 1 周 | Verification Task/Answer、策略引擎      | 完整验证闭环         |
| **M6：RAG**            | 2 周 | Document→Chunk→Embedding→Retrieval      | 文档检索能力         |
| **M7：异步增强**       | 1 周 | RabbitMQ 接入、Import 任务              | 异步处理能力         |
| **M8：Flutter 完整**   | 2 周 | 所有页面 UI + 联调                      | 可用的完整产品       |

**MVP 范围（M0-M4）**：对话 + Learning State + 继续学习，约 8 周。


## 八、项目亮点总结

### 8.1 技术亮点
1. **证据驱动的学习状态建模**：不存"拍脑袋的分数"，所有判断都有 Evidence 可追溯
2. **自适应验证策略**：Agent Confidence 不足时主动提问，形成"被动观察+主动验证"双通道
3. **自研 Agent Harness**：不依赖复杂框架，自己实现 Agent Loop + Tool Calling + Tracing
4. **异步解耦架构**：RabbitMQ 实现 Java ↔ Python 的可靠任务派发
5. **可观测性优先**：Agent Run 全链路追踪，调试效率极高

### 8.2 产品亮点
1. **打破 AI 平台壁垒**：支持导入 ChatGPT/Gemini/Claude 历史对话
2. **长期学习状态追踪**：跨对话、跨模型、跨设备的学习记忆
3. **可解释的学习评价**：点击任意掌握度，都能看到支撑它的证据链
4. **主动教学而非被动回答**：系统会在不确定时主动出题验证

### 8.3 能力互补（结合已有项目）

| 项目                  | 核心能力                                      | 与本次项目的关系      |
| :-------------------- | :-------------------------------------------- | :-------------------- |
| Mini-Raft             | C++ / Linux / OS / Distributed                | 系统层基础            |
| CVM 智能诊断          | Linux / Profiling / Agent / Tool              | Agent + Tool 实战经验 |
| **AI Learning Space** | Java / Redis / RabbitMQ / Agent / RAG / State | **后端 + AI 系统层**  |
| 真实设备项目          | Flutter / HarmonyOS / Device                  | 应用层交付            |


## 九、参考项目与致谢

本项目的架构设计参考了以下开源项目：

| 项目               | 借鉴内容                                         |
| :----------------- | :----------------------------------------------- |
| [LearningMAP][1]   | 知识图谱 → 诊断 → 自适应学习闭环                 |
| [DeerFlow][2]      | Harness、Agent Runtime、Memory、Tools 架构       |
| [RAGFlow][3]       | Document → Chunk → Indexing → Retrieval 分层设计 |
| [Dify][4]          | Knowledge Pipeline、Workflow、Agent 架构思想     |
| [Rabbithole][5]    | 文档内局部提问 → 深挖 → 持续学习交互模式         |
| ChatGPT Study Mode | 竞品能力边界研究（不正面竞争）                   |

[1]: https://github.com/ai-for-edu/LearningMAP
[2]: https://github.com/bytedance/deer-flow
[3]: https://github.com/infiniflow/ragflow
[4]: https://dify.ai
[5]: https://github.com/sigiuscom/rabbithole


## 十、附录

### A. API 接口清单（核心）

| 模块           | 方法 | 路径                                          | 说明            |
| :------------- | :--- | :-------------------------------------------- | :-------------- |
| Auth           | POST | `/api/auth/register`                          | 注册            |
| Auth           | POST | `/api/auth/login`                             | 登录            |
| Dashboard      | GET  | `/api/dashboard/continue-learning`            | 获取推荐知识点  |
| Dashboard      | GET  | `/api/dashboard/spaces`                       | 获取空间列表    |
| Space          | GET  | `/api/spaces/{id}`                            | 空间详情        |
| Space          | POST | `/api/spaces`                                 | 新建空间        |
| Space          | GET  | `/api/spaces/{id}/conversations`              | 对话列表        |
| Space          | POST | `/api/spaces/{id}/conversations`              | 新建对话        |
| Space          | GET  | `/api/spaces/{id}/documents`                  | 文档列表        |
| Space          | POST | `/api/spaces/{id}/documents`                  | 上传文档        |
| Space          | GET  | `/api/spaces/{id}/concepts`                   | 知识图谱数据    |
| Chat           | POST | `/api/conversations/{id}/messages`            | 发送消息（SSE） |
| Chat           | GET  | `/api/conversations/{id}/messages`            | 历史消息        |
| Highlight      | POST | `/api/messages/{id}/highlights`               | 标记高亮        |
| Verification   | GET  | `/api/verification/pending`                   | 待回答验证题    |
| Verification   | POST | `/api/verification/{id}/answer`               | 提交答案        |
| Learning State | GET  | `/api/learning-state/overview`                | 掌握度总览      |
| Learning State | GET  | `/api/learning-state/concepts/{id}`           | 知识点状态      |
| Learning State | GET  | `/api/learning-state/concepts/{id}/evidences` | 证据时间线      |
| Import         | POST | `/api/import`                                 | 导入对话        |
| Import         | GET  | `/api/import/{id}/status`                     | 导入进度查询    |

### B. 环境要求

| 组件     | 版本  | 备注            |
| :------- | :---- | :-------------- |
| JDK      | 17+   | Spring Boot 3.x |
| MySQL    | 8.0+  |                 |
| Redis    | 7.0+  |                 |
| RabbitMQ | 3.12+ |                 |
| Python   | 3.10+ | Agent Runtime   |
| Flutter  | 3.16+ |                 |


> **文档版本**：v1.0  
> **最后更新**：2026-09-09  
> **状态**：设计定稿，准备进入 M1 开发阶段