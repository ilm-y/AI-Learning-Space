# docs 文档索引

本索引按用途分组，只做导航，不修改任何现有文档。

**标记说明**：⚠ = 被代码 / 测试 / 脚本 / 结论文档引用，**禁止移动或改名**；
未标记的文件仅被其他文档链接，移动时仍需同步更新链接。

> 当前基线：`main` 分支，Phase 5 已**附条件关闭**（2026-11-10）。
> 生产代码与测试的权威边界见 [delivery/KNOWN_LIMITATIONS.md](delivery/KNOWN_LIMITATIONS.md)。

---

## 1. 当前状态与总览

| 文件 | 说明 |
|---|---|
| [testing/phase5-final-report.md](testing/phase5-final-report.md) ⚠ | Phase 5 Step 5 解盲、L2/L3 汇总与“附条件关闭”判定；遗留 KI-1~KI-7 |
| [delivery/TEST_MATRIX.md](delivery/TEST_MATRIX.md) ⚠ | 测试 / 质量矩阵与报告索引（截至 2026-10-30） |
| [delivery/KNOWN_LIMITATIONS.md](delivery/KNOWN_LIMITATIONS.md) ⚠ | 面向交付的已知限制、未完成验收与不要做的事 |
| [audit/PROJECT_BASELINE.md](audit/PROJECT_BASELINE.md) | 2026-11-07 项目基线只读审计与总体结论 |
| [testing/current-state-audit.md](testing/current-state-audit.md) | Phase 0 现状审计：代码、测试与文档对齐情况 |
| [prd.md](prd.md) | 产品需求文档：目标用户、核心流程与验收边界 |

## 2. 审计与契约

### 2.1 核心设计与契约

| 文件 | 说明 |
|---|---|
| [delivery/ARCHITECTURE.md](delivery/ARCHITECTURE.md) ⚠ | 运行时 / 服务架构、数据方向与未实现能力 |
| [delivery/LEARNING_LIFECYCLE.md](delivery/LEARNING_LIFECYCLE.md) ⚠ | Evidence / Understanding / Revision / LearningState 生命周期与存储语义 |
| [delivery/ENVIRONMENT.md](delivery/ENVIRONMENT.md) ⚠ | 依赖、启动顺序、环境变量、建表与迁移 |
| [delivery/DEMO.md](delivery/DEMO.md) ⚠ | TLB 七步可重复演示（明确标注 Mock） |
| [delivery/GIT_WORKSPACE.md](delivery/GIT_WORKSPACE.md) ⚠ | 工作区文件分类、风险与提交建议 |
| [delivery/INTERVIEW.md](delivery/INTERVIEW.md) ⚠ | 面试讲解提纲：核心设计、关键缺陷与修复 |
| [session-design.md](session-design.md) ⚠ | Session 的定义、创建时机、归属解析与生命周期契约 |
| [database&API_design.md](database&API_design.md) ⚠ | 数据库表、字段语义与 API 设计 |
| [analysis.md](analysis.md) ⚠ | 项目设计取舍与市场校准分析 |
| [analysis-mq.md](analysis-mq.md) ⚠ | RabbitMQ 学习分析链路的拓扑、消息契约与失败分类 |
| [analysis-mq-reliability-review.md](analysis-mq-reliability-review.md) | MQ 异步分析链路的可靠性问题复盘 |
| [design/FRONTEND_API_SPEC.md](design/FRONTEND_API_SPEC.md) ⚠ | 前端 API / SSE 事件契约（源码审计结果） |
| [design/MEMORY_HYBRID_SEARCH_DESIGN.md](design/MEMORY_HYBRID_SEARCH_DESIGN.md) | Memory 混合检索设计：FULLTEXT + Outbox + Qdrant + RRF |
| [design/MEMORY_MINIMAL_FIX.md](design/MEMORY_MINIMAL_FIX.md) | Memory 最小状态机修复设计 |
| [design/RAG_MINIMAL_RELIABILITY.md](design/RAG_MINIMAL_RELIABILITY.md) | RAG 文档索引可靠性审查与最小修复设计 |
| [operations/rag-04-legacy-reindex.md](operations/rag-04-legacy-reindex.md) ⚠ | legacy 文档重建 Runbook（RAG-04 阶段 A） |
| [design/LOCAL_DEMO_GUIDE.md](design/LOCAL_DEMO_GUIDE.md) ⚠ | Web + Spring 本地启动、注册 / 上传 / 对话 / 记忆联调 |

### 2.2 审计与实施记录

| 文件 | 说明 |
|---|---|
| [audit/D4-1_CONTEXT_ASSEMBLY_AUDIT.md](audit/D4-1_CONTEXT_ASSEMBLY_AUDIT.md) | Agent 上下文组装链路只读审计与 Manifest Schema 方案 |
| [audit/D4-2C_CONCEPT_CONTINUITY.md](audit/D4-2C_CONCEPT_CONTINUITY.md) | Concept 连续性契约 |
| [audit/D4-2D_RETRIEVAL_POLICY.md](audit/D4-2D_RETRIEVAL_POLICY.md) | Retrieval Policy 契约 |
| [audit/INTEGRATION_VERIFICATION.md](audit/INTEGRATION_VERIFICATION.md) | Phase 1 + Phase 2 集成验证与单次全量证据链 |
| [audit/TEST_EVIDENCE_MODES.md](audit/TEST_EVIDENCE_MODES.md) | 测试证据等级与运行模式对照 |
| [audit/THREE_PHASE_FINAL_REVIEW.md](audit/THREE_PHASE_FINAL_REVIEW.md) | Memory / RAG / Citation 三阶段最终独立审查 |
| [audit/PHASE1_MEMORY_FIX.md](audit/PHASE1_MEMORY_FIX.md) | Phase 1B Memory 状态机最小修复实施记录 |
| [audit/PHASE2_AGENT_SYNC.md](audit/PHASE2_AGENT_SYNC.md) | Agent 向量化接口与同步通道实施报告 |
| [audit/PHASE2_RAG_RELIABILITY.md](audit/PHASE2_RAG_RELIABILITY.md) | RAG 文档索引可靠性落地报告 |
| [audit/PHASE3_CITATION_CLOSURE.md](audit/PHASE3_CITATION_CLOSURE.md) | Citation 最小闭环落地报告 |
| [audit/PHASE3_OUTBOX_RRF.md](audit/PHASE3_OUTBOX_RRF.md) | Outbox 异步同步 + RRF 混合检索实施报告 |
| [testing/phase5-pre-step3-review.md](testing/phase5-pre-step3-review.md) ⚠ | Phase 5 Step 3 前置复核（含 HISTORY_GUARD 实现点） |
| [testing/phase5-preflight-audit.md](testing/phase5-preflight-audit.md) | Phase 5 Step 1 预检审计 |

### 2.3 本地未跟踪笔记

| 文件 | 说明 |
|---|---|
| [interview-project-deep-dive.md](interview-project-deep-dive.md) | 个人面试准备笔记；被 `.gitignore` 排除，不入库 |
| [delivery/INTERVIEW-DEADLOCK-DEEP-DIVE.md](delivery/INTERVIEW-DEADLOCK-DEEP-DIVE.md) | 面试专题：两处真实死锁；被 `.gitignore` 排除，不入库 |

## 3. 评测

| 文件 | 说明 |
|---|---|
| [testing/agent-evaluation-plan.md](testing/agent-evaluation-plan.md) | Agent 评测方案（Phase 1 / Phase 1.1） |
| [testing/backend-test-plan.md](testing/backend-test-plan.md) | Spring Boot 后端测试方案与 Phase 2 执行口径 |
| [testing/phase3-agent-quality-plan.md](testing/phase3-agent-quality-plan.md) | Phase 3 真实 LLM Agent 质量评测计划 |
| [testing/phase3.1-evaluator-calibration.md](testing/phase3.1-evaluator-calibration.md) | Phase 3.1 评测器校准与历史 artifact 重判 |
| [testing/phase4-rag-e2e-plan.md](testing/phase4-rag-e2e-plan.md) | Phase 4 真实 RAG 端到端验证计划与执行口径 |
| [eval/D4-5_CONTEXT_QUALITY_EVALUATION.md](eval/D4-5_CONTEXT_QUALITY_EVALUATION.md) | D4-5 上下文质量离线评测集与基线（当前 21/21） |
| [audit/MEMORY_RETRIEVAL_EVALUATION.md](audit/MEMORY_RETRIEVAL_EVALUATION.md) ⚠ | Phase 4 Memory 检索召回率离线评测报告 |

评测数据（**被代码硬编码，禁止移动**）：

| 文件 | 说明 |
|---|---|
| [testing/agent-evaluation-cases.jsonl](testing/agent-evaluation-cases.jsonl) ⚠ | Phase 1 / 3 评测用例源数据，`evaluation/phase1`、`phase3` 直接读取 |
| [testing/phase3-agent-quality-cases.jsonl](testing/phase3-agent-quality-cases.jsonl) ⚠ | Phase 3 Agent 质量评测 10 条用例 |
| [testing/fixtures/phase1_two_users_two_spaces.json](testing/fixtures/phase1_two_users_two_spaces.json) ⚠ | Phase 1 / 3 双用户双空间 fixture |
| [testing/fixtures/phase1_auto_space.json](testing/fixtures/phase1_auto_space.json) ⚠ | Phase 1 自动建空间 fixture |

## 4. 测试记录

### 4.1 Phase 1-4

| 文件 | 说明 |
|---|---|
| [testing/phase1-results.md](testing/phase1-results.md) | Phase 1：40 条设计用例，执行 38 / 明确结论 35 / 0 FAIL |
| [testing/phase1.1-security-results.md](testing/phase1.1-security-results.md) | Phase 1.1 安全修复结果（B07 / B06 与上下文缺陷） |
| [testing/phase2-results.md](testing/phase2-results.md) | Phase 2：Spring + 真实依赖 45 场景；JUnit 49/49 |
| [testing/phase2.1-integration-results.md](testing/phase2.1-integration-results.md) | Phase 2.1 集成与端到端补充结果 |
| [testing/phase2.1-llm-stream-diagnostics.md](testing/phase2.1-llm-stream-diagnostics.md) | Phase 2.1 真实 LLM 空流诊断 |
| [testing/phase2.1-security-results.md](testing/phase2.1-security-results.md) | Phase 2.1 安全边界与真实 HTTP 验证 |
| [testing/phase3-agent-quality-results.md](testing/phase3-agent-quality-results.md) | Phase 3 真实 LLM Agent 质量评测结果（已被 3.1 更正） |
| [testing/phase3.1-results.md](testing/phase3.1-results.md) | Phase 3.1 内部身份边界与评测器校准；9 PASS / 1 FAIL |
| [testing/phase3.1-human-review-sheet.md](testing/phase3.1-human-review-sheet.md) ⚠ | Phase 3.1 人工复核评分表（脚本生成路径） |
| [testing/phase3.1-security-results.md](testing/phase3.1-security-results.md) | Phase 3.1 内部身份签发边界安全结果 |
| [testing/phase3.2-agent-review-readiness.md](testing/phase3.2-agent-review-readiness.md) | Phase 3.2 Agent 人工评审准备情况 |
| [testing/phase3.2-human-review-blind.md](testing/phase3.2-human-review-blind.md) ⚠ | Phase 3.2 盲评版人工复核评分表（脚本生成路径） |
| [testing/phase3.2-mq-e2e-results.md](testing/phase3.2-mq-e2e-results.md) | Phase 3.2 MQ 契约端到端验证 |
| [testing/phase3.2-security-verification.md](testing/phase3.2-security-verification.md) | Phase 3.2 身份边界与部署面安全复核 |
| [testing/phase3.2-skipped-audit.md](testing/phase3.2-skipped-audit.md) | Phase 3.2 跳过测试审计（跳过不等于通过） |
| [testing/phase3.3-agent-review-status.md](testing/phase3.3-agent-review-status.md) | Phase 3.3 Agent 人工评审状态与测试优先级 |
| [testing/phase3.3-schema-rollout-review.md](testing/phase3.3-schema-rollout-review.md) | Phase 3.3 消息升级策略评审：schema v1 → v2 |
| [testing/phase3.3-security-boundary.md](testing/phase3.3-security-boundary.md) | Phase 3.3 委托声明、队列延迟与部署面 |
| [testing/phase4-rag-agent-results.md](testing/phase4-rag-agent-results.md) | Phase 4 Context Builder 与真实模型结果 |
| [testing/phase4-rag-index-results.md](testing/phase4-rag-index-results.md) | Phase 4 索引与生命周期：硬检查 7/7 PASS |
| [testing/phase4-rag-retrieval-results.md](testing/phase4-rag-retrieval-results.md) | Phase 4 真实检索、权限隔离与来源追溯 |
| [testing/phase4.1-agent-human-review.md](testing/phase4.1-agent-human-review.md) ⚠ | Phase 4.1 真实回答盲评表（脚本生成路径） |
| [testing/phase4.1-context-budget-results.md](testing/phase4.1-context-budget-results.md) | Phase 4.1 F1：检索状态与预算后最终状态 |
| [testing/phase4.1-index-recovery-results.md](testing/phase4.1-index-recovery-results.md) | Phase 4.1 F2：中断索引的超时回收 |
| [testing/phase4.1-source-attribution-results.md](testing/phase4.1-source-attribution-results.md) | Phase 4.1 F3：事实级来源归属 |

### 4.2 Phase 5

| 文件 | 说明 |
|---|---|
| [testing/phase5-connectivity-check.md](testing/phase5-connectivity-check.md) | Step 2 连通性验证记录 |
| [testing/phase5-preflight-audit.md](testing/phase5-preflight-audit.md) | Step 1 预检审计 |
| [testing/phase5-pre-step3-review.md](testing/phase5-pre-step3-review.md) ⚠ | Step 3 前置复核（生产代码注释引用） |
| [testing/phase5-pilot-results.md](testing/phase5-pilot-results.md) ⚠ | Step 3 Pilot：A/B/C 小规模真实调用 |
| [testing/phase5-abc-results.md](testing/phase5-abc-results.md) ⚠ | Step 3 全量 10 场景 × 3 策略 × 3 次 A/B/C 对照 |
| [testing/phase5-abc-rerun.md](testing/phase5-abc-rerun.md) ⚠ | Step 3 判据修正与受影响场景重跑；含 Batch 5/6/7 |
| [testing/phase5-c-verification.md](testing/phase5-c-verification.md) ⚠ | pre-Step3 批次 3：C 策略真实链路验证 |
| [testing/phase5-budget-validation.md](testing/phase5-budget-validation.md) | 预算裁剪下的检索状态与引用边界验证 |
| [testing/phase5-known-issues.md](testing/phase5-known-issues.md) ⚠ | KI-1 / KI-2 / KI-3 现象、证据与状态登记 |
| [testing/phase5-step4-results.md](testing/phase5-step4-results.md) ⚠ | Step 4：真实 MySQL + Qdrant + Embedding + LLM 全链路 |
| [testing/phase5-step5-results.md](testing/phase5-step5-results.md) ⚠ | Step 5：A/B/C 对照、盲审准备与 L1/L2/L3 分层 |
| [testing/phase5-blind-review.md](testing/phase5-blind-review.md) ⚠ | Step 5 盲审材料与评分模板 |
| [testing/phase5-blind-review-completed1.md](testing/phase5-blind-review-completed1.md) ⚠ | 评审 1 完成稿（自述单一 AI 辅助评审） |
| [testing/phase5-blind-review-completed2.md](testing/phase5-blind-review-completed2.md) ⚠ | 评审 2 完成稿（自述独立评审 A） |
| [testing/phase5-final-report.md](testing/phase5-final-report.md) ⚠ | 解盲、L2/L3 汇总与 Phase 5 附条件关闭判定 |

### 4.3 5.3 / 5.4 / Phase 6-9 交付验证

| 文件 | 说明 |
|---|---|
| [testing/phase53-real-model-20261030.md](testing/phase53-real-model-20261030.md) ⚠ | 5.3 真实模型运行时复验（2026-10-30） |
| [testing/phase53-spring-e2e-20261030.md](testing/phase53-spring-e2e-20261030.md) ⚠ | 5.3 Spring SSE → Agent → 真实模型服务端 E2E |
| [testing/phase54-real-model-20261030.md](testing/phase54-real-model-20261030.md) ⚠ | 5.4 MQ 真实模型、文档引用、重复采样与并发压测 |
| [testing/phase6-offline-baseline.md](testing/phase6-offline-baseline.md) ⚠ | Phase 6：恢复当前 HEAD 的离线评测基线 |
| [testing/phase7-stream-truncation.md](testing/phase7-stream-truncation.md) | Phase 7：流式回答截断终态 |
| [testing/phase8-boundary-review.md](testing/phase8-boundary-review.md) | Phase 8：Phase 7 交付边界复核 |
| [testing/phase9-evidence-boundary.md](testing/phase9-evidence-boundary.md) ⚠ | Phase 9：来源过滤、知识状态边界与项目冻结 |
| [testing/resume-evidence.md](testing/resume-evidence.md) | 简历证据清单（Phase 2.1 后；链接大量测试记录） |

## 5. 已知问题

| 文件 | 说明 |
|---|---|
| [testing/phase5-known-issues.md](testing/phase5-known-issues.md) ⚠ | Phase 5 KI-1 / KI-2 / KI-3 的完整登记与缓解记录 |
| [audit/D4-KNOWN_ISSUES.md](audit/D4-KNOWN_ISSUES.md) | Phase 4 收尾缺陷登记 |
| [delivery/KNOWN_LIMITATIONS.md](delivery/KNOWN_LIMITATIONS.md) ⚠ | 已知限制、未完成验收与部署边界（交付口径） |
| [audit/KNOWN_LIMITATIONS.md](audit/KNOWN_LIMITATIONS.md) ⚠ | 三阶段审查 + 封版前确认的残余风险（SQL 迁移注释引用） |
| [testing/open-risks.md](testing/open-risks.md) | Phase 2.1 后的风险与待决策清单 |
| [testing/failure-analysis.md](testing/failure-analysis.md) | Phase 1-2 失败与阻塞分析 |
| [testing/phase3-failure-analysis.md](testing/phase3-failure-analysis.md) | Phase 3 失败分析 |
| [testing/phase4-failure-analysis.md](testing/phase4-failure-analysis.md) | Phase 4 失败与风险分析 |
| [testing/phase3.2-skipped-audit.md](testing/phase3.2-skipped-audit.md) | 跳过的测试及原因审计 |

## 6. 附录：测试证据产物

| 路径 | 说明 |
|---|---|
| [audit/artifacts/](audit/artifacts/) | 2026-11-07 的 pytest / Maven 原始 stdout、XML 与元数据 **（证据快照，禁止移动或改名）** |
| [audit/artifacts/agent-pytest-default-20261107T195424.meta.md](audit/artifacts/agent-pytest-default-20261107T195424.meta.md) | Agent pytest 全量运行记录（默认模式）元数据 |
| [audit/artifacts/maven-default-verify-20261107T2051.meta.md](audit/artifacts/maven-default-verify-20261107T2051.meta.md) | 后端 `mvn verify` 全量运行记录（默认模式）元数据 |
| [audit/artifacts/maven-memory-fulltext-gated-20261107T2053.meta.md](audit/artifacts/maven-memory-fulltext-gated-20261107T2053.meta.md) | 后端 `mvn verify` 运行记录（Memory IT 门控模式）元数据 |
| [.gitignore](.gitignore) | docs 本地忽略规则 |

---

### 维护约定

1. 新增文档后在本索引登记；被代码 / 测试 / 脚本引用的文件保持原位。
2. 移动任何文档前，先按本文 ⚠ 标记确认引用位置，并同步更新链接与脚本路径。
3. Phase 5 记录属于关闭证据，不删除、不改写结论；后续跟踪写入新文件。
