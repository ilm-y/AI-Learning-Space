# D4-5 Context Quality Evaluation

> 范围：D4-5 离线上下文质量评测。
> 约束：未修改 D4-2A/B/C/D、D4-3、D4-4 的生产契约与实现；未新增表/中间件；
> 未调用真实 MySQL、Qdrant 或外部 LLM；未 commit、未 push；未修改 Java/SQL。

---

## 1. 代码核实结论

### 1.1 可复用资产（以当前代码为准）

| 资产 | 位置 | 评测中的用途 |
|---|---|---|
| `plan_retrieval` 纯函数 | `agent/app/context/policy.py` | 每个 case 先产出 `RetrievalPlan`，评测按 Plan 模拟 Runtime 检索接线 |
| `SourceDecision` / `SessionDecision` / `RetrievalPlan` | 同上 | 直接核对 REQUIRED / OPTIONAL / SKIP、goal/focus 注入、概念匹配与原因 |
| `ContextBuilder.build` | `agent/app/context/builder.py` | 唯一的上下文选择、去重、分层压缩、预算与渲染入口 |
| `BuiltContext.context_manifest` | 同上 | 逐 source 核对 `SELECTED / TRUNCATED / OMITTED / EXCLUDED` 与实际原因 |
| `ContextSection` / tier / `compression_reason` | 同上 | 核对 full / structured / one_line 以及 semantic redundancy |
| `_apply_session_policy` | `agent/app/runtime.py` | 在 Builder 之前按 SessionDecision 清空旧 goal/focus，评测复用同一函数 |
| `build_trajectories` / `collect_evidence_ids` / `attach_evidences` | `agent/app/context/trajectory.py` | 模拟复盘路径的 Revision → Evidence 精确回查 |
| `AgentState` 状态字段 | `agent/app/state/agent_state.py` | 承载 `retrieval_plan`、各类状态、knowledge injected/dropped ids |
| Fake Retriever 模式 | `agent/tests/eval/` 既有模式 | 以声明式 fixture 替代网络与数据库 |

### 1.2 核实到的事实

1. **D4-1 文档存在，但它是 D4-2 之前的只读审计**：
   `docs/audit/D4-1_CONTEXT_ASSEMBLY_AUDIT.md` 明确写明“本批没有运行任何测试”，
   其中 Understanding / Memory / Revision 的“失败 = 空”等缺口是当时的静态审计结论。
   本报告不把这些历史行号当作当前缺陷证据，所有结论都重新对照当前代码与本次评测。
2. **D4-2C、D4-2D 的独立审计文档不存在**。仓库内只有 D4-1 审计文档；
   D4-2C 的 Session 概念不匹配逻辑与 D4-2D 的意图 × 数据源矩阵，以
   `policy.py` 与现有测试为准。
3. **Plan 语义**：`REQUIRED` / `OPTIONAL` 在本批都表示发起检索，只有 `SKIP` 不发起；
   无概念锚点时 U / M / E / Revision 统一被覆盖为 SKIP，Knowledge 独立。
4. **Session 不匹配**：只有“Session 文本有概念、本轮也有概念、且两边完全不相交”
   才判 `concept_match=False`；此时 `inject_goal=False`、`inject_current_focus=False`，
   Runtime 在 Builder 前清空字段，Builder 不渲染空 Session 区块。
5. **Builder 的选择边界**：`_apply_plan_scope` 按 Plan 过滤候选，`_dedupe` 按
   `dedupe_key` 去重；Builder 本身**不做 concept 维度的过滤**。因此评测 fixture
   只预置“本轮概念”的 U / M / E / Revision，这与 Runtime 的检索口径一致，避免
   把“Runtime 没查旧概念”误测成“Builder 能过滤旧概念”。
6. **失败可见性契约**：`DEGRADED` 不得被折叠成 `EMPTY`；知识被预算裁剪时，
   Builder 在预算之后补 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED` 状态区块，
   并明确禁止改写成“没有检索到资料”。
7. **protected 区块**：状态说明与 Session 先占预算、不参与类型额度竞争；
   `_fit_budget` 允许 protected 超总预算，Manifest 如实记录 `used > effective` 并给出
   `budget_overrun_reason`。
8. **多概念 Memory**：`HttpBackend.get_memories` 对 concept_ids 去重后逐概念并发请求，
   `_merge_memory_results` 轮转交错、按 memory.id 去重、任一 DEGRADED 则整体 DEGRADED；
   没有跨概念统一 RRF（代码注释已声明该限制）。
9. **语义冗余**：`_is_semantically_redundant` 使用字符 bigram 重合率 ≥ 0.6；
   只把 Memory / Evidence / Related Evidence 降为 structured，不删除、不合并冲突内容。

### 1.3 未发现需要修改生产代码的“极小明确缺陷”

本次评测未修改生产 Policy / Builder。已复现的冗余阈值误判属于边界行为，
修复会改变 D4-3 的压缩语义，超出“极小修复”边界，因此只记录为已知限制与后续建议。

---

## 2. 评测数据集

位置：`agent/tests/eval/context_quality/dataset.py`。

- 10 类场景、每类至少 2 个 case（预算压力 3 个，额外覆盖分层截断），共 21 个 case。
- 每个 case 声明：`case_id`、意图、用户消息、概念锚点、Session / History、
  预置 Understanding / Memory / Evidence / Knowledge / Revision、预期检索、
  预期注入 / 不注入、预期来源标记、禁止断言、DEGRADED 可见性、预算。
- 所有时间、ID、文本标记固定，重复运行结果一致。
- 数据只覆盖评测需要的字段；不是生产数据分布，也不是真实模型回答。
- 每个非 `SELECTED` 的预期注入都声明 `injection_reasons`
  （`policy_skip` / `intent_not_review` / `type_budget` / `tier:structured` /
  `tier:one_line` 等）；`SELECTED` 默认 `within_budget`。pytest 会强制非
  `SELECTED` 必须声明原因，并逐条比对 Manifest 的实际 reason。
- 四种注入结果都有覆盖：`SELECTED`、`EXCLUDED`、`TRUNCATED`、`OMITTED`。

| 类别 | case_id | 关键预期 |
|---|---|---|
| 普通知识问答 | `explain-derivative-definition` | U/M/K REQUIRED，E/Revision SKIP；旧 Evidence/Revision 即使被塞入 state 也 EXCLUDED |
| 普通知识问答 | `explain-limit-epsilon-delta` | Memory EMPTY、Knowledge NO_MATCH；不得写成“资料不存在/用户没有历史” |
| 同 Session 连续学习 | `continue-derivative-session` | CONTINUE_TASK；Session goal/focus 注入 |
| 同 Session 连续学习 | `continue-tlb-session` | 带历史轮次；U/M/K 注入 |
| 跨 Session 同概念 | `cross-session-derivative-new-session` | 新 Session 但概念匹配，仍读取同概念认知与记忆 |
| 跨 Session 同概念 | `cross-session-derivative-no-session` | 无 Session 帧，但跨会话 U/M 仍注入 |
| 复盘错误 | `review-derivative-misconception` | Revision REQUIRED；E SKIP；按 Revision 精确回查证据，不取最近证据 |
| 复盘 + 依据请求 | `review-continuity-with-evidence-request` | Revision 与 Evidence 都触发；历史与当前分区 |
| 评估掌握程度 | `meta-derivative-mastery` | 当前快照 + 正反 Evidence；Revision SKIP |
| 评估薄弱点 | `meta-tlb-weakness` | 当前误区 + 答错证据显式可见 |
| 文档查询 | `knowledge-query-derivative-doc` | Knowledge REQUIRED；U/M OPTIONAL |
| 文档查询 | `knowledge-query-pagetable-textbook` | 多片段、文档名/章节/页码可追溯 |
| 主题切换 | `topic-switch-derivative-to-tcp` | 旧 Session 概念不匹配；旧 goal/focus 不进入 Prompt |
| 主题切换 | `topic-switch-tcp-to-pagetable` | 同上，换方向验证 |
| 历史与当前冲突 | `conflict-current-understanding-vs-old-evidence` | 当前快照与旧 Evidence/Memory 都保留，以当前快照为准 |
| 历史与当前冲突 | `conflict-corrected-tlb-misconception` | 已纠正误区只在轨迹中写成“过去”，不得写成当前仍在 |
| DEGRADED | `degraded-understanding-memory-knowledge` | U/M/K DEGRADED；状态可见，不得说成没有记录 |
| DEGRADED | `degraded-revision-review` | Revision DEGRADED；不得断言“过去没有误区” |
| 预算压力 | `budget-knowledge-partial` | 长知识片段被类型预算 OMITTED，短片段 SELECTED；最终 `SUCCESS_PARTIAL` |
| 预算压力 | `budget-tier-truncation` | 总预算够、类型额度不足：Understanding / Memory 降档为 TRUNCATED，来源标记仍可见 |
| 预算压力 | `budget-protected-overflow` | protected 状态说明超出总预算；Manifest 记录 `used > effective` |

---

## 3. 评测脚本

入口：`agent/tests/eval/context_quality/run_context_quality_eval.py`。

```bash
cd agent
python -m tests.eval.context_quality.run_context_quality_eval
python -m tests.eval.context_quality.run_context_quality_eval --json /tmp/d4-5.json
```

执行流程：

1. `plan_retrieval` 生成 Plan；
2. 按 `triggered` 调用 Fake Retriever（REQUIRED / OPTIONAL 查，SKIP 不查）；
3. 复用 `_apply_session_policy` 处理 Session；
4. 写回 `AgentState`，调用 `ContextBuilder.build`；
5. 对照 Manifest、渲染文本、预算逐项检查；
6. 运行 A / B / C 基线与 4 个已知风险探针。

每个 case 输出 PASS / FAIL 与失败原因；聚合输出 8 项指标、平均 token、overflow 数。
退出码为 1 的条件：

- 任一 C case 失败；
- C 在任一“越大越好 / 越小越好”指标上劣于 A 或 B；
- 任一安全探针失败。

A/B/C 定义：

- **A**：只注入 User Input + Knowledge，不注入个性化上下文；
- **B**：`plan=None` 的旧 Builder 路径 + 全部检索结果，DEGRADED 折叠为 EMPTY，
  Session 不剥离。**这是对 D4-2A 之前行为的近似，不是旧提交的逐行复现**；
- **C**：当前 D4-2D + D4-3。

---

## 4. 指标定义与 C 结果

| 指标 | 定义 | 说明 |
|---|---|---|
| Relevant Context Recall | gold 中被实际 SELECTED/TRUNCATED 的比例 | gold = 预期注入的内容型 source_id；状态型 source 不计入 |
| Context Precision | 实际注入内容中属于 gold 的比例 | 衡量无关内容 |
| Irrelevant Context Rate | `1 - precision` | 越低越好 |
| Source Traceability | 每个预期来源标记的出现/缺失与预期注入状态一致的比例 | 不是引用准确性评测 |
| Budget Utilization | `min(1, used / effective)` | 只报告，不参与“优于”判定 |
| Failure Visibility | DEGRADED 状态说明按预期出现的比例 | 出现“结果确实为空”则该项归零 |
| Personalization Accuracy | 预期个性化来源实际注入且标记可见的比例 | U/M/E/Trajectory |
| Grounded Answer Accuracy | required / forbidden / section 文本约束的通过率 | 结构级，不是 LLM 语义评分 |

C 当前结果（21/21 通过）：

| 指标 | C |
|---|---:|
| Relevant Context Recall | 1.000 |
| Context Precision | 1.000 |
| Irrelevant Context Rate | 0.000 |
| Source Traceability | 1.000 |
| Budget Utilization | 0.441 |
| Failure Visibility | 1.000 |
| Personalization Accuracy | 1.000 |
| Grounded Answer Accuracy | 1.000 |
| 平均上下文 token | 921.9 |
| overflow case 数 | 1 |

---

## 5. A / B / C 对照

| 指标 | A | B | C | 判定 | C≤A | C≤B |
|---|---:|---:|---:|---|---:|---:|
| Relevant Context Recall | 0.222 | 0.968 | 1.000 | 越高越好 | ✅ | ✅ |
| Context Precision | 1.000 | 0.950 | 1.000 | 越高越好 | ✅ | ✅ |
| Irrelevant Context Rate | 0.000 | 0.050 | 0.000 | 越低越好 | ✅ | ✅ |
| Source Traceability | 0.306 | 0.949 | 1.000 | 越高越好 | ✅ | ✅ |
| Budget Utilization | 0.160 | 0.417 | 0.441 | 仅报告 | — | — |
| Failure Visibility | 0.889 | 0.889 | 1.000 | 越高越好 | ✅ | ✅ |
| Personalization Accuracy | 0.095 | 0.976 | 1.000 | 越高越好 | ✅ | ✅ |
| Grounded Answer Accuracy | 0.487 | 0.909 | 1.000 | 越高越好 | ✅ | ✅ |
| 平均上下文 token | 213.2 | 920.4 | 921.9 | — | — | — |
| overflow case 数 | 1 | 2 | 1 | — | — | — |

**C 的预期结论**：C 在 Relevant Context Recall、Irrelevant Context Rate、
Failure Visibility 三项要求上不劣于 A/B，并且没有出现 pre-D4-3 的
“检索到但被裁掉却说没有检索到”的确定性错误。本次结果满足该预期。

需要如实说明：

- A 的 Precision 为 1.0 是因为它只注入与 gold 相同的 Knowledge，不是“A 更精准”；
  它的 Recall 只有 0.222，个性化来源几乎全部缺失。
- B 的 Recall 为 1.0，但它注入了 Plan 本应 SKIP 的旧 Evidence / Revision，
  并且把 DEGRADED 折叠成 EMPTY，因此 Precision、Failure Visibility 均低于 C。
- A/B 的指标都以 C 的预期作为 gold 计算；它们不是历史线上表现。

---

## 6. 已知风险逐项验证

### 6.1 知识被预算裁掉时的“未检索”误述

- 探针：`budget_all_dropped`。
- 观察：`knowledge_state=SUCCESS_BUT_DROPPED`，状态说明存在，
  `false_no_retrieval_claim=False`，被丢 chunk id 为 `[9901]`。
- 结论：**确定性层已覆盖**。Builder 明确写出“检索成功、因预算未放入”，
  并禁止改写成“没有检索到资料”。
- 残留风险：`test_runtime_must_not_deliver_no_retrieval_claim_when_chunks_dropped`
  仍是 xfail。原因是模型仍可能用自然语言误述；该句属于语义断言，确定性状态与
  引用 ID 无法验证，契约也不允许用关键词拦截或硬编码改写。该项是已记录风险，
  不是本次新引入的失败。

### 6.2 旧 Session 挂靠但概念不匹配

- 探针：`session_mismatch`，复用 `topic-switch-derivative-to-tcp`。
- 观察：`session_concept_mismatch`，`inject_goal=False`、`inject_current_focus=False`，
  Prompt 中没有旧标记，`session:30` 没有任何 Manifest entry。
- 结论：**通过**。Runtime 在 Builder 之前清空旧 goal/focus。

### 6.3 多概念 Memory 逐概念 HTTP

- 代码：`HttpBackend.get_memories` 对 concept_ids 去重后逐概念并发请求；
  `_merge_memory_results` 轮转交错、按 memory.id 去重、任一 DEGRADED 则整体 DEGRADED，
  warning/detail 去重保留。
- 现有测试：
  - `test_multi_concept_memory_queries_each_concept_and_merges`
  - `test_multi_concept_memory_partial_failure_stays_degraded`
  均通过。
- 已知限制：没有跨概念统一 RRF，最终只按合并顺序截断到 top_k；
  代码注释已声明。未发现“漏查某个概念”的当前缺陷。

### 6.4 protected 区块超出总预算

- 探针：`protected_overflow`，复用 `budget-protected-overflow`。
- 观察：`total=60`、`effective=60`、`used=599`、`budget_exceeded=True`，
  `budget_overrun_reason` 非空，DEGRADED 状态说明仍可见。
- 结论：**通过**。Manifest 如实记录溢出，没有伪造“已裁剪到预算内”。

### 6.5 语义冗余阈值 0.6

- 改写方向：Understanding “TLB 缓存页表项” vs Memory “快表保存页表条目副本”，
  bigram 重合 `0.222 < 0.6`，未判冗余，tier 仍为 `full`。
  → 保守漏判，只影响节省预算，不丢内容。
- 否定方向：两段 30 句不同 filler 高度重合，Memory 结尾“不是变化率”与
  Understanding 结尾“变化率为正”语义相反，重合 `0.857 ≥ 0.6`，被判冗余并降为
  `structured`，压缩文本中“不是”丢失。
  → **已复现误判与关键否定丢失**。
- 结论：这是当前字符级 bigram 阈值的已知边界。按本批边界未修改生产 Builder；
  建议后续以“否定词/实体保护 + 更细粒度语义判定”单独立项，且必须配合回归集。

---

## 7. 关键发现、缺口与后续建议

### 发现

1. 当前 C 在 21 个声明式场景上全部通过，且 A/B/C 对照满足要求指标不劣于基线。
2. 失败可见性从 B 的 0.889 提升到 1.0：DEGRADED 状态说明与知识预算后最终状态
   都能进入 Prompt。
3. Session 概念不匹配的旧 goal/focus 已被 Runtime 层剥离，Builder 不渲染空区块。
4. 知识“检索成功但被裁掉”在确定性层已不再伪装成“没有检索到”。

### 缺口

1. **没有 D4-2C / D4-2D 独立审计文档**，规则依据只能落在代码与测试上。
2. **评测数据是人工 fixture**，不覆盖真实文档分布、长尾概念、真实 Embedding 质量、
   真实模型对上下文的利用。
3. **A/B 是近似基线**，B 不是旧提交的逐行复现；对照只能说明当前链路相对两个
   构造基线的方向，不是历史线上 A/B 测试。
4. **Grounded Answer Accuracy 是结构级**，不能证明模型回答事实正确。
5. **多概念 Memory 没有跨概念 RRF**，只保证每个概念都有候选并去重。
6. **冗余阈值对否定句会误判**，长文本 structured 截断可能丢关键否定。
7. **自然语言“没有检索到”仍可能由模型自行说出**，确定性层无法完全阻止（xfail）。

### 建议

1. 为 D4-2C / D4-2D 补正式审计文档，至少记录 Session 匹配口径与意图矩阵的
   稳定契约版本。
2. 把本评测接入 CI，先以 `python -m tests.eval.context_quality.run_context_quality_eval`
   作为离线门禁；真实模型/真实检索另设端到端评测，不与本离线集混用。
3. 单独评估冗余判定：先加“否定词/数字/实体差异不判冗余”的保护性规则，
   再用本报告的 paraphrase / negation 两个探针做回归。
4. 为多概念 Memory 记录“每概念候选数 / 被 top_k 截掉数”，把当前不可观测的
   漏召回风险显式化；若要统一排序，应改后端接口，而不是在 Agent 侧伪造 RRF。
5. 为“模型误述没有检索到”增加回答后置校验或评测，而不是在 Runtime 做关键词拦截。

---

## 8. 测试结果与回归

| 命令 | 结果 |
|---|---|
| `python -m tests.eval.context_quality.run_context_quality_eval` | 21/21 case 通过；CI status PASS；exit 0 |
| `python -m pytest tests/test_context_quality_eval.py -q` | 11 passed |
| `python -m pytest tests/test_context_d4_3.py tests/test_retrieval_policy.py tests/test_retrieval_status_contract.py tests/test_runtime_budget_contract.py tests/test_context_quality_eval.py -q` | 63 passed, 1 xfailed |
| `python -m pytest -q` | 580 passed, 9 skipped, 1 xfailed |

唯一的 xfail 是 `test_runtime_must_not_deliver_no_retrieval_claim_when_chunks_dropped`，
原因是已记录的自然语言误述风险，不是本次改动导致的回归。

---

## 9. 未完成项与风险

- 未连接真实 MySQL / Qdrant / Embedding / LLM，未验证真实检索质量与真实回答。
- 未复现旧提交的 B 基线；B 是“plan=None + 全量结果 + DEGRADED→EMPTY”的近似。
- 未修改生产 Policy / Builder，因此冗余否定误判仍未修复。
- 未新增表、中间件、外部 LLM 评分或大型评测框架。
- 未修改 Java / SQL。
- 未配置仓库 CI 文件；脚本已具备非零退出码，可由 CI 直接调用。
- 人工评审尚未进行。

---

## 10. Git 状态

本批只新增评测代码与本文档，未修改已跟踪的生产文件，未 commit、未 push。

```text
?? agent/tests/eval/context_quality/
?? agent/tests/test_context_quality_eval.py
?? docs/eval/
```

（最终以执行 `git status --short` 的结果为准。）

---

## 11. 需要人工评审的决策点

1. **是否接受 A/B 的近似定义**：B 不是旧提交复现，只能证明方向性改进。
2. **是否把本离线评测接入 CI**：建议以 C 全通过 + 无 A/B 回归为门禁；
   xfail 的自然语言风险是否单列。
3. **冗余阈值 0.6 是否本批修复**：当前只记录；若要修复，需要接受对 D4-3
   压缩行为的变更，并补否定/数字/实体回归。
4. **多概念 Memory 是否要求统一 RRF**：当前只保证逐概念候选与去重；
   统一排序需要后端接口变更，属于新范围。
5. **是否接受“结构级 Grounded Answer Accuracy”**：它不是事实正确率，
   真实事实性仍需模型评测或人工抽检。
6. **是否补 D4-2C / D4-2D 审计文档**：当前规则依据只存在于代码与测试。
