# AI Learning Space — Backend

Spring Boot 3.3.5 / Java 17 / MyBatis-Plus / MySQL 8 的 REST 服务，实现了「AI 学习空间」的核心学习闭环。

本文档说明后端的架构、每个目录的职责、三种写入语义、SSE 事件契约，以及当前哪些能力已经写实、哪些还是骨架。

---

## 一、它在整个系统里的位置

产品的核心模型是这条环：

```text
Conversation → Evidence → Understanding(+Revision) / Memory → LearningState → 未来的 Context
```

- 用户在与 AI 的 **Conversation** 里边学边聊；
- 系统从对话、批注、测试回答中抽取 **Evidence**（证据）；
- 证据不断推进 **Understanding**（用户当前怎么理解这个概念），每次真正变化留下一条 **Revision**；
- 同时沉淀 Agent 的长期上下文 **Memory**；
- **LearningState** 是 Understanding 的物化投影，用来回答「现在最该关注什么」。

后端就是这条环的**唯一权威实现**。Python Agent Runtime（尚未接入）负责「理解自然语言、判断学得怎么样」，它产出的结论必须回到后端走同一条写入链路，不存在第二条改状态的捷径。

**后端不做什么**：不做任何掌握度打分。你看不到 `mastery` / `confidence` / `stability` 这类数字 —— 状态是 5 个离散值（`UNKNOWN / EXPOSED / PARTIAL / DEMONSTRATED / MISCONCEPTION`），想知道依据就去翻 Evidence 和 Revision。

---

## 二、数据模型：谁是谁的 source of truth

22 张表，按职责分四层：

```text
┌─ 身份与容器 ────────────────────────────────────────────────┐
│  user                                                       │
│  learning_space ── concept ── concept_relation              │
│  conversation ── message ── highlight                       │
│  learning_session    document ── document_chunk              │
│  learning_plan       import_task                             │
└─────────────────────────────────────────────────────────────┘
                            │ 用户的行为
                            ▼
┌─ 证据层（append-only，永不修改、永不删除）──────────────────┐
│  learning_evidence ── evidence_source                       │
└─────────────────────────────────────────────────────────────┘
                            │ 证据推进理解
                            ▼
┌─ 理解层（真相在这里）───────────────────────────────────────┐
│  learning_understanding        ← 当前快照，唯一真相          │
│  understanding_revision        ← 不可变的历史版本            │
│  memory ── memory_source       ← Agent 长期上下文            │
└─────────────────────────────────────────────────────────────┘
                            │ 单向派生
                            ▼
┌─ 投影层（只读派生，没有独立写入入口）──────────────────────┐
│  learning_state                                             │
│  test_task ── test_answer      ← 验证手段，判分结果回流证据层│
└─────────────────────────────────────────────────────────────┘
                            ▲
                            │ 执行轨迹
              agent_run  ───┘
```

三条关键关系，记住这三条就不会搞错：

| 关系                     | 含义                                                                      |
| ------------------------ | ------------------------------------------------------------------------- |
| **Understanding 是真相** | 「用户现在怎么理解这个概念」只有这一个答案，就在 `learning_understanding` |
| **LearningState 是投影** | 它不产生判断，只把 `understanding.status` 复制过来，方便筛选与推荐        |
| **Evidence 是依据**      | append-only。证据冲突时不是改掉旧的，而是再追加一条                       |

`test_task` / `test_answer` 属于「验证手段」而不是「结论」：判分结果会作为一条 Evidence 流回证据层，再走同一条链路推进理解。

---

## 三、目录结构

```text
src/main/java/com/ails/backend/
├── BackendApplication.java     启动类
├── agent/                      ★ Backend 与 Agent Runtime 的边界
├── common/                     统一响应体、业务异常、全局异常处理
├── config/                     Security / MyBatis-Plus / 字段自动填充 / 线程池
├── controller/                 REST 入口（17 个：12 个写实，5 个骨架）
├── dto/                        请求与响应对象（按领域分包）
├── entity/                     22 张表对应的实体
├── enums/                      全部枚举（与 MySQL ENUM 值一一对应）
├── mapper/                     MyBatis-Plus Mapper，全部是裸 BaseMapper
├── model/                      领域值对象与不依赖数据库的规则（误区生命周期）
├── security/                   JWT 过滤器与鉴权失败处理
├── service/                    业务逻辑与写入语义
└── utils/                      JWT / 安全上下文 / JSON 工具

src/main/resources/
├── application.yml
├── schema.sql                  ★ 建表（幂等，每次启动执行）
└── schema-dev-reset.sql        ★ 清库（手工执行，绝不自动跑）
```

### `agent/` — 与 Agent Runtime 的边界

整个后端只有这一个地方「知道将来要接 Python」：

```text
ChatController → ChatService → AgentGateway ─┬→ StubAgentGateway       （现在）
                                             └→ HttpPythonAgentGateway  （将来）
```

- `AgentGateway` —— 唯一的接口定义点：`streamReply(request, handler)`
- `AgentStreamHandler` —— `onDelta` / `onComplete` / `onError` 三个回调，把「怎么推」和「推到哪」解耦
- `StubAgentGateway` —— 桩实现，把用户输入回显成引导语并切块推出

因为有了这层，`ChatController` 里**不存在**任何「以后要调 Python Agent」的注释。将来接入 Python，只需要新增一个实现类，Controller 与 Service 一行都不用改。

### `common/` — 统一响应

所有非流式接口都返回 `ApiResponse<T>{ code, message, data }`，`code = 0` 表示成功，否则是 HTTP 风格错误码。

`GlobalExceptionHandler` 负责把异常翻译成响应：`BusinessException` 按其 `code` 设置 HTTP 状态；参数校验失败拼成 `字段名 + 提示`；请求体解析失败（含枚举值不合法）返回 400；兜底 500 只回「服务器内部错误」，堆栈只进日志。

`dto/` 里每个请求对象用 `jakarta.validation` 标注**中文**提示，前端可以直接展示。

### `config/`

| 类                         | 作用                                                                   |
| -------------------------- | ---------------------------------------------------------------------- |
| `SecurityConfig`           | `/api/auth/**` 与 OPTIONS 放行，其余需要认证；无状态；CORS 全开        |
| `MybatisPlusConfig`        | `@MapperScan("com.ails.backend.mapper")`，所以 Mapper 都不加 `@Mapper` |
| `MyBatisMetaObjectHandler` | 自动填充 `createdAt` / `updatedAt` / `answeredAt`                      |
| `AsyncConfig`              | `chatStreamExecutor` 线程池，SSE 生成不占用 Tomcat 请求线程            |

### `entity/` 与 `mapper/`

实体与表一一对应，`mapper/` 里全部是：

```java
public interface XMapper extends BaseMapper<X> {
}
```

复杂查询都在 Service 里用 `LambdaQueryWrapper` 拼，Mapper 保持裸接口 —— 需要加自定义 SQL 时再开对应方法，不要提前建 XML。

### `model/` — 领域值对象与纯规则

存放**不属于某一张表、但属于领域本身**的东西：

| 类                       | 作用                                                                             |
| ------------------------ | -------------------------------------------------------------------------------- |
| `Misconception`          | 当前快照里的一个误区条目（存进 `learning_understanding.misconceptions` 的 JSON） |
| `MisconceptionLifecycle` | 误区的状态迁移规则，**纯函数、不碰数据库**，所以可以脱离 Spring 与 MySQL 单测    |

把误区规则放在这里而不是塞进 `UnderstandingService`，是为了让它可测、可改：将来把
「两次独立确认」升级成「一次高质量解释 + 一次测试正确」，只改这一个类。

### `dto/`

按领域分包：`auth / user / space / concept / conversation / highlight / understanding / memory / test / state / agent`。

统一风格：Java `record` + 静态 `from(...)` 工厂（带空值守卫，返回 `null` 而不是抛异常）。JSON 列在实体里是 `String`，出参时用 `utils/JsonUtil` 转成 `List<String>` 或 `List<Misconception>`。

`test/` 包替代了 v1 的 `verification/`；`state/` 里的 `ContinueLearningItem` 已被 `AttentionItem` 与 `TopicCoverageItem` 取代。

---

## 四、三种写入语义

这是整个后端最重要的约定。三个 Service 各管一种语义，不要混用。

### 1. EvidenceService —— append-only

`learning_evidence` 只会被追加。**没有 update，没有 delete，也没有对应的 REST 入口。**

证据冲突时的正确做法是「再加一条」：用户上次理解错了、这次理解对了，两条证据都在，Revision 历史完整记录了判断是怎么变的。删掉旧证据等于篡改学习历史。

**关于幂等。** 目标不是「一个来源只能产生一条证据」—— 同一个 Message 本来就可以为不同 Concept 各产生一条 Evidence。真正要防的是**同一次 analysis run / 同一个 analyzer 版本重复写入相同观察**，所以按两个键查重：

```text
agent_run_id + concept_id + observation_hash
source_type + source_id + concept_id + analyzer_version + observation_hash
```

数据库侧另有 `UNIQUE(agent_run_id, concept_id, observation_hash)` 兜底；MySQL 唯一索引允许多个 NULL，所以没有 run 上下文的手工写入不受该约束限制。

### 2. UnderstandingService —— 快照 + 版本

`learning_understanding` 是**可变的当前快照**，但每次**真正发生变化**都必须写一条 `understanding_revision` 并改写 `current_revision_id`。

- 没有变化就不留 Revision —— Revision 的数量等于「理解真正演进过的次数」
- 第一次创建初始 UNKNOWN 快照**不算变化**，所以打完第一个批注后 revisions 里只有 Revision 1
- 同样没有删除入口：理解只能被新证据推进，不能被直接改写

快照里只有 `known_parts / uncertain_parts` 是「只增不减」的清单。**`misconceptions` 不是** —— 它由
`model/MisconceptionLifecycle` 管理生命周期，见下面「误区不是历史，是当前状态」。

### 3. MemoryService —— 生命周期，不是删除

```text
CANDIDATE（Agent 推断，待确认） → ACTIVE（生效） → ARCHIVED（归档）
```

用户自己创建的 Memory 直接落 `ACTIVE`（这是他说的）；AI 推断的默认只能停在 `CANDIDATE`，要用户确认才升上去。对外那个 `DELETE /api/memories/{id}` 内部是**归档**，不触碰 Evidence 与 Understanding。

---

## 五、一次分析跑完整条链路

`LearningAnalysisService` 是编排者。顺序本身就是语义，不能调换：

```text
1. getOrCreateUnderstanding(user, concept)   不存在则建 UNKNOWN 快照
2. Evidence                                     append-only + 查重
3. Understanding Update                         当前快照
4. INSERT UnderstandingRevision                 历史版本
5. UPDATE current_revision_id                   指向最新版本
6. LearningState Projection                     物化投影
```

第 1 步必须在第 2 步之前 —— Evidence 要挂 `understanding_id`，快照得先存在。

整段由一条 `agent_run` 串起来（`run_type = EXTRACTION`），它的主键就是 Evidence 去重用的 `agentRunId`。

### 证据从哪来

| 来源                                | 映射                                            |
| ----------------------------------- | ----------------------------------------------- |
| Message 提到某个 Concept            | `UNKNOWN` —— 只说明接触过，状态推进到 `EXPOSED` |
| Highlight `MY_UNDERSTANDING`        | `CORRECT` —— 用户自己的解释，最强信号           |
| Highlight `MISCONCEPTION_CANDIDATE` | `INCORRECT`                                     |
| Highlight `QUESTION`                | `UNKNOWN` + 需要跟进                            |
| Highlight `IMPORTANT`               | 不产生证据（普通标记）                          |
| Highlight `CONCEPT_LINK`            | 不产生证据（只建概念关联）                      |
| TestAnswer 判分                     | `CORRECT` / `PARTIAL` / `INCORRECT`             |

**返回 0 条证据是完全正常的。** 普通闲聊不涉及任何已注册 Concept 时，`analyzeMessage` 返回空列表，不写证据、不动理解。

### 状态怎么派生

状态由**快照内容**派生，而不是由单条证据直接决定 —— 否则一条 `INCORRECT` 就能把长期积累的理解抹掉：

```text
存在未解除的误区               → MISCONCEPTION
knownParts ≥ 2 且 uncertain 空 → DEMONSTRATED
knownParts 非空                → PARTIAL
有证据但没形成判断             → EXPOSED
完全没有证据                   → UNKNOWN
```

### 误区不是历史，是当前状态

这是最容易搞错的一点：

```text
曾经答错        ≠  现在仍然不会
曾经出现误区    ≠  当前永远 MISCONCEPTION
```

所以 `learning_understanding.misconceptions` 里存的**只是当前仍然成立的误区**，每条带自己的状态：

```text
               错误证据
                  ↓
              ┌────────┐
              │ ACTIVE │  当前存在这个误区
              └───┬────┘
                  │  反向正确证据（CORRECT + 推理质量达标）
                  ↓
              ┌────────┐
              │PENDING │  可能正在解除，还没被独立确认
              └───┬────┘
                  │  再次独立确认
                  ↓
               移除      视为已解除
```

- **解除不是一条 `RESOLVED` 记录，而是条目从当前快照里消失。** 当前快照只回答「现在还存在什么误区」，所以 `misconceptions: []` 是干净的。要知道「曾经有过什么误区、什么时候被纠正的」，去看 `GET /api/understandings/{conceptId}/revisions` 和 Evidence：**历史一条都没删。**
- **一次答对只说明「这一次答对了」**，所以第一次反向证据先把误区降到 `PENDING`（状态仍是 `MISCONCEPTION`），第二条独立的正确证据才解除。证据是按 source 去重的，两条不同证据天然来自两次不同的 Message / Highlight / TestAnswer。
- **「几次算解除」写在 `MisconceptionLifecycle` 的常量里，不是数据库字段**，也不是快照里的计数 —— 快照存的是采信过的证据 id，将来换成更细的规则不需要动数据模型和已有历史。
- 解除之后如果又出现错误证据，会重新激活为 `ACTIVE`（按新一轮重新计时）。
- 已知简化：本地规则版分析器分不清一条正确证据反驳的是**哪一个**误区，因此对它一视同仁地计一次确认；接入 Agent 后可以由 Agent 指明针对的是哪一条。

`learning_state` 只是把 `understanding.status` 复制过去，并按状态派生出复习间隔与优先级：

| 情形           | 优先级 | 复习间隔 | attention 理由            |
| -------------- | -----: | -------: | ------------------------- |
| 误区 `ACTIVE`  |      9 |     1 天 | `MISCONCEPTION`           |
| 误区 `PENDING` |      6 |     2 天 | `MISCONCEPTION_RESOLVING` |
| `PARTIAL`      |      5 |     3 天 | 到期后 `NEEDS_REVIEW`     |
| `EXPOSED`      |      4 |     2 天 | 到期后 `NEEDS_REVIEW`     |
| `UNKNOWN`      |      3 |     1 天 | 到期后 `NEEDS_REVIEW`     |
| `DEMONSTRATED` |      1 |     7 天 | 到期后 `NEEDS_REVIEW`     |

所以「第一次发现错误 → 红色重点关注；开始答对 → 仍然关注但降级；确认已经掌握 → 从 attention 消失」，
不会出现一条老误区把概念永久钉在最高优先级的情况。

`ACTIVE` 还是 `PENDING` 属于 Understanding，`learning_state` 表里**没有**冗余这一列 ——
投影只把结论落到 `review_priority`（9 / 6），attention 的推荐理由回读快照现算。

> 当前的「抽取」是**本地规则版**（按 Concept 名称匹配文本 + 批注类型映射），分析器版本号 `rules-v1`。接入 Python Agent 后由 Agent 的分析结果替换，链路与写入语义不变。

---

## 六、SSE 事件契约

`POST /api/conversations/{id}/messages` 返回 `text/event-stream`，四类事件：

```text
message.start   { runId, conversationId }
message.delta   { delta }
message.end     { messageId, runId }
message.error   { runId, code, message }
```

- **`runId` 从第一个事件起就存在** —— 它是一条 `agent_run`（`run_type = CHAT`）的主键，状态走 `RUNNING → COMPLETED / FAILED`。前端拿到 `runId` 就能追溯这次请求。
- 用户消息在请求线程里**同步落库**（保证 `GET /messages` 立刻看得到、序号按请求顺序分配），生成与推送在 `chatStreamExecutor` 线程池里跑。
- 对话结束后会静默跑一次消息分析；**分析失败不影响已经推完的对话** —— 用户已经拿到回复了，回滚没有意义。
- 前端必须用支持流式的 HTTP 客户端（Flutter 侧是 `http` 的 `StreamedResponse`），普通 `fetch` 拿不到中间态。

**关于 `sequence_no`：** 不能写成 `SELECT MAX(sequence_no) + 1`，两个并发请求会读到同一个最大值然后撞号。实现是先在事务里 `SELECT ... FOR UPDATE` 锁住所属 conversation 那一行，把同一对话的写入串行化；数据库侧另有 `UNIQUE(conversation_id, sequence_no)` 兜底，真撞上会直接报错而不是静默错序。

---

## 七、REST API 一览

### 已实现

| 域       | 接口                                                                                                                             |
| -------- | -------------------------------------------------------------------------------------------------------------------------------- |
| 认证     | `POST /api/auth/register`、`POST /api/auth/login`                                                                                |
| 用户     | `GET/PUT /api/user/me`、`PUT /api/user/me/password`                                                                              |
| 空间     | `GET/POST /api/spaces`、`GET/PUT/DELETE /api/spaces/{id}`                                                                        |
| 知识点   | `GET/POST /api/spaces/{spaceId}/concepts`、`GET/PUT/DELETE /api/concepts/{id}`                                                   |
| 对话     | `GET/POST /api/spaces/{spaceId}/conversations`、`GET/PUT/DELETE /api/conversations/{id}`、`POST /api/conversations/{id}/archive` |
| 对话流   | `GET /api/conversations/{id}/messages`、`POST /api/conversations/{id}/messages`（SSE）                                           |
| 批注     | `POST /api/messages/{messageId}/highlights`、`GET /api/messages/{messageId}/highlights`、`GET /api/highlights`                   |
| 理解     | `GET /api/understandings?spaceId=`、`GET /api/understandings/{conceptId}`、`GET /api/understandings/{conceptId}/revisions`       |
| 证据     | `GET /api/understandings/{conceptId}/evidence`、`GET /api/evidence/{evidenceId}`                                                 |
| 记忆     | `GET/POST /api/memories`、`GET/PATCH/DELETE /api/memories/{id}`、`GET /api/memories/search`                                      |
| 测试     | `POST /api/tests`、`GET /api/tests/pending`、`GET /api/tests/{testId}`、`POST /api/tests/{testId}/answer`                        |
| 学习状态 | `GET /api/learning-state/overview`、`/concepts/{conceptId}`、`/attention`、`/topic-coverage?spaceId=`                            |

`/api/learning-state/*` **只有查询接口**。LearningState 是投影，没有「改状态」的入口 —— 想让它变，就去产生新证据。

`/api/understandings/*` 与 `/api/evidence/*` 同样**只有读接口**，没有删除或改快照的路径。

### 骨架（返回 501）

`document` / `export` / `imports` / `learning-plans` / `sessions` 五个域的 Controller 已经占住路由，但直接返回 `501` 并说明「尚未接入」。

这是有意的：**假装成功比明确说没做更浪费时间**。前端联调时能立刻分辨「这个接口还没做」和「这个接口出错了」。表结构（`document` / `document_chunk` / `import_task` / `learning_plan` / `learning_session`）已经在位。

---

## 八、四处对设计文档的刻意加强

这四处偏离了 `database&API_design.md` 的列清单，都是有意的，理由如下。

### 1. `message` 加了唯一约束

```sql
UNIQUE KEY `uk_msg_conv_seq` (`conversation_id`, `sequence_no`)
```

文档只写了普通索引。但 `sequence_no` 是并发写入的，没有唯一约束的话撞号会**静默**发生 —— 消息顺序错乱且难以排查。加上约束后，真撞上会直接报错。

### 2. `learning_evidence` 加了分析去重列

```sql
`agent_run_id`      BIGINT       NULL,
`analyzer_version`  VARCHAR(64)  NULL,
`observation_hash`  VARCHAR(64)  NULL,
UNIQUE KEY `uk_evidence_dedupe` (`agent_run_id`, `concept_id`, `observation_hash`)
```

文档的 Evidence 表没有足够信息表达「这条证据是哪次分析产生的」，无法做到按 run / analyzer 版本去重。详见第四节。

### 3. 软删除只覆盖用户内容，级联收敛

`deleted_at` **只加在 4 张表上**：`learning_space` / `conversation` / `message` / `concept`。

`learning_evidence` / `learning_understanding` / `understanding_revision` **完全没有软删，也没有任何删除入口** —— 学习历史和证据链必须长期可追溯。`memory` 也不删，用 `CANDIDATE / ACTIVE / ARCHIVED` 管生命周期。

对应地，外键级联**只保留 4 组**：`test_task → test_answer`、`memory → memory_source`、`learning_evidence → evidence_source`、`document → document_chunk`。`learning_space / conversation / message / concept` 的外键去掉了 `ON DELETE CASCADE` —— 否则一个 delete 就能顺着级联把长期学习历史炸掉。

### 4. `misconceptions` 的语义收窄为「当前」，且存对象而不是字符串

文档写的是「当前/历史误区」，实际存的是**只包含当前仍未解除的误区**，且每条是带状态的对象：

```json
[{ "text": "...", "status": "ACTIVE", "firstDetectedAt": "...", "lastEvidenceAt": "...", "reversalEvidenceIds": [7] }]
```

理由是「历史」已经有两处权威记录（`understanding_revision` 与 `learning_evidence`），在快照里再存一份会让它的状态语义自相矛盾
—— 快照说「用户现在是这样理解的」，却塞着一条早就不成立的旧错误。详见第五节。**不需要改表结构**，JSON 列不受影响。

保留的既有约定：MySQL ENUM 的值与 Java 枚举常量名完全一致（MyBatis-Plus 按 `name()` 存取），全库 utf8mb4。

---

## 九、本地启动

### 环境要求

JDK 17+、MySQL 8、Maven Wrapper（随仓库自带）。

### 配置：本机差异写 `application-local.yml`，不要设环境变量

`application.yml` 里的默认值（MySQL `root`/`root`）是团队约定，**本机不一样的部分写在
`src/main/resources/application-local.yml`** —— 这个文件已被 `.gitignore` 排除，不会提交：

```yaml
spring:
  datasource:
    username: root
    password: "你的密码"
```

它是靠 `application.yml` 里这一行走自动合并的，**不需要设环境变量，也不需要 `--spring.profiles.active`**：

```yaml
spring:
  config:
    import: optional:classpath:application-local.yml
```

`optional:` 前缀的含义是「文件不在就跳过」，所以新克隆的仓库没有这个文件也能正常启动（走 `application.yml` 的默认值）。
优先级上，local 文件高于 `application.yml`，低于命令行参数与环境变量 —— 临时覆盖用环境变量仍然有效。

> 注意：打包时它会一起进本地 jar，别拿这个 jar 做分发。

### 环境变量（可选，优先级最高）

| 变量                | 默认值       | 说明                                                           |
| ------------------- | ------------ | -------------------------------------------------------------- |
| `MYSQL_HOST`        | `localhost`  |                                                                |
| `MYSQL_PORT`        | `3306`       |                                                                |
| `MYSQL_DB`          | `ails`       | 连接串带 `createDatabaseIfNotExist=true`，**库不存在会自动建** |
| `MYSQL_USER`        | `root`       |                                                                |
| `MYSQL_PASSWORD`    | `root`       | 本机不同请写进 `application-local.yml`                         |
| `JWT_SECRET`        | 开发用默认值 | 生产**必须**覆盖                                               |
| `JWT_EXPIRATION_MS` | `604800000`  | 7 天                                                           |

### 建表脚本

两份脚本职责分离，不要合并：

| 文件                   | 职责                                                   | 何时执行                                                 |
| ---------------------- | ------------------------------------------------------ | -------------------------------------------------------- |
| `schema.sql`           | **只 CREATE**，全部 `CREATE TABLE IF NOT EXISTS`，幂等 | `spring.sql.init` 在**每次启动**时自动执行，无需手工干预 |
| `schema-dev-reset.sql` | **只 DROP**，`SET FOREIGN_KEY_CHECKS = 0/1` 包住       | 需要清库重建时**手工**执行                               |

**为什么必须分开**：`schema.sql` 每次启动都会跑。如果把 DROP 写进去，`./mvnw spring-boot:run` 每重启一次就会清空一次数据库。

需要清库重建时：

```bash
mysql -uroot -p ails < src/main/resources/schema-dev-reset.sql
mysql -uroot -p ails < src/main/resources/schema.sql
```

`schema-dev-reset.sql` 会顺带清掉 v1 遗留的 `verification_task` / `verification_answer`（已按文档更名为 `test_task` / `test_answer`）。

### 启动

```bash
cd backend/backend
./mvnw spring-boot:run
# 或
./mvnw -DskipTests clean package && java -jar target/backend-0.0.1-SNAPSHOT.jar
```

**不需要带任何环境变量**：数据库密码等本机差异已经由 `application-local.yml` 提供（见上一节）。
IDE 里直接跑 `BackendApplication` 主类同理。

启动后 `SHOW TABLES` 应为 **22** 张（外加 MySQL 自身的表）。库被删掉也不要紧，连接串的
`createDatabaseIfNotExist=true` 会重新建库，`schema.sql` 会接着把 22 张表建回来 —— 都是幂等的。

### 编译

```bash
./mvnw -q -DskipTests clean compile
./mvnw -DskipTests clean package
```

### 测试

```bash
# 误区生命周期是纯函数，脱离 Spring、不需要 MySQL
./mvnw test -Dtest=MisconceptionLifecycleTest
```

| 测试                         | 依赖       | 说明                                                                                  |
| ---------------------------- | ---------- | ------------------------------------------------------------------------------------- |
| `MisconceptionLifecycleTest` | 无         | 误区 ACTIVE → PENDING → 解除的迁移规则，9 个用例                                      |
| `BackendApplicationTests`    | 本地 MySQL | `@SpringBootTest` 上下文加载冒烟测试，标了 `@Disabled`，默认整包 `./mvnw test` 会跳过 |

所以默认的 `./mvnw test` 是安全的：不需要数据库也能过。要在本地跑上下文冒烟测试，去掉那个 `@Disabled` 即可。

---

## 十、当前状态与待接入

### 已经写实

Conversation / Chat（SSE）/ Highlight / Understanding / Evidence / Memory / Test / LearningState 八个域，包括完整的证据闭环、Revision 历史、状态投影与推荐。

### 待接入

| 项目                                     | 现状                               | 接入方式                                                                              |
| ---------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------- |
| Python Agent Runtime                     | `StubAgentGateway` 回显桩          | 新增 `HttpPythonAgentGateway implements AgentGateway`，替换 Bean                      |
| 证据抽取与判分                           | 本地规则版 `rules-v1`              | 由 Agent 产出分析结果替换 `LearningAnalysisService` 的抽取逻辑与 `TestService` 的判分 |
| 出题策略                                 | 由调用方指定或生成本地简答题       | Evidence Gap + 验证策略应由 Agent 决定                                                |
| 文档 / 导出 / 导入 / 学习计划 / 学习会话 | Controller 返回 501                | 表结构已在位                                                                          |
| Memory 向量检索                          | 关键词 `LIKE`                      | 填 `memory.embedding_ref`，接向量库                                                   |
| Agent 写 Memory                          | `createCandidate` 已就绪但无调用方 | Agent 推断结果走 `createCandidate` 落 `CANDIDATE`                                     |
| `agent_run.session_id`                   | 恒为 null                          | 等学习会话实现                                                                        |

### 已知取舍

- **`LearningAnalysisService` 的规则版分析器会漏抽**。匹配不到 Concept 名称就不产生证据 —— 这是刻意的：宁可少抽，也不瞎猜。真正的语义理解是 Agent 的活。
- **`TestService` 的简答题判分是启发式**。没有标准答案时看长度和是否点到概念名，这不是语义判断，等 Agent 接管后整段替换。
- **`learning_state` 的复习间隔与优先级是硬编码的状态映射**，不是调度算法。
- **误区的解除规则是「两条独立正确证据」，而且不区分反驳的是哪一条误区**。规则版分析器只能给出整条证据的 `correctness`，无法指出它针对的是哪个误区，所以对该 Concept 下所有未解除的误区一视同仁地计一次确认。接入 Agent 后由 Agent 指明目标误区，并把「两次确认」升级成更细的规则 —— 改 `MisconceptionLifecycle` 一个类即可，数据模型与已有历史都不用动。
- **`misconceptions` 的 JSON 结构在本次修改中变了**（字符串数组 → 对象数组）。旧库里的行无法被新代码解析，需要按第九节重建；这是开发期的一次性动作，不涉及线上数据。

这些都写在对应类的 Javadoc 里，不会让人误以为它们已经是最终实现。

### 已知问题与手写 SQL 清单

上面记的是「刻意的取舍」。另有一类**已知但尚未处理**的问题 —— 跨 run 的 Evidence 去重缺数据库兜底、
`learning_space_id` 为 NULL 的记忆对 Agent 不可见、线程池拒绝分支未被执行验证 ——
连同**全部手写 SQL 的清单**（5 个 mapper / 6 条语句 + 2 处 `.last()` 片段，
以及每一条为什么必须手写）记在 [AUDIT_NOTES.md](AUDIT_NOTES.md)。

手写 SQL 值得单独看一遍的原因是：MyBatis-Plus 的 `@TableLogic` 只在注入自带方法时
才拼 `deleted_at IS NULL`，手写语句不会被追加 —— 这既是机制也是陷阱，
`message` 那个序号重复的 500 就是这么踩出来的。
