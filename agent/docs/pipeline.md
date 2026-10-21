# Agent 流水线：各层职责与数据契约

> 本文说明 `agent/app/` 里每一层负责什么、不负责什么、为什么这样切。
> 与 `agent-architecture.md` 对应的是它第四十一节那条链路的**同步路径**：
> Chat（回答）与 Analyze（学习分析）两条入口。

---

## 一、总览

```text
              POST /agent/chat                    （main.py）
                     │
                     ▼
        ┌────────────────────────┐
        │ 1. AgentState          │  运行时状态，用完即弃        state/agent_state.py
        └───────────┬────────────┘
                    ▼
        ┌────────────────────────┐
        │ 2. 概念识别             │  规则版：名称匹配 + 历史回看  retriever/concept_detector.py
        └───────────┬────────────┘
                    ▼
        ┌────────────────────────┐
        │ 3. Retriever           │  三条线 + Evidence 按需      retriever/*
        │   Understanding        │
        │   Memory               │
        │   Knowledge            │
        └───────────┬────────────┘
                    ▼
        ┌────────────────────────┐
        │ 4. Context Builder     │  去重 / 优先级 / Token 预算   context/builder.py
        └───────────┬────────────┘
                    ▼
        ┌────────────────────────┐
        │ 5. Tutor               │  教学契约 + 消息结构校验      tutor/tutor.py
        └───────────┬────────────┘
                    ▼
        ┌────────────────────────┐
        │ 6. LLM Adapter         │  流式生成                    llm/*
        └───────────┬────────────┘
                    ▼
             meta → delta × N → done                （SSE）
```

编排者是 `runtime.py` 的 `AgentRuntime`。它本身**无状态**：只持有配置与 LLM 适配器，
每次请求自己开一个 Retriever、构造一个 AgentState、结束就丢。

分析入口复用前两步，但**不走 Context Builder 与 Tutor** —— 它要回答的是「这一轮
暴露了什么」，不是「下一轮该怎么讲」：

```text
              POST /agent/analyze                 （main.py）
                     │
                     ▼
        1. 概念识别                              （与 Chat 同一套规则）
                     │
                     ▼
        2. Retriever                             （同一套只读接口）
                     │
                     ▼
        3. Learning Analyzer                     结构化输出 → 校验 / 收敛   analysis/analyzer.py
                     │
                     ▼
        4. LLM Adapter.complete()                非流式
                     │
                     ▼
              AnalyzeResult                      候选，不写库
```

`llm_provider=mock` 时跳过第 4 步，走 `_heuristic_analysis()`：它不是「假装有智能」，
而是让整条写入链路在没有 API Key 时仍然可测。真实 provider 走结构化 JSON，
解析失败抛 `AnalysisError`，后端据此回退 `rules-v1`。

---

## 二、入口层

| 入口 | 用途 |
| --- | --- |
| `POST /agent/chat` | 回答链路，SSE 流式返回 |
| `POST /agent/analyze` | 学习分析链路，返回候选结果（不流式、不写库） |
| `POST /agent/context/preview` | 只检索与组装，**不调用模型** |
| `GET /health` | 当前生效的适配器与预算 |
| `python -m app.cli` | 不起服务跑一次流水线（含 `--preview`） |

chat 与 preview 共用 `runtime.prepare()`，所以「给人看的上下文」与「发给模型的上下文」
不可能不一致 —— 这是刻意避免的一类 bug：调试工具与真实链路各写一份组装逻辑。

---

## 三、AgentState

`app/state/agent_state.py`

字段与 `agent-architecture.md` 第三十八节一一对应，另加三个中间产物：

```text
请求身份   run_id / user_id / conversation_id / space_id
当前输入   user_message / history
检索结果   detected_concepts / understandings / memories / knowledge / evidences
组装产出   context / response
观测       trace / pending_tasks
```

**它是运行时状态，不是数据库实体。** 不落库、不跨请求复用。
跨对话的连续性全部来自后端数据 —— 否则同一个用户的两个对话会各自看到不同的世界。

`pending_tasks` 默认是空列表：Evidence 抽取、Understanding 更新、Memory 收敛已经由
独立的 `POST /agent/analyze` 承担，**不属于**一次 chat run。早先版本把它固定写成三项待办，
结果是每次 chat 的 summary 都显示「未完成」—— 那等于把另一条链路的状态错误地记到了这一条上。
字段本身保留，留给将来确实存在、且属于当前 run 的异步收尾动作。

---

## 四、概念识别

`app/retriever/concept_detector.py`

回答「用户这句话在说哪些 Concept」，是**规则版**，不是语义理解。

```text
本轮用户消息
   ├─ 命中 canonical_name / display_name  → score 1.0
   ├─ 命中别名表（快表 → TLB）             → score 0.9
   └─ 都没命中 → 回看最近 4 轮对话
                  先看用户说过的话        → 0.6 起，每往回一轮衰减 0.1
                  再看 AI 说过的话
```

三条刻意的设计：

1. **宁可漏召回，也不瞎召回。** 匹配不到就返回空 —— 与后端 `rules-v1` 分析器同一条原则。
   误召回一个不相干的概念，会以高优先级污染整段上下文。
2. **ASCII 名称要求词边界。** `TLBSize` 不该被当成提到了 TLB。
3. **回看历史时用户轮优先于 AI 轮。** AI 的回答里几乎必然会提到概念名，
   如果两者同等对待，「用户在学什么」会被 AI 自己的措辞带偏。

这套规则将来会换成一次 LLM 结构化输出调用（含跨语言、指代消解）。
换掉时只需要保持 `detect_concepts(user_message, history, concepts) -> [(concept, score, matched_on)]`
这个签名，上层不用动。

---

## 五、Retriever

`app/retriever/`

对应 `agent-architecture.md` 第八节的三条检索线，加上按需召回的 Evidence：

| 检索线 | 回答什么问题 | 数据来源（HTTP 模式） |
| --- | --- | --- |
| Understanding | 我面对这个用户该怎么讲？ | `GET /api/understandings/{conceptId}` |
| Memory | 这个用户有什么长期信息值得影响当前回答？ | `GET /api/memories/search?spaceId=&keyword=` |
| Knowledge | 我需要参考什么知识？ | 无 —— 文档接口还是 501，固定返回空 |
| Evidence | 为什么系统会这样判断？ | `GET /api/understandings/{conceptId}/evidence` |

### Evidence 是按需的

不是每次回答都召回（架构文档第十二节）。当前用一组触发词做规则判断
（「为什么」「凭什么」「你怎么知道」「依据」「证据」「你确定」「搞错」「复盘」…），
命中才取，取最近 3 条。trace 里会记 `evidenceRequested`，所以「这次为什么带了证据」
是可查的。

### 为什么不直接连 MySQL

因为架构文档第二节写着：业务数据由 Backend 管理，Agent 不绕过 Backend 改库。
**读也走同一扇门**，这样权限、软删除、状态过滤（比如 Memory 只认 ACTIVE）
永远只有一份实现，不会在 Agent 里长出第二套规则。

### 两个实现 + 降级策略

```text
AILS_RETRIEVER_MODE = http     → 只用真实后端；连不上就报错，不偷偷用假数据
                    = fixture  → 只用本地样例数据
                    = auto     → 带 authToken 就用后端，否则用 Fixture；
                                 后端连不上时降级到 Fixture，但**留下轨迹**
```

auto 模式降级时会打 WARNING 日志、并在 trace 里体现。降级本身是合理的
（后端没起来不该让 Agent 完全不能演示），但**静默降级会让人把「召回不生效」
误判成 Agent 的 bug**，所以不能无声发生。

失败策略的区分也很重要：

- 连不上后端 / JWT 失效 → `RetrievalError`，向上抛，最终变成 SSE 的 `error` 事件；
- 某个 Concept 没有快照 → **正常返回空**，不是错误（用户还没学过这个知识点）。

---

## 六、Context Builder

`app/context/builder.py`

架构文档第七节说得很直接：不能 `SELECT *` 然后全部塞进 Prompt。
检索回来的是**候选**，这一层负责五件事。

### 1. 优先级

数字越大越先保留，顺序照抄架构文档第六节：

| 片段 | 优先级 | 能否被裁 |
| --- | ---: | --- |
| 本轮用户输入 | 100 | 只截断，**永不丢弃** |
| 对话历史（越近越高） | 90, 89, 88 … | 整条丢 |
| Understanding | 70 | 可截断保留 |
| Memory | 50 | 整段丢 |
| Knowledge | 40 | 整段丢 |
| Evidence | 20 | 整段丢 |

历史消息用**递减优先级**表示，于是「预算不够时先丢最早的几轮」不需要单独写逻辑，
它自然从排序里长出来。

### 2. 去重

按 `dedupe_key`：同一个 Concept 只留一份快照，同一条 Memory / 片段 / 证据只出现一次。
去重发生在排序之后，所以留下的总是优先级更高的那一份。

### 3. Token 预算

`AILS_MAX_CONTEXT_TOKENS`（默认 3000）。估算方式见 `context/tokens.py`：
中文 1 字 ≈ 1 token，非中文 4 字符 ≈ 1 token。不引 tiktoken，因为各厂商分词器不同，
引一个特定厂商的会让估算对别的模型不准；宁可误差方向已知（偏高一点，早一点裁剪）。

装不下的处理分两种：

- Understanding 允许截断（裁掉尾部仍然有用）；
- 历史、知识、证据是**原子**的 —— 半条消息比没有更误导，所以整段丢。

被丢弃与被截断的片段都会记进 `warnings` 与 `sections[].dropped`，preview 里直接可见。

### 4. 结构与冲突

产物分成两部分，这个划分很重要：

```text
system message  ← 教学契约 + 认知背景（Understanding / Memory / Knowledge / Evidence）
对话消息        ← 历史 + 本轮输入
```

前半是「关于这个用户的资料」，模型参考它但**不应该复述它**；后半才是要回应的话。

冲突处理写在 System Instruction 里，而不是靠拼接顺序暗示：
**用户本轮明确表达的意思，永远优先于记录里的历史偏好。**

### 5. 渲染格式

背景按固定区块渲染，每一块都带一句「这一块该怎么用」的说明：

```markdown
## 学习者的当前认知（系统长期记录）
> 这是系统对「这个用户现在怎么理解这些概念」的判断，用来决定**从哪讲起**。
> 不要向用户复述这一段的结构，也不要说「根据我的记录」。

### TLB ｜ 状态：存在误区
- 当前理解：用户理解 TLB 是页表项（PTE）的缓存。
- 已经理解：TLB 是页表项的缓存；TLB Hit 与 TLB Miss 的区别
- 仍然存在的误区（要针对性纠正，指出推理在哪一步分叉，而不是直接说「你错了」）：
  - [当前存在] 把 TLB Miss 与 Page Fault 混为一谈
- 还不确定（本次可以重点补）：Page Table 本身存放在哪里
- 用户自己的说法：「它应该就是 Page Table 的缓存。」
```

注意**状态是用中文标签渲染的**（`MISCONCEPTION` → 「存在误区」），
误区也带上了「当前存在 / 已有反向证据」—— 因为 ACTIVE 与 PENDING 该讲的方式不一样。

---

## 七、Tutor

`app/tutor/tutor.py` + `app/prompts.py`

Tutor 是架构图里「LLM」那一格。它**不检索、不判断学得怎么样、不更新任何状态**。
职责只有两条：

1. 守住教学契约：消息必须是「一条 system + 按时间序的对话 + 最后是本轮输入」；
2. 守住流式链路：空流、模型报错都要变成明确的失败，不能推着推着就没声了。

教学契约（`prompts.py`）里三条硬性规则值得单独说：

- 不要复述背景记录的结构，不要说「根据我的记录」；
- 不要向用户输出任何内部分析信号 —— 正确性判断、置信度、推理质量、证据编号，
  以及 Evidence / Understanding / Revision 这类内部术语。这是 PRD 第六节的要求；
- 记录为空时**不要编造**「我们上次聊过」这类不存在的记忆。

改教学风格只动 `prompts.py` 一个文件，上下文组装逻辑不受影响。

---

## 八、LLM Adapter

`app/llm/`

```python
class LLMAdapter(Protocol):
    async def stream_chat(messages, *, temperature=None, max_tokens=None) -> AsyncIterator[str]
    async def complete(messages, *, temperature=None, max_tokens=None) -> str
    async def aclose() -> None
```

| 实现 | 用途 |
| --- | --- |
| `OpenAICompatibleAdapter` | DeepSeek / OpenAI / Qwen / vLLM / Ollama —— 只差 base_url、api_key、model |
| `MockLLMAdapter` | 确定性假输出，离线跑通链路 & 测试断言用 |

适配器**只认 messages 与 token 参数**，不认识 Understanding、Memory、Tutor。
这是 `LLM ≠ Agent` 的直接落地：换厂商不改业务，换业务不改适配器。

`stream_chat` 服务回答链路，`complete` 服务分析链路（`LearningAnalyzer` 需要一次性拿到
完整的 JSON 才能解析）。两者共用同一份适配器协议，所以换厂商时两条链路一起生效。

---

## 九、运行轨迹

`app/trace.py`

对应架构文档第三十六、三十七节：一次运行留下 `Input Summary / Output Summary /
调用 / 状态 / 耗时`，**不保存 LLM 内部思维链**。

| 事件 | 记录什么 |
| --- | --- |
| `RUN_START` | userId / conversationId / spaceId / 输入长度 |
| `CONCEPT_DETECT` | 候选数量、命中了哪些、命中依据（`message:` / `history-2:`） |
| `RETRIEVE` | 检索源、命中的 Concept、各线取回多少条、是否请求了 Evidence |
| `CONTEXT_BUILT` | Token 估算、被丢弃的片段、warnings |
| `RUN_END` / `RUN_ERROR` | 状态与耗时 |

`matched_on` 这类字段是给排查用的：用户看不出「为什么这次召回了 TLB」，
但排查召回问题时需要它。

轨迹目前只在进程内与日志里，持久化到后端 `agent_run` 表由 Backend 负责 ——
Agent 不直接写库。

---

## 十、配置项

全部前缀 `AILS_`，写在 `agent/.env`（已被 gitignore）。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `BACKEND_BASE_URL` | `http://localhost:8080` | HTTP 检索的目标 |
| `LLM_PROVIDER` | `mock` | `mock` / `openai` |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 任何 OpenAI 兼容服务 |
| `LLM_API_KEY` | 空 | 配 `openai` 时不填会启动即报错 |
| `LLM_MODEL` | `deepseek-chat` | |
| `RETRIEVER_MODE` | `auto` | `auto` / `http` / `fixture` |
| `FIXTURE_PATH` | `fixtures/demo_space.json` | |
| `MEMORY_TOP_K` | 5 | |
| `KNOWLEDGE_TOP_K` | 5 | |
| `EVIDENCE_TOP_K` | 3 | 按需召回时的条数 |
| `RECENT_HISTORY_LIMIT` | 12 | 进入上下文的历史条数上限 |
| `MAX_CONTEXT_TOKENS` | 3000 | 上下文预算 |

---

## 十一、与后端的数据对应

`schema.sql` 里的表 → Agent 读到的模型（`app/schemas.py`）：

| 后端 | Agent 模型 | 读接口 |
| --- | --- | --- |
| `concept` | `Concept` | `GET /api/spaces/{id}/concepts` |
| `learning_understanding` | `Understanding` + `Misconception` | `GET /api/understandings/{conceptId}` |
| `memory` | `MemoryItem` | `GET /api/memories/search` |
| `learning_evidence` | `EvidenceItem` | `GET /api/understandings/{conceptId}/evidence` |
| `document_chunk` | `KnowledgeChunk` | 无（501） |
| `message` | `HistoryMessage` | 由后端随请求直接带过来，不额外拉取 |

枚举值直接沿用后端常量名（`PARTIAL` / `ACTIVE` / `PREFERENCE`…），
所以序列化不需要任何映射表。Pydantic 模型同时接受 camelCase 与 snake_case，
后端 Java record 的 Jackson 输出可以直接喂进来。
