# Phase 4 Context Builder 与真实模型结果

- 日期：2026-10-28
- 运行批次：`20261027_172403`
- 真实模型：`deepseek-flash`（Spring SSE → Agent → LLM）
- 真实检索：`text-embedding-v4` + Qdrant `ails_phase4_e2e_text-embedding-v4_1024`
- 原则：真实模型调用与确定性检索分开统计；没有人工评分时不声称语义质量通过

## 1. 结果总表

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| Context preview：支持问题 | PASS | 目标 chunk 18 未被丢弃，标签在 system instruction 中 |
| Context preview：无匹配 | PASS | `THRESHOLD_FILTERED` 状态说明存在，无知识 chunk |
| Context preview：权限范围 | PASS | A1 只注入 18/19；A2 只注入 20；B1 只注入 21 |
| Context preview：问题/历史顺序 | PASS | 历史 USER→ASSISTANT→当前 USER；优先级 100/90/89/40 |
| 来源标签映射 | PASS | 两个标签精确解析回 chunk 18/19 |
| 预算压力 | **FAIL（CONTEXT_BUDGET，结构性）** | 知识 chunk 全被丢弃，且 SUCCESS 没有状态说明 |
| 真实模型硬断言 | **6/6 PASS** | 5 个主用例 + 1 个补充错误前提用例 |
| 语义质量 | **UNREVIEWED** | 无人工评分、无校准 Judge；不写成 PASS |

## 2. Context Builder 实测

### 2.1 支持问题

问题：`ORBIT-4721 新部署在失败后等待多久再重试？`

| 项 | 实测 |
| --- | --- |
| 检索状态 | SUCCESS |
| 注入 chunk | 18（目标）、19（干扰） |
| 目标 chunk 18 | `dropped=false`，estimated tokens 161 |
| 干扰 chunk 19 | `dropped=false`，estimated tokens 101 |
| system instruction 含目标标签 | 是 |
| context tokens / budget | 415 / 3000 |
| dropped sections | 无 |

干扰 chunk 没有被当成“现行值”注入逻辑：它作为一份可核对的旧版本资料进入
上下文，模型在回答中明确区分“旧值 12 秒已下线”。这不是把干扰当事实。

### 2.2 无匹配问题

问题：`镓的熔点是多少摄氏度？`

| 项 | 实测 |
| --- | --- |
| 检索状态 | `THRESHOLD_FILTERED` |
| 注入知识 chunk | 0 |
| `knowledge_status` section | 存在 |
| 状态说明 | 明确要求先说明“本轮上传资料中没有检索到可确认的依据”，再区分通用知识 |
| Guard | 生成后追加了同义说明（真实回答首句已声明） |

### 2.3 预算压力（发现缺口）

构造 12 条长历史消息（约 900 字符/条），问上述支持问题：

| 项 | 实测 |
| --- | --- |
| context tokens / budget | 2836 / 3000（未超预算） |
| 注入知识 chunk | **0** |
| dropped sections | `knowledge:18`, `knowledge:19` |
| `knowledge_status` section | **不存在** |
| warnings | “以下片段因超出 Token 预算被丢弃：knowledge:18, knowledge:19” |

原因在实现里是确定的：

- `_render_knowledge_status` 只对
  `NO_VALID_DOCUMENTS` / `NO_MATCH` / `THRESHOLD_FILTERED` / `DEGRADED`
  渲染说明（`_KNOWLEDGE_STATUS_NOTES`），`SUCCESS` 没有说明；
- `Runtime.generate` 的 `GUARD_STATUSES` 也不包含 `SUCCESS`。

结果是：检索 **SUCCESS**，但所有知识片段因预算被丢弃时，模型看到的既没有
知识片段，也没有“检索成功但被预算裁掉”的说明。它无法区分“本轮没有检索到
资料”和“检索到了但上下文放不下”。这属于 **`CONTEXT_BUDGET`** 风险。

**没有**把它写成“模型已经因此答错”：本轮没有在这个上下文下调用真实 LLM。
它是结构性缺口 + preview 证据，不是语义失败结论。

### 2.4 历史与优先级

补充 preview（`supplemental_context.json`）：

```text
user_input        100
history:2          90
history:1          89
knowledge:19       40
knowledge:18       40
```

消息顺序：

```text
system
user     历史问题：先记下 ORBIT-4721。
assistant 历史回答：已记下。
user     ORBIT-4721 当前重试窗口是多少？
```

- 当前问题在最后，未被历史挤掉；
- 历史保持 USER→ASSISTANT 顺序；
- 知识优先级低于用户输入与历史，符合当前设计；预算不足时先丢知识是预期
  策略，但缺少“知识被预算丢弃”的状态说明（见 2.3）。

### 2.5 来源标签映射

最终 Context 实际注入 18/19，Prompt 中出现：

```text
[资料《orbit_current_20261027_172403.md》#0]
[资料《orbit_legacy_20261027_172403.md》#0]
```

两个标签都能解析回真实 chunk 18/19，没有指向未注入 chunk 的标签。

## 3. 真实模型用例

### 3.1 用例与硬断言

| # | 用例 | 问题意图 | 检索状态 | 注入 | 引用（合法/非法） | 硬断言 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | supported | 当前 17 秒 | SUCCESS | 18,19 | 4 / 0 | PASS |
| 2 | conflicting_versions | 当前值 + 旧值是否适用 | SUCCESS | 18,19 | 3 / 0 | PASS |
| 3 | insufficient_detail | 17 秒 + 未提供的仪表盘颜色 | SUCCESS | 18,19 | 3 / 0 | PASS |
| 4 | legacy_only | 旧调度器 12 秒 | SUCCESS | 18,19 | 3 / 0 | PASS |
| 5 | no_match | 镓熔点 | THRESHOLD_FILTERED | 0 | 0 / 0 | PASS |
| 6 | supplemental_false_premise | “当前是 12 秒，对吗？” | SUCCESS | 18,19 | 3 / 0 | PASS |

硬断言包括：

- SSE 有 `message.end`，无 `message.error`；
- 回答非空；
- 没有 malformed / invalid 引用；
- 每个被引用 chunk 都在最终 Context 注入集合内；
- 引用文档属于本轮注入范围；
- 无匹配时不得引用任何文档；
- 支持类问题必须出现 17；旧版问题必须出现 12。

**6/6 硬断言通过。** 这只证明引用合法、来源范围正确、关键事实词出现，
不证明语义质量已经通过人工评审。

### 3.2 语义观察（不是质量 PASS）

以下是人工快速阅读的观察，仅作为线索，不是校准评分：

| 用例 | 观察 |
| --- | --- |
| supported | 首句给 17 秒；主动说明 12 秒属于已下线旧版；引用现行与旧版两份资料 |
| conflicting_versions | 明确“当前 17、旧值 12 不再适用”；没有抹平冲突 |
| insufficient_detail | 给出 17 秒；明确说仪表盘颜色“上传的资料里查不到”，没有编造 |
| legacy_only | 给 12 秒，同时说明只适用于已下线旧版，并补充当前 17 秒 |
| no_match | 先声明“本轮上传资料中没有检索到可确认的依据”，再给通用知识；0 引用 |
| supplemental_false_premise | 直接纠正“不对，当前是 17 秒，不是 12 秒”；旧值 12 秒被正确归到旧版 |

**语义质量状态：`UNREVIEWED`。** 本轮没有人工评分、没有校准 Judge；以上观察
不能写成“语义正确率 100%”。人工评审队列仍应保留。

### 3.3 引用支持度

`validate_citations` 的客观部分：

- 6 例共 16 条合法引用，0 非法、0 malformed；
- 所有 `chunkId` 都落在最终注入集合内；
- `citedDocuments` 没有超出注入文档。

启发式部分出现 2 条 `numericConflict=true`：

1. `insufficient_detail`：句子把“旧值 12 秒”归到现行规范引用上，但现行规范
   原文只写“旧值”，没有写 12；
2. `legacy_only`：句子包含“改回 12 秒”，引用的是现行规范；现行规范只写 17
   和“不要改回旧值”，没有 12。

这不是非法引用（标签和片段都存在），但说明**一句话里合并了多个来源，只标了
其中一个**。分类为 `SOURCE_MAPPING` 观察；详见失败分析。合法引用不能当作
事实证明，这两条就是例子。

## 4. Guard 行为

- 支持的 4 个 SUCCESS 用例：没有追加知识缺失 Guard，模型正确使用资料；
- 无匹配用例：`THRESHOLD_FILTERED` 状态说明 + 生成后 Guard 都存在，回答与
  状态一致，没有把“未检索到”说成“资料中不存在”；
- 预算压力：没有状态说明，也没有 Guard（因为状态是 SUCCESS）；这是 2.3 的
  缺口；
- 没有观察到 Guard 覆盖正确知识或制造矛盾。

## 5. 用例状态汇总

| 类别 | PASS | FAIL | BLOCKED | SKIPPED / NOT_COVERED |
| --- | --- | --- | --- | --- |
| 配置安全修复 | 6 个单测 + 真实启动 | 0 | 0 | 0 |
| 索引/生命周期 | 7/7 | 0 | 0 | 0 |
| 确定性检索/权限 | 5 查询 + 4 过滤 | 0 | 0 | 0 |
| Context Builder | 3 项主 preview + 5 项补充 preview | **1 项预算缺口** | 0 | 0 |
| 真实模型硬断言 | 6/6 | 0 | 0 | 0 |
| 语义质量 | 0（UNREVIEWED） | 0 | 人工评分不可用 | 大规模/校准未做 |

## 6. 证据

- `evaluation/phase4/reports/20261027_172403/agent_results.json`
- `evaluation/phase4/reports/20261027_172403/context_results.json`
- `evaluation/phase4/reports/20261027_172403/supplemental_context.json`
- `evaluation/phase4/reports/20261027_172403/supplemental_agent.json`
- `evaluation/phase4/reports/20261027_172403/summary.json`

## 7. 不能声称的内容

- 不能声称“真实 RAG 语义质量已通过”：没有人工评分，Judge 未校准；
- 不能声称“Recall/MRR 代表生产效果”：N=5、每条 1 个相关 chunk；
- 不能声称“预算裁剪不会影响回答”：已发现 SUCCESS + 全量知识被丢弃时缺少
  状态说明；
- 不能声称“引用合法 = 事实正确”：2 条 numericConflict 已说明来源映射风险；
- 不能声称“生产环境/真实用户数据已验证”：全部为隔离合成数据。
