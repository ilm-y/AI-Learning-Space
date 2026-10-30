# 测试与质量矩阵（截至 2026-10-30，Phase 9 + 5.3 真实模型复验 + 服务端 E2E）

> **不要把这些数字合并成一个「总体通过率」。** 它们的执行时间、代码版本、评分规则、
> 测试语义都不同：Mock 只证明链路，真实模型才证明语义；RAG v1 / v2 是两套评分规则；
> 5.1 / 5.2 / 5.3 的断言数量也不可相加。

## 1. 结论速览

| 层级 | 最近结果 | 证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| Agent 单元 / 集成 | 383 passed，5 skipped，1 xfailed | 函数与运行时契约、来源过滤、预算边界 | 真实模型语义质量 |
| Spring 单元 / 集成 | 255 条（182 执行通过 / 73 skipped / 0 failed） | 服务 / 网关 / MQ 消费者契约 | 真实 RabbitMQ Testcontainers 全链路 |
| 5.1 inline E2E | 101 / 0 / 0 | Mock 下的对话 → 分析 → 证据 → 状态闭环 | 真实模型回答质量 |
| 5.1 MQ E2E | 27 / 0 / 0（有效重跑） | 真 RabbitMQ + 真 Worker 的异步机制 | 真实模型语义 |
| 5.2 学习轨迹 E2E | 94 / 0 / 0 | 跨会话状态、轨迹、权限、冲突 | 真实模型是否遵守历史边界 |
| RAG 真实模型 | v1 / v2 分别记录，不合并 | 检索、上下文、引用在真实模型下的表现 | 教学效果 |
| 5.3 真实模型（2026-10-27 历史） | 8 次调用；A/D 通过，B PARTIAL，C FAIL | 生产 Runtime 下的真实行为 | 完整通过 |
| 5.3 真实模型复验（2026-10-30） | 6 次调用 / 5 场景；A/B/C/D PASS，C-only PASS（fallback） | 真实 `deepseek-flash` 在 A/B/C/D 不再编造；C-only 被守卫误判后安全兜底 | 稳定合规、完整产品验收 |
| 5.3 服务端 E2E（2026-10-30） | 3 轮聊天 + 3 次 inline 分析；**47 PASS / 0 FAIL** | Spring SSE → Agent → `deepseek-flash`、消息 / run 落库、`agent-v1` 分析、显式复盘轨迹 | Flutter、MQ 真实模型、文档 RAG、重复采样 |
| 5.3.2 历史校验（2026-10-30） | 已用真实模型复验；C 无 issue，C-only `no_history_denial` → fallback | 校验器能拦截并兜底；同时暴露一次否定句误报 | 校验器无误报、模型独立通过 |

## 2. Agent 测试

命令（在 `agent/`）：

```bash
python -m pytest -q
```

最近结果（2026-10-30，5.3 契约修复后）：

```text
383 passed, 5 skipped, 1 xfailed
```

1 个 `xfail(strict=True)` 不是跳过，也不是通过：它记录「检索成功但片段被裁剪时，
模型可能用自然语言说没有检索到资料」这一当前架构无法确定性拦截的语义风险。
详见 `docs/testing/phase9-evidence-boundary.md`。

5 个 skip 都是环境门控，**跳过不等于通过**：

| 跳过项 | 条件 |
| --- | --- |
| `test_knowledge_e2e.py` ×1 | 需要 `AILS_KNOWLEDGE_E2E=true` 与真实链路 |
| `test_qdrant_integration.py` ×2 | 需要 `AILS_QDRANT_IT=true` 与真实 Qdrant |
| `test_worker_broker.py` ×2 | 需要 `AILS_TEST_RABBITMQ_URL` 与真实 broker |

本轮没有设置上述变量，所以真实 Qdrant / 真实 broker / 真实知识 E2E 均**未执行**。

## 3. Spring 测试

命令（在 `backend/backend/`）：

```bash
./mvnw test
```

最近结果（2026-10-29，Phase 9 重跑）：

```text
Tests run: 255, Failures: 0, Errors: 0, Skipped: 73
Surefire 聚合：39 个 report，255 total / 182 executed / 182 passed / 73 skipped
```

`Tests run` 包含 skipped，不能写成「255 passed」。73 个 skip 全部是环境门控
（真实 MySQL / Docker / 环境变量）：

| 测试类 | skip | 条件 |
| --- | ---: | --- |
| `BackendApplicationTests` | 1 | 需要本地 MySQL 与库表 |
| `ChatSseIntegrationTest` | 8 | `AILS_CHAT_SSE_IT=true` |
| `ChatSseOrderingProbeTest` | 1 | `AILS_CHAT_SSE_IT` + `AILS_P8_ORDER_PROBE` |
| `DocumentIndexE2EIntegrationTest` | 1 | `AILS_DOCUMENT_E2E_IT` |
| `DocumentIndexServiceIntegrationTest` | 15 | `AILS_DOCUMENT_INDEX_IT` |
| `DocumentServiceIntegrationTest` | 7 | `AILS_DOCUMENT_IT` |
| `InternalIdentityHttpIntegrationTest` | 5 | 内部身份集成开关 |
| `Phase2MqProcessorIntegrationTest` | 5 | 真实 MySQL + MQ |
| `Phase2RabbitMqBrokerTest` | 5 | 真实 broker |
| `Phase2RealStateIntegrationTest` | 6 | 真实学习状态链路 |
| `SecurityContextIntegrationTest` | 19 | `AILS_SECURITY_IT` |

`AnalysisMqIntegrationTest` 在默认 `./mvnw test` 下仍是 **0 个用例执行**：它需要
`-Pmq-integration` 与 Docker（Testcontainers 拉起 RabbitMQ）。没有把它算进 255，
也没有把「0 executed」写成「通过」。

CI 配置（`.github/workflows/ci.yml`）会跑：

```bash
./mvnw -B clean test -Pmq-integration
```

本轮**没有执行 CI**，也没有在本地跑 `-Pmq-integration`。

## 4. 端到端（E2E）

所有 E2E 的详细前置条件见各脚本头部。共同点：

- 后端 `8080`（inline）或 `8081`（mq）；
- Agent `8000`；除 `phase53-spring-e2e.py` 外均为 `AILS_LLM_PROVIDER=mock`；
- MySQL 可连，凭证从 `application-local.yml` 读取；
- 脚本只通过公开 API / SSE / 分析链路产生数据；直接 SQL 只用于只读核对。

| 脚本 | 结果（2026-10-27） | 说明 |
| --- | --- | --- |
| `scripts/phase51-e2e.sh` | **101 PASS / 0 FAIL / 0 SKIP** | inline 分析、SSE、Evidence、Session、状态 |
| `scripts/phase51-mq-e2e.sh` | **27 PASS / 0 FAIL / 0 SKIP** | 真 RabbitMQ + 真 Worker；重复投递幂等 |
| `scripts/phase52-e2e.sh` | **94 PASS / 0 FAIL / 0 SKIP** | A 当前误区 / B 已纠正 / C 复盘 / D Memory 冲突 / E 权限与闲聊 |
| `scripts/phase6-demo.sh` | **23 PASS / 0 FAIL** | TLB 七步演示：证据 / Revision / 新会话 / 复盘 / SSE / 只读查库 |
| `scripts/phase53-spring-e2e.py` | **47 PASS / 0 FAIL（2026-10-30）** | 真实 `deepseek-flash`；Spring SSE、消息 / run 落库、inline 分析、复盘轨迹 |
| `scripts/regress-closed-loop.sh` | 本轮未重跑 | 旧闭环脚本；不要引用为当前结果 |

除 `phase53-spring-e2e.py` 外，这些 E2E 全部使用 **Mock LLM**。它们能证明检索、
上下文注入、持久化、状态流转、权限隔离和 SSE 事件序列；不能证明真实模型的语义判断
或教学效果。真实模型服务端 E2E 的边界见
[`docs/testing/phase53-spring-e2e-20261030.md`](../testing/phase53-spring-e2e-20261030.md)：
没有 Flutter、MQ、文档 RAG、重复采样。

### MQ 小节

`scripts/phase51-mq-e2e.sh` 的前置是：

```bash
# RabbitMQ
cd docker && docker compose up -d
# 后端 mq 模式（8081）
cd backend/backend
SERVER_PORT=8081 AILS_ANALYSIS_MODE=mq \
  AILS_ANALYSIS_COMPENSATION_ENABLED=false \
  AILS_INTERNAL_TOKEN=phase51-internal-token \
  ./mvnw -q -o spring-boot:run
# Worker
cd agent
AILS_INTERNAL_TOKEN=phase51-internal-token python -m app.worker
```

执行：

```bash
BACKEND_LOG=/path/to/backend-mq.log bash scripts/phase51-mq-e2e.sh
```

结果见本文件末尾的「最近执行记录」。

**第一次尝试失败，不是产品缺陷，而是测试环境争用。** 第一次运行时另有一个常驻
Worker 也已连接同一队列，脚本在「Worker 未启动」窗口观察到了该 Worker 写入的
Evidence，失败路径也被它提前消费，结果 18 PASS / 9 FAIL。产物保留在
`backend/backend/target/phase51-mq-e2e/20261027-021523/`，不要删除。
停掉常驻 Worker、清空 request/result 队列后重跑，脚本自己启动并停止 Worker，
得到 27 PASS / 0 FAIL（`.../20261027-021732/`）。这说明该脚本要求**独占队列**，
README 与演示前必须先确认没有其他 Worker 在消费。

## 5. 回归评测（`evaluation/`）

### 5.1 离线用例（当前跑过）

```bash
python -m evaluation
```

最近结果（2026-10-27，代码版本 `74d99a1`）：

```text
离线：40 条，通过 40，失败 0，跳过 0，通过率 100.0%
断言：124 / 124
```

报告：`evaluation/reports/latest.md`、`evaluation/reports/latest.json`（每次运行覆盖）。

### 5.2 全量 online 用例（历史结果，未重跑）

`evaluation/reports/` 里还有一份 **2026-10-21、代码版本 `34b59b5`** 的 `--mode all` 结果：
49 条用例全部通过、194 条断言。它证明的是那个提交上的行为，**不是当前工作区**。
本轮没有重跑 `--mode all`。

## 6. RAG / 引用评测（真实模型）

这些报告是 **2026-10-26** 用 `qwen-plus` + `text-embedding-v4` 跑的真实模型结果。
检索参数（top_k=5、min_score=0.35、max_context_tokens=3000）没有在 5.3.1 / 5.3.2 中改动。

### 6.1 v1（Phase 4.2 规则）

来源：`evaluation/rag/reports/real_20261026_full_revalidated/summary.md`

| 模式 | 运行 | 自动通过 | 检索通过 | 上下文通过 | 答案通过 | 引用通过 | 平均延迟 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| plain | 37 | 10 | N/A | 100.0% | 20.0% | 70.3% | 14470 ms |
| rag | 40 | 29 | 100.0% | 100.0% | 85.0% | 82.5% | 8536 ms |
| rag+state | 41 | 23 | 100.0% | 100.0% | 80.5% | 61.0% | 9728 ms |

### 6.2 v2（Phase 4.3 规则，不能与 v1 直接比较百分比）

来源：`evaluation/rag/reports/real_20261026_v2_offline_final/summary.md`

| 模式 | 运行 | 自动通过 | 检索通过 | 上下文通过 | 答案通过 | 引用通过 | 平均延迟 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| plain | 37 | 11 | N/A | 100.0% | 23.3% | 70.3% | 14470 ms |
| rag | 40 | 30 | 100.0% | 100.0% | 87.5% | 85.0% | 8536 ms |
| rag+state | 41 | 31 | 100.0% | 100.0% | 82.9% | 78.0% | 9728 ms |

引用指标各有分母，不能横向比较；来源支持度只是词面启发式，不是事实正确率。

### 6.3 Phase 4.4 非成功检索兜底

| 产物 | 结果 | 说明 |
| --- | --- | --- |
| `reports/real_20261026_phase44_guard_targeted` | rag 3/3、rag+state 3/3 通过 | 真实模型；D01/D04/D05 等目标用例 |
| `reports/real_20261026_phase44_guard_controls` | rag 2/2 通过 | 真实模型对照 |
| `reports/real_20261026_phase44_guard_sse` | S04 通过（229 事件 / 227 delta） | 真实模型 + 真实 Spring SSE |
| `reports/real_20261026_phase44_guard_offline` | rag 30→34、rag+state 31→32（自动通过） | **离线模拟**，不调用模型；只把兜底说明套到已保存回答上 |
| `reports/spring_sse_20261026` | **S04 未通过** | 4.2 基线失败样本，保留不删；4.4 guard 后 S04 通过 |

## 7. 阶段 5.3 真实模型行为评测

### 7.1 调用次数

| 轮次 | 调用数 | 说明 |
| --- | ---: | --- |
| 5.3 第一轮 | 4 | A / B / C / C-only |
| 5.3.1 第二轮 | 4 | B / C / C-only / D 的修正后复验 |
| 5.3.2（2026-10-27） | **0** | 当时本地没有 `AILS_LLM_API_KEY`；不伪造真实调用 |
| 5.3 复验（2026-10-30） | **6** | A/B/C/C-only/D；C-only 触发一次重生成 |

### 7.2 结果

来源：`backend/backend/target/phase53-eval/20261027-004622/REPORT-5.3.1.md`

| 场景 | 5.3.1 第二轮结果 | 判定 |
| --- | --- | --- |
| A | 未重复调用（已通过基线） | 基线保持 |
| B | OS 软件处理错误已消除；不再把 MMU/SRAM 归入已掌握；仍有「高频访问」超范围、权限检查简化，且丢失 `TLB miss ≠ page fault` 区分 | **PARTIAL**，不是通过 |
| C Trajectory | 能引用时间线与真实事件，不泄露内部 ID；仍虚构「很可能通过重读定义迁移」，漏掉显式批注 | **FAIL**，保留失败样本 |
| C Current-only | 不再断言「你以前没有错误」，明确说明无法确认；技术表述仍有错误 | 历史缺失处理通过；技术层面未通过 |
| D Memory 冲突 | 明确「过去曾有，现在已修正」；未推断原因；未删除/改写 Memory | 通过（未引用 Memory 时间/来源，留有余地） |

### 7.3 2026-10-30 真实模型复验

模型 `deepseek-flash`，`temperature=0.6`，`max_tokens=4096`；5 场景、6 次调用。
完整报告：[`docs/testing/phase53-real-model-20261030.md`](../testing/phase53-real-model-20261030.md)。

| 场景 | 结果 | 关键证据 |
| --- | --- | --- |
| A | **PASS** | 区分页表 / 页表项 / 缺页；不假设已掌握 |
| B | **PASS** | 保留 `TLB miss ≠ 缺页`；未把扩展内容写成已掌握 |
| C | **PASS** | 只陈述记录，明确「记录里看不出原因」；无旧版编造 |
| C-only | **PASS（fallback）** | 模型两次拒绝编造；`no_history_denial` 误判后确定性 fallback |
| D | **PASS** | 旧 Memory 标为历史，当前状态不同 |

所有调用 `finish_reason=stop`、`truncated=false`。注意：C-only 的最终文本不是
模型原文，而是 `build_history_fallback()`；本轮不能证明「模型独立通过」。

### 7.4 保留的失败样本

C 的原始输出（5.3.1 `runtime-eval/C.json` 的 `call.rawAnswer`）包含：

> 你很可能通过重读定义 + 对比缓存通用含义迁移了过来……而且没有外部提示或资料介入。

C-only 的原始输出包含：

> 你已经越过了一个常见误区……你也避开了另一个典型混淆……

这些是真实模型输出，没有被删除、没有被改写成「通过」。

### 7.5 Spring 服务端 E2E（2026-10-30）

独立启动 Spring 8081 + Agent 8000，通过公开 API 跑 3 轮。完整报告：
[`docs/testing/phase53-spring-e2e-20261030.md`](../testing/phase53-spring-e2e-20261030.md)。

| 轮次 | HTTP / SSE | 模型输出 | run / message | inline 分析 |
| --- | --- | --- | --- | --- |
| 普通问题 | 200；74 事件 / 72 delta；1828 ms | 123 字，`finish=stop` | CHAT 536 / message 415 | ANALYSIS 537，`evidence=1`，`agent-v1` |
| 错误解释 | 200；886 事件 / 884 delta；17686 ms | 1476 字，`finish=stop` | CHAT 538 / message 417 | ANALYSIS 539，`evidence=3`，`agent-v1` |
| 显式复盘 | 200；11 事件 / 9 delta；10688 ms | 1035 字，`finish=stop` | CHAT 541 / message 419 | ANALYSIS 542，`evidence=0`（允许） |

三次 `message.truncated=0`；显式复盘轨迹 `trajectoryRevisions=3`、
`trajectoryEvidenceIds=[398,400,402]`、`history_guard issues=[]`、`fallback=false`。
没有 `message.error`。

首轮曾发现 `verificationDecision.trigger` 被真实模型写成自由文本，导致分析契约失败并
退回 `rules-v1`；修复 `_coerce_verification_enums()`、补充 prompt 枚举和契约测试后，
正式轮恢复 `agent-v1`。失败样本保留在
`backend/backend/target/phase53-spring-e2e/20261030-210931/`。

## 8. 阶段 5.3.2 历史校验复验

| 项目 | 结果 | 性质 |
| --- | --- | --- |
| 离线回放 C | 881 → 551 字；4 个 issue；fallback=true | 不调用模型，只验证校验器 |
| 离线回放 C-only | 924 → 176 字；2 个 issue；fallback=true | 同上 |
| 生产 SSE（Mock） | `message.start` → 5 × `message.delta` → `message.end`；551 字；fallback=true | Mock 链路验收 |
| 真实模型（2026-10-30） | C：927 字、0 issue；C-only：首答/重生成均被 `no_history_denial` 误判，fallback=true（176 字） | 真实调用；C 通过，C-only 依赖 fallback |

产物：

- `backend/backend/target/phase53-eval/20261027-004622/phase532-eval/offline-replay.json`
- `.../phase532-eval/production-sse.sse`
- `.../phase532-eval/production-sse-summary.json`
- `.../phase532-eval/production-trace.json`
- `.../REPORT-5.3.2.md`

## 9. 未执行 / 环境门控清单

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| Agent 真实 Qdrant 集成（2） | skipped | 未设 `AILS_QDRANT_IT=true` |
| Agent 真实 broker（2） | skipped | 未设 `AILS_TEST_RABBITMQ_URL` |
| Agent 知识 E2E（1） | skipped | 未设 `AILS_KNOWLEDGE_E2E=true` |
| Spring MQ Testcontainers | **未执行** | 未跑 `-Pmq-integration` |
| Spring MySQL / 文档集成（29） | skipped | 未设对应 `AILS_*_IT` |
| `python -m evaluation --mode all` | 未重跑 | 只有 2026-10-21 的历史结果 |
| `scripts/regress-closed-loop.sh` | 未重跑 | 旧闭环脚本 |
| GitHub Actions CI | 未执行 | 本地未触发 |
| Flutter 测试 | 未执行 | 仍是脚手架，没有业务代码 |
| 5.3 / 5.3.2 真实模型复验 | **已执行 6 次（5 场景）** | 2026-10-30；A/B/C/D PASS，C-only fallback |
| Spring SSE 真实模型端到端 | **已执行（2026-10-30，47 / 0）** | 3 轮聊天 + 3 次 inline 分析；不含 Flutter、MQ、文档 RAG |
| MQ 真实模型端到端 | 未执行 | 本轮 `AILS_ANALYSIS_MODE=inline` |
| 真实模型重复采样 / 人工评分 | 未执行 | 每个场景 1 轮；判定由脚本完成 |

## 10. 最近执行记录

本节只记录本轮实际跑过的命令与结果；未跑的项目不写入。

```text
2026-10-27  Agent   python -m pytest -q                 232 passed, 5 skipped
2026-10-27  Spring  ./mvnw test                          189 passed, 29 skipped, 0 failed
2026-10-27  Eval    python -m evaluation                 40/40, 124/124 assertions
2026-10-27  E2E     scripts/phase51-e2e.sh               101 PASS / 0 FAIL / 0 SKIP
2026-10-27  E2E     scripts/phase52-e2e.sh               94 PASS / 0 FAIL / 0 SKIP
2026-10-27  E2E     scripts/phase51-mq-e2e.sh            27 PASS / 0 FAIL / 0 SKIP（独占队列重跑）
2026-10-27  Demo    scripts/phase6-demo.sh               23 PASS / 0 FAIL（Mock）
2026-10-29  Agent   python -m pytest -q                 382 passed, 5 skipped, 1 xfailed
2026-10-29  Agent   citation 定向回归                   22 passed, 1 xfailed
2026-10-29  Spring  ./mvnw -B test                        255 条 / 182 执行通过 / 73 skipped / 0 failed
2026-10-29  Eval    python -m evaluation                 40/40, 124/124 assertions
2026-10-29  Agent   Phase 6 注入与安全回归               16 passed
2026-10-30  E2E     scripts/phase53-setup.sh             23 PASS / 0 FAIL（Mock 播种）
2026-10-30  E2E     scripts/phase53-setup-clean.sh       11 PASS / 0 FAIL（Mock 播种）
2026-10-30  Real    5.3 runtime-eval                     A/B/C/D PASS；C-only fallback；6 次调用
2026-10-30  Guard   phase532_replay_guard.py            C 0 issue；C-only no_history_denial → fallback
2026-10-30  Agent   python -m pytest -q                 383 passed, 5 skipped, 1 xfailed
2026-10-30  E2E     scripts/phase53-spring-e2e.py       47 PASS / 0 FAIL（真实 deepseek-flash）
```

## 11. 质量报告索引

| 报告 | 路径 |
| --- | --- |
| Agent 离线评测（当前） | `evaluation/reports/latest.md` |
| Phase 9 证据边界 | `docs/testing/phase9-evidence-boundary.md` |
| 5.3 真实模型复验（2026-10-30） | `docs/testing/phase53-real-model-20261030.md` |
| 5.3 Spring 服务端 E2E（2026-10-30） | `docs/testing/phase53-spring-e2e-20261030.md` |
| 5.3.2 复验报告（2026-10-27） | `backend/backend/target/phase53-eval/20261027-004622/REPORT-5.3.2.md` |
| 5.3.1 真实模型报告 | `backend/backend/target/phase53-eval/20261027-004622/REPORT-5.3.1.md` |
| RAG v1 全量 | `evaluation/rag/reports/real_20261026_full_revalidated/summary.md` |
| RAG v2 全量 | `evaluation/rag/reports/real_20261026_v2_offline_final/summary.md` |
| 4.4 guard 离线模拟 | `evaluation/rag/reports/real_20261026_phase44_guard_offline/guard_offline.json` |
| 4.4 guard 真实模型 | `evaluation/rag/reports/real_20261026_phase44_guard_targeted/summary.md` |
| 4.4 guard Spring SSE | `evaluation/rag/reports/real_20261026_phase44_guard_sse/spring_sse.md` |
| 4.2 基线 SSE 失败样本 | `evaluation/rag/reports/spring_sse_20261026/spring_sse.md` |
| 历史全量评测 | `evaluation/reports/latest.md`（已被 2026-10-27 离线运行覆盖；历史值见 git 历史） |
