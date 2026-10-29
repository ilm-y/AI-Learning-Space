# Phase 3 失败分析

- 日期：2026-10-27
- 范围：`phase3-baseline-20261027`（10 次）与 `phase3-repeats-20261027`（12 次）
- 原则：只记录真实执行证据；根因不能确认时用 `UNKNOWN`；不修改标准答案，
  不删除失败样本，不把 BLOCKED/截断算作语义 PASS

## 1. 失败与缺陷总览

| 编号 | 运行 | 硬断言 | 语义分 | 归因标签 | 一句话 |
| --- | --- | --- | --- | --- | --- |
| F1 | B05-r1 | FAIL | 2.0 | `UNKNOWN` + `CONTEXT_BUDGET` | 固定措辞/Guard 契约不匹配，且输出被 4096 截断 |
| F2 | E01-r3 | FAIL | 2.0 | `GUARD_FALSE_POSITIVE` | “还不能说已经完全掌握”被否定句误判 |
| F3 | E01-r2 | PASS | 2.0 | `CONTEXT_BUDGET` | 输出被 4096 截断，硬断言与评审都未识别 |
| F4 | A01-r2 | PASS | 2.0 | `GUARD_FALSE_POSITIVE` | 合规的“记录里没有”被 History Guard 误报 |
| F5 | D01-r1 | PASS | 2.0 | `UNKNOWN` | 合法引用与畸形 `[资料#0]` 并存 |

同类计数（22 次运行）：

- 输出截断（`finish_reason=length`）：2 次（B05-r1、E01-r2）
- History Guard 误报并回退：1 次（A01-r2）
- 畸形引用标签：1 次（D01-r1）
- 硬断言失败：2 次（B05-r1、E01-r3）
- 空流 / BLOCKED：0 次
- 检索未命中导致的失败：0 次
- 历史证据被忽略：0 次
- 掌握状态越界：0 次
- 无依据断言：0 次（按 LLM Judge；人工未确认）

## 2. F1：B05 硬断言失败与输出截断

### 现象

- `retrieval.knowledgeStatus=NO_MATCH`、`knowledgeChunkCount=0`，检索行为正确；
- 回答开头自行声明“本轮上传的资料里没有检索到可以确认这个问题的依据，
  下面的内容属于通用知识”，语义上满足“不得说成资料中不存在”；
- 但硬断言要求：
  - `answer.evidenceGuard.added == True`，实际 `False`；
  - 必须包含固定句“本轮上传资料中没有检索到与本问题直接相关的依据”，
    实际是不同措辞；
- `finish_reason=length`、`completion_tokens=4096`、`reasoning_tokens=3168`，
  回答结尾停在“OS 处理时没有空闲页框才轮到”，**被截断**；
- LLM Judge 给 `task_completion=2`，未识别截断。

### 根因

1. **断言契约写反了**：`evidenceGuard.added` 的语义是“Guard 是否**追加**了固定
   说明”；Guard 的设计是“模型自己已经说明时不追加”。用实际回答复跑
   `already_discloses("NO_MATCH", answer)` 返回 `True`，所以 `added=False` 是
   正确行为，断言却要求 `True`。固定子串又只接受一种措辞，进一步放大了误报。
2. **输出预算耗尽**：4096 completion token 中 3168 用于 reasoning，正文
   1826 字符后被截断。
3. **评审盲区**：评审输入没有 `finish_reason` 与完整性字段，无法判断回答
   是否讲完。

### 影响

- 该次运行为硬 FAIL，不能计 PASS；
- 但把它写成“Agent 事实错误/编造引用”是不准确的：事实与边界表述正确；
- 也不能因为语义 2 分就忽略截断：任务并未完整结束。

### 处理

- 保留 `evaluation/phase3/artifacts/phase3-baseline-20261027/B05-r1.json` 原始证据；
- 本轮**未修改**用例标准、断言或业务代码；
- 下一轮修复方向：
  1. Guard 契约改为“固定说明存在或模型自述等价边界”，并保留结构化标记；
  2. 把 `finish_reason=length` 视为不完整输出，重试或标记 `TRUNCATED`；
  3. 评审 prompt 增加 finish_reason/completion 信息，截断时 `task_completion`
     不得给 2。

## 3. F2：E01-r3 否定句误报

### 现象

回答开头：

> 先说结论：**还不能说已经完全掌握。**

硬断言 `answer.text not_contains '已经完全掌握'` 命中该子串，判 FAIL。
实际语义是否定“完全掌握”，与 PARTIAL 校准要求一致；LLM Judge 六维均为 2。

### 根因

纯子串匹配不具备否定/引用语境识别能力。该断言在“不能/不算/尚未”等
否定结构中产生假阳性。

### 影响

- 硬断言 FAIL，进入人工复核；
- 语义分与硬断言分歧，不能直接用任一方下结论；
- 这是**测试实现缺陷**，不是模型掌握状态越界。

### 处理

- 保留 artifact：`evaluation/phase3/artifacts/phase3-repeats-20261027/E01-r3.json`；
- 归因 `GUARD_FALSE_POSITIVE`；
- 下一轮改为结构化状态检查（如回答应包含“部分/尚未”语义且不得出现
  肯定式“已经完全掌握”），或引入否定窗口/句法规则，而不是裸子串。

## 4. F3：E01-r2 截断但 PASS

### 现象

- `finish_reason=length`、`completion_tokens=4096`、`reasoning_tokens=3412`；
- 回答 1577 字符，停在一张流程图的“③ 这个区域是什么类型？├──”处；
- 硬断言全部通过，LLM Judge 六维均为 2。

### 根因

- 与 F1 相同的输出预算问题；
- 现有硬断言没有“回答是否完整”的检查，`answer.length >= 20` 不能识别截断；
- 评审输入缺少 finish_reason。

### 影响

- 该次运行被计为硬 PASS，但输出不完整；
- 这暴露了统计口径风险：**PASS 不等于完整回答**；
- 不能据此声称 E01 在 3 次复跑中完全稳定。

### 处理

- 在结果报告中单独标注 `finish_reason=length` 2/22；
- 下一轮增加 `finish_reason != length` 硬断言，或将截断运行单列
  `TRUNCATED`，不计完整 PASS。

## 5. F4：A01-r2 History Guard 误报

### 现象

Guard 首轮将以下合规表述判为问题：

- “你当时为什么会那样理解、后来是怎么改过来的、有没有借助外部资料，
  这些在记录里都没有，我不做推测” → `external_absence`
- “那次纠正是独立完成的还是有人指出——同样没有记录” → `external_absence`
- “你当时的心情、感受，以及是否意识到问题的根源——这些都没有记录”
  → `internal_process`

触发一次重生成；重生成后仍有 2 条误报，最终 `fallback=true`，返回
627 字符的确定性模板。回答仍包含可核对时间线，语义分 2。

### 根因

History Guard 的规则把“提到外部资料/心理过程”与“断言这些内容发生过”
混为一谈；对“记录没有写、我不推测”的合规免责句缺少豁免。

### 影响

- 多消耗 1 次模型调用（22 次运行中唯一一次重生成）；
- 回答变短、模板化，虽然本次语义仍合格；
- 若误报率更高，会系统性降低回答质量并增加成本。

### 处理

- 保留 artifact：`evaluation/phase3/artifacts/phase3-repeats-20261027/A01-r2.json`；
- 归因 `GUARD_FALSE_POSITIVE`；
- 下一轮让 Guard 识别“否定/免责语境”，或把确定性回退限制在真正的
  肯定式编造。

## 6. F5：D01 畸形引用标签

### 现象

- `valid=2`、`invalid=0`，硬断言 PASS；
- `malformedLabels=[\"[资料#0]\", \"[资料#1]\"]`；
- 正文代码块里同时出现缺少文档名的简写标签。

### 根因

模型在代码块中复用了简写标签；现有硬断言只检查 `invalid=0`，
未检查 `malformed=0`，引用校验也没有阻断这类标签。

### 影响

- 不构成编造引用（对应片段真实存在），但用户无法从标签直接定位文档；
- 属于 P2 引用卫生问题，不影响本轮 PASS 判定；
- 说明“引用合法”这一指标目前只覆盖 invalid，不覆盖格式完整性。

### 处理

- 保留 artifact：`evaluation/phase3/artifacts/phase3-baseline-20261027/D01-r1.json`；
- 归因 `UNKNOWN`（现有标签集合未覆盖引用格式）；
- 下一轮增加 `malformedLabels.length == 0` 硬断言，或在输出前规范化标签。

## 7. 归因标签覆盖情况

| 标签 | 本轮是否出现 | 证据 |
| --- | --- | --- |
| `RETRIEVAL_MISS` | 否 | 所有命中断言符合 fixture；B05 的 `NO_MATCH` 是预期 |
| `CONTEXT_BUDGET` | 是（2 次） | B05-r1、E01-r2 均 `finish_reason=length` |
| `HISTORICAL_EVIDENCE_IGNORED` | 否 | A01/A02/A03/B02 均引用 revision/evidence |
| `UNSUPPORTED_CLAIM` | 否 | LLM Judge 未发现；人工未确认 |
| `MASTERY_OVERCLAIM` | 否 | E01 三次均明确“未完全掌握” |
| `GUARD_FALSE_POSITIVE` | 是（2 次） | E01-r3 否定句、A01-r2 免责句 |
| `GUARD_FALSE_NEGATIVE` | 否 | 未观察到应拦未拦 |
| `MODEL_EMPTY_OUTPUT` | 本轮 22 次否 | 1024/2048 冒烟曾出现，见计划 2.2 |
| `STREAM_PARSE_ERROR` | 否 | 全部 HTTP 200、SSE 正常结束 |
| `UNKNOWN` | 是（2 次） | B05 断言契约、D01 引用格式 |

## 8. 下一轮最值得做的三件事

1. **把“截断”变成一等失败状态**
   - 硬断言增加 `finish_reason != length`；
   - 截断运行不计完整 PASS，单列 `TRUNCATED`；
   - 评审 prompt 传入 finish_reason/completion，截断时禁止 `task_completion=2`；
   - 评估更高预算或按案例自适应预算，但必须保留 1024/2048 空流证据。

2. **把硬断言从“字面匹配”改成“语义/结构判定”**
   - E01 的否定句误报：识别“不能/尚未/不算”语境；
   - B05 的 Guard 契约：接受模型自述的等价边界说明，或统一由 Guard 注入；
   - D01 增加 `malformedLabels=0`。

3. **完成评审校准，而不是继续堆满分**
   - 先人工填写 `human-review-queue-20261027.json` 的 9 条；
   - 计算 LLM Judge 与人工的逐维一致率/加权 kappa；
   - 若一致率不足，调整评审 prompt 或换独立 provider；
   - 在人工确认前，不对外声称“语义质量 2.0”。

## 9. 未修复声明

本轮没有修改业务逻辑、用例标准答案、硬断言或 Guard 规则；上述缺陷均保留
原始证据并进入下一轮。任何“已修复”结论都必须有新的回归运行与对照 artifact，
不能只凭分析推断。
