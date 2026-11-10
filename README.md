# AI Learning Space

> 跨对话保留“学习者当前怎么理解某个知识点”，并用这些记录调整后续回答的 AI Learning Agent。
> 当前实现以操作系统中的 TLB（Translation Lookaside Buffer）为主要演示概念。

**状态：可本地复现的开发基线，不是生产就绪产品。** 核心链路已闭环并有真实模型测试记录；
但 Flutter 客户端、人工评分 / 留存指标、多实例与持续压测仍未验收。历史与当前能力边界见
[docs/delivery/KNOWN_LIMITATIONS.md](docs/delivery/KNOWN_LIMITATIONS.md)。

## 当前能力与边界

- **已闭环**：Spring Boot + FastAPI Agent + MySQL + Qdrant；可选 RabbitMQ 异步分析；
  Vue 3 Web 客户端（登录、SSE 对话、文档、记忆、学习状态）；Memory 混合检索
  （MySQL FULLTEXT + Qdrant + RRF）；Citation 流式过滤；LearningState 投影。
- **已做真实模型验证**：`deepseek-flash` 单轮行为复验、Spring SSE → Agent 服务端 E2E、
  MQ / 文档引用 / 重复采样 / 小规模并发；最新登记 Agent 测试 **650 passed / 9 skipped /
  1 xfailed**，D4-5 离线评测 **21/21**（2026-11-10）；Spring 测试 **388 条运行 /
  300 执行通过 / 88 跳过 / 0 失败**（2026-11-08，未随 Phase 5 重跑）。
- **未闭环 / 不能声称**：Flutter 仍是脚手架；没有人工评分与留存指标；并发只到
  24 个短问题、单机单实例；没有 SLO、持续压测或断线重连验证；无 Flyway / Liquibase；
  文档 RAG 无 Reranker；引用校验是格式 + 词面启发式，不等于事实正确。
- **真实模型仍不稳定**：保留供应商 `ConnectError`、非法 JSON、上游断连的失败样本；
  失败时可能由 `rules-v1` 确定性兜底，不能据此宣称“真实模型已稳定”。

## 已知限制（Phase 5 登记 KI-1 ~ KI-7）

| ID | 问题 | 状态 |
|---|---|---|
| KI-1 | 模型历史使用不稳定，可能把 Understanding 转述称为用户原话 | 开放，已通过来源定义与 provenance 标签缓解，未根治 |
| KI-2 | `HISTORY_GUARD` 否定 / 对比语境误判，可能触发多余重生成或回退 | 已修复（2026-11-10） |
| KI-3 | L2-3 判据缺少“相关性”限定，话题切换场景出现评分口径分歧 | 新登记，下一轮修判据 |
| KI-4 | L3 收益阈值未预注册，只能使用事后判据 | 新登记（流程），不能声称达到预注册阈值 |
| KI-5 | 评审独立性 / 来源不可核验，两份评分表均无人工签名 | 新登记（流程），PASS 仅为内容判定 |
| KI-6 | L2-4“引用了哪些来源”口径不统一（16/30 条来源集合不同） | 新登记，评分表需拆分注入来源与实际引用 |
| KI-7 | Step 5 单次运行、缺少 repeat，B/C 组内排序可能含采样波动 | 新登记（方法学），补 repeat 前不说 C 优于 B |

详见 [docs/testing/phase5-known-issues.md](docs/testing/phase5-known-issues.md) 与
[docs/testing/phase5-final-report.md](docs/testing/phase5-final-report.md)。

## 架构概览

```text
Vue 3 / curl ──REST + SSE──> Spring Boot :8080 ──> MySQL 8
                                  │  └──> RabbitMQ（可选）──> Python Worker
                                  └──HTTP──> FastAPI Agent :8000 ──> Qdrant / LLM
```

Python Agent 不直接写 MySQL：Agent 只返回候选 / 回答，Spring 负责校验、去重与事务写入。
Evidence 追加不可变，Understanding 是当前快照，Revision 是不可变历史。

## 快速开始

```bash
# 1) MySQL 8 可用；库不存在会自动创建，表由 schema.sql 幂等初始化
# 2) Agent（默认 Mock + Fixture，不需要 Key）
cd agent && pip install -r requirements.txt && python -m app.main   # :8000
# 3) Spring Boot（另开终端；application-local.yml 不入库）
cd backend/backend && ./mvnw spring-boot:run                        # :8080
# 4) Web（另开终端）
cd web && npm install && npm run dev                                # :5173
```

真实模型凭据只通过环境变量或 gitignored 的 `.env` / `application-local.yml` 注入，
不要写进仓库。完整依赖、迁移顺序与常见失败见
[docs/delivery/ENVIRONMENT.md](docs/delivery/ENVIRONMENT.md)。

## 文档入口

- 文档总索引：[docs/README.md](docs/README.md)
- 架构与生命周期：[ARCHITECTURE](docs/delivery/ARCHITECTURE.md) ·
  [LEARNING_LIFECYCLE](docs/delivery/LEARNING_LIFECYCLE.md)
- 本地演示：[LOCAL_DEMO_GUIDE](docs/design/LOCAL_DEMO_GUIDE.md) ·
  测试矩阵：[TEST_MATRIX](docs/delivery/TEST_MATRIX.md)
- 已知限制：[delivery/KNOWN_LIMITATIONS](docs/delivery/KNOWN_LIMITATIONS.md) ·
  [audit/KNOWN_LIMITATIONS](docs/audit/KNOWN_LIMITATIONS.md)

## Phase 1-5 进展

| 阶段 | 范围 | 结论 | 证据 |
|---|---|---|---|
| Phase 1 | Agent 评测（40 条设计用例） | 执行 38、明确结论 35、0 FAIL、3 阻塞、2 未执行 | [phase1-results](docs/testing/phase1-results.md) |
| Phase 2 | Spring 测试（真实 MySQL / RabbitMQ / Qdrant） | 45 场景：41 PASS / 1 PARTIAL / 0 FAIL / 1 BLOCKED / 2 未覆盖；JUnit 49/49 | [phase2-results](docs/testing/phase2-results.md) |
| Phase 3 | 真实 LLM Agent 质量 | 校准后 9 PASS / 1 FAIL（D01）；LLM Judge 未经人工确认 | [phase3.1-results](docs/testing/phase3.1-results.md) |
| Phase 4 | Context Builder + 真实 RAG | 索引 7/7、检索隔离 PASS；预算压力存在结构性缺口 | [phase4-rag-agent-results](docs/testing/phase4-rag-agent-results.md) |
| Phase 5 | A/B/C 个性化策略与双人盲审 | L1 PASS；L2 PASS（89/90）；L3 方向性 PASS；**附条件关闭**，遗留 KI-1、KI-3~KI-7（KI-2 已修复） | [phase5-final-report](docs/testing/phase5-final-report.md) |

> Phase 5 的 L2/L3 PASS 是内容判定，不是评审组织合规判定：L3 阈值未预注册，
> 两份评分表为 AI 辅助 / 独立评审记录，缺少可核验人工签名。

## 一句话边界

> 当前回答依赖 Conversation，个性化依赖 Understanding，长期连续性依赖 Memory，
> 所有学习判断依赖 Evidence；以上能力均未达到生产验收标准。
