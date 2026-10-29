# Phase 5：预算裁剪下的检索状态验证

本目录只验证一个行为：**检索成功、但片段因上下文 Token 预算没有进入最终 Prompt 时，
系统是否仍能让模型区分「检索到了」与「最终上下文里没有」，并且不引用未注入的片段。**

它不复用 Phase 4 的 `budget_pressure_llm.json` 单例，而是：

- 24 条基于现有合成语料的真实 Qdrant 检索查询（`realUserTraffic=false`）；
- 3 条固定 chunk 的构造边界用例；
- 同样走生产 `ContextBuilder`，用合成历史/记忆制造可复现的预算压力；
- 真实模型调用，原始回答、最终 system instruction、注入/丢弃 chunk 全部落盘。

## 文件

| 文件 | 作用 |
| --- | --- |
| `queries.jsonl` | 查询集 + 场景（normal / budget / 固定边界） |
| `run_budget_queries.py` | 真实检索 + 预算构建 + 真实模型调用 + 指标汇总 |
| `repro_runtime_gap.py` | 确定性构造测试：脚本化模型输出是否被运行时拦截 |
| `repro_runtime_gap.json` | 上述构造测试的实际输出（`notModelBehavior=true`） |
| `reports/<timestamp>/` | `runs.jsonl`、`summary.json`、`summary.md`；`runs.v2.jsonl` / `summary.v2.*` 为修正筛查规则后的重算，模型输出不变 |

## 运行

在 `agent/` 目录下执行，保证 `app` 与 `agent/.env` 可加载：

```bash
# 1. 先确认预算压力真的把知识片段裁掉（不调用模型）
python ../evaluation/phase5_budget/run_budget_queries.py --dry-run

# 2. 真实模型基线（固定 temperature=0.0，模型与配置写入 summary.json）
python ../evaluation/phase5_budget/run_budget_queries.py

# 3. 确定性边界复现（不联网、不调用模型）
python ../evaluation/phase5_budget/repro_runtime_gap.py
```

## 测试

运行时契约的确定性测试在 `agent/tests/test_runtime_budget_contract.py`
（不联网、不调用真实模型）：

```bash
cd agent
python -m pytest tests/test_runtime_budget_contract.py -q -rx
# 3 passed, 2 xfailed
```

两条 `xfail(strict=True)` 记录的是**尚未满足的目标契约**：运行时仍会把
「没有检索到资料」和被预算丢弃片段的引用原样交付。测试没有修复生产行为。

真实 Qdrant 语料通过 `--collection-prefix`、`--user-id`、`--space-id` 指定，
默认是 Phase 4.2 本地验证语料：`ails_phase42_real_e2e` / user 14 / space 22。
脚本只读 Qdrant，不写文档、不重索引、不改阈值。

## 口径

- **分母**：明确标注为预算场景（`scenario != normal`）、检索状态 `SUCCESS`、
  最终状态为 `SUCCESS_PARTIAL` / `SUCCESS_BUT_DROPPED`、回答非空且
  `finish_reason != length` 的运行；
- **分子**：上述运行中出现「检索状态误述」或「引用越界」的运行；
- **状态误述**：关键词筛查（仅匹配“没有检索到 / 没有可用资料”类陈述，
  排除同时提到预算/上下文的句子），不是语义判分；
- **引用越界**：`validate_citations` 判定 `invalid > 0`，即引用了未注入上下文
  或根本不存在的 `资料《文件名》#序号`；
- 未达到预算压力、模型报错、回答为空或被截断的运行单列，不强行计入分母。
