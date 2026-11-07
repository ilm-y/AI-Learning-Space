# Memory FULLTEXT 改造：后端 `mvn verify` 全量运行记录（默认模式）

- 运行时间：2026-11-07T20:51+08:00（`Finished at: 2026-11-07T20:51:32+08:00`）
- 仓库版本：`8490c1c`（工作区另含本次未提交的 Phase 1 改动）
- 运行目录：`backend/backend/`
- 模式：**默认模式**（不设置任何 `AILS_*` 环境变量）
- 环境门控状态：运行前 `AILS_*` 全部未设置
- 命令：

```bash
cd backend/backend
./mvnw -B clean verify
```

- 退出码：`0`
- 结果：**BUILD SUCCESS**，`Total time: 45.868 s`

## Surefire（`*Test`）

`: 46 个测试类，335 个测试，0 失败，0 错误，88 跳过`

跳过全部来自环境门控（未设置相应的 `AILS_*` 变量），不是失败：

| 测试类 | 跳过数 |
| --- | ---: |
| `BackendApplicationTests` | 1 |
| `mq.AnalysisMqIntegrationTest` | 10 |
| `mq.Phase2MqProcessorIntegrationTest` | 5 |
| `mq.Phase2RabbitMqBrokerTest` | 5 |
| `security.InternalIdentityHttpIntegrationTest` | 5 |
| `security.SecurityContextIntegrationTest` | 19 |
| `service.ChatSseIntegrationTest` | 9 |
| `service.ChatSseOrderingProbeTest` | 1 |
| `service.DocumentIndexE2EIntegrationTest` | 1 |
| `service.DocumentIndexFencingIntegrationTest` | 4 |
| `service.DocumentIndexServiceIntegrationTest` | 15 |
| `service.DocumentServiceIntegrationTest` | 7 |
| `service.Phase2RealStateIntegrationTest` | 6 |
| **合计** | **88** |

## Failsafe（`*IT`）

`: 2 个测试类，12 个测试，0 失败，0 错误，12 跳过`

| 测试类 | 测试数 | 跳过 |
| --- | ---: | ---: |
| `MemoryFulltextSearchIT`（本轮新增） | 8 | 8 |
| `MemoryStateMachineConcurrencyIT`（既有） | 4 | 4 |
| **合计** | **12** | **12** |

默认模式下两个 IT 都因门控关闭而跳过，因此这份产物**不构成** FULLTEXT 功能的验证证据；
真实 MySQL 上的验证在门控运行产物中（见同目录 `maven-memory-fulltext-gated-*`）。

## 原始产物

- `maven-default-verify-20261107T2051.txt`：完整 Maven 输出（含 Surefire/Failsafe 汇总）
- `maven-default-verify-20261107T2051-surefire-xml.zip`：`target/surefire-reports/TEST-*.xml`
- `maven-default-verify-20261107T2051-failsafe-xml.zip`：`target/failsafe-reports/TEST-*.xml`
- `maven-default-verify-20261107T2051-summary.txt`：按测试类汇总的统计

## 说明

这份产物证明两件事：

1. 新代码可以编译，默认测试集没有回归（335 个测试中 247 个实际执行，全部通过）；
2. **默认模式不会执行任何依赖真实 MySQL / RabbitMQ 的测试**，因此不能据此判断
   FULLTEXT 检索是否正确。
