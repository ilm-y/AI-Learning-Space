# Phase 6：恢复当前 HEAD 的离线评测基线

- 运行日期：2026-10-29
- 代码基线：`6d05486`（`6d054869ebfb7b13fd89053530cf64bd4e45dd7f`，2026-10-29 13:31:00 +0800）
- 工作区状态：包含本轮 Phase 6 未提交修改（见文末变更清单）
- 原始日志：`evaluation/reports/phase6-20261029/`
- 当前报告：`evaluation/reports/latest.md` / `latest.json`

> 本文只记录**当前 HEAD + 本轮未提交修改**的实测结果。历史版本的结果单独标注，
> 不与本轮数字混用。离线评测是确定性契约回归，不等于真实模型语义质量验证。

## 1. 结论

| 项目 | 修复前（当前 HEAD） | 修复后（当前工作区） |
| --- | --- | --- |
| `python -m evaluation --no-report -v` | 通过 1 / 失败 39 / 跳过 0 | 通过 40 / 失败 0 / 跳过 0 |
| 退出码 | 1 | 0 |
| 失败原因 | 39 条均为 `RetrievalError`（分析链路禁止 fixture） | 无 |
| 断言 | 未完整执行 | 124 / 124 通过 |

修复前唯一通过的是 `contract-empty-input`：空白消息在构造检索源之前就返回空结果，
因此没有触发 fixture 保护。其余 39 条（包括全部 `expectError` 用例）都在调用模型前
被同一个 `RetrievalError` 拦下。

修复后 40 条全部通过。没有通过删除用例、放宽断言或修改历史报告来消除失败。

## 2. 根因

调用链：

```text
evaluation.runner.offline._run_one
  → Settings(retriever_mode="fixture")
  → AgentRuntime.analyze(request)
  → LearningAnalyzer.analyze(request, trace)
  → build_retriever(..., allow_degradation=False)
  → RetrievalError: 分析链路禁止使用 Fixture 检索源
```

两侧都是正确行为：

- 分析结果会写进长期认知，生产链路必须 fail closed，不能拿样例概念顶替真实检索；
- 离线评测必须使用固定 fixture 才能在没有 MySQL / 登录态 / API Key 时运行。

问题在于评测没有**显式**声明自己使用的是评测替身，而是让生产默认分支去猜。
修复不能放宽 `allow_degradation=False`，只能增加一个与生产默认路径隔离的注入点。

## 3. 最小化依赖注入方案

### 3.1 生产代码

| 文件 | 变更 |
| --- | --- |
| `agent/app/analysis/analyzer.py` | `LearningAnalyzer.analyze(..., *, retriever=None)`；仅当 `retriever is None` 时调用生产 `build_retriever(..., allow_degradation=False)`；注入的检索源由调用方关闭 |
| `agent/app/runtime.py` | `AgentRuntime.analyze(..., *, retriever=None)`，原样转交给 analyzer |
| `evaluation/runner/offline.py` | 每条用例显式构造 `FixtureRetriever(Path(settings.fixture_path))`，通过 `runtime.analyze(..., retriever=...)` 注入，并在 `finally` 中关闭 |

默认参数为 `None`，因此：

- `agent/app/main.py` 的 HTTP 分析入口不传该参数；
- `agent/app/messaging/worker.py` 的异步分析入口不传该参数；
- 两条生产链路仍然执行 `build_retriever(..., allow_degradation=False)`。

该注入点与 `AgentRuntime.prepare` 已有约定一致：
**不传 = 生产工厂构造并由 Runtime 关闭；传入 = 调用方自己的替身，由调用方关闭。**

### 3.2 为什么不是放宽生产保护

没有修改 `build_retriever` 的 fixture 分支，也没有去掉 `allow_degradation=False`。
生产默认路径与修复前完全相同；变化只发生在评测执行器显式传入替身的那一次调用。

## 4. 回归测试

新增 `agent/tests/test_evaluation_injection.py`，4 条测试：

| 测试 | 证明 |
| --- | --- |
| `test_offline_evaluator_explicitly_injects_fixture_retriever` | 离线执行器能用固定 `FixtureRetriever` 跑通此前失败的两条用例（一条 `recorded`、一条 `heuristic`） |
| `test_explicit_injection_does_not_change_default_analysis_security` | 显式注入成功后，同一配置下新 runtime 不传 `retriever` 仍抛 `RetrievalError` |
| `test_default_analyze_rejects_fixture_before_llm_call` | 生产默认分析路径在调用模型前就拒绝，LLM 调用次数为 0 |
| `test_injected_retriever_is_used_without_falling_back_to_factory` | 注入路径不调用生产 `build_retriever`；注入的检索源不由 analyzer 关闭 |

已有安全回归继续保留并通过：

- `agent/tests/test_phase11_regressions.py::test_b06_fixture_mode_is_rejected_when_degradation_disallowed`
- `agent/tests/test_phase11_regressions.py::test_b06_analyze_fails_before_any_llm_call_when_fixture_bypassed`
- `agent/tests/test_phase11_regressions.py::test_b06_fixture_is_still_allowed_for_chat_when_degradation_allowed`

### 4.1 测试结果

| 命令 | 结果 | 日志 |
| --- | --- | --- |
| `python -m pytest tests/test_evaluation_injection.py -q` | 4 passed，exit 0 | `test-evaluation-injection.log` |
| `python -m pytest tests/test_analysis.py tests/test_phase11_regressions.py tests/test_evaluation_injection.py tests/test_runtime_budget_contract.py -q` | 30 passed, 2 xfailed，exit 0 | `related-agent-tests.log` |
| `python -m pytest -q -rs`（`agent/`） | 358 passed, 5 skipped, 2 xfailed，exit 0 | `full-agent-pytest.log` |

> 上表是 Phase 6 时点结果。Phase 9 关闭其中 1 条 xfail 后重跑为
> **382 passed / 5 skipped / 1 xfailed**，离线评测仍为 40/40、124/124；
> 详见 [`phase9-evidence-boundary.md`](phase9-evidence-boundary.md)。

5 条 skip 的原因：

- 3 条需要显式环境开关：真实 Knowledge E2E（`AILS_KNOWLEDGE_E2E=true`）、真实 Qdrant 集成测试 ×2（`AILS_QDRANT_IT=true`）；
- 2 条需要 `AILS_TEST_RABBITMQ_URL`，本机未启动 RabbitMQ；CI 会设置该变量并真实执行这两条。

2 条 xfailed 是 Phase 5 保留的预算契约缺口，本轮未处理，见
`docs/testing/phase5-budget-validation.md`。

## 5. 离线评测实测

### 5.1 修复前（当前 HEAD，未应用本轮修改）

```bash
PYTHONPATH="$PWD:$PWD/agent" python -m evaluation --no-report -v
```

- 离线：40 条，通过 1，失败 39，跳过 0，通过率 2.5%
- 退出码：1
- 39 条失败均为：
  `RetrievalError: 分析链路禁止使用 Fixture 检索源：样例概念会被写进真实用户的长期认知。`
- 无其他真实业务断言失败；失败都发生在检索源构造阶段，模型未被调用。

原始日志：`evaluation/reports/phase6-20261029/eval-before.log`。

### 5.2 修复后（当前工作区）

```bash
PYTHONPATH="$PWD:$PWD/agent" python -m evaluation -v
```

- 离线：40 条，通过 40，失败 0，跳过 0，通过率 100.0%
- 断言：124 / 124 通过（100.0%）
- 退出码：0
- 报告：`evaluation/reports/latest.md`（生成时间 2026-10-29T18:53:38+08:00，代码版本 `6d05486`）

原始日志：`evaluation/reports/phase6-20261029/eval-after-with-report.log`。

> 报告中的“代码版本”字段来自 `git rev-parse --short HEAD`，只标识提交基线；
> 本轮修改尚未提交，因此报告不会显示 dirty 状态。本文的工作区说明补齐了这一点。

## 6. 历史基线与当前结果的区别

| 版本 | 日期 | 离线结果 | 说明 |
| --- | --- | --- | --- |
| `67172e4` | 2026-10-27 13:20 | 40 / 40 | 历史报告 `evaluation/reports/latest.md` 原内容，不能作为当前 HEAD 的证据 |
| `6d05486`（修复前） | 2026-10-29 | 1 / 40，exit 1 | 本轮实际复现 |
| `6d05486` + Phase 6 修改 | 2026-10-29 | 40 / 40，exit 0 | 本轮修复后实测 |

历史 40/40 与当前 40/40 的用例集合和断言数量相同，但代码版本不同。
两者不能互相替代；本文所有当前数字均来自 2026-10-29 的本地实跑日志。

## 7. 文档与 CI 说明

- `evaluation/README.md`：补充“评测替身显式注入”与生产 fail-closed 边界说明；
- `.github/workflows/ci.yml`：在离线评测步骤补充注释，说明评测注入不改变生产安全策略；
- `evaluation/reports/latest.md` / `latest.json`：由当前 HEAD 实跑重新生成；
- 本文件与 `evaluation/reports/phase6-20261029/`：记录修复前后原始日志。

## 8. 尚未解决的问题

1. 本轮未提交代码，也未实际触发远程 CI；“CI 不再失败”仅在本地按 CI 同等命令验证。
2. 本机没有 RabbitMQ，`test_worker_broker.py` 两条真实 Broker 测试仍为 skip；
   CI 设置 `AILS_TEST_RABBITMQ_URL` 后会执行。
3. 真实 Knowledge E2E 与 Qdrant 集成测试需要显式环境开关，仍为 skip；这不是本轮引入的。
4. Phase 5 的 2 条严格 xfail 仍保留，表示运行时对预算误述/越界引用的交付防护尚未实现；
   本轮按要求未处理 `finish_reason=length`、预算误述防护或引用交付行为。
   （**Phase 9 修订**：越界引用交付防护已实现并转为 PASS；误述防护仍为 strict XFAIL，
   见 `docs/testing/phase9-evidence-boundary.md`。Phase 9 重跑离线评测仍为
   40/40 用例、124/124 断言，因此本节基线数字继续有效。）
5. `evaluation/phase1/executors.py` 的分析用例仍通过 monkeypatch `build_retriever`
   注入检索源，未改为新的显式参数；它不影响本轮离线评测，但属于后续可清理的技术债。

## 9. 变更清单

| 类型 | 文件 |
| --- | --- |
| 生产代码 | `agent/app/analysis/analyzer.py`、`agent/app/runtime.py` |
| 评测执行器 | `evaluation/runner/offline.py` |
| 回归测试 | `agent/tests/test_evaluation_injection.py`（新增） |
| 评测报告 | `evaluation/reports/latest.md`、`evaluation/reports/latest.json` |
| 原始日志 | `evaluation/reports/phase6-20261029/*.log`（新增） |
| 文档 | `evaluation/README.md`、`.github/workflows/ci.yml`、本文件 |
