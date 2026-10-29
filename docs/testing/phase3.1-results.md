# Phase 3.1 结果：内部身份边界与评测器校准

- 日期：2026-10-28
- 状态：代码修复、确定性回归、真实 HTTP 安全测试、真实模型校准运行均已完成；
  人工评分 0/9，完整三进程 E2E 与部署网络限制未验证
- 原则：未执行、被跳过、环境阻塞的测试不计 PASS；真实模型输出、硬断言、
  Judge 分数、人工分数分开报告

## 1. 本阶段实际做了什么

| 工作 | 结果 | 证据 |
| --- | --- | --- |
| 审计身份签发路径与密钥读取 | 完成；未打印任何密钥值 | 本报告第 2 节；[安全结果](phase3.1-security-results.md) |
| 移除 Agent 自签后端身份 | 完成；Agent 只转发 Spring 的 `backendIdentity` | `agent/app/retriever/routing.py`、`agent/app/schemas.py`、`agent/app/messaging/task.py` |
| 拆分 identity / delegation 密钥 | 完成；delegation 只给 Spring；启动校验 fail closed | `InternalIdentitySigner.java`、`InternalSecurityConfigValidator.java` |
| MQ 任务 schema 1→2 | 完成；v1 消息被拒绝并转死信 | `AnalysisTaskMessage.java`、`agent/app/messaging/task.py`、`test_worker_reliability.py` |
| 输出完整性独立状态 | 完成；TRUNCATED 强制 FAIL，`task_completion` 最高 1 | `evaluation/phase1/completion.py`、`evaluation/phase3/runner.py` |
| B05/E01/D01 断言修正 | 完成；历史 artifact 重判 19 PASS / 3 FAIL | [评测器校准](phase3.1-evaluator-calibration.md) |
| A01 History Guard 误报 | 完成；合规免责句正反样本回归 | `agent/app/history_guard.py`、`test_history_guard.py` |
| 人工复核表 | 完成；9 条 PENDING，未填任何人工分 | [评分表](phase3.1-human-review-sheet.md) |
| 真实模型校准运行 | 完成 10 次；9 PASS / 1 FAIL；0 BLOCKED | `evaluation/phase3/reports/B05.json` |
| 完整 Spring → Agent → 后端 MQ E2E | **未执行**；脚本已更新为 v2 | `scripts/phase51-mq-e2e.sh`（未运行） |
| 部署网络限制取证 | **未验证** | 无部署环境证据 |

## 2. 身份签发边界：审计与修正

### 2.1 审计发现

修正前，Agent 的检索路由在异步分析链路中使用 `internal_identity_secret`
自行签发 `aud=learning-backend` 的声明。后端接受该声明并建立用户上下文。
结果是：**持有 Agent 密钥的一方可以为任意 `sub` 生成后端信任的用户身份**。
共享密钥同时承担服务认证与用户身份签发，边界不成立。

相关代码路径：

- `agent/app/retriever/routing.py`：旧路径自签后端 audience；
- `agent/app/internal_identity.py`：签名/校验实现；
- `backend/.../InternalServiceAuthenticationFilter.java`：消费 `X-Internal-Identity`；
- `backend/.../InternalIdentitySigner.java`：签发/校验；
- `backend/.../application.yml` 的 `ails.internal.*`。

### 2.2 最小修正

- `identity-secret`（Spring 与 Agent 共享）：只用于 `aud=learning-agent`；
- `delegation-secret`（仅 Spring）：用于 `aud=learning-backend`、
  `scope=analysis:read` 的短期委托声明；
- 同步链路：Agent 原样透传用户 JWT，不签发任何后端身份；
- 异步链路：Spring 在 `AnalysisTaskMessage.backendIdentity` 中放入委托声明，
  Agent/Worker 只转发，不能改 `sub`、不能重新签名；
- 消息入口校验：`schemaVersion>=2` 但 `backendIdentity` 缺失或为空时，
  pydantic 直接拒绝该消息（`MESSAGE_INVALID`，不可重试、进死信），
  不再等到 Analyze 阶段才 fail closed；
- 无 `backendIdentity` 的 analyze 请求 fail closed（503），不退回 Fixture；
- 后端内部凭据只对 `GET /api/**` 建立身份；写方法不建立内部身份；
- 启动时若密钥缺失、两把密钥相同、密钥与 token 相同、两个 audience 相同，直接失败。

## 3. 测试统计（严格分开）

### 3.1 Agent：确定性测试

执行：`cd agent && python -m pytest -q`

| 指标 | 数量 |
| --- | --- |
| passed | **328** |
| skipped | **5** |
| failed | 0 |
| 真实模型调用 | 0 |

5 条 skipped 的原因（跳过 ≠ 通过）：

1. `test_knowledge_e2e.py`：未设置 `AILS_KNOWLEDGE_E2E=true`；
2. `test_qdrant_integration.py`：2 条，未设置 `AILS_QDRANT_IT=true`；
3. `test_worker_broker.py`：2 条，未设置 `AILS_TEST_RABBITMQ_URL`。

### 3.2 后端：单元 + 集成

| 范围 | 结果 | 说明 |
| --- | --- | --- |
| 安全/网关定向单测 | **39/39 passed** | signer 6、validator 5、两个 Gateway 10+10、AnalysisTaskService 8 |
| 真实 HTTP 安全集成 | **24/24 passed** | `InternalIdentityHttpIntegrationTest` 5 + `SecurityContextIntegrationTest` 19 |
| 全量后端 | **243 tests, 0 failures, 0 errors, 41 skipped** | 41 条外部依赖测试未启用，未计 PASS |

真实 HTTP 环境：MySQL 8.0.37 容器（127.0.0.1:3307），独立库
`ails_security_it`，真实 Tomcat 随机端口，`AILS_SECURITY_IT=true`。

### 3.3 真实模型运行

| 指标 | 结果 |
| --- | --- |
| 运行数 | 10 |
| 有效输出 | 10 |
| BLOCKED | 0 |
| `COMPLETE` | 10 |
| `TRUNCATED` | 0 |
| `PASS` | 9 |
| `FAIL` | 1（D01，malformed 引用标签） |
| Agent 调用 | 10 |
| Judge 调用 | 10 |

**重要说明：** 命令 `python -m evaluation.phase3 --run B05` 中的 `B05` 是运行标签，
不是单用例筛选，因此本轮实际执行了 10 条用例，而不是计划中的小样本 smoke。
这里如实记录，不把它描述成“只跑了 B05”。

### 3.4 历史 artifact 重判

Phase 3 的 22 次运行（基线 10 + 重复 12）用当前规则重新判读：

| 指标 | 结果 |
| --- | --- |
| `PASS` | 19 |
| `FAIL` | 3（B05-r1、E01-r2、D01-r1） |
| `COMPLETE` | 20 |
| `TRUNCATED` | 2（B05-r1、E01-r2） |

### 3.5 人工评分

| 指标 | 数量 |
| --- | --- |
| 待复核记录 | 9 |
| 已完成人工评分 | **0** |
| 待填写 | **9** |

没有填写任何人工分数；没有计算 Judge–human 一致性或 Kappa。

## 4. 十个问题的回答

### 问题 1：Agent 还能用自己的签名密钥签发任意用户身份吗？

**不能（运行路径）。** Agent 不再自签 `aud=learning-backend`；后端只接受
delegation 密钥签发的声明，而该密钥不进入 Agent 进程。用 Agent 的
`identity-secret` 伪造后端身份的真实 HTTP 测试返回 401
（`agentIdentitySecretCannotForgeBackendAcceptedUser`）。

**边界说明：** `agent/app/internal_identity.py` 的开发 CLI 在显式提供
`AILS_INTERNAL_DELEGATION_SECRET` 时仍能签发后端声明；这不是请求路径，正常部署
不设置该变量。若运维把 delegation 密钥注入 Agent 环境，边界会被重新打开。
证据：[安全结果](phase3.1-security-results.md) 第 2 节。

### 问题 2：用户身份的可信签发方是谁？为什么其他服务信任这些 claims？

可信签发方是 **Spring Boot**。它用仅自己持有的 `delegation-secret` 签发：

```text
iss=learning-backend
sub=<userId>
aud=learning-backend
iat / exp=<短期>
jti=<审计关联 id>
scope=analysis:read
```

后端用同一把密钥做 HMAC-SHA256 校验，并逐项校验 issuer、audience、subject、
时间窗、TTL 上界、scope 与 `jti` 非空。共享 `X-Internal-Token` 只证明“调用方是
内部服务”，单独存在时不建立用户身份。

证据：`InternalIdentitySigner.verifyDelegation`、
`InternalServiceAuthenticationFilter.doFilterInternal`、
`sharedTokenWithoutSignedIdentityCannotImpersonateAUser`、
`delegationWithWrongIssuerOrScopeIsRejected`。

### 问题 3：`jti` 的实际语义是什么？有重放检测吗？

`jti` 是**短期多用的关联 id**，只校验非空。**没有重放检测**：没有已用 `jti`
存储、没有 replay cache。声明在 `exp` 前可以重复使用。

当前 TTL：委托声明默认 900 秒、最大 3600 秒。泄露的声明在过期前仍可重放；
MQ 消息里的副本也会持久化到队列/死信。这是明确接受的残余风险，不能对外宣称
“一次性令牌”。

证据：`InternalIdentitySigner` 类注释与 `verify` 实现；
[安全结果](phase3.1-security-results.md) 第 6 节。

### 问题 4：两次历史截断能被正确识别为“不完整回答”吗？

**能。** B05-r1 与 E01-r2 的 `finish_reason=length` 被 `completion.py` 判为
`TRUNCATED`；runner 在硬断言之前直接判 `FAIL`，理由写明截断。E01-r2 从旧报告的
`PASS` 改为 `FAIL`；B05-r1 内容断言 15/15 通过，但仍因截断 `FAIL`。
语义分把 `task_completion` 从 2 降到 1 并标记 `completionAdjusted`。

证据：`test_b05_r1_content_passes_but_truncation_forces_fail`、
`test_e01_r2_is_truncated_and_cannot_keep_old_pass`、
`test_truncation_clamps_task_completion_score`。

### 问题 5：B05、E01、D01、A01 的断言修复后表现如何？

| 用例 | 修复后行为 | 实际结果 |
| --- | --- | --- |
| B05 | 不再要求 Guard `added=True`；检查 `finalDiscloses`、`duplicateNotice`、`addedOrDisclosed` | 历史 r1 内容 15/15 通过但截断 FAIL；新运行 COMPLETE、PASS |
| E01 | 改为 `masteryClaim.affirmativeCurrent` 从句方向判断，不再用关键词否定 | E01-r3 从 FAIL 纠正为 PASS；E01-r1 PASS；E01-r2 截断 FAIL |
| D01 | 新增 `malformedCount=0`、`syntaxValid=true`；语法与事实支持分开 | 历史与新运行均因 `[资料#0]`/`[资料#1]` FAIL；`invalid=0` 不再等于通过 |
| A01 | 合规免责句不再触发 Guard；保留对编造心理过程/假历史的拒绝 | 24 条 Guard 回归通过；旧 artifact 的回退结果未追溯修复 |

证据：[评测器校准](phase3.1-evaluator-calibration.md) 第 4 节；
`agent/tests/test_phase31_evaluator_regressions.py`；
`agent/tests/test_history_guard.py`。

### 问题 6：LLM Judge 开始有区分度了吗？如果没有，为什么？

**没有足够证据说它有区分度。**

- 2026-10-28 的 10 次运行中，适用维度几乎全是 2 分，只有
  `uncertainty_citation_honesty` 出现 1 次 1 分；
- 其他维度没有 0 分，分布缺少变化；
- 没有人工分数，无法计算一致率或 Kappa；
- Judge 与 Agent 使用同一 provider，独立性不足。

因此只能报告“自动 Judge 成功评分 10/10”，不能报告“Judge 已校准”或
“Judge 能区分质量”。要回答区分度，需要人工完成 9 条评分，并补充包含
0/1 分的样本。

### 问题 7：人工评分完成了多少？还有多少待完成？

**完成 0 条，待完成 9 条。** 原始队列 9 条全部保留为 `PENDING`；
`docs/testing/phase3.1-human-review-sheet.md` 提供逐条评分表，但没有任何预填分数。
在人工填写前，不计算 Judge–human 一致性。

### 问题 8：哪些证据真正支持 Agent 语义质量结论？

**支持“能运行、能产出”的证据：**

- 2026-10-28 的 10 次真实模型运行，10 个有效输出、0 BLOCKED；
- 历史 22 次真实运行 artifact；
- 逐条硬断言（检索事实、状态标签、内部 ID 泄漏、隔离、引用语法等）。

**支持“语义质量好”的证据：**

- 目前只有自动 Judge 分数（待人工核对）；
- 还没有人工评分，因此**没有足够证据**支持“语义质量已通过”；
- 真实模型输出能证明回答存在且部分符合硬约束，不能证明教学效果或事实全对。

### 问题 9：最严重的剩余安全风险是什么？

**委托声明在有效期内可重放，且没有撤销机制。** 具体包括：

1. `jti` 只做非空校验，没有重放检测；
2. 委托声明随 MQ 消息持久化，队列/死信中的副本在 TTL 内可用；
3. 默认 TTL 900 秒、最大 3600 秒，泄露后有窗口；
4. 共享 token 仍是服务级 bearer；与有效委托声明同时泄露时可在 TTL 内代表用户读取。

影响被 `scope=analysis:read`、只读 GET、空间归属校验限制，但不能宣称“不可重放”。
缓解方向（未实现）：更短 TTL、按 `jti` 的一次性消费、mTLS/工作负载身份、密钥轮换。

### 问题 10：哪些结果可以写进简历？哪些仍需验证？

**可以写（有测试/真实执行支撑）：**

- 发现并修复“Agent 持有共享 HMAC 密钥即可签发后端接受的任意用户身份”的边界缺陷；
- 拆分 identity/delegation 密钥，Spring 独占委托签发，Agent 只转发；
- 真实 HTTP 安全测试 24/24：无凭据、篡改、过期、错 audience/issuer/scope、
  声明与 Header 不一致、body `userId` 覆盖、内部凭据写操作均被拒绝；
- 启动配置 fail closed 5/5；
- 把“输出完整性”从 `case_status` 中拆出，`finish_reason=length` 强制 FAIL，
  `task_completion` 最高 1；
- 用历史 artifact 复现并纠正 B05/E01/D01 的误判；
- 建立 9 条人工复核评分表，明确区分自动分与人工分。

**仍需验证，不能写进简历：**

- “Agent 语义质量已通过” / “Judge 已校准” / “Judge 有区分度”；
- 完整 Spring → Agent → 后端三进程 MQ E2E；
- 生产网络隔离、密钥轮换、重放防护；
- `jti` 一次性消费或 mTLS；
- 9 条人工评分的最终结论（目前 0 条完成）。

## 5. 产物索引

| 产物 | 路径 |
| --- | --- |
| 安全结果 | [phase3.1-security-results.md](phase3.1-security-results.md) |
| 评测器校准 | [phase3.1-evaluator-calibration.md](phase3.1-evaluator-calibration.md) |
| 人工评分表 | [phase3.1-human-review-sheet.md](phase3.1-human-review-sheet.md) |
| 风险清单 | [open-risks.md](open-risks.md) |
| 简历证据 | [resume-evidence.md](resume-evidence.md) |
| 人工队列（新） | `evaluation/phase3/reports/human-review-queue-20261028.json` |
| 真实模型报告 | `evaluation/phase3/reports/B05.json` |
| 历史重判模块 | `evaluation/phase3/reclassify.py` |
| 人工表生成器 | `evaluation/phase3/build_human_review.py` |

## 6. 边界声明

- 未执行 Git commit；未删除用户未提交修改；未更改数据库 schema；
- 未切换模型；未修改 `max_tokens` 或流式协议；
- 所有真实模型数字来自实际运行，未生成估计值；
- 所有密钥值未写入代码、报告或日志；
- 跳过、未执行、未验证的项目均未计入 PASS。
