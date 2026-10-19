# AI Learning Space — Backend

Spring Boot 3.3.5 / Java 17 / MyBatis-Plus / MySQL 8 的 REST 服务，实现了「AI 学习空间」的核心学习闭环。

本文档说明后端的架构、每个目录的职责、三种写入语义、SSE 事件契约，以及当前哪些能力已经写实、哪些还是骨架。

---

## 一、它在整个系统里的位置

产品的核心模型是这条环：

```text
Conversation → Evidence → Understanding(+Revision) / Memory → LearningState → 下一次 Context
```

- 用户在与 AI 的 **Conversation** 里边学边聊，这些对话归入一段 **LearningSession**（学习活动）；
- 系统从对话、批注、测试回答中抽取 **Evidence**（证据）；
- 证据不断推进 **Understanding**（用户当前怎么理解这个概念），每次真正变化留下一条 **Revision**；
- 同时沉淀 Agent 的长期上下文 **Memory**；
- **LearningState** 是 Understanding 的物化投影，用来回答「现在最该关注什么」；
- 而环的最后一格 —— 下一次对话的 **Context** —— 由 Python Agent 组装：它带着用户 token
  回访后端的只读检索接口，把 Understanding / Memory / Session 装进 prompt。

后端就是这条环的**唯一权威实现**。Python Agent Runtime 负责「理解自然语言、判断学得怎么样」，
它产出的结论必须回到后端走同一条写入链路，不存在第二条改状态的捷径。**当前的闭环只接了
chat 这一跳**：Agent 只读检索 + 生成回复，还不会写回分析结果（`/agent/analyze` 尚未接），
所以「从普通聊天里自动抽出用户的理解」这件事仍然是规则版分析器在做，会漏抽 —— 见第十节。

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

四条关键关系，记住这四条就不会搞错：

| 关系                     | 含义                                                                      |
| ------------------------ | ------------------------------------------------------------------------- |
| **Understanding 是真相** | 「用户现在怎么理解这个概念」只有这一个答案，就在 `learning_understanding` |
| **LearningState 是投影** | 它不产生判断，只把 `understanding.status` 复制过来，方便筛选与推荐        |
| **Evidence 是依据**      | append-only。证据冲突时不是改掉旧的，而是再追加一条                       |
| **Session 是容器**       | 它不承载任何长期认知，只回答「这几个 Conversation 是围绕哪个目标在学」    |

`test_task` / `test_answer` 属于「验证手段」而不是「结论」：判分结果会作为一条 Evidence 流回证据层，再走同一条链路推进理解。

`learning_session` 与 `understanding` / `evidence` / `memory` 是**两套不同时间尺度的东西**，
不要混：Session 管「这一段学习活动在做什么」（可以几周，也可以明天就结束），
Understanding / Memory 管「这个用户长期是怎么理解的、偏好什么」。所以 **Session 结束了，
认知不会消失** —— 它本来就不在 Session 里。这条边界写在 `docs/session-design.md` §二 与 §十。

---

## 三、目录结构

```text
src/main/java/com/ails/backend/
├── BackendApplication.java     启动类
├── agent/                      ★ Backend 与 Agent Runtime 的边界
├── common/                     统一响应体、业务异常、全局异常处理
├── config/                     Security / MyBatis-Plus / 字段自动填充 / 线程池
├── controller/                 REST 入口（17 个：13 个写实，4 个骨架）
├── dto/                        请求与响应对象（按领域分包）
├── entity/                     22 张表对应的实体
├── enums/                      全部枚举（与 MySQL ENUM 值一一对应）
├── mapper/                     MyBatis-Plus Mapper，全部是裸 BaseMapper
├── model/                      领域值对象与不依赖数据库的规则（误区生命周期、Session Resolver）
├── security/                   JWT 过滤器与鉴权失败处理
├── service/                    业务逻辑与写入语义
└── utils/                      JWT / 安全上下文 / JSON 工具

src/main/resources/
├── application.yml
├── schema.sql                  ★ 建表（幂等，每次启动执行）
└── schema-dev-reset.sql        ★ 清库（手工执行，绝不自动跑）
```

### `agent/` — 与 Agent Runtime 的边界

整个后端只有这一个地方「知道要接 Python」：

```text
ChatController → ChatService → AgentGateway ─┬→ StubAgentGateway       （桩）
                                             └→ HttpPythonAgentGateway  （真的调 Python）
```

- `AgentGateway` —— 唯一的接口定义点：`streamReply(request, handler)`
- `AgentStreamHandler` —— `onDelta` / `onComplete` / `onError` 三个回调，把「怎么推」和「推到哪」解耦
- `StubAgentGateway` —— 桩实现，把用户输入回显成引导语并切块推出
- `HttpPythonAgentGateway` —— 真实实现，`POST {base-url}/agent/chat`，逐行解析 Agent 的 SSE

两者靠配置项二选一，默认 `stub`：

```yaml
ails:
  agent:
    gateway: stub                       # stub | http
    base-url: http://127.0.0.1:8000
    connect-timeout-ms: 3000
    read-timeout-ms: 240000             # 必须严格小于 ChatService 的 SSE 5 分钟超时
```

本机联调时在 `application-local.yml` 里把它切成 `http`（见第九节）。**默认保持 `stub`** 是有意的：
`scripts/` 下的端到端脚本用 `./mvnw -o spring-boot:run` 起后端，断言的是 SSE 事件序列而不是回复内容，
所以 Python Agent 没起也不该让它们失败。

几个实现上的硬约束，改这个类之前先读：

| 约束 | 原因 |
| --- | --- |
| 用 JDK 自带的 `java.net.http.HttpClient` | `pom.xml` 里没有任何 HTTP 客户端依赖，而构建是离线的（`./mvnw -o`）。不为这一个类引新依赖 |
| `BodyHandlers.ofLines()` 逐行读 | Agent 的 SSE 不带 `id:` / `retry:` 字段，「空行分隔 + 读 `event:` / `data:` 行」就够了，不需要 SSE 客户端库 |
| 显式 `.version(HTTP_1_1)` | 默认 `HTTP_2` 会让明文请求先带 `Connection: Upgrade` 去试探 h2c。uvicorn 不支持，只记一行日志就**丢掉请求体**，FastAPI 随后回 422。这个 bug 只在真机上暴露过一次，所以 `HttpPythonAgentGatewayTest` 里钉了一条断言 |
| `error` 事件的 code **原样透传** | Agent 用 503 区分「我的后端检索不可用」。翻译层自己编常量就丢掉了这个分类 |
| 失败全部转 `onError`，不抛 | 连接被拒 / 超时 → 503；HTTP ≥ 400 → 该状态码；流干净结束但没等到 `done` → 502（半截话不能落库） |

因为有了这层抽象，`ChatController` 里**不存在**任何「以后要调 Python Agent」的注释，
加 `HttpPythonAgentGateway` 时 Controller 与 Service 一行都没改。

### `common/` — 统一响应

所有非流式接口都返回 `ApiResponse<T>{ code, message, data }`，`code = 0` 表示成功，否则是 HTTP 风格错误码。

`GlobalExceptionHandler` 负责把异常翻译成响应：`BusinessException` 按其 `code` 设置 HTTP 状态；参数校验失败拼成 `字段名 + 提示`；请求体解析失败（含枚举值不合法）返回 400；兜底 500 只回「服务器内部错误」，堆栈只进日志。

`dto/` 里每个请求对象用 `jakarta.validation` 标注**中文**提示，前端可以直接展示。

### `config/`

| 类                         | 作用                                                                   |
| -------------------------- | ---------------------------------------------------------------------- |
| `SecurityConfig`           | `/api/auth/**` 与 OPTIONS 放行，其余需要认证；无状态；CORS 全开        |
| `MybatisPlusConfig`        | `@MapperScan`、`OptimisticLockerInnerInterceptor`、`PaginationInnerInterceptor` |
| `MyBatisMetaObjectHandler` | 自动填充 `createdAt` / `updatedAt` / `answeredAt`                      |
| `AsyncConfig`              | `chatStreamExecutor` 线程池，SSE 生成不占用 Tomcat 请求线程            |

`OptimisticLockerInnerInterceptor` **不是可选项**：不注册它，带 `@Version` 的实体照常读写，
只是并发更新会静默互相覆盖 —— 没有任何报错提示。目前只有 `Memory` 带 `@Version`。
`AgentRun` **刻意不加**：`ChatService.finishRunIfRunning` 用的是「带 `status = RUNNING` 条件
的 UPDATE」，本身已经做了数据库侧仲裁，再叠一层乐观锁只会两边打架。

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
| `SessionResolver`        | 一条对话该归入哪个学习活动的判定规则，**纯函数、不碰数据库**                     |

把误区规则放在这里而不是塞进 `UnderstandingService`，是为了让它可测、可改：将来把
「两次独立确认」升级成「一次高质量解释 + 一次测试正确」，只改这一个类。

`SessionResolver` 是同一个理由的第二次应用。它的输入输出契约（含 `NONE` 分支与
`now` 参数为什么必须由调用方传入）写在 `docs/session-design.md` 的「Session Resolver
的输入/输出契约」一节。规则本身只有四条，但**`NONE` 不能省**：它表示「这次交互不产生、
也不归属任何学习活动」，是 `conversation.session_id` 可以为 NULL 的第二种合理状态
（第一种是「用户建了对话还没说话」）。把 `NONE` 实现成「落到 CREATE」会让每句闲聊
都生出一个垃圾 Session；实现成「不处理」又不写进规则，则会让 NULL 变成无法解释的脏数据。

### `dto/`

按领域分包：`auth / user / space / concept / conversation / highlight / understanding / memory / test / state / agent`。

统一风格：Java `record` + 静态 `from(...)` 工厂（带空值守卫，返回 `null` 而不是抛异常）。JSON 列在实体里是 `String`，出参时用 `utils/JsonUtil` 转成 `List<String>` 或 `List<Misconception>`。

`test/` 包替代了 v1 的 `verification/`；`state/` 里的 `ContinueLearningItem` 已被 `AttentionItem` 与 `TopicCoverageItem` 取代。

---

## 四、三种写入语义

这是整个后端最重要的约定。三个 Service 各管一种语义，不要混用。

### 1. EvidenceService —— append-only + 幂等 upsert

`learning_evidence` 只会被追加。**没有 update，没有 delete，也没有对应的 REST 入口。**

证据冲突时的正确做法是「再加一条」：用户上次理解错了、这次理解对了，两条证据都在，Revision 历史完整记录了判断是怎么变的。删掉旧证据等于篡改学习历史。

**关于幂等：`dedupe_key` 是这条证据的唯一身份。**

```text
dedupe_key = SHA256(user_id | concept_id | evidence_type | 主来源 | normalize(observation))
```

数据库侧 `UNIQUE(dedupe_key)` 是最终保证，应用层那次 `findByDedupeKey` 只是避免白跑一次插入。

最关键的一点是**指纹里不含 `agent_run_id`**。同一份观察被两个 run 各分析一次，是两件执行记录、但只是同一条证据 —— 不该因为「开了两次 run」就多出一行。所以：

```text
append(Draft) → 命中 dedupe_key ？→ 不新增行
                                   ├─ 补挂新的 evidence_source（去重键：evidence_id + source_type + source_id）
                                   └─ 把 last_seen_run_id 推进到本次 run

                               → 没命中 ？→ INSERT，first_seen_run_id = last_seen_run_id = 本次 run
```

返回值 `AppendResult{evidence, created}` 里的 `created` 就是给调用方看的：`false` 时 `LearningAnalysisService`
会**跳过 Understanding 投影** —— 同一条观察重复进来，快照本来也不会变，再跑一遍只会白白产生一条内容相同的 Revision。

`analyzer_version` 与 `first_seen_run_id` / `last_seen_run_id` 都是**纯溯源**字段，不参与去重：
观察内容真的变了，`dedupe_key` 自然就变了，不需要版本号兜着。

早期版本用的是 `MD5(observation)` + `UNIQUE(agent_run_id, concept_id, observation_hash)`，
它有个致命的方向性错误：换个 run 再分析一次同一条消息，唯一索引**拦不住**（`agent_run_id` 不同），
跨 run 的去重等于没有。这段历史记在 [AUDIT_NOTES.md](AUDIT_NOTES.md) §1。

指纹规则由 `EvidenceDedupeKeyTest` 的 9 条纯函数单测覆盖，其中「换个 run 指纹不变」是核心断言。

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
2. Evidence                                     append-only + dedupe_key upsert
3. Understanding Update                         当前快照
4. INSERT UnderstandingRevision                 历史版本
5. UPDATE current_revision_id                   指向最新版本
6. LearningState Projection                     物化投影
```

第 1 步必须在第 2 步之前 —— Evidence 要挂 `understanding_id`，快照得先存在。
第 2 步命中 `dedupe_key` 时，3~6 步整段跳过（理由见上一节）。

整段由一条 `agent_run` 串起来（`run_type = ANALYSIS`），它记录**本次执行**的轨迹
（`first_seen_run_id` / `last_seen_run_id` 指向它），但**不再是去重依据**。

### 证据从哪来

| 来源                                | `evidence_type`    | correctness                                     |
| ----------------------------------- | ------------------ | ----------------------------------------------- |
| Message 提到某个 Concept            | `OBSERVATION`      | `UNKNOWN` —— 只说明接触过，状态推进到 `EXPOSED` |
| Highlight `MY_UNDERSTANDING`        | `USER_EXPLANATION` | `CORRECT` —— 用户自己的解释，最强信号           |
| Highlight `MISCONCEPTION_CANDIDATE` | `USER_CORRECTION`  | `INCORRECT`                                     |
| Highlight `QUESTION`                | `HIGHLIGHT_SIGNAL` | `UNKNOWN` + 需要跟进                            |
| Highlight `IMPORTANT`               | 不产生证据（普通标记） | —                                            |
| Highlight `CONCEPT_LINK`            | 不产生证据（只建概念关联） | —                                        |
| TestAnswer 判分                     | `TEST_RESULT`      | `CORRECT` / `PARTIAL` / `INCORRECT`             |

`QUESTION` 归到 `HIGHLIGHT_SIGNAL` 而不是 `OBSERVATION` 是有意的：它记的是
「**用户自己标记了「这里有疑问」**」这个动作，而不是「系统观察到他在谈这个概念」。
两者混成一个取值，以后想统计「用户主动标注的疑点有多少」就分不出来了。

**返回 0 条证据是完全正常的。** 普通闲聊不涉及任何已注册 Concept 时，`analyzeMessage` 返回空列表，不写证据、不动理解。

### 这一轮归属哪个学习活动

同一条链路上还顺手做了一件与证据无关的事：**Session 归属**。它在 `ChatService.stream()`
的第 3 步完成，早于把请求丢进线程池 —— 早这一步是硬要求，理由写在 `ChatService` 那一段的注释里
（新对话的第一条消息发出时 `session_id` 还是 NULL，放到回复生成之后，最该继承上个 Session 目标的
那一轮反而什么都带不上）。判定规则见 `docs/session-design.md` §五。

同一轮的 Concept 匹配结果会被算一次、用两处：Agent 上下文里的 `detectedConcepts`，
和 `analyzeQuietly` 的证据抽取 —— 不重复查库。

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

**用 `curl` 手工验证时 `-N` 必须加：**

```bash
curl -N -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8080/api/conversations/1/messages \
  -d '{"content":"TLB 是什么？"}'
```

不加 `-N` 时 curl 会缓冲，你会以为流没通；加了之后事件逐条到达，`message.start` 立刻可见。
`scripts/regress-closed-loop.sh` 的第 10 步就是这么验的。

**跨进程的关联键是 `trace_id`，不是 `runId`。** `runId` 只在各自进程里有效（后端有一条 `agent_run`，
Agent 那边有自己的 `run_id`），所以 `ChatService.createRun` 会生成一个 16 字节随机数的 32 位十六进制
`trace_id`，写进 `agent_run.trace_id` 并透传进 `ChatAgentRequest`，Agent 侧记进 `RunTrace` 并从
`/agent/context/preview` 的 `data.trace.traceId` 暴露出来。排查「这次回答为什么不对」时，
拿这一个值就能把两跳的日志对上。

用 `SecureRandom` 而不是 `ThreadLocalRandom`：这个值会出现在日志里，可预测的 trace id 让
「构造一个 id 去翻别人的执行记录」变得容易。它不参与任何业务判断，纯粹是关联键，所以不加唯一约束。

`ANALYSIS` 与 `TEST` 类型的 run **没有 trace_id**（`NULL`），这是对的：它们不出进程，
没有第二半日志需要对上。

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
| 学习活动 | `GET /api/sessions/current?spaceId=`、`GET /api/sessions?spaceId=&status=`、`GET /api/sessions/{id}`、`POST /api/sessions`、`PATCH /api/sessions/{id}`、`POST /api/sessions/{id}/complete` |
| 理解     | `GET /api/understandings?spaceId=`、`GET /api/understandings/{conceptId}`、`GET /api/understandings/{conceptId}/revisions`       |
| 证据     | `GET /api/understandings/{conceptId}/evidence`、`GET /api/evidence/{evidenceId}`                                                 |
| 记忆     | `GET/POST /api/memories`、`GET/PATCH/DELETE /api/memories/{id}`、`GET /api/memories/search?spaceId=&conceptId=&keyword=`         |
| 测试     | `POST /api/tests`、`GET /api/tests/pending`、`GET /api/tests/{testId}`、`POST /api/tests/{testId}/answer`                        |
| 学习状态 | `GET /api/learning-state/overview`、`/concepts/{conceptId}`、`/attention`、`/topic-coverage?spaceId=`                            |

`/api/learning-state/*` **只有查询接口**。LearningState 是投影，没有「改状态」的入口 —— 想让它变，就去产生新证据。

`/api/understandings/*` 与 `/api/evidence/*` 同样**只有读接口**，没有删除或改快照的路径。

**学习活动（`/api/sessions/*`）是唯一一组「系统自动创建、用户也能手动改」的资源**，两点值得说清：

- `GET /api/sessions/current?spaceId=` 在没有学习活动时返回 `data: null`，**不是 404**。
  「当前没有学习活动」是正常状态，不是「你要的东西不存在」—— 用 404 会让前端把两种情况
  混成一个错误分支。
- `PATCH /api/sessions/{id}` 是为了让 `PAUSED` / `ABANDONED` **可达**才加的。没有它，
  `SessionStatus` 里这两个值只能靠直接改库产生，而 Resolver 的候选集又依赖 `PAUSED`
  （暂停不等于结束，旧目标仍然相关）—— 一条推不动的状态机。`PATCH` 的
  `currentFocus: ""` 表示清空，`null` 表示不改（这两个语义在 JSON 里必须区分开）。
- `POST /api/sessions` 是用户显式「另起一段」的入口，**系统自己不会用** ——
  自动归属一律走 Resolver。

### 骨架（返回 501）

`document` / `export` / `imports` / `learning-plans` 四个域的 Controller 已经占住路由，但直接返回 `501` 并说明「尚未接入」。

这是有意的：**假装成功比明确说没做更浪费时间**。前端联调时能立刻分辨「这个接口还没做」和「这个接口出错了」。表结构（`document` / `document_chunk` / `import_task` / `learning_plan`）已经在位。

`sessions` 曾经也在这份名单里，现在是实打实实现了 —— 见上面的接口表。

---

## 八、对设计文档的刻意偏离（5 处）

下面这几处偏离了 `database&API_design.md` 的列清单，都是有意的，理由如下。
**这里只列偏离。** B 阶段把 Evidence 去重、Memory 作用域、Session 归属三处从「代码偏离文档」
改成了「代码与文档一致」（含新增的 `evidence_type` / `dedupe_key` / `scope_type` / `scope_key` /
`trace_id` / `goal` / `PAUSED`），那些是对齐、不是偏离，写进了设计文档本身。

### 1. `message` 加了唯一约束

```sql
UNIQUE KEY `uk_msg_conv_seq` (`conversation_id`, `sequence_no`)
```

文档只写了普通索引。但 `sequence_no` 是并发写入的，没有唯一约束的话撞号会**静默**发生 —— 消息顺序错乱且难以排查。加上约束后，真撞上会直接报错。

### 2. 软删除只覆盖用户内容，级联收敛

`deleted_at` **只加在 4 张表上**：`learning_space` / `conversation` / `message` / `concept`。

`learning_evidence` / `learning_understanding` / `understanding_revision` **完全没有软删，也没有任何删除入口** —— 学习历史和证据链必须长期可追溯。`memory` 也不删，用 `CANDIDATE / ACTIVE / ARCHIVED` 管生命周期。

对应地，外键级联**只保留 4 组**：`test_task → test_answer`、`memory → memory_source`、`learning_evidence → evidence_source`、`document → document_chunk`。`learning_space / conversation / message / concept` 的外键去掉了 `ON DELETE CASCADE` —— 否则一个 delete 就能顺着级联把长期学习历史炸掉。

### 3. `misconceptions` 的语义收窄为「当前」，且存对象而不是字符串

文档写的是「当前/历史误区」，实际存的是**只包含当前仍未解除的误区**，且每条是带状态的对象：

```json
[{ "text": "...", "status": "ACTIVE", "firstDetectedAt": "...", "lastEvidenceAt": "...", "reversalEvidenceIds": [7] }]
```

理由是「历史」已经有两处权威记录（`understanding_revision` 与 `learning_evidence`），在快照里再存一份会让它的状态语义自相矛盾
—— 快照说「用户现在是这样理解的」，却塞着一条早就不成立的旧错误。详见第五节。**不需要改表结构**，JSON 列不受影响。

### 4. `learning_understanding` 用 `SELECT ... FOR UPDATE` 而不是乐观锁

文档给这张表设计了 `version` 乐观锁。实际实现改成了在事务里先
`SELECT ... FOR UPDATE` 锁住那一行，再读改写。

两者都能防丢更新，这里是**后者更强**：乐观锁的失败方式是把冲突交给调用方重试，
而 `UnderstandingService.apply` 的语义是「拿当前快照 + 新证据算下一版快照」——
重试要重读证据、重算投影，写起来比锁一行复杂得多，还容易漏。锁一行则天然串行化，
且窗口就是一次投影计算。表上仍然保留 `version` 列，但**不参与**并发控制，只作诊断字段。

### 5. `memory.memory_key` 可为 NULL，缺省由 content 哈希兜底

```sql
`memory_key` VARCHAR(255) DEFAULT NULL COMMENT '同作用域内的记忆标识；缺省时由 content 哈希兜底',
UNIQUE KEY `uk_memory_identity` (`user_id`, `scope_key`, `memory_key`)
```

文档假设调用方总会给出 `memory_key`。但「记住用户喜欢从硬件路径理解 OS」这种自动沉淀
根本没有什么自然的 key 可起，强行让调用方编一个只会得到 `memo-1` / `memo-2` 这类噪声 ——
既不稳定也没意义。所以缺省时由 `MemoryService` 算出
`auto:SHA256(normalize(content)).substring(0,32)`，语义是「同一作用域下，内容相同就是同一条」；
想给显式 key 的调用方照常传 `memoryKey`，此时可并存（`auto:…` 与 `hardware-first-2` 互不冲突）。

**代价**：内容改一个字就是另一条记忆。这是有意的 —— 记忆的「更新」应该走
`PATCH /api/memories/{id}`（带乐观锁），而不是重新 POST 一条指望它覆盖旧的。

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

> **改过 `schema.sql` 的列清单之后，`IF NOT EXISTS` 不会帮你加列。** 必须走一次
> 清库重建（`schema-dev-reset.sql` → `schema.sql`），否则新列在库里根本不存在，
> 而错误会以「字段读出来全是 null」的形式出现，不在启动日志里。B 阶段加
> `evidence_type` / `dedupe_key` / `scope_type` / `trace_id` / `goal` 就是这么过来的。

### 联调 Python Agent（本机）

完整链路是 `Postman → 后端 → Python Agent → LLM`，所以后端跑起来还不够：

```bash
# 终端 1：Python Agent
cd agent
python -m app.main                  # 监听 127.0.0.1:8000

# 终端 2：后端，并把网关切成 http
cd backend/backend
# 先在 application-local.yml 里写 ails.agent.gateway: http
./mvnw spring-boot:run
```

`application-local.yml` 里那两行（该文件已被 gitignore，不会提交）：

```yaml
ails:
  agent:
    gateway: http
```

**Agent 没起也不要紧**：`HttpPythonAgentGateway` 会把连接被拒翻译成一次
`message.error { code: 503 }` 推给前端，而不是抛异常或挂住。默认的 `stub` 网关更是
完全不依赖 Python。

自检顺序（**先查检索，再查模型** —— 这个项目最难排查的不是「模型答得不好」，
而是「Agent 到底有没有检索到用户的历史理解」）：

```bash
# 1) Agent 活着，且知道自己在用哪套适配器
curl -s http://127.0.0.1:8000/health

# 2) 只检索 + 组装上下文，不调模型、不烧 token
curl -s -X POST http://127.0.0.1:8000/agent/context/preview \
  -H 'Content-Type: application/json' \
  -d '{"runId":1,"userId":1,"conversationId":1,"learningSpaceId":1,
       "userMessage":"TLB Miss 和 Page Fault 什么关系","authToken":"<JWT>"}' \
  | python -m json.tool | head -60
```

第 2 步的 `data.context.sections` 里应当出现 `understanding:{conceptId}` 区块；
没有的话看 `data.context.warnings`（它会直说「没有检索到任何 Understanding」）。

⚠️ **空间里没有 Concept 时检索会整体跳过**（`detect_concepts` 命中为空 → 没有 conceptId
可查 → 回答退化成通用回答）。建 Concept 不是可选项。

端到端回归（本轮闭环的验收脚本，20 项断言）：

```bash
bash scripts/regress-closed-loop.sh
```

它覆盖 Evidence 去重的库级保证、Memory 三档作用域的可见性、Session Resolver 的
CREATE / ATTACH / NONE 三个分支（从 Agent 日志反查）、以及 SSE 事件序列。

### 编译

```bash
./mvnw -q -DskipTests clean compile
./mvnw -DskipTests clean package
```

### 测试

```bash
./mvnw test                                    # 全部 43 条，不需要 MySQL 之外的任何东西
./mvnw test -Dtest=MisconceptionLifecycleTest  # 单跑某个纯函数测试类
```

现有测试全是**纯 JUnit 5 + AssertJ，不起 Spring 上下文、不连数据库**：

| 测试类                          | 覆盖                                                                 |
| ------------------------------- | -------------------------------------------------------------------- |
| `MisconceptionLifecycleTest`     | 误区的状态迁移规则                                                   |
| `SessionResolverTest`            | CREATE / ATTACH / NONE 四类判定，含「7 天内」这条时间规则            |
| `EvidenceDedupeKeyTest`          | 去重指纹的规则（**不含 `agent_run_id`** 是核心断言）                 |
| `HttpPythonAgentGatewayTest`     | SSE 事件翻译与失败映射，用 JDK 自带的 `HttpServer` 起假 Agent        |

`BackendApplicationTests` 是脚手架自带的 `@SpringBootTest` 上下文加载冒烟测试，
标了 `@Disabled("需要本地 MySQL 与库表已初始化，CI/无库环境下跳过")`，所以默认的
`./mvnw test` 不需要数据库也能过（输出里那 1 条 skipped 就是它）。要在本地跑它，去掉 `@Disabled` 即可。

---

## 十、当前状态与待接入

### 已经写实

Conversation / Chat（SSE）/ Highlight / Understanding / Evidence / Memory / Test / LearningState，
加上 **Session（学习活动，含 Resolver 自动归属）**，共九个域 —— 包括完整的证据闭环、
Revision 历史、状态投影与推荐，以及 `Postman → 后端 → Python Agent → LLM` 这一跳。

闭环的验收方式见第九节：`bash scripts/regress-closed-loop.sh`（20 项断言，全绿）。

### 待接入

| 项目               | 现状                               | 接入方式                                                                              |
| ------------------ | ---------------------------------- | ------------------------------------------------------------------------------------- |
| 证据抽取与判分     | 本地规则版 `rules-v1`              | 由 Agent 产出分析结果替换 `LearningAnalysisService` 的抽取逻辑与 `TestService` 的判分 |
| 出题策略           | 由调用方指定或生成本地简答题       | Evidence Gap + 验证策略应由 Agent 决定                                                |
| 文档 / 导出 / 导入 / 学习计划 | Controller 返回 501    | 表结构已在位                                                                          |
| Memory 向量检索    | 关键词 `LIKE`                      | 填 `memory.embedding_ref`，接向量库                                                   |
| Agent 写 Memory    | `createCandidate` 已就绪但无调用方 | Agent 推断结果走 `createCandidate` 落 `CANDIDATE`                                     |
| `run_type = MEMORY` | 枚举值已在位但无调用方            | Agent 接管记忆收敛时使用                                                              |
| Session 归属升级   | 文本命中近似（见 session-design.md §六） | 换成「与 Session 内 Evidence 的 Concept 交集」，只改 `SessionResolver` 一个类   |

### 已知取舍

- **`LearningAnalysisService` 的规则版分析器会漏抽**。匹配不到 Concept 名称就不产生证据 —— 这是刻意的：宁可少抽，也不瞎猜。真正的语义理解是 Agent 的活。
- **`TestService` 的简答题判分是启发式**。没有标准答案时看长度和是否点到概念名，这不是语义判断，等 Agent 接管后整段替换。
- **`learning_state` 的复习间隔与优先级是硬编码的状态映射**，不是调度算法。
- **误区的解除规则是「两条独立正确证据」，而且不区分反驳的是哪一条误区**。规则版分析器只能给出整条证据的 `correctness`，无法指出它针对的是哪个误区，所以对该 Concept 下所有未解除的误区一视同仁地计一次确认。接入 Agent 后由 Agent 指明目标误区，并把「两次确认」升级成更细的规则 —— 改 `MisconceptionLifecycle` 一个类即可，数据模型与已有历史都不用动。
- **`misconceptions` 的 JSON 结构在本次修改中变了**（字符串数组 → 对象数组）。旧库里的行无法被新代码解析，需要按第九节重建；这是开发期的一次性动作，不涉及线上数据。

这些都写在对应类的 Javadoc 里，不会让人误以为它们已经是最终实现。

### 已知问题与手写 SQL 清单

上面记的是「刻意的取舍」。另有一类**已知但尚未处理**的问题，连同**全部手写 SQL 的清单**
（5 个 mapper / 6 条语句 + 2 处 `.last()` 片段，以及每一条为什么必须手写）记在
[AUDIT_NOTES.md](AUDIT_NOTES.md)。

它开头的两条（跨 run 的 Evidence 去重缺数据库兜底、`learning_space_id` 为 NULL 的记忆
对 Agent 不可见）**已经在 B 阶段修掉**，从「待决策」移进了「已修」——那份清单值得回头看一遍，
因为它们是两个「文档早就写清楚了、代码却是另一个样子」的典型。

手写 SQL 值得单独看一遍的原因是：MyBatis-Plus 的 `@TableLogic` 只在注入自带方法时
才拼 `deleted_at IS NULL`，手写语句不会被追加 —— 这既是机制也是陷阱，
`message` 那个序号重复的 500 就是这么踩出来的。
