# Git 工作区分类、风险与提交建议

**快照时间：** 2026-10-30（阶段 5.4）

**当前分支：** `main`

**状态：** 尚未执行 5.4 的 `git add` / `git commit` / `git push`；5.3 及之前的提交已存在。

> 本文只是分类和建议。提交前必须由仓库所有者确认；不要用 `git add .`
> 把整个工作区一次性提交。

## 1. 文件分类

### A. 交付源码（应提交）

| 路径 | 所属阶段 | 说明 |
| --- | --- | --- |
| `agent/app/context/trajectory.py` | 5.3 | 学习轨迹构建与证据关联 |
| `agent/app/history_guard.py` | 5.3.2 | 历史回答句子级校验与确定性回退 |
| `agent/app/context/__init__.py` | 5.3 | 导出 trajectory |
| `agent/app/context/builder.py` | 5.3 | 轨迹区块、来源 ID、预算优先级 |
| `agent/app/retriever/base.py` | 5.3 | Revision / Evidence 明细接口 |
| `agent/app/retriever/fixture.py` | 5.3 | Fixture 轨迹与来源 |
| `agent/app/retriever/http_backend.py` | 5.3 | 回查后端 revisions / evidence |
| `agent/app/retriever/routing.py` | 5.3 | 检索路由适配 |
| `agent/app/schemas.py` | 5.3 | 轨迹 / 来源 / Memory 时间字段 |
| `agent/app/runtime.py` | 5.3 / 5.3.2 | 复盘分支、重生成、回退、Trace |
| `agent/app/state/agent_state.py` | 5.3 | `trajectories` 与复盘标记 |
| `agent/app/trace.py` | 5.3 | 轨迹 / 证据 Trace 字段 |
| `agent/app/prompts.py` | 5.3 | 复盘与证据边界提示 |
| `agent/app/llm/openai_compatible.py` | 5.3 | 与历史校验相关的调用适配 |
| `agent/app/tutor/tutor.py` | 5.3 | 复盘回答约束 |
| `backend/.../service/ChatService.java` | 5.3.2 | SSE 完成后分析与异常边界 |
| `backend/.../service/UnderstandingService.java` | 5.3.2 | 快照归一化与 uncertain 处理 |
| `backend/.../entity/Conversation.java` | 5.4 | `last_message_seq`（禁止 `updateById` 回写） |
| `backend/.../mapper/ConversationMapper.java` | 5.4 | 序号原子自增 / 读回 |
| `backend/.../mapper/LearningSpaceMapper.java` | 5.4 | 首次 Session 归属锁空间行 |
| `backend/.../service/MessageService.java` | 5.4 | 序号分配改用计数器 |
| `backend/.../service/SessionService.java` | 5.4 | 首次归属串行化 + 统一锁顺序 |
| `backend/.../service/ChatService.java` | 5.4 | `finishRun` 先于 `message.end` |
| `backend/.../resources/schema-migration-phase54-message-seq.sql` | 5.4 | 已有库迁移（幂等） |

### B. 测试（应提交）

| 路径 | 所属阶段 | 说明 |
| --- | --- | --- |
| `agent/tests/test_history_guard.py` | 5.3.2 | 校验器 14 个用例 |
| `agent/tests/test_history_guard_runtime.py` | 5.3.2 | Runtime 缓冲 / 重生成 / 回退 |
| `agent/tests/test_learning_trajectory.py` | 5.3 | 轨迹构建 |
| `agent/tests/test_context_builder.py` | 5.3 | 优先级 / 轨迹注入 |
| `agent/tests/test_evidence_guard.py` | 4.4 | 非成功检索兜底 |
| `backend/.../service/ChatServiceTest.java` | 5.3.2 | SSE 完成 / 分析顺序 |
| `backend/.../service/UnderstandingServiceUncertainTest.java` | 5.3.2 | uncertain 归一化 |

### C. 复现 / 验收脚本（应提交）

| 路径 | 所属阶段 | 说明 |
| --- | --- | --- |
| `scripts/phase51-e2e.sh` | 5.1 | inline 学习闭环，101 项 |
| `scripts/phase51-mq-e2e.sh` | 5.1 | 真 RabbitMQ + Worker，27 项 |
| `scripts/phase52-e2e.sh` | 5.2 | 跨会话轨迹 / 权限 / 冲突，94 项 |
| `scripts/phase53-setup.sh` | 5.3 | 5.3 场景数据 |
| `scripts/phase53-setup-clean.sh` | 5.3 | B/C 干净数据 |
| `scripts/phase6-demo.sh` | 6 | TLB 七步演示 |
| `agent/scripts/phase53_runtime_eval.py` | 5.3 | 真实模型 Runtime 评测 |
| `agent/scripts/phase53_real_model_eval.py` | 5.3 | 真实模型调用脚本 |
| `agent/scripts/phase532_replay_guard.py` | 5.3.2 | 离线回放 / 生产 SSE 复验 |
| `scripts/phase54-rag-citation-e2e.py` | 5.4 | 真实 Qdrant / Embedding / LLM 文档引用 |
| `scripts/phase54-mq-real-e2e.py` | 5.4 | 真实 MQ + Worker + 真实模型 |
| `scripts/phase54-concurrency-e2e.py` | 5.4 | 单机 SSE 并发基线 |

这些脚本里出现的 `Passw0rd!23` 与 `phase51-internal-token` 是**本地测试占位值**，
不是生产密钥；脚本没有内嵌任何真实 API Key。

### D. 交付文档（应提交）

| 路径 | 说明 |
| --- | --- |
| `README.md` | 根说明，已按阶段 6 的 11 个主题重写 |
| `agent/README.md` | 测试口径为 383 passed / 5 skipped / 1 xfailed |
| `backend/backend/README.md` | 测试口径为 255 条（182 执行 / 73 skipped） |
| `docs/delivery/ARCHITECTURE.md` | 架构图与职责 |
| `docs/delivery/LEARNING_LIFECYCLE.md` | 学习状态生命周期 |
| `docs/delivery/DEMO.md` | 七步演示（Mock 标注） |
| `docs/delivery/ENVIRONMENT.md` | 环境与启动 |
| `docs/delivery/TEST_MATRIX.md` | 测试 / 质量矩阵 |
| `docs/delivery/KNOWN_LIMITATIONS.md` | 已知限制 |
| `docs/delivery/GIT_WORKSPACE.md` | 本文 |
| `docs/delivery/INTERVIEW.md` | 面试提纲 |
| `docs/testing/phase54-real-model-20261030.md` | 5.4 MQ / 文档引用 / 重复采样 / 并发报告 |

### E. 生成产物（单独决定，不要混进源码提交）

| 路径 | 性质 | 建议 |
| --- | --- | --- |
| `evaluation/reports/latest.json` | 每次运行覆盖的离线评测产物 | 可单独提交，并在 message 中写明版本 / 结果 |
| `evaluation/reports/latest.md` | 同上 | 同上 |
| `backend/backend/target/**` | 构建 / E2E / 演示产物 | **已被 .gitignore 忽略，不提交** |
| `agent/.env` | 本机密钥 / 配置 | **已被忽略，不提交** |
| `backend/.../application-local.yml` | 本机数据库密码 / gateway | **已被忽略，不提交** |

`evaluation/reports/latest.*` 原本就是 tracked 文件，所以会出现在 `git status` 里。
本轮离线评测把它覆盖成 **40/40、124/124 断言、代码版本 `74d99a1`**；
2026-10-21 的 49/49 `--mode all` 历史结果仍可从 git 历史取回，**不要把它和
本轮 40/40 相加或比较**。

## 2. 工作区风险

1. **分支名与内容不一致。** 当前分支叫 `feat/phase-4.4-evidence-guard`，
   但工作区包含 5.1 / 5.2 / 5.3 / 5.3.2 / 6 的改动。直接在该分支提交会让历史难读。
   建议先新建交付分支（非破坏性），再由仓库所有者确认。
2. **README 是整文件重写。** diff 很大，但内容是删掉过时的 67 / 111 / 40+9 口径，
   改成当前验证值。审查时请按第 8 / 9 / 10 节的数字逐项核对。
3. **`latest.*` 是生成产物。** 它每次运行都会变；提交它会让后续评测产生新的 diff。
   如果只想冻结源码，可以暂不提交这两个文件，但不要删除它们。
4. **未跟踪文件多。** `docs/delivery/`、E2E 脚本、5.3 评测脚本都是本轮新增；
   必须逐路径添加，不能 `git add .`。
5. **CRLF 警告。** 当前是 Windows 工作区，Git 提示 LF 将被替换为 CRLF；
   这是换行配置问题，不是内容损坏。不要为了消除警告批量改写历史文件。
6. **敏感文件已在忽略列表中。** 已确认 `agent/.env`、
   `backend/backend/src/main/resources/application-local.yml`、`backend/backend/target/`
   均被忽略；不要把 `!` 规则改成强制跟踪。
7. **失败样本必须保留。** 第一次 MQ E2E 的 18 PASS / 9 FAIL 产物在
   `backend/backend/target/phase51-mq-e2e/20261027-021523/`；
   5.3.1 的 C 失败输出在 `backend/backend/target/phase53-eval/20261027-004622/`。
   它们不会被提交（target 已忽略），但**不要删除**，它们是验收证据。

## 3. 建议的提交顺序（等待确认后执行）

先跑一遍门禁：

```bash
cd agent && python -m pytest -q
cd ../backend/backend && ./mvnw test
cd ../.. && python -m evaluation
git diff --check
```

然后按主题逐组添加**明确路径**：

```bash
# 0) 可选：从当前状态新建交付分支，不移动、不删除任何内容
git switch -c feat/phase-6-delivery

# 1) Agent 学习轨迹
git add agent/app/context/trajectory.py \
        agent/app/context/__init__.py \
        agent/app/context/builder.py \
        agent/app/retriever/base.py \
        agent/app/retriever/fixture.py \
        agent/app/retriever/http_backend.py \
        agent/app/retriever/routing.py \
        agent/app/schemas.py \
        agent/app/state/agent_state.py \
        agent/app/trace.py \
        agent/tests/test_learning_trajectory.py \
        agent/tests/test_context_builder.py

# 2) 历史回答证据约束
git add agent/app/history_guard.py \
        agent/app/runtime.py \
        agent/app/prompts.py \
        agent/app/llm/openai_compatible.py \
        agent/app/tutor/tutor.py \
        agent/tests/test_history_guard.py \
        agent/tests/test_history_guard_runtime.py \
        agent/tests/test_evidence_guard.py \
        agent/scripts/phase53_runtime_eval.py \
        agent/scripts/phase53_real_model_eval.py \
        agent/scripts/phase532_replay_guard.py

# 3) 后端 SSE / 快照修复
git add backend/backend/src/main/java/com/ails/backend/service/ChatService.java \
        backend/backend/src/main/java/com/ails/backend/service/UnderstandingService.java \
        backend/backend/src/test/java/com/ails/backend/service/ChatServiceTest.java \
        backend/backend/src/test/java/com/ails/backend/service/UnderstandingServiceUncertainTest.java

# 4) E2E / 演示脚本
git add scripts/phase51-e2e.sh scripts/phase51-mq-e2e.sh \
        scripts/phase52-e2e.sh scripts/phase53-setup.sh \
        scripts/phase53-setup-clean.sh scripts/phase6-demo.sh

# 5) 交付文档
git add README.md agent/README.md backend/backend/README.md \
        docs/delivery/ARCHITECTURE.md \
        docs/delivery/LEARNING_LIFECYCLE.md \
        docs/delivery/DEMO.md \
        docs/delivery/ENVIRONMENT.md \
        docs/delivery/TEST_MATRIX.md \
        docs/delivery/KNOWN_LIMITATIONS.md \
        docs/delivery/GIT_WORKSPACE.md \
        docs/delivery/INTERVIEW.md

# 6) 可选：评测产物（单独提交）
git add evaluation/reports/latest.json evaluation/reports/latest.md
```

每组的建议 message：

```text
feat(agent): 注入学习轨迹并按证据约束历史回答
fix(backend): 稳定 SSE 完成顺序与理解快照归一化
test(e2e): 补齐 5.1/5.2/5.3/6 验收与演示脚本
docs(delivery): 阶段 6 交付文档与测试口径
chore(eval): 更新离线评测报告（40/40，124 断言）
```

**不要做：**

- `git add .` / `git add -A`；
- `git commit --amend` 已有提交；
- `git reset --hard`、`git clean -fd`、强推；
- 删除历史报告或失败样本；
- 提交 `.env`、`application-local.yml`、`target/`。

以上命令**都没有执行**。等待仓库所有者确认后再逐组提交。
