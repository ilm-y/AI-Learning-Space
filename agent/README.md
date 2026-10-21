# AI Learning Space — Learning Agent

Python 侧的 Learning Agent Runtime，**最小可用版本**。

它负责两件事：

1. **回答之前**：把「这个用户现在是怎么理解这个知识点的」读进来，据此决定从哪里讲起；
2. **回答之后**：分析这一轮是否产生了学习信号，产出 Evidence / Understanding / Memory /
   验证策略的**候选**，交给后端在事务里落库。

```text
回答（POST /agent/chat）：
ChatRequest
  → 概念识别 → 检索（Understanding / Memory / Knowledge，按需 Evidence）
  → Context Builder（排序 / 去重 / Token 预算 / 区块）
  → Tutor → LLM Adapter → SSE 流式回答

分析（POST /agent/analyze）：
AnalyzeRequest
  → 概念识别 → 检索（同一套只读接口）
  → LearningAnalyzer → LLM Adapter
  → AnalyzeResult（候选，不落库）→ 后端事务写入
```

Agent **始终不连数据库**：analyze 返回的是提案，Evidence 去重、Understanding 合并、
Memory 生命周期、验证任务创建全部由后端负责。后端的写入链路见
`backend/README.md` 第五节。

- 各层职责与数据契约：[docs/pipeline.md](docs/pipeline.md)
- 完成进度、边界与下一步：[docs/progress.md](docs/progress.md)

---

## 一、它在整个系统里的位置

```text
Flutter
   │  REST / SSE
   ↓
Spring Boot  ────────────────  MySQL
   │  HTTP  POST /agent/chat            ← HttpPythonAgentGateway（回答）
   │  HTTP  POST /agent/analyze         ← HttpPythonAgentAnalysisGateway（分析）
   ↓
Python Learning Agent
   │  HTTP 只读回调（Concepts / Understanding / Memory / Evidence）
   └──────────────→ Spring Boot
```

边界只有两条，都不许绕：

| 原则                                           | 落地方式                                        |
| ---------------------------------------------- | ----------------------------------------------- |
| 业务数据由 Backend 管理，认知判断由 Agent 管理 | Agent **不连数据库**，读写都走后端接口          |
| Agent 只提出候选，写入语义归 Backend           | analyze 返回 `AnalyzeResult`，不执行任何写操作  |

analyze 返回的 Evidence / Proposal / Memory 都是**建议**：后端会再次校验 conceptId、
按 `dedupe_key` 去重、走误区生命周期与 Memory 状态机。这样 Agent 的判断可以错、
可以重放，但数据库不会因此出现重复或越权数据。

**边界的落点是 token，不是 userId。** Agent 检索时不「以服务身份访问」，
而是**拿着用户自己的 JWT** 去调后端读接口（`ChatAgentRequest.authToken`），
所以后端的鉴权逻辑一行都不用为 Agent 开后门，越权访问在结构上就不可能。
代价是后端必须把这个 token 一路带到生成线程 —— SSE 的生成线程上没有 `SecurityContext`。

---

## 二、快速开始

```bash
cd agent
pip install -r requirements-dev.txt      # 运行时装 requirements.txt 即可
cp .env.example .env                     # 可选：不建也能跑，默认全离线
```

### 跑一次完整流水线（不起服务、不开后端）

```bash
python -m app.cli "TLB Miss 和 Page Fault 到底是什么关系？"
```

默认是 **Mock LLM + Fixture 数据**，所以不需要 API Key、不需要 MySQL、不需要登录。
Mock LLM 会把上下文里的认知背景回读出来，于是你能一眼看出这次到底检索到了什么：

```text
[meta] 命中概念：['TLB（快表）', '缺页异常'] ｜ 上下文约 1288 tokens
...
- 已经理解：TLB 是页表项的缓存；TLB Hit 与 TLB Miss 的区别；TLB Miss 之后需要继续查 Page Table
- [当前存在] 把 TLB Miss 与 Page Fault 混为一谈，认为 TLB 里没有就等于缺页
- 还不确定（本次可以重点补）：Page Table 本身存放在哪里
```

### 只看检索与组装（不调用模型）

```bash
python -m app.cli --preview "为什么你认为我理解了 TLB？"
```

输出每一段上下文的来源、优先级、Token 估算、是否被裁剪 —— 排查「Agent 到底有没有
召回用户的历史理解」时，这比读最终回答快得多。

### 起 HTTP 服务

```bash
python -m app.main          # 或 uvicorn app.main:app --port 8000
curl -s http://127.0.0.1:8000/health
```

### 接真实后端数据

后端的读接口按登录用户鉴权，所以要把用户自己的 JWT 透传进来：

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"******"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")

python -m app.cli --token "$TOKEN" --space 1 "那 TLB Miss 呢？"
```

### 与后端联调（完整链路）

这是 `Postman → 后端 → Agent → LLM` 这一跳真正接通时的样子。**两边都要开**：

```bash
# 终端 1：Agent
cd agent
cp .env.example .env        # 不存在就建一个，见下面「.env 怎么配」
python -m app.main          # 127.0.0.1:8000

# 终端 2：后端 —— 必须把网关切到 http，默认的 stub 根本不会调过来
cd backend/backend
# application-local.yml 里写上：ails.agent.gateway: http
./mvnw spring-boot:run

# 终端 3：自检
curl -s http://127.0.0.1:8000/health
```

`/health` 会直接告诉你当前生效的是哪套适配器，先看这个再看别的：

```json
{"status":"ok","llm_provider":"mock","retriever_mode":"auto",
 "backend_base_url":"http://localhost:8080","max_context_tokens":3000}
```

`retriever_mode` 是 `auto` 时：**带 `authToken` 走 HTTP，不带就退回 Fixture**。
所以联调时链路没通，第一件事是确认 `authToken` 真的传过来了 ——
`auto` 的降级是静默的，它会老老实实给你 fixture 的答案，看起来「一切正常」。

#### `.env` 怎么配

`.env` 已被 `agent/.gitignore` 排除，不会提交。本仓库里的那份是这么配的：

```bash
AILS_LLM_PROVIDER=mock          # 现在是 mock，见下面
AILS_BACKEND_BASE_URL=http://localhost:8080
AILS_RETRIEVER_MODE=auto
```

**先用 `mock` 验管道，再用 `openai` 验质量**，这个顺序不能反。理由：Mock LLM 会把
prompt 里的认知背景**原样回读出来**，所以它能证明「检索到的 Understanding / Session 真的
进了 prompt」这件事 —— 而这恰恰是全项目最容易悄悄断掉的一环。切到真实模型之前，
这一步必须是绿的。

Mock 的回复是确定性的、也是假的，所以它证明不了回答质量。要接真实模型，把上面那行注释掉，
放开这三行并填上自己的 Key：

```bash
AILS_LLM_PROVIDER=openai
AILS_LLM_BASE_URL=https://api.deepseek.com/v1   # 任何 OpenAI 兼容服务都可以
AILS_LLM_API_KEY=sk-xxx
```

配了 `openai` 但没填 Key 会**启动即报错**，不会静默退回 Mock ——
否则你会以为模型在工作，实际看的是假输出。

#### 联调时最值得先跑的一条命令

```bash
curl -s -X POST http://127.0.0.1:8000/agent/context/preview \
  -H 'Content-Type: application/json' \
  -d '{"runId":1,"userId":1,"conversationId":1,"learningSpaceId":1,
       "userMessage":"TLB Miss 和 Page Fault 什么关系","authToken":"<JWT>"}' \
  | python -m json.tool | head -60
```

看 `data.context.sections` 里有没有 `understanding:{conceptId}` 区块、
`data.retriever` 是不是 `http:http://localhost:8080`（而不是 `fixture:...`）、
以及 `data.context.warnings` 有没有直说「没有检索到任何 Understanding」。
**不调模型、不烧 token**，但能定位绝大多数「回答不个性化」的问题。

⚠️ **空间里没有 Concept 时检索会整体跳过**：`detect_concepts` 命中为空 → 没有 conceptId
可查 → 检索线整段不执行，回答退化成通用回答。建 Concept 不是可选项。

### 测试

```bash
python -m pytest -q          # 42 个用例，0.6 秒，不需要数据库与网络
```

---

## 三、目录结构

```text
agent/
├── app/
│   ├── main.py                       HTTP 入口：/agent/chat、/agent/analyze、
│   │                                 /agent/context/preview、/health
│   ├── cli.py                        命令行入口：不起服务跑一次流水线
│   ├── runtime.py                    ★ AgentRuntime：把各层串成一次运行
│   ├── config.py                     配置（环境变量前缀 AILS_）
│   ├── schemas.py                    ★ 与后端对齐的数据契约（唯一的数据边界）
│   ├── prompts.py                    教学契约（System Instruction）
│   ├── trace.py                      结构化运行轨迹（不存思维链）
│   │
│   ├── state/agent_state.py          ★ AgentState：运行时状态，用完即弃
│   │
│   ├── llm/                          ★ LLM Adapter
│   │   ├── base.py                   协议：stream_chat / complete
│   │   ├── openai_compatible.py      DeepSeek / OpenAI / Qwen / vLLM 通用实现
│   │   ├── mock.py                   确定性假输出，离线跑通链路 & 测试断言用
│   │   └── factory.py                按配置装配，配置不全直接报错
│   │
│   ├── retriever/                    ★ Retriever
│   │   ├── base.py                   协议：concepts / understandings / memories / knowledge / evidences
│   │   ├── concept_detector.py       概念识别（规则版，含名称别名与历史回看）
│   │   ├── http_backend.py           调后端只读接口（真实跨对话数据）
│   │   ├── fixture.py                读本地样例数据（离线兜底）
│   │   └── routing.py                选择与降级策略
│   │
│   ├── context/                      ★ Context Builder
│   │   ├── builder.py                去重 / 优先级 / Token 预算 / 区块渲染
│   │   └── tokens.py                 Token 估算与截断
│   │
│   ├── analysis/                     ★ Learning Analyzer
│   │   ├── analyzer.py               结构化输出 → 候选收敛（校验 / 去重 / 裁剪 / 验证策略）
│   │   └── prompts.py                分析契约（只提出候选，不做判断以外的写入）
│   │
│   └── tutor/tutor.py                ★ Tutor：守住教学契约与流式链路
│
├── fixtures/demo_space.json          样例认知数据（README 里那个 TLB 场景）
├── tests/                            42 个用例：概念识别 / 上下文组装 / 分析器 / 端到端
├── docs/pipeline.md                  各层职责与数据契约
├── docs/progress.md                  进度、边界与下一步
├── requirements.txt / requirements-dev.txt
├── pyproject.toml                    pytest 与 ruff 配置
└── .env.example
```

（★ 是这条链路上的六个核心组件，与 `agent-architecture.md` 的划分一一对应。）

---

## 四、一次请求发生了什么

以「那它没命中的时候呢？」为例（上一轮在聊 TLB）：

```text
1. AgentState 初始化          请求 → 运行时状态（run_id / trace_id / session / user / space / message / history）
2. 概念识别                   本轮没提任何概念名 → 回看历史 → 命中 TLB
3. 检索                       并行取 Understanding、Memory、Knowledge；
                              Evidence 只在用户质疑判断时才取（本轮不取）
4. Context Builder            Session + 认知背景 → system message；
                              历史 + 本轮输入 → 对话消息；超预算时从低优先级开始丢
5. Tutor                      校验消息结构 → 交给 LLM Adapter
6. LLM Adapter                流式产出增量文本
7. SSE                        meta → delta × N → done

注：第 1 步的 session 与 trace_id **由后端给**，Agent 不自己推断归属 ——
「这条对话属于哪个学习活动」是后端 Session Resolver 的职责（含 ATTACH / CREATE / NONE
三种判定），Agent 只是消费方。这也是 AgentState 用完即弃的前提：所有连续性都在后端。
```

一个关键设计：**本轮输入永不从上下文里丢弃**。模型看不到问题，这次回答就没有意义，
所以即使它单独超出预算，也只截断而不丢。

---

## 五、四个关键设计决定

### 1. 为什么 LLM Adapter 这么薄

`agent-architecture.md` 开篇就写着 `LLM ≠ Agent`。适配器只认 messages 与 token 参数，
不认识 Understanding、Memory，也不认识 Tutor。所以：

- 换模型厂商，只改 `config.py` 的三个配置项；
- 换业务逻辑，不动适配器。

它同时提供 `stream_chat`（Tutor 用）与 `complete`（LearningAnalyzer 用）。
分析必须等模型把整段 JSON 输出完才能解析，所以那条链路天生不适合流式。

### 2. 为什么 AgentState 用完即弃

`AgentState` 刻意不落库、不跨请求复用：一次请求构造一个，结束就丢。

跨对话的连续性**全部来自后端数据**，不来自进程内缓存。否则同一个用户开两个对话时，
两个进程会各自看到不同的世界；多实例部署后问题会更难查。
这就是 `agent-architecture.md` 第三十九节的落地方式 ——
thread-scoped state 在内存里，跨 thread 的信息在 store（这里是后端数据库）里。

### 3. 为什么有 `/agent/context/preview`

这个项目最难排查的问题不是「模型答得不好」，而是「Agent 到底有没有召回用户的历史理解」。
如果只能从最终回答去猜，每次都得花掉一次模型调用，还不一定看得准。

preview 只检索与组装、**不调用模型**，把每一段的来源、优先级、Token、裁剪情况摊开。
它用的是和 `/agent/chat` 完全相同的 `runtime.prepare()`，所以两条路不可能跑偏。

### 4. 为什么默认跑在 Fixture 上

不是「假装后端已经跑起来了」，而是三个具体理由：

1. **离线可跑**：没有 MySQL、没有登录态时整条链路仍能演示与单测；
2. **可回归**：测试需要固定输入，而真实数据库是会变的；
3. **是文档**：`fixtures/demo_space.json` 用 README 那个 TLB 场景，
   把「Understanding / Memory / Knowledge 长什么样」写成了可执行的例子。

Fixture 与 HTTP 用的是**同一套 Pydantic 模型**，所以切换不需要改上层任何代码。

---

## 六、接口契约

### `POST /agent/chat`

请求体与后端 `ChatAgentRequest` 对齐（camelCase），字段逐个同名、零映射：

```json
{
  "runId": 1024,
  "traceId": "9f2c1a...32 位 hex",
  "userId": 1,
  "conversationId": 7,
  "learningSpaceId": 3,
  "userMessage": "那 TLB Miss 呢？",
  "history": [{"role": "USER", "content": "TLB 是什么？"}],
  "session": {
    "id": 20,
    "title": "虚拟内存地址转换",
    "goal": "理解 VA → TLB → Page Table → PA",
    "currentFocus": "卡在 TLB Miss 与 Page Fault 的关系"
  },
  "authToken": "eyJhbGciOi..."
}
```

两个字段值得单独说：

**`traceId`** —— 后端生成、32 位十六进制，是**两跳链路里唯一能跨进程把日志对上的键**。
`runId` 做不到：后端的 `agent_run` 与 Agent 的 `run_id` 是两套自增，互不相干。
它被记进 `RunTrace`、出现在 `trace.as_dict()` 和 `/agent/context/preview` 的
`data.trace.traceId` 里。缺失只影响排查、不影响执行，所以是可选的。

**`session`** —— 本阶段学习目标（`docs/session-design.md` §一.4 的「短期工作上下文」）。
它进 System Instruction 的最前面，渲染成：

```text
## 本阶段学习目标（当前这段学习活动）
- 这一阶段要解决：理解 VA → TLB → Page Table → PA
- 当前焦点：卡在 TLB Miss 与 Page Fault 的关系
```

`null` 是**常态**，不是错误：Session 懒创建，一条刚建出来的对话在第一条有效学习消息
之前没有归属；而用户在闲聊时（Resolver 判 NONE）也不会产生 Session。
两种情况都**不渲染占位区块** —— 编一个「未设置」会让模型以为自己知道什么。

⚠️ 两边都靠 pydantic 的 `extra="ignore"` + camelCase alias 生成器对齐字段名，
所以**改字段名而只改一边是不会报错的，它只会静默丢掉那一整块上下文**。
`HttpPythonAgentGatewayTest` 里有一条断言专门钉住这个契约。

响应是 `text/event-stream`。**事件名与对前端的 `message.*` 刻意不同名** ——
这是 Backend 与 Agent 之间的内网一跳，同名会让排查时分不清一条事件是谁发的：

```text
meta    { runId, detectedConcepts, estimatedTokens, warnings }
delta   { delta }
done    { runId, detectedConcepts, estimatedTokens, elapsedMs }
error   { runId, code, message }     code: 500 内部错误 / 502 模型上游失败 / 503 后端检索不可用
```

`error` 里的 `code` 是 int，直接喂给后端的 `AgentStreamHandler.onError(int, String)` ——
网关在这一跳上不要再自己编一个常量，否则「谁的错」就丢了。

### `POST /agent/analyze`

回答结束后，后端把这一轮（用户消息 + 助手回复 + 历史 + Session）发过来，Agent 返回
**候选**而不是写入结果：

```json
{
  "evidenceCandidates": [
    {
      "conceptId": 10,
      "evidenceType": "USER_EXPLANATION",
      "observation": "用户知道 TLB 是页表项缓存",
      "correctness": "CORRECT",
      "reasoningQuality": 0.7,
      "specificity": 0.8,
      "aiConfidence": 0.85,
      "needFollowUp": false,
      "userExplanation": "我理解 TLB 就是页表项的缓存"
    }
  ],
  "understandingProposal": {
    "conceptId": 10,
    "knownParts": ["TLB 是页表项缓存"],
    "uncertainParts": ["TLB Miss 与 Page Fault 的关系"],
    "misconceptions": [],
    "latestUserExplanation": "我理解 TLB 就是页表项的缓存"
  },
  "memoryCandidates": [],
  "verificationDecision": { "action": "SKIP" },
  "analyzerVersion": "agent-v1",
  "detectedConcepts": ["TLB"]
}
```

几个契约要点：

- **`conceptId` 必须来自本轮检索到的 Concept。** 模型给出不存在的 id 时，分析器会
  丢弃该条候选或回退到主概念 —— 宁可少写，也不把证据挂到别的知识点上；
- **`correctness = UNKNOWN` 不等于「没有学习信号」**：只要消息提到了已注册的
  Concept，就会产生一条 `OBSERVATION`，后端的 `markExposed` 据此把状态从
  `UNKNOWN` 推进到 `EXPOSED`（见 `evaluation/bad_cases/BC-001-*.md`）；
- **`verificationDecision.action`** 三选一：`ASK_NOW` 必须带 `questionText`；
  `ASK_LATER` 只记录、不打断；`SKIP` 表示这一轮不需要验证；
- 越界分数会被裁剪到 `[0, 1]`，未知字段被忽略（`extra="ignore"`）；但**枚举值不认识、
  结构不符合契约**时整份结果作废：抛 `AnalysisError` → HTTP 502，后端回退 `rules-v1`。
  刻意不把坏输出当成「这一轮没有学习信号」—— 那会变成一次没有任何记录的静默降级；
- `llm_provider=mock` 走**启发式分析**（不调用模型），用于离线测试与本地联调；
  其他 provider 走结构化 JSON 输出，解析失败时同样有明确的错误码。

### `POST /agent/context/preview`

同样的请求体，返回检索与组装结果（不调用模型）：

```json
{
  "code": 0,
  "data": {
    "summary": {
      "detectedConcepts": ["TLB"],
      "sessionId": 20,
      "retrieved": {"understandings": 1, "memories": 2, "...": 0}
    },
    "retriever": "http:http://localhost:8080",
    "degraded": false,
    "context": { "estimatedTokens": 1298, "droppedSections": [], "sections": [], "messages": [] },
    "trace": { "traceId": "9f2c1a...", "events": [] }
  }
}
```

### 从后端读到的 Memory 长什么样

`MemoryItem` 在 B 阶段跟着后端一起改了，两个字段与旧版不同 —— **改一边不改另一边会静默丢数据**
（`extra="ignore"` 会把认不出的字段直接扔掉）：

| 字段            | 说明                                                                |
| --------------- | ------------------------------------------------------------------- |
| `scope_type`    | `GLOBAL` / `SPACE` / `CONCEPT`。渲染进 prompt 时标注成「全局 / 本空间 / 本知识点」 |
| `memory_key`    | 取代了旧的 `path`。同作用域内的记忆标识，缺省时后端算出 `auto:<hash>` |

`scope_type` 渲染进 prompt 是有用的，不是装饰：标着「全局」的是跨知识点都成立的长期偏好，
模型不该把它当成只是当前话题的细节。检索的可见性规则（`GLOBAL OR 本空间 OR 本知识点`）
在后端侧执行，Agent 只负责把拿到的结果如实标注出来。

---

## 七、当前状态

**已完成**：两条链路都已接通 ——

- **回答**：LLM Adapter、AgentState、Retriever（含真实后端读接口）、Context Builder
  （含 Session 区块）、Tutor，经 `POST /agent/chat` 被后端真实调用；
- **分析**：LearningAnalyzer 经 `POST /agent/analyze` 产出 Evidence / Proposal /
  Memory / Verification 候选，后端 `AgentAnalysisWriteService` 在事务里落库；
- 42 个不依赖数据库与网络的测试；端到端验收见 `scripts/regress-closed-loop.sh`
  与 `evaluation/`。

**明确没做**：

| 项目                                        | 现状                                     | 计划阶段 |
| ------------------------------------------- | ---------------------------------------- | -------- |
| 语义概念识别                                | 规则版匹配名称 + 别名 + 历史回看         | M2       |
| 向量检索 / RAG                              | 无。`get_knowledge` 走 HTTP 时固定返回空 | M6       |
| 主动验证的执行闭环                          | Agent 给出 `ASK_NOW / ASK_LATER / SKIP`，TestTask 由后端创建；作答判分仍是后端启发式 | M5 |
| 真实模型的记忆收敛                          | Memory 候选已能落库，冲突合并规则在后端  | M3       |
| 多轮工具调用（Agent Tools）                 | 无。analyze 是一次线性结构化输出         | 后续     |
| 异步分析队列（RabbitMQ）                    | 无。后端在 SSE 关闭后用线程池同步执行    | M7       |

细节与优先级见 [docs/progress.md](docs/progress.md)。
