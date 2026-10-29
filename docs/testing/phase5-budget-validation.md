# Phase 5：预算裁剪下的检索状态与引用边界验证

- 日期：2026-10-29
- 代码版本：`6d05486`（工作区未修改生产代码）
- 范围：只验证一个行为 —— 检索成功、但片段因上下文 Token 预算未进入最终
  Prompt 时，系统能否区分「检索状态」与「最终注入状态」，以及回答是否会把
  未注入片段当作依据、或把预算丢弃说成「没有检索到资料」。
- 本轮**没有**修改业务代码或既有测试；新增了评测脚本、查询集、结果文件，
  以及运行时契约测试
  [agent/tests/test_runtime_budget_contract.py](../../agent/tests/test_runtime_budget_contract.py)。

## 1. 目标问题确认

### 1.1 问题定义

一次知识检索有两个不同的事实：

| 事实 | 来源 | 含义 |
| --- | --- | --- |
| 检索状态 | Retriever | 有没有可用文档、有没有命中、是否被阈值过滤、是否降级 |
| 最终注入状态 | Context Builder | 预算裁剪后，模型实际在 Prompt 里看到了哪些片段 |

两者在预算充足时一致；在预算不足时会分叉。目标错误行为是：

1. **证据状态误述**：检索成功且片段被预算丢弃，回答却说「本轮没有检索到资料 /
   没有可用资料」，把「没放下」说成「没检索到」；
2. **引用越界**：回答引用了最终上下文里不存在的 `资料《文件名》#序号`，
   尤其是被预算丢弃的片段。

### 1.2 历史证据：这个问题确实发生过

Phase 4 的预算压力 preview 记录了一个已确认的实现缺口：

- 检索层状态 `SUCCESS`，找到 2 个知识片段；
- 最终注入 0 个片段，`knowledge:18`、`knowledge:19` 都在 `dropped_sections`；
- system instruction 中没有任何「已检索到但被预算丢弃」的说明。

也就是说，修复前模型只能看到「没有知识片段」，无法区分「没检索到」和
「检索到了但没放下」。证据见
[phase4.1-context-budget-results.md](phase4.1-context-budget-results.md) §1。

该缺口在 Phase 4.1 的 `2835deb` 中修复：Context Builder 引入
`SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`，在预算裁剪之后补一个不参与预算的
`knowledge_status` 区块，并让引用校验按「实际注入的 section」过滤。
代码位置：[agent/app/context/builder.py](../../agent/app/context/builder.py)
的 `_final_knowledge_state`、`_upsert_budget_knowledge_status`、
`_budget_knowledge_status_note`；引用侧
[agent/app/citations.py](../../agent/app/citations.py) 的
`provided_labels_from_context`。

**因此本轮不是预设「问题仍然存在」**：历史缺口有明确证据，且已有修复；
本轮要验证的是修复后的**当前版本**在更多真实检索查询与 `SUCCESS_PARTIAL`
场景下是否仍符合预期，以及运行时是否还有未覆盖的边界。

### 1.3 当前实现的控制流

1. `AgentRuntime._retrieve` 把检索结果和 `knowledge_status` 写进 `AgentState`；
2. `ContextBuilder.build` 先按优先级 `_fit_budget`，再按渲染后的 token 估算
   `_reduce_one`，然后由 `_final_knowledge_state` 计算最终状态；
3. 只有检索状态为 `SUCCESS` 时才派生 `SUCCESS` / `SUCCESS_PARTIAL` /
   `SUCCESS_BUT_DROPPED`；其他检索状态保持原值；
4. 若最终状态是 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`，
   `_upsert_budget_knowledge_status` 在预算之后补/替换状态区块，明确写出
   「检索成功」、进入/丢弃数量，以及「不等于没有检索到资料」；
5. `provided_labels_from_context` 只返回未被 `dropped` 的 knowledge section；
   `validate_citations` 把不在其中的标签判为非法；
6. `AgentRuntime.generate` 的 Evidence Guard 只处理
   `NO_VALID_DOCUMENTS` / `NO_MATCH` / `THRESHOLD_FILTERED` / `DEGRADED`，
   **不处理** `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`。

第 6 点是刻意设计：预算状态已经写进 Prompt，Guard 再追加「没有可用资料」
会与 Context 自相矛盾。但它也意味着：如果模型无视 Prompt、仍说「没有检索到
资料」，运行时不会纠正。这个边界由本轮的确定性构造测试单独验证，不能与
真实模型行为混为一谈。

## 2. 查询集与构造测试

查询集在 [evaluation/phase5_budget/queries.jsonl](../../evaluation/phase5_budget/queries.jsonl)，
共 **27 条**，展开为 **46 次运行**（同一问题可有 normal / budget 两个场景）。

### 2.1 真实性说明

- 仓库内**没有找到可用的真实用户学习提问记录**，本轮也没有接入生产流量；
- 因此 27 条全部标记 `realUserTraffic: false`；
- 其中 18 条来自既有评测集 `evaluation/rag/dataset_v2.jsonl` 的问题，
  6 条基于现有合成语料新构造，3 条是固定 chunk 的边界构造；
- 这不构成真实用户验证，也不构成产品市场验证，只能说明「在固定语料与
  固定查询上，当前版本的实际行为」。

### 2.2 覆盖范围

| 类别 | 条数 | 代表用例 | 覆盖点 |
| --- | --- | --- | --- |
| 短问 / 口语化 | 5 | P5-01「TLB 是啥？」、P5-02 | 短输入、检索命中、预算丢弃 |
| 历史依赖 | 2 | P5-05、P5-06 | 用户错误说法、缺少历史轨迹时不得编造 |
| 多文档 / 版本冲突 | 8 | P5-07、P5-09、P5-20、P5-21 | A/B 集群、ZenFrame/ZealDeck、来源归属 |
| 文档中无答案 | 5 | P5-13…P5-17 | 不得编造 CEO、价格、不存在的文档 |
| 多事实 / 条件 | 4 | P5-18、P5-19、P5-23、P5-24 | 多事实拆句、条件分支 |
| 小计（真实检索查询） | **24** |  |  |
| 预算 / 引用边界（另有） | 3 | P5-B01…P5-B03 | 部分注入、全部丢弃、答案片段被丢弃 |

真实 Qdrant 检索用例 24 条（上表前五类之和），固定 chunk 构造用例 3 条，
合计 27 条；表中「预算 / 引用边界」是额外构造用例，不与前五类相加。
固定用例使用 `ORBIT-4721` 合成规范与历史迁移文档，答案片段分别被放大到足以在
`budget=400` / `3000` 下被整体丢弃，用于稳定制造
`SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`。

### 2.3 每条查询记录什么

`queries.jsonl` 为每条记录：来源、是否真实用户流量、类别、问题、期望相关文档、
期望事实、期望行为、禁止行为、检索方式（`qdrant` / `fixed`）与场景
（预算、合成历史条数、合成记忆条数）。

运行结果 `runs.jsonl` 另存：真实检索状态与候选、完整 chunk、最终
system instruction、注入/丢弃 chunk ID、模型原始回答、`finish_reason`、
引用校验报告与逐条判定。

## 3. 指标定义

### 3.1 目标错误行为发生率

在**明确标注为预算压力**的场景中：

```text
分子 = 出现「证据状态误述」或「引用越界」的运行数
分母 = 检索状态 SUCCESS、最终状态 SUCCESS_PARTIAL / SUCCESS_BUT_DROPPED、
       回答非空、finish_reason != length、模型调用未报错的运行数
```

- **证据状态误述（硬口径）**：回答句中出现「没有检索到资料 / 文档 / 材料 /
  片段」或「没有可用资料」类陈述，且同一句没有提到预算、上下文、未放入、
  截断、prompt 等限定词；
- **引用越界**：`validate_citations` 的 `invalid > 0`，即标签不在
  `provided_labels_from_context` 中；其中被预算丢弃的片段单独计数；
- **软口径（不计入分子）**：「资料中没有提到 X」这类文档内容陈述。
  它可能是对已注入片段的正确描述，也可能是忽略了被丢弃片段；
  单独报告，供人工复核，不混进硬指标；
- **分母排除**：未达到预算压力（最终状态仍为 `SUCCESS`）、模型报错、
  回答为空、`finish_reason=length` 的运行。排除原因逐条写入 `summary.json`。

### 3.2 为什么不做修复前后对比

本轮没有修改生产代码，也没有恢复修复前的代码行为。历史 Phase 4 的证据是
**另一个上下文、另一个脚本**留下的 preview / 单样本，不能与本轮 46 次运行
直接相减。因此本报告只给「当前版本结果」，不编造修复前错误率，也不把
Phase 4.1 的 1 条受控样本当作本指标的基线。

### 3.3 口径限制

- 状态误述是关键词筛查，不是语义判分；所有命中句与回答原文都会落盘，
  供人工复核；
- 一次运行只调用一次模型，没有重复采样；temperature 固定为 0.0 以便复现，
  这与生产默认 `0.6` 不同；
- 只覆盖 1 个模型、1 套合成语料、24 条真实检索查询（对应 19 次预算运行，
  其中 17 次进入指标）+ 3 个构造边界；不能外推为「所有问题 / 所有模型都不会误述」。

## 4. 运行条件

| 项 | 值 |
| --- | --- |
| 代码版本 | `6d05486` |
| Agent 配置 | `agent/.env`，`retriever_mode=auto` 不参与本脚本（脚本直接调用 `KnowledgeSearchService`） |
| 模型 | `deepseek-flash`（OpenAI-compatible） |
| temperature / max_tokens | 0.0 / 1200 |
| 上下文预算 | 3000 tokens（构造边界另用 400） |
| Embedding | `text-embedding-v4`，1024 维 |
| Qdrant | `http://127.0.0.1:6333`，collection `ails_phase42_real_e2e_text-embedding-v4_1024` |
| 语料 | user 14 / space 22，文档 85–91，283 points |
| Prompt | `BASE_SYSTEM_INSTRUCTION`，SHA-256 前 16 位写入 `summary.json` |

复现命令：

```bash
# 在 agent/ 目录下
python ../evaluation/phase5_budget/run_budget_queries.py --dry-run
python ../evaluation/phase5_budget/run_budget_queries.py
python ../evaluation/phase5_budget/repro_runtime_gap.py
```

`--dry-run` 只构建上下文、不调用模型，用于确认预算压力真的把知识片段裁掉；
它的状态不能替代真实模型结果。

## 5. 当前版本基线结果

原始结果目录：
[`evaluation/phase5_budget/reports/20261029T101821Z/`](../../evaluation/phase5_budget/reports/20261029T101821Z/)
（`runs.jsonl` 为首轮判定，`runs.v2.jsonl` 为修正筛查规则后的重算；
模型输出在两次判定之间没有变化）。逐条运行明细见
[summary.v2.md](../../evaluation/phase5_budget/reports/20261029T101821Z/summary.v2.md)。

### 5.1 查询数、运行数、有效运行与引用数

先把四种计数分开，避免把「27 条查询」误读成「27 次模型调用」或
「20 个独立问题」：

| 计数 | 数值 | 构成 |
| --- | --- | --- |
| 查询数 | **27** | 24 条真实 Qdrant 检索查询 + 3 条固定 chunk 构造查询 |
| 运行数 | **46** | 19 条真实查询各有 normal + budget 两次运行（38）；其余 5 条（P5-13…P5-17）只有 normal（5）；3 条构造查询各 1 次（3） |
| 有效运行数 | **20** | 17 次真实预算运行 + 3 次构造运行 |
| 引用数 | **83** | normal 81 + 构造 2；真实预算运行 0 |
| 产生引用的运行数 | **24** | normal 22 + 构造 2；真实预算运行 0 |

46 次运行的场景与最终状态：

| 项 | 数 |
| --- | --- |
| normal 场景 | 24 |
| budget 场景 | 19 |
| 固定部分注入（P5-B01） | 1 |
| 固定全部丢弃（P5-B02） | 1 |
| 固定答案片段注入（P5-B03） | 1 |
| 最终状态 `SUCCESS` | 23 |
| 最终状态 `SUCCESS_BUT_DROPPED` | 20 |
| 最终状态 `SUCCESS_PARTIAL` | 2 |
| 最终状态 `THRESHOLD_FILTERED` | 1 |
| `finish_reason=stop` | 39 |
| `finish_reason=length` | 6 |
| 模型空返回 / 报错 | 1 |
| **计入指标的有效运行** | **20** |

### 5.1.1 26 次未计入运行的逐类原因

| 排除类别 | 运行数 | 原因 |
| --- | --- | --- |
| normal 未达到预算压力 | 19 | 最终状态仍为 `SUCCESS`，不是预算场景，不能进入预算指标 |
| normal 被截断 | 4 | `finish_reason=length`：`P5-03`、`P5-05`、`P5-06`、`P5-24` |
| normal 模型空返回 | 1 | `P5-17`：`reasoning_exhausted_budget` |
| budget 被截断 | 2 | `finish_reason=length`：`P5-05::budget`、`P5-24::budget` |
| **合计** | **26** | 46 − 20 |

其中 24 次 normal = 19 次「未达到预算压力」+ 4 次截断 + 1 次空返回；
2 次 budget 截断与 normal 的 4 次截断合计为 6 次 `finish_reason=length`。
`P5-17::normal` 的检索状态本身是 `THRESHOLD_FILTERED`，不是预算裁剪，
因此它既不属于「预算错误」，也不能算作预算场景的成功样本。

有效运行的 20 次由以下状态构成：`SUCCESS_BUT_DROPPED` 18 次
（17 次真实预算 + B02）、`SUCCESS_PARTIAL` 2 次（B01、B03）。

### 5.2 指标结果

| 指标 | 分子/分母 | 结果 |
| --- | --- | --- |
| **目标错误行为率** | **0/20** | **0.0** |
| 证据状态误述率（硬口径） | 0/20 | 0.0 |
| 引用越界率 | 0/20 | 0.0 |
| 软口径「资料中没有」陈述（不计入指标） | 0/20 | 0.0 |
| 明确说明预算/上下文未包含 | 19/20 | 0.95 |
| 期望文档确实被检索到（doc 级） | 20/20 | 1.0 |
| 期望文档进入最终上下文 | 1/20 | 0.05 |

最后一行是**操控检查**，不是质量指标：预算场景本来就应该把期望文档裁掉；
只有 `P5-B03` 的答案片段被保留。

### 5.3 引用分布

| 范围 | 运行数 | 有引用运行 | 引用总数 | valid | invalid | malformed | unsupported | numericConflict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| normal | 24 | 22 | 81 | 81 | 0 | 0 | 4 | 17 |
| 预算/构造 | 22 | 2 | 2 | 2 | 0 | 1 | 0 | 0 |
| 合计 | 46 | 24 | 83 | 83 | 0 | 1 | 4 | 17 |

- **0 条 invalid**：所有真实模型引用都指向本轮实际注入的标签；
- 计入指标的 20 次运行里，只有构造用例 `P5-B01`（引用注入的干扰片段）和
  `P5-B03`（引用注入的当前规范）各产生 1 条引用；17 次真实预算运行全部
  没有引用，因为没有任何标签进入上下文。因此「引用越界 0/20」的
  **暴露面很小**，不能据此断言模型不会越界；
- `malformed=1` 是 `P5-22::budget` 回答里的格式示例
  `[资料《…》#n]`，不是真实引用；
- `numericConflict=17`、`unsupported=4` 是词面/数字启发式诊断，**不是**
  本轮的目标错误，也没有人工语义评分；不能写成「来源归属正确率」。

### 5.4 筛查规则修正（透明记录）

首轮关键词筛查把 3 条**正确披露**误报为状态误述：

- `P5-11::budget`：「这既不等于『没有相关资料』……」；
- `P5-22::budget`：「我不能说『没有检索到相关资料』——检索是成功的」；
- `P5-21::budget`：「在对话内容里，我没有找到……记录」——这是历史记录，
  不是文档检索状态。

人工检查原始回答后，收紧了筛查：硬口径必须带「资料/文档/材料/内容/依据/
片段」宾语，并排除「不等于 / 不能说 / 不是」引述；同时把「资料中没有提到
X」这类内容陈述单独列为软口径，不进入分子。随后对**已保存的原始回答**
重算，模型输出没有重新生成。重算结果才是第 5.2 节的 0/20。
这条修正本身说明：关键词筛查会误报，最终判定仍需人工复核。
首轮 3 条误报的完整回答见
[summary.json](../../evaluation/phase5_budget/reports/20261029T101821Z/summary.json)
的 `aggregate.errors`；重算后的空列表见
[summary.v2.json](../../evaluation/phase5_budget/reports/20261029T101821Z/summary.v2.json)
的 `aggregate.errors`。

### 5.5 未计入运行与选择偏差

- 6 次 `finish_reason=length`：`P5-03::normal`、`P5-05::normal`、
  `P5-05::budget`、`P5-06::normal`、`P5-24::normal`、`P5-24::budget`。
  其中 `P5-24::budget` 只输出 123 字符就被截断（completion 1200 tokens 中
  1127 是 reasoning tokens），无法判断它原本是否会披露预算；
- 1 次空返回：`P5-17::normal`，`reasoning_exhausted_budget`；
- 因此分母排除了「回答很长/推理很长」的运行，可能存在选择偏差：
  被截断的预算回答没有机会展示完整状态说明。
- 这些运行既不算成功，也不算失败；不能通过重复运行把它们「补成」失败或成功。

## 6. 案例与边界

### 6.1 已验证案例：部分注入时正确区分可见/不可见片段

`P5-B01::partial_answer_dropped`（固定 chunk，构造边界）：

- 问题：「ORBIT-4721 新部署在失败后等待多久再重试？」；
- 检索：2 个片段，`SUCCESS`；
- 最终状态：`SUCCESS_PARTIAL`，注入 `[702]`（日志格式干扰片段），
  丢弃 `[701]`（含 17 秒的答案片段）；
- 模型回答（原文摘录）：

> 上传的资料里没有这个数值，因此无法确认 ORBIT-4721 新部署失败后的重试等待时间。
>
> 我能看到的这份资料片段讲的是日志格式本身。……所以它不是讲重试策略的文档，
> 不能从中读出一个秒数或分钟数。
>
> 本轮检索到两个片段，但只有上面这一个进入了本次上下文；另一个因预算限制
> 没有纳入。所以准确的说法是：**可见的这份资料不含重试窗口**。至于另一个
> 片段是否有相关信息，我现在看不到，不能替它断言「没有」，也不能猜它的内容。

- 引用：1 条，指向实际注入的 `[资料《orbit_log_format_p5.md》#0]`，合法；
- 未引用被丢弃的 `orbit_current_p5.md#0`，也没有编造 17 秒或 12 秒。

这个案例同时满足：说明「检索到了但没放入」、只使用可见片段、不引用被丢弃
片段。它是**构造边界**，不是真实用户问题；证明的是当前链路在该输入下行为
符合预期，不是模型普遍如此。

### 6.2 已验证案例：全部丢弃时拒绝编造

`P5-B02::dropped_all`：2 个片段全部被预算丢弃，注入 `[]`。回答原文摘录：

> 原因是：本轮确实检索到了 2 个资料片段，但它们因上下文 Token 预算限制
> 没能进入我这次可见的上下文。所以我手上没有能支撑这个具体数值的原文，
> 也就不能把任何数字说成 ORBIT-4721 的配置。

引用 0 条，没有编造 17 秒或 12 秒。

### 6.3 构造边界：运行时不会拦截脚本化误述

`evaluation/phase5_budget/repro_runtime_gap.py` 用固定 `FakeLLM` 输出：

> 本轮没有检索到任何相关资料，因此无法确认 ORBIT-4721 的重试窗口。
> [资料《orbit_current_p5.md》#0]

在生产 `AgentRuntime.generate` 下复现结果：

| 项 | 值 |
| --- | --- |
| 检索状态 | `SUCCESS` |
| 最终状态 | `SUCCESS_BUT_DROPPED` |
| 注入 / 丢弃 | `[]` / `[701]` |
| 运行时输出 | 与脚本化模型输出**逐字相同** |
| Guard | `SUCCESS_BUT_DROPPED` 不在 `GUARD_STATUSES`，未追加说明 |
| 引用校验 | `invalid=1`，但只记录，不修改回答 |

这证明：预算状态的保护目前**只在 Prompt 层**；如果模型无视 Prompt，
运行时不会纠正状态误述，也不会阻断越界引用。它是确定性构造测试，
不是「模型会这样说」的样本。

### 6.4 已计入指标但第三轴未披露：B03

`P5-B03::partial_answer_injected` 是 20 次有效运行之一：当前规范片段
`[721]` 注入，历史片段 `[722]` 被丢弃。回答引用
`[资料《orbit_current_p5.md》#0]` 并给出 17 秒。它必须按三个轴分开看，
不能合并成一个「成功/失败」：

| 轴 | 本案例 | 依据 |
| --- | --- | --- |
| 没有误述检索状态 | **是** | 回答没有说「没有检索到资料」；它承认只看到一条具体数值 |
| 没有引用越界 | **是** | 只引用实际注入的 `[721]`，没有引用被丢弃的 `[722]` |
| 披露了所有被裁剪资料 | **未披露** | 回答没有说明 `[722]` 被预算丢弃；是否应披露、披露到什么程度，本轮没有人工定义 |

因此它在硬指标里**计为一次非错误运行**，但这只是「按当前硬口径没有命中」，
不等于「三个轴都合规」。「未披露」是观察到的事实，不是本轮定义的失败；
在没有人工定义的披露标准之前，本报告**不把它单独判为成功或失败**；
`semanticReview` 仍为 `PENDING`。这个案例说明硬指标 0/20 不等于
「所有方面都完美」。

## 7. 能证明什么、不能证明什么

### 7.1 可以证明

- 在固定语料与固定查询上，Context Builder 能区分三种注入状态，并在
  预算裁剪后把 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED` 写进最终 Prompt；
- 引用校验按「实际注入的 section」判定，被预算丢弃的标签会被判为非法；
- 在本次真实模型样本中，硬口径状态误述与引用越界的实际发生次数
  （见第 5 节），以及哪些运行因压力不足/报错/截断未计入分母；
- 确定性构造测试可以稳定复现：当模型输出固定为「没有检索到资料 + 引用丢弃
  片段」时，运行时不会改写回答，引用报告只记录非法、不阻断。

### 7.2 不能证明

- **不能**证明真实用户会遇到或不会遇到该问题：没有真实用户流量，
  所有查询均为语料内问题或构造问题；
- **不能**证明语义正确率：状态误述只做关键词筛查，引用校验只做标签合法性，
  `lexical_support` / `numericConflict` 是启发式，不是事实蕴含；
- **不能**证明所有模型行为一致：只测了 `deepseek-flash`，temperature=0.0，
  每例一次；生产默认 temperature=0.6，结果可能不同；
- **不能**证明 20 次计入指标的运行相互独立：同一问题最多有一次预算运行，
  其中 17 次来自真实检索查询（17 个问题）、3 次来自构造边界，样本量仍然很小；
- **不能**把确定性构造测试写成「模型会这样说」：它只证明运行时兜底的范围；
- **不能**把本轮结果与 Phase 4.1 的单样本或 Phase 4 preview 相减当作修复前后
  对比。

## 8. 简历描述建议（需与证据一起使用）

### Agent 方向

- 「为有状态学习 Agent 建立预算裁剪行为评测：27 条查询（24 条真实 Qdrant
  检索 + 3 条构造边界）展开 46 次运行，验证检索状态与最终注入状态的区分，
  以及引用越界/证据状态误述的发生率；原始回答、Prompt、注入/丢弃 chunk
  全部落盘，指标口径与排除原因可复核。」
  - 证据：[queries.jsonl](../../evaluation/phase5_budget/queries.jsonl)、
    [summary.v2.json](../../evaluation/phase5_budget/reports/20261029T101821Z/summary.v2.json)、
    [runs.v2.jsonl](../../evaluation/phase5_budget/reports/20261029T101821Z/runs.v2.jsonl)。
  - 可写「46 次运行中 20 次计入指标，目标错误 0/20」；
    **不要**写成「0% 线上错误率」或「27 个真实用户问题」。
- 「用确定性测试固定 Context Builder 的状态契约：`SUCCESS_PARTIAL` 只允许
  引用实际注入的片段，`SUCCESS_BUT_DROPPED` 必须在 Prompt 中说明，且不被
  Evidence Guard 追加成『没有检索到』；并定位到运行时 Guard 只覆盖非成功
  检索状态、引用校验只观测不阻断的边界。」
  - 证据：[agent/app/context/builder.py](../../agent/app/context/builder.py)
    `_final_knowledge_state` / `_upsert_budget_knowledge_status`；
    [agent/app/citations.py](../../agent/app/citations.py)
    `provided_labels_from_context`；
    [repro_runtime_gap.json](../../evaluation/phase5_budget/repro_runtime_gap.json)
    （`statusInGuardStatuses=false`、`citationValidationObservedOnly=true`）。
  - 这里说的是「状态契约由代码固定」，**不是**「模型一定遵守」；
    模型行为证据是第 5 节的 0/20，两者要分开写。

### 后端方向

- 「梳理 Spring → Python Agent 的 SSE `done → onComplete → 消息落库` 链路，
  确认 Agent 的 `done` 事件 payload 不含 `finish_reason`，网关也不读取；
  因此被截断的回答会作为完整助手消息落库并触发后续分析；该结论用于界定
  本轮评测只统计 `finish_reason != length` 的有效样本。」
  - 证据：[runtime.py:239-247](../../agent/app/runtime.py#L239-L247)
    （`done` 只发送 `runId` / `detectedConcepts` / `estimatedTokens` /
    `elapsedMs`）；
    [HttpPythonAgentGateway.java:284](../../backend/backend/src/main/java/com/ails/backend/agent/HttpPythonAgentGateway.java#L284)
    （`case "done" -> finish(handler, terminated, true, 0, null)`，
    不解析 `node`）；
    [ChatService.java:249-266](../../backend/backend/src/main/java/com/ails/backend/service/ChatService.java#L249-L266)
    （`buffer` 整段落库、run 置 `COMPLETED`）。
  - 这是代码路径结论，本轮**未**修改后端，也**未**做端到端截断演示；
    简历中不要写成「已修复截断落库问题」。

> 简历里不要写「真实用户验证」「线上错误率」「RAG 语义准确率」；
> 本轮没有这些证据。所有指标都只适用于「固定合成语料 + 固定查询 +
> `deepseek-flash` + temperature 0.0」这一条件。

## 9. 结论与待审核事项

### 9.1 结论

- 当前版本在结构上已经具备「检索状态 ≠ 注入状态」的表达与引用过滤；
- 真实模型基线为 **0/20 目标错误**（硬口径状态误述 0、引用越界 0），
  但引用暴露面只有 2/20，且 19/20 的预算披露说明 Prompt 层的状态区块
  确实被模型读到并复述；
- 确定性构造测试确认了一个**运行时边界**：预算状态不在 `GUARD_STATUSES`，
  `validate_citations` 只观测不阻断；如果模型无视 Prompt，系统不会纠正；
- 该边界在本轮真实模型样本中**没有触发**，因此它是「未覆盖的兜底缺口」，
  不是「已观测到的线上错误」；
- 若真实模型样本未出现硬口径错误，只能说明**本样本未观察到**，不能说明
  问题已永久消除。

### 9.2 方案状态

**方案 A（已按审核意见实施为测试，未改生产行为）：见第 10 节。**

- `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED` 的 `provided_labels_from_context`
  只包含实际注入的 chunk；
- `validate_citations` 把被预算丢弃的标签判为 invalid；
- `SUCCESS_BUT_DROPPED` 进入最终 Prompt，且不在 `GUARD_STATUSES`；
- 运行时当前不会拦截错误输出的两条目标契约，以 `xfail(strict=True)`
  记录为**尚未通过**。

**方案 B（不推荐，未实施）：在运行时用关键词检测「没有检索到」并追加纠正。**

- 本轮筛查已经证明关键词会误报（3/20 正确披露被误判）；生产环境直接
  改写用户回答的风险高于收益；
- 若将来确有线上误述证据，应先做人工标注集，再考虑结构化约束，
  而不是复用本报告的筛查正则。

**方案 C（独立问题，未实施）：在 `done` 事件中补充
`finishReason` / `citationInvalidCount`。**

- 它解决的是「截断回答被当作完整消息落库」和「引用非法不可观测」，
  与本轮的状态区分问题相邻但不同；
- 需要同时改 Agent SSE payload 与后端消费逻辑，超出「最小修复」范围，
  建议单独立项。

本轮**没有修改**生产代码或既有测试；只新增了测试文件与评测文档/脚本。

## 10. 方案 A 测试补充结果

### 10.1 已有覆盖检查

新增测试前先核对了四组现有测试，避免重复其断言：

| 现有测试 | 已覆盖内容 | 本文件是否重复 |
| --- | --- | --- |
| [test_context_builder.py:421-506](../../agent/tests/test_context_builder.py#L421-L506) | `SUCCESS` / `SUCCESS_BUT_DROPPED` / `SUCCESS_PARTIAL` 的渲染文案、计数、注入/丢弃 chunk ID | 不重复文案断言；只检查 runtime 组装的最终 context 与可引用标签 |
| [test_citations.py:77-103](../../agent/tests/test_citations.py#L77-L103) | `provided_labels_from_context` 过滤 dropped section、dropped 标签非法 | 单测层已覆盖；新增的是 `AgentRuntime.validate_citations` 的生产接线 |
| [test_evidence_guard.py:216-249](../../agent/tests/test_evidence_guard.py#L216-L249) | 预算状态不被 Guard 追加「没有资料」说明 | 不重复 Guard 断言；新增的是引用来源边界与交付契约 |
| [test_runtime_citations.py:93-135](../../agent/tests/test_runtime_citations.py#L93-L135) | Phase 9 后：非法引用被过滤，原始内容写入 `CITATION_FILTER` trace | 用的是「不存在的文档」；新增的是「检索到但被预算丢弃」的 chunk |

### 10.2 新增测试

文件：[agent/tests/test_runtime_budget_contract.py](../../agent/tests/test_runtime_budget_contract.py)

| 测试 | 结果 | 验证点 |
| --- | --- | --- |
| `test_success_but_dropped_state_reaches_final_model_context` | PASS | `SUCCESS_BUT_DROPPED` 进入最终 system instruction；检索层仍持有 chunk，但 `provided_labels` 为空 |
| `test_partial_budget_exposes_only_injected_chunk_as_source` | PASS | 部分注入时只有实际注入的 chunk 进入 `provided_labels`；检索层仍持有两个 chunk |
| `test_validate_citations_rejects_dropped_chunk_as_source` | PASS | `AgentRuntime.validate_citations` 能把 dropped chunk 的引用判为 invalid |
| `test_runtime_must_not_deliver_dropped_chunk_citation` | **XFAIL（未通过）** | 目标契约：越界引用不得原样交付；当前仍交付 |
| `test_runtime_must_not_deliver_no_retrieval_claim_when_chunks_dropped` | **XFAIL（未通过）** | 目标契约：检索成功但片段被裁掉时，不得把「没有检索到」原样交付；当前仍交付 |

两条 `xfail` 使用 `strict=True`：它们是**尚未满足的目标契约**，不是跳过，
也不是对现状的认可。如果将来生产代码补上防护，它们会变成 XPASS 并让
测试失败，提醒移除标记。

### 10.3 执行结果

```text
# 新增测试
cd agent
python -m pytest tests/test_runtime_budget_contract.py -q -rx
3 passed, 2 xfailed

# 相关回归
python -m pytest tests/test_context_builder.py tests/test_runtime_citations.py \
  tests/test_evidence_guard.py tests/test_runtime_budget_contract.py -q
47 passed, 2 xfailed

# 全量 Agent 测试
python -m pytest -q
354 passed, 5 skipped, 2 xfailed, 1 warning
```

原始输出：
[test_runtime_budget_contract.log](../../evaluation/phase5_budget/reports/test_runtime_budget_contract.log)、
[related_agent_pytest.log](../../evaluation/phase5_budget/reports/related_agent_pytest.log)、
[full_agent_pytest.log](../../evaluation/phase5_budget/reports/full_agent_pytest.log)。

### 10.4 契约状态

| 契约 | 状态 | 证据 |
| --- | --- | --- |
| 预算状态写入最终模型上下文 | **满足** | 1 条 PASS（`test_success_but_dropped_state_reaches_final_model_context`）；另有既有 context/guard 测试 |
| 被裁剪 chunk 不作为可引用来源 | **满足** | `provided_labels` 只含注入标签 |
| 引用校验识别 dropped 标签 | **满足** | `report.invalid == 1` |
| 运行时拦截 dropped 标签的交付 | **未满足** | XFAIL |
| 运行时拦截「没有检索到」误述的交付 | **未满足** | XFAIL |

**新增测试没有修复任何运行时行为。** 它们只是把现有契约与缺口固定下来；
第 6.3 节的确定性复现仍然成立，第 5 节真实模型样本也只能说明「本样本
未观察到」硬口径错误。

### 10.5 尚未关闭的缺口

1. `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED` 不在 `GUARD_STATUSES`；
   模型无视 Prompt 时，状态误述会原样到达用户。
2. `validate_citations` 只写 trace / 返回报告，不修改或阻断交付；
   被丢弃片段的引用可以原样到达用户。
3. Agent 的 `done` 事件不携带 `finish_reason`，后端无法区分截断回答；
   这是相邻问题，本轮未改。
4. 真实模型样本只有 20 次有效运行、引用暴露面 2/20，且无人工语义评分；
   不能据此宣称运行时防护「不需要」。

### 10.6 Phase 9 修订（2026-10-29，保留上文历史记录）

上文第 10.2–10.5 节记录的是 Phase 5 当时的状态，**继续保留为历史口径**。
Phase 9 已改变其中两项结论：

| 原缺口 | Phase 9 状态 | 证据 |
| --- | --- | --- |
| 被丢弃片段的引用原样交付（第 2 条、10.4 第 4 行） | **已关闭** | `agent/app/citation_filter.py` 的 `StreamingCitationFilter`；`test_runtime_must_not_deliver_dropped_chunk_citation` 由 XFAIL 转为 PASS；`tests/test_citation_filter.py` 覆盖合法/越界/跨 delta/残缺/无检索/跨请求 |
| 模型误述「没有检索到资料」（第 1 条、10.4 第 5 行） | **仍开放，且有明确风险记录** | Prompt 已写入 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`；但自然语言断言无法在不做关键词拦截的前提下确定性识别，故保留一条 strict XFAIL；见 `docs/testing/phase9-evidence-boundary.md` |

同时，Python 全量测试数字随新增测试变为 **382 passed / 5 skipped / 1 xfailed**
（Phase 9 实跑）。上文 354/365 等数字是各自时点的真实记录，不改写。
