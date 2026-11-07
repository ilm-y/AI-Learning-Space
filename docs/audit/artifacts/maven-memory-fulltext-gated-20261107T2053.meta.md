# Memory FULLTEXT 改造：后端 `mvn verify` 全量运行记录（Memory IT 门控模式）

- 运行时间：2026-11-07T20:53+08:00（`Finished at: 2026-11-07T20:54:42+08:00`）
- 仓库版本：`8490c1c`（工作区另含本次未提交的 Phase 1 改动）
- 运行目录：`backend/backend/`
- 模式：**仅开启 Memory 集成测试门控**
- 环境门控状态：

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `AILS_MEMORY_IT` | `true` | 启用 `MemoryFulltextSearchIT` / `MemoryStateMachineConcurrencyIT` |
| `AILS_IT_MYSQL_PASSWORD` | （已设置，不落盘） | 本机 MySQL 密码；用户名默认 `root` |
| `AILS_IT_MYSQL_USER` | 未设置 | 回退默认 `root` |
| 其余 `AILS_*`（RabbitMQ / Qdrant / Agent / E2E） | 未设置 | 因此 Surefire 仍有 88 个环境门控测试被跳过 |

- 数据库：`jdbc:mysql://127.0.0.1:3306/ails_memory_it`（`createDatabaseIfNotExist=true` 自动建库）
- MySQL：8.0.37，`ngram_token_size = 2`（运行前已确认）
- 命令：

```bash
cd backend/backend
AILS_MEMORY_IT=true AILS_IT_MYSQL_PASSWORD=*** ./mvnw -B clean verify
```

- 退出码：`0`
- 结果：**BUILD SUCCESS**，`Total time: 01:02 min`

## Surefire（`*Test`）

`: 46 个测试类，335 个测试，0 失败，0 错误，88 跳过`

88 个跳过与默认模式完全一致，全部来自**其他**环境门控（RabbitMQ、真实 DB、SSE 等），
本轮的 Memory 集成测试在 Failsafe 阶段执行。重点回归项：

- `MemoryStateMachineTest`：**19 / 19 通过**（状态机 mock 测试）
- `MemorySourceConcurrencyTest`：3 / 3 通过

## Failsafe（`*IT`）

`: 2 个测试类，12 个测试，0 失败，0 错误，0 跳过`

| 测试类 | 结果 | 耗时 |
| --- | --- | ---: |
| `MemoryFulltextSearchIT`（本轮新增，8 条） | **8 / 8 通过** | 8.244 s |
| `MemoryStateMachineConcurrencyIT`（既有，4 条） | **4 / 4 通过** | 3.656 s |

`MemoryFulltextSearchIT` 覆盖：

| 用例 | 验证点 |
| --- | --- |
| F1 | 中文关键词命中 ACTIVE 记忆 |
| F2 | 英文关键词命中 |
| F3 | 长句原话（多词）仍能命中 |
| F4 | `user_id` 隔离 |
| F5 | `CANDIDATE` / `ARCHIVED` 不参与检索 |
| F6 | GLOBAL / SPACE / CONCEPT 可见性矩阵 |
| F7 | 空 keyword 保持旧的列表语义（`importance DESC`） |
| F8 | `LIMIT` 上限生效 |

测试同时会检查 `information_schema.statistics`，在缺少 `ft_memory_content` 时补建索引，
因此这次运行也验证了迁移脚本中的 `ALTER TABLE ... WITH PARSER ngram` 在真实 MySQL 上可执行。

## 原始产物

- `maven-memory-fulltext-gated-20261107T2053.txt`：完整 Maven 输出
- `maven-memory-fulltext-gated-20261107T2053-surefire-xml.zip`：`target/surefire-reports/TEST-*.xml`
- `maven-memory-fulltext-gated-20261107T2053-failsafe-xml.zip`：`target/failsafe-reports/TEST-*.xml`
- `maven-memory-fulltext-gated-20261107T2053-summary.txt`：按测试类汇总的统计

## 说明

- 本产物证明 **FULLTEXT 检索 + 状态机测试** 在真实 MySQL 上通过；
- 它**不**代表默认 CI 会执行这些用例：默认模式下 12 个 IT 全部跳过；
- 本轮没有启用 RabbitMQ / Qdrant / Agent 门控，因此这份结果不能用于证明 RAG、MQ 链路。
