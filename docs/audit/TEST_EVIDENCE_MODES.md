# 测试证据与运行模式对照

- 适用报告：`PHASE1_MEMORY_FIX.md`、`PHASE2_RAG_RELIABILITY.md`、
  `PHASE3_CITATION_CLOSURE.md`、`INTEGRATION_VERIFICATION.md`、
  `THREE_PHASE_FINAL_REVIEW.md`
- 整理日期：2026-11-07
- 仓库版本：`13cec31`
- 目的：把“默认模式”和“门控全量模式”的数字分开，避免把不同运行模式的结果混写。

---

## 1. 两种模式的定义

| 模式 | 环境变量 | 后端命令 | 实际执行范围 | Agent 命令 |
| --- | --- | --- | --- | --- |
| **默认模式** | 不设置任何 `AILS_*_IT` / `AILS_KNOWLEDGE_E2E` / `AILS_TEST_RABBITMQ_URL` | `mvn -B test` | 只跑 Surefire；Failsafe 不进入生命周期 | `python -m pytest -q`（门控默认关闭） |
| **门控全量模式** | 显式打开 MySQL / Qdrant / RabbitMQ / Agent 相关门控 | `mvn -B clean verify` | Surefire + Failsafe | 显式打开 `AILS_KNOWLEDGE_E2E` / `AILS_QDRANT_IT` / `AILS_TEST_RABBITMQ_URL` |

两种模式下的测试类总数可以相同，但 **pass / skip 的含义不同**：
默认模式下门控测试被计入 skip；门控全量模式下它们真实执行并计入 pass。
不能把默认模式的 “335 tests” 与门控全量的 “335 tests” 当成同一组已执行结果。

---

## 2. 后端：当前实测数字

### 2.1 默认模式（2026-11-07 19:56，本轮实测）

命令（`backend/backend`）：

```bash
mvn -B test
```

结果：

```text
Surefire: classes=46  tests=335  failures=0  errors=0  skipped=88
Failsafe: 未执行（mvn test 不触发 integration-test / verify）
```

- 通过：**247**（335 - 88）
- 跳过：**88** = 11 条既有 skip + 77 条环境门控测试
- 后端登记总数（此模式下）：**335**
- 原始输出：`artifacts/maven-default-test-20261107T195424.txt`
- XML 摘要：`artifacts/maven-default-test-20261107-summary.txt`
- XML 压缩包：`artifacts/maven-default-test-20261107-surefire-xml.zip`

### 2.2 门控全量模式（2026-11-07 19:20，历史运行，产物已归档）

命令（`backend/backend`，全部 `AILS_*_IT` 门控打开）：

```bash
./mvnw.cmd -B clean verify
```

结果：

```text
Surefire: classes=46  tests=335  failures=0  errors=0  skipped=11
Failsafe: classes=1   tests=4    failures=0  errors=0  skipped=0
```

- 通过：**328**（Surefire 324 + Failsafe 4）
- 跳过：**11**（`BackendApplicationTests` 1 + `AnalysisMqIntegrationTest` 10）
- 后端登记总数：**339**（Surefire 335 + Failsafe 4）
- 原始 XML：`artifacts/maven-gated-full-20261107-xml.zip`
- XML 摘要：`artifacts/maven-gated-full-20261107-summary.txt`

### 2.3 两种后端的差异

| 项 | 默认模式 | 门控全量模式 |
| --- | ---: | ---: |
| Surefire tests | 335 | 335 |
| Surefire pass | 247 | 324 |
| Surefire skip | 88 | 11 |
| Failsafe tests | 0（不执行） | 4 |
| Failsafe pass | 0 | 4 |
| 后端总计 | 335 | 339 |
| 后端 pass | 247 | 328 |
| 后端 skip | 88 | 11 |

差异的 81 条门控测试：

- Surefire 中 77 条：`Phase2MqProcessorIntegrationTest` 5、`Phase2RabbitMqBrokerTest` 5、
  `InternalIdentityHttpIntegrationTest` 5、`SecurityContextIntegrationTest` 19、
  `ChatSseIntegrationTest` 9、`ChatSseOrderingProbeTest` 1、
  `DocumentIndexE2EIntegrationTest` 1、`DocumentIndexFencingIntegrationTest` 4、
  `DocumentIndexServiceIntegrationTest` 15、`DocumentServiceIntegrationTest` 7、
  `Phase2RealStateIntegrationTest` 6。
- Failsafe 中 4 条：`MemoryStateMachineConcurrencyIT`（`AILS_MEMORY_IT=true`）。

> 默认模式的 88 skip = 77 条上述门控 + 11 条既有 skip。门控全量的 11 skip
> 只剩 `BackendApplicationTests` 1 与 `AnalysisMqIntegrationTest` 10。
> 门控全量仍不执行 `AnalysisMqIntegrationTest`：Docker/Testcontainers Npipe HTTP 400，
> 10 条以显式 skip 计入，不是通过。

---

## 3. Failsafe 的覆盖边界（为什么 Fencing 在 Surefire）

`backend/backend/pom.xml` 的 Failsafe 配置只有：

```xml
<includes>
  <include>**/*IT.java</include>
</includes>
```

因此：

| 类 | 命名 | Surefire | Failsafe |
| --- | --- | --- | --- |
| `MemoryStateMachineConcurrencyIT` | `*IT` | 不匹配默认 include | **执行**（唯一的 Failsafe 类） |
| `DocumentIndexFencingIntegrationTest` | `*IntegrationTest` | **执行**（以 `Test` 结尾） | 不匹配 `*IT` |
| `DocumentIndexServiceIntegrationTest` 等 | `*IntegrationTest` | **执行** | 不匹配 `*IT` |

结论：`DocumentIndexFencingIntegrationTest` 运行在 **Surefire**，不是 Failsafe。
这是**类名与 `**/*IT.java` 不匹配**，不是 Failsafe 配置遗漏；它的 4 条测试已经计入
Surefire 的 335 条，Failsafe 的 4 条只属于 `MemoryStateMachineConcurrencyIT`。

本轮选择**只修正文档**，不改 pom：

- 把 `**/*IntegrationTest.java` 加进 Failsafe 会让 10 个 `*IntegrationTest` 类在
  Surefire 和 Failsafe 各跑一次，`mvn verify` 的计数和耗时都会翻倍；
- 若要让 Fencing 进入 Failsafe，正确做法是把类改名为 `DocumentIndexFencingIT`；
  这会改变测试清单和两种模式的计数，属于测试代码变更，不在本轮“只做文档对齐”的边界内。

---

## 4. Agent：两种模式

### 4.1 默认模式（2026-11-07 19:54，本轮实测，原始产物已保留）

命令（`agent/`）：

```bash
python -m pytest -q -rs \
  --junitxml=../docs/audit/artifacts/agent-pytest-default-20261107T195424.xml
```

结果：

```text
416 collected
408 passed, 7 skipped, 1 xfailed, 1 warning in 17.51s
exit code 0
```

- 原始输出：`artifacts/agent-pytest-default-20261107T195424.txt`
- JUnit XML：`artifacts/agent-pytest-default-20261107T195424.xml`
- 运行元数据：`artifacts/agent-pytest-default-20261107T195424.meta.md`
- 7 skip：knowledge E2E 1、Qdrant 4、真实 Broker 2
- 1 xfailed：`test_runtime_budget_contract.py` 的既有 strict xfail

### 4.2 门控全量模式（历史声明，本轮未复现）

历史报告记录：

```text
415 passed, 0 skipped, 1 xfailed
```

这是显式打开 `AILS_KNOWLEDGE_E2E=true`、`AILS_QDRANT_IT=true`、
`AILS_TEST_RABBITMQ_URL` 后的结果。**本轮没有保留这次运行的 pytest 原始输出**，
也没有启动 knowledge E2E 所需的 Spring Boot 后端，因此不能把 415/0/1 当作
本轮可复核产物；它只能作为“历史文档声明（C 级证据）”。

415 与 408 + 7 在算术上一致，但“7 条门控全部真实执行”这一点没有本轮原始产物证明。

---

## 5. 文档引用规则

1. 写后端数字时必须带模式：**默认 `mvn test`** 或 **门控 `mvn clean verify`**。
2. 写 `335` 时必须说明它是 Surefire；不要把 `339` 与 `335` 混用。
3. 写 `Failsafe 4/0/0/0` 时只能指 `MemoryStateMachineConcurrencyIT`；
   `DocumentIndexFencingIntegrationTest` 的 4/4 属于 Surefire。
4. 写 Agent `415/0/1` 时必须标注“门控全量、历史声明、无本轮原始产物”；
   写 `408/7/1` 时标注“默认模式、有原始产物”。
5. 历史基线（255、277、309、321、325 等）只能标为历史，不得当作当前最终结果。

---

## 6. 本次修正记录（逐项原因）

| 文档 / 位置 | 修正前 | 修正后 | 原因 |
| --- | --- | --- | --- |
| `PHASE2_RAG_RELIABILITY.md` §9.1 | 历史默认 **325 / 0 / 0 / 78** | 当前默认 **335 / 0 / 0 / 88**（247 passed） | 2026-11-07 19:56 实际执行 `mvn -B test`；325/78 是旧基线，已被当前测试清单取代 |
| `PHASE2_RAG_RELIABILITY.md` §9.4.7 | “新增 fencing IT 4/4” | “fencing 集成测试 4/4，运行在 Surefire，不是 Failsafe” | 类名 `*IntegrationTest` 不匹配 Failsafe 的 `**/*IT.java`；Failsafe 4 条属于 `MemoryStateMachineConcurrencyIT` |
| `INTEGRATION_VERIFICATION.md` §0.1 | Agent 产物写“本轮 pytest 输出”，结果 **415 / 0 / 1** | 标为“门控全量（历史）、无本轮原始产物”；新增默认 **408 / 7 / 1** 行 | 415 没有原始输出；本轮实际保留的是默认模式产物，不能把两者混为一次运行 |
| `INTEGRATION_VERIFICATION.md` §0.2 | “Agent 全量无 skip” | “门控全量声明无 skip，但无原始产物；默认模式 7 skip” | 默认模式实测 7 条门控 skip；415 的证据等级只能是 C |
| `INTEGRATION_VERIFICATION.md` §3.1 | 只给门控 **335 / 11** | 增加默认 **335 / 88** 对照 | `mvn test` 不执行 Failsafe，且 77 条门控测试计入 skip；两模式必须并列 |
| `PHASE1_MEMORY_FIX.md` 结论 / §4.5 | 339 / 11 与 Agent 415 未标模式 | 标为“门控全量模式”；新增默认 335 / 88、Agent 408 / 7 / 1 | 同上；339 含 Failsafe，默认模式不含 |
| `PHASE3_CITATION_CLOSURE.md` §7.1 | 命令表把全门控 415 与 `mvn clean verify` 混列 | 表增加“模式”列；415 标历史声明；新增默认两行 | 原表没有区分运行模式，读者无法判断 415 是否需要后端/Qdrant |
| `PHASE3_CITATION_CLOSURE.md` §7.3 | 321 / 74 与最终 335 / 11 并列但未强调历史 | 321/74 标为历史默认基线；335/11 标为门控全量 | 321/74 是 Phase 3 收尾时的默认基线，不是当前默认（当前 335/88） |
| `THREE_PHASE_FINAL_REVIEW.md` EV-01 | “没有保留 pytest 产物” | 补充默认模式产物已保留；415 仍为 C 级 | 2026-11-07 19:54 已保存 stdout/XML，但那是 408/7/1，不是 415/0/1 |
| `THREE_PHASE_FINAL_REVIEW.md` EV-02 | “本轮没有在默认模式下重跑” | “2026-11-07 19:56 已补跑：335 / 88” | 本轮实际执行了默认 `mvn -B test` |

