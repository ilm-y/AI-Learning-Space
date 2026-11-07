# Agent pytest 全量运行记录（默认模式）

- 运行时间：2026-11-07T19:54:24+08:00
- 仓库版本：`13cec31`（工作区另含未提交的审计文档修改）
- 运行目录：`agent/`
- 模式：**默认模式**（不设置任何 `AILS_KNOWLEDGE_E2E` / `AILS_QDRANT_IT` / `AILS_TEST_RABBITMQ_URL` 等门控变量）
- 环境门控状态：运行前 `env | grep '^AILS_'` 无输出；`AILS_*` 全部未设置
- 命令：

```bash
cd agent
python -m pytest -q -rs \
  --junitxml=../docs/audit/artifacts/agent-pytest-default-20261107T195424.xml
```

- 退出码：`0`
- 结果：**408 passed, 7 skipped, 1 xfailed, 1 warning in 17.51s**
- 收集数：`416`（JUnit XML：`tests=416`；XML 把 1 条 xfail 也记为 skipped，因此 `skipped=8`）

## 7 条 skip 的逐项原因

| 数量 | 文件 / 行 | 原因 |
| ---: | --- | --- |
| 1 | `tests/test_knowledge_e2e.py:209` | 未设置 `AILS_KNOWLEDGE_E2E=true`；真实链路 E2E 不执行 |
| 4 | `tests/test_qdrant_integration.py:38,123,197,262` | 未设置 `AILS_QDRANT_IT=true`；真实 Qdrant 集成测试不执行 |
| 2 | `tests/test_worker_broker.py:110,139` | 未设置 `AILS_TEST_RABBITMQ_URL`；真实 Broker 测试不执行 |

## xfailed

`1 xfailed` 是既有的 `tests/test_runtime_budget_contract.py` 预期失败（strict xfail），
不是环境跳过，也不是本轮新增失败。

## 原始产物

- `agent-pytest-default-20261107T195424.txt`：完整 stdout/stderr（含进度、skip 明细、汇总）
- `agent-pytest-default-20261107T195424.xml`：JUnit XML

## 说明

这份产物**只能**代表默认模式。它与文档中“门控全量 415 passed / 0 skipped / 1 xfailed”
不是同一次运行；门控全量的原始 pytest 输出本轮没有保留，不能把 415 当作本产物的结果。
