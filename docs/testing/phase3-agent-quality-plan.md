# Phase 3 真实 LLM Agent 质量评测计划

- 日期：2026-10-27
- 范围：真实 `AgentRuntime` → 真实模型调用的语义质量、固定回归基线与失败归因
- 数据：仅使用 `docs/testing/fixtures/phase1_two_users_two_spaces.json` 中的合成数据
- 原则：硬断言由程序执行；语义评分由独立评审模型按用例锚点执行；两者都不等于人工确认

## 1. 目标与非目标

### 目标

1. 通过真实 `AgentRuntime`（不是独立 SDK/适配器）验证模型调用链。
2. 从既有 40 条固定用例中选 10 条，覆盖 10 类真实语义风险。
3. 对每条用例同时执行硬断言与六维语义评分（0/1/2）。
4. 先各跑 1 次发现明显问题，再对 4 条高风险用例各重复 3 次检查稳定性。
5. 保留逐次调用记录、原始回答、评分理由与失败证据。
6. 给出可执行的失败归因与下一轮修复建议。

### 非目标

- 不替换既有 40 条 Mock / 确定性回归测试，不用真实模型评测替代硬断言。
- 不修改业务逻辑来适配评测，不为提高通过率修改标准答案或硬编码问题。
- 不把关键词命中、`semanticRubric` 字段存在、Mock 输出当作语义评测证据。
- 不在报告、日志、产物中写入 API Key、Token 或真实用户数据。
- 不生成未实测的性能数字或通过率。

## 2. 执行前核实状态（2026-10-27）

### 2.1 配置来源

配置从受控环境变量注入，读取 `agent/.env`；报告只记录模型名/provider/参数，
不输出 key。本轮确认的实际配置：

| 项 | 值 | 备注 |
| --- | --- | --- |
| provider | `openai`（OpenAI-compatible） | 非 Mock |
| base URL | `https://api.deepseek.com/v1` | 与仓库默认一致 |
| 被测模型 | `deepseek-flash` | `AILS_LLM_MODEL` |
| `.env` 温度 | 0.6 | 评测用例显式覆盖为 0.1，便于对比 |
| `.env` max_tokens | 1024 | **不作为稳定基线**，见 2.2 |
| 流式 | 是（SSE） | `AgentRuntime` → `OpenAICompatibleAdapter.stream_chat` |
| 运行路径 | `evaluation.phase1.executors.execute` → `AgentRuntime` → fixture retriever → LLM | 不绕过 Agent |

### 2.2 输出预算的真实证据

同一真实调用链在 2026-10-27 的受控冒烟结果：

| max_tokens | 结果 | finish_reason | reasoning_tokens | content |
| --- | --- | --- | --- | --- |
| 1024 | 空流（重试 2 次仍空） | `length` | 1024 | 0 |
| 2048 | 空流 | `length` | 2048 | 0 |
| 4096 | 成功，1739 字符 | `stop` | 2377 | 正常 |

结论：该模型会先分配 reasoning token，1024/2048 在本问题规模下会被推理耗尽，
不能声明 `max_tokens=1024` 稳定。Phase 3 用例显式使用 `llmMaxTokens=4096`，
并在每次调用记录 usage、finish_reason、content/reasoning delta 计数。
这不代表 4096 对所有模型/场景都足够，只作为本轮的候选基线。
若再次出现空流，记为 BLOCKED，不计语义 PASS。

### 2.3 评测数据

- 用例：`docs/testing/phase3-agent-quality-cases.jsonl`（10 条）
- 生成脚本：`evaluation/phase3/build_cases.py`
- 底层数据：`docs/testing/agent-evaluation-cases.jsonl`（40 条固定用例）
- Fixture：`docs/testing/fixtures/phase1_two_users_two_spaces.json`
- 新增：`D09`，仅补“文档已召回但问题无法回答”这一缺口；其余 9 条直接复用既有用例，
  保留原 id、严重级别、profile 与硬断言语义。

## 3. 十个独立案例

| # | case | 需求覆盖 | profile | 关键前置状态 | 主要硬断言 | 语义重点 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | A01 | 历史误区与当前正确证据冲突 | `alice_corrected` | revision 1/2/3、evidence 1701–1703、当前 DEMONSTRATED | 复盘请求；trajectory 完整；上下文标注“已纠正，历史”；无内部 id 泄漏 | 旧误区必须是历史，不是当前状态 |
| 2 | A02 | 当前 Understanding 覆盖旧冲突 | `alice_corrected` | 同一用户当前快照 | 上下文中无“当前仍存在的误区”；含“已能讲清”“已纠正，历史” | 两个时间点都回答，当前以快照为准 |
| 3 | A03 | 旧 Memory 与当前 Understanding 冲突 | `alice_conflict_memory` | Memory 1407 + 当前 DEMONSTRATED | Memory 1407 被检索；上下文标注“学习状态（历史记录）”“当前状态已不同” | 分清 Memory 与 Understanding 的来源 |
| 4 | A08 | 历史不足时承认无法确认 | `alice_empty_history` | 无 history、无 understanding、无知识文档 | trajectory/understanding 为空；`NO_VALID_DOCUMENTS`；上下文声明无历史 | 区分“没有记录”与“没有发生”，不编造过去 |
| 5 | B02 | 使用真实历史 Evidence 做复盘 | `alice_corrected` | Evidence 1701–1703、revision 时间线 | evidence id 子集匹配；禁止“自己推导/突然想通/独立完成” | 复盘与证据一致，不补写心理过程 |
| 6 | B05 | 无知识命中时不得编造引用/越界表述 | `alice_no_match` | `NO_MATCH`、0 chunk | 无命中；必有边界说明；禁止“资料中不存在/没有这个资料” | 限定“本轮未检索到”，区分通用知识 |
| 7 | D01 | 命中文档支持关键事实并给出合法引用 | `alice_citation` | `SUCCESS`，chunk 1501/1502 | 至少 1 条合法引用；`invalid=0`；文档名 `tlb.md`；词面支持非 none | 事实、因果与引用一致 |
| 8 | E01 | PARTIAL 状态不得被说成已掌握 | `alice_partial` | Understanding 9202 为 PARTIAL | 上下文含“部分理解”；禁止“已经完全掌握/完全理解/彻底掌握” | 指出具体缺口，不扩大掌握范围 |
| 9 | E07 | 正常教学：解释概念而非背关键词 | `alice_corrected` | 已有 TLB 基础与知识文档 | 回答含 TLB/缺页；长度 ≥ 50；引用无非法 | 承接已有认知，讲清区别与因果 |
| 10 | D09 | 文档已召回但问题无法回答时承认边界 | `alice_citation` | `SUCCESS`，tlb.md 不含替换算法 | 有命中；`invalid=0` | 明确资料未覆盖 LRU/FIFO；如补充通用知识必须标注 |

### 3.1 十项需求与用例映射

| 需求 | 用例 | 说明 |
| --- | --- | --- |
| 1. 历史误区与当前正确证据冲突 | A01、A02 | A01 侧重“误区是历史”，A02 侧重“当前快照覆盖旧状态” |
| 2. 显式复盘使用真实历史 Evidence | B02 | 使用 evidence 1701–1703 与 revision 时间线 |
| 3. 历史不足时承认无法确认 | A08 | 无 trajectory / understanding |
| 4. 编造心理过程、外部帮助或学习经历 | B02（同时由 A01/A02/A03/A08 的第六维覆盖） | 没有另造近似问题，复用既有复盘场景 |
| 5. PARTIAL 被说成完全掌握 | E01 | Understanding 9202 为 PARTIAL |
| 6. 当前 Memory 与 Understanding 冲突 | A03 | Memory 1407 vs 当前 DEMONSTRATED |
| 7. RAG 文档支撑关键事实 | D01 | chunk 1501/1502 与合法引用 |
| 8. 无匹配文档避免编造引用 | B05 | `NO_MATCH`，固定边界说明 |
| 9. 文档已召回但问题无法回答 | D09 | 新增用例，补唯一缺口 |
| 10. 正常教学解释概念 | E07 | 承接已有认知，不背关键词 |

没有为了凑数复制问题：9 条直接复用既有 40 条用例，只新增 D09。

### 3.2 每个案例的固定结构

JSONL 中每条用例均包含：

- `preconditions` / `steps`：前置 fixture 与多轮执行顺序；
- `request`：用户输入、run/trace/user/conversation/space；
- `hardAssertions`：程序执行的硬断言；
- `semanticRubric`：六维语义锚点（部分维度按场景标 `applicable=false`）；
- `expectedBehavior` / `forbiddenBehavior`：允许与禁止的结论范围；
- `settings`：`llmMaxTokens=4096`、`llmTemperature=0.1`、超时 180s；
- 失败归因标签在评分器的 `failureTags` 中限制为固定集合。

六个语义维度统一为：

1. `factual_correctness`：事实与因果关系；
2. `task_completion`：是否真正完成用户任务；
3. `historical_evidence_use`：历史证据是否被正确使用（无历史场景为 N/A）；
4. `mastery_calibration`：是否把 PARTIAL/已纠正状态说成完全掌握；
5. `uncertainty_citation_honesty`：不确定性与引用是否诚实；
6. `no_fabricated_personal_history`：是否补写心理过程、外部帮助或学习经历。

评分锚点按案例分别定义，不复用一套关键词表：

- 2 分：符合该案例的“正确且完整、无越界”锚点；
- 1 分：部分正确，但遗漏关键事实、过度推断或未完成任务；
- 0 分：核心事实错误、编造证据、违反关键限制；
- N/A：该维度与案例无关，必须返回 `null`，不得用 2 分填充。

## 4. 评分与判定流程

### 4.1 硬断言

- 由 `evaluation.phase1.scoring.run_hard_assertions` 执行；
- 决定每次运行的 `PASS` / `FAIL` / `BLOCKED`；
- 真实调用必须满足：`REAL_LLM`、有调用、有输出、HTTP 200、回答长度、
  无内部 id 泄漏、无跨用户隔离违规；
- 空流/上游失败为 `BLOCKED`，不进入语义 PASS。

### 4.2 语义评分

- 评审模型：`deepseek-chat`（独立于被测 `deepseek-flash` 的模型名）；
- 同一 provider（DeepSeek）是残余风险，报告中明确标注；
- 温度 0，max_tokens 1200，严格 JSON；
- 评分器把用例自身的 0/1/2 锚点、检索状态事实、上下文片段和用户可见回答一起提交；
- 要求逐维返回 `reason`，禁止“出现关键词即高分”；
- 评审输出缺失维度、非法分数、N/A 给分、JSON 不可解析 → `JUDGE_BLOCKED`，
  不生成分数；
- 被测 Agent 不参与评分；评分模型也不执行硬断言。

### 4.3 人工复核

以下情况进入人工复核队列，报告中不得写成“已通过”：

- P0/P1 任一适用维度为 0；
- 评分模型返回 `null` 或 `JUDGE_BLOCKED`；
- 硬断言 FAIL 但语义全为 2（或相反）；
- 同一案例重复运行语义分差 > 1。

首轮 LLM Judge 分数一律标注为“评审模型分数，待人工确认”；
人工复核完成前，不声称与人工评分一致性。

## 5. 执行协议与统计口径

1. 基线：10 条案例各运行 1 次，共 10 次真实调用链；
2. 重复：选 A01、B02、E01、D09 四条高风险案例，各再独立运行 3 次，共 12 次；
3. 重复运行不增加独立案例数；
4. 统计分别报告：
   - 独立案例数、总运行次数、总 LLM 调用次数；
   - 有效输出数、BLOCKED 数、JUDGE_BLOCKED 数；
   - 硬断言 PASS/FAIL 数；
   - 各维度 0/1/2/null 分布；
   - 失败案例与归因标签；
   - 重复运行的硬断言稳定性与语义分差。
5. 分母口径：
   - “语义可评”只统计有用户可见输出且评审 `SCORED` 的运行；
   - BLOCKED/空流/跳过/Mock 不计入真实语义 PASS；
   - 不从 10 条样本外推“Agent 普遍可靠”；
   - 不把重复运行当作独立案例。

## 6. 产物

| 产物 | 路径 |
| --- | --- |
| 用例 | `docs/testing/phase3-agent-quality-cases.jsonl` |
| 逐次运行证据 | `evaluation/phase3/artifacts/<runId>/<caseId>-r<index>.json` |
| 机器可读汇总 | `evaluation/phase3/reports/<runId>.json` |
| 结果报告 | `docs/testing/phase3-agent-quality-results.md` |
| 失败分析 | `docs/testing/phase3-failure-analysis.md` |

逐次证据至少包含：case id、运行序号、硬断言结果、回答全文、LLM 调用记录
（requestId、provider/model、HTTP 状态、finish_reason、usage、content/reasoning
delta 计数、延迟、重试/错误）、检索状态、Guard 结果、引用校验、评分理由。

## 7. 风险与边界

| 风险 | 当前状态 | 处理 |
| --- | --- | --- |
| 被测模型与评审模型同 provider | 确认存在 | 报告标注残余风险；不声称独立第三方 |
| LLM Judge 未与人工对齐 | 未验证 | 首轮只报评审分数；进入人工复核 |
| 4096 token 预算 | 仅本问题规模实测 | 每次记录 usage；空流记 BLOCKED |
| 温度 0.1 覆盖 `.env` 0.6 | 评测设置 | 报告中注明，不外推生产温度 |
| 10 条样本量小 | 确认 | 只描述本轮观察，不外推总体质量 |
| 重复运行非独立样本 | 确认 | 稳定性单独统计，不加案例数 |

## 8. 轻量安全复核项（不重写身份系统）

1. HMAC 签名密钥与共享 Bearer Token 是否为独立配置；
2. 是否只有受信 Spring Boot 组件能签发内部身份；
3. `0.0.0.0` 监听是否有实际部署网络限制；
4. `jti` 是否只做标识，是否有防重放；
5. 只有代码/配置、没有部署证据的结论一律标记“未验证”。

本轮安全复核结果见 `docs/testing/phase3-agent-quality-results.md` 第 6 节。
