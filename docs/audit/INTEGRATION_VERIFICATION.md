# Phase 1 + Phase 2 集成验证与单次全量证据链

- 执行日期：2026-11-07
- 分支：`main`（未提交 Git）
- 本轮性质：**证据链修复 + 单次全量复核**。修复测试产物缺失/静默问题，执行一次干净的
  `mvn clean verify` 与一次 Agent 全量 `pytest`，使审计可以只依据这一组 XML / pytest
  输出复核。**不是功能开发轮。**
- 相关报告：[`PHASE1_MEMORY_FIX.md`](PHASE1_MEMORY_FIX.md)、
  [`PHASE2_RAG_RELIABILITY.md`](PHASE2_RAG_RELIABILITY.md)、
  [`PHASE3_CITATION_CLOSURE.md`](PHASE3_CITATION_CLOSURE.md)

## 0. 结论摘要

### 0.1 单次运行结果

| 套件 | 命令 | 结果 | 产物 |
| --- | --- | --- | --- |
| 后端 Surefire | `./mvnw.cmd -B clean verify` | **335 tests / 0 failures / 0 errors / 11 skipped** | `backend/backend/target/surefire-reports/TEST-*.xml` |
| 后端 Failsafe（`*IT`） | 同上 | **4 tests / 0 failures / 0 errors / 0 skipped** | `backend/backend/target/failsafe-reports/TEST-*.xml` |
| Agent 全量 | `python -m pytest -q` | **415 passed / 0 skipped / 1 xfailed** | 本轮 pytest 输出 |

后端合计登记 **339** 条（Surefire 335 + Failsafe 4），其中真实通过 **328** 条，
跳过 **11** 条。跳过不是通过；11 条逐项原因见 §3.3。

### 0.2 关键结论

1. **Memory 并发 IT 已有可复核产物。** `MemoryStateMachineConcurrencyIT` 通过
   Maven Failsafe 执行，`target/failsafe-reports/` 中 XML 为
   `tests=4 / failures=0 / errors=0 / skipped=0`。
2. **AnalysisMq 的 10 条测试不再静默消失。** 原 `@BeforeAll Assumptions.abort`
   会使 Surefire XML 记为 `tests=0 / skipped=0`；现在每条用例前执行 assumption，
   XML 明确记录 `tests=10 / skipped=10`，skip 原因可读。
3. **Phase 2 集成测试在本次运行中全部真实执行并通过。** 原 27 条 + 新增 1 条
   Qdrant 精确版本删除测试 = **28/28 PASS**；RAG-01 fencing IT **4/4 PASS**。
4. **Security / SSE / Phase 2 / Broker 门控测试本轮全部解锁并执行。**
   `SecurityContextIntegrationTest` 19/19、`InternalIdentityHttpIntegrationTest` 5/5、
   `ChatSseIntegrationTest` 9/9、`Phase2RealStateIntegrationTest` 6/6、
   `Phase2MqProcessorIntegrationTest` 5/5、`Phase2RabbitMqBrokerTest` 5/5。
5. **Agent 全量无 skip。** Qdrant 集成 4 条、knowledge E2E 1 条、真实 Broker 2 条
   均已在本轮执行；1 条 `xfailed` 是既有预期失败，不是环境跳过。
6. **未提交 Git。**

---

## 1. 证据链修复

### 1.1 `MemoryStateMachineConcurrencyIT`：改用 Maven Failsafe

**选择：在 `pom.xml` 为 `**/*IT.java` 配置 Failsafe，而不是把类改名为 `*Test`。**

理由：

1. 类名改成 `*Test` 会让真实 MySQL 并发测试进入默认 `mvn test`，拖慢日常反馈；
   Failsafe 只在 `integration-test` / `verify` 阶段执行，职责边界清楚。
2. Failsafe 在 `target/failsafe-reports/` 产出独立 XML，和 Surefire 的单元测试
   产物分开，审计能明确区分“单测基线”和“集成测试”。
3. 保留 `*IT` 命名，符合 Maven 生态既有约定；不需要为了产物而改变测试语义。

配置位置：`backend/backend/pom.xml`，版本与 Surefire 对齐为 `3.2.5`，
`includes` 明确为 `**/*IT.java`，执行 `integration-test` + `verify`。

验证产物：

```text
target/failsafe-reports/TEST-com.ails.backend.service.MemoryStateMachineConcurrencyIT.xml
tests=4 failures=0 errors=0 skipped=0
```

### 1.2 `AnalysisMqIntegrationTest`：`@BeforeAll` abort 修复

**原因。** 原实现：

```java
@BeforeAll
static void setUp() {
    if (!DockerClientFactory.instance().isDockerAvailable()) {
        ...
        Assumptions.abort("Docker 不可用...");
    }
    ...
}
```

JUnit 在 `@BeforeAll` 中收到 assumption abort 时会终止整个测试类。
Surefire XML 因此写成 `tests="0" skipped="0"`，10 条 `@Test` 既不计入通过，
也不计入跳过，属于统计盲区。

**修复。** `@BeforeAll` 只记录 `unavailableReason` 并返回；新增加：

```java
@BeforeEach
void skipWhenBrokerUnavailable() {
    Assumptions.assumeTrue(unavailableReason == null,
            unavailableReason == null ? "RabbitMQ Broker 可用" : unavailableReason);
}
```

`@AfterEach purgeQueues()` 增加 `props == null` 守卫，避免跳过时解引用未初始化字段。
`-Pmq-integration` 的语义保持不变：设置 `ails.it.require-docker=true` 且 Docker
不可用时仍在 `@BeforeAll` 显式失败，不会把“没验证”伪装成“通过”。

**验证。** 即使本机 Docker 对 Testcontainers 不可用，XML 现在也是：

```text
com.ails.backend.mq.AnalysisMqIntegrationTest
tests=10 failures=0 errors=0 skipped=10
skip 原因：Docker 不可用，跳过真实 RabbitMQ 集成测试（跳过 ≠ 通过）
```

10 条测试重新进入统计，skip 计数和原因均可复核。

> 本机 Docker CLI 可用，但 Testcontainers 的 Npipe 策略返回 HTTP 400，
> 因此本轮 10 条 MQ 测试没有真实执行。这是环境限制，不是代码失败；
> `Phase2RabbitMqBrokerTest` 直连本机 `ails-rabbitmq`，5/5 已真实通过。

### 1.3 `DocumentIndexE2EIntegrationTest`：等待异步旧版本清理

RAG-01 修复后，`complete`（READY 提交点）之后才异步清理旧版本。E2E 测试原在
`awaitStatus(READY)` 后立即统计 DB chunk 与 Qdrant point，存在测试侧竞态：
第二次索引时可能先看到 4 条（新旧版本）再收敛到 2 条。

修复为有界等待 `awaitConsistency(documentId, 2, 10s)`，轮询到“DB chunk 数 +
Qdrant point 数都等于 2”后再断言；超时仍抛出真实差异，断言强度未降低。
这与 `DocumentIndexServiceIntegrationTest` 中 `awaitChunksOfVersion` 的处理一致。

### 1.4 测试库 schema 漂移

首轮全量运行暴露 2 个测试库缺列：

- `ails_phase2_it`：`conversation.last_message_seq` 不存在；
- `ails_security_it`：同类历史 schema 漂移导致 500。

`spring.sql.init.mode=always` 只执行 `CREATE TABLE IF NOT EXISTS`，不会给已有表补列。
本轮在**独立测试库**上执行 `DROP DATABASE IF EXISTS` 后由 `schema.sql` 重建：

```text
ails_document_it, ails_document_index_it, ails_document_e2e_it,
ails_document_fencing_it, ails_memory_it, ails_phase2_it,
ails_security_it, ails_chat_sse_it, ails_knowledge_e2e_it
```

开发库 `ails` 未改动。重建后的最终单次运行中上述失败全部消失。

---

## 2. 单次运行环境与命令

### 2.1 依赖

| 依赖 | 地址 / 容器 | 状态 |
| --- | --- | --- |
| MySQL 8.0.37 | `127.0.0.1:3306` | 可达 |
| Qdrant | `127.0.0.1:6333`，容器 `ails-qdrant` | 可达 |
| RabbitMQ | `127.0.0.1:5672`，容器 `ails-rabbitmq` | 可达，`guest/guest` |
| FastAPI Agent | `127.0.0.1:8010` | 本轮启动；Java E2E 用 Hash，knowledge E2E 用真实 Embedding |
| Spring Boot | `127.0.0.1:8080` | 仅 knowledge E2E 启动 |

### 2.2 后端最终单次运行

```powershell
cd backend/backend
$env:AILS_MEMORY_IT='true'
$env:AILS_PHASE2_IT='true'
$env:AILS_PHASE2_MQ_IT='true'
$env:AILS_PHASE2_MQ_PASSWORD='guest'
$env:AILS_SECURITY_IT='true'
$env:AILS_CHAT_SSE_IT='true'
$env:AILS_P8_ORDER_PROBE='true'
$env:AILS_DOCUMENT_IT='true'
$env:AILS_DOCUMENT_INDEX_IT='true'
$env:AILS_DOCUMENT_E2E_IT='true'
$env:AILS_DOCUMENT_FENCING_IT='true'
$env:AILS_IT_MYSQL_PASSWORD='...'
$env:AILS_E2E_AGENT_BASE_URL='http://127.0.0.1:8010'
$env:AILS_E2E_INTERNAL_TOKEN='...'
$env:AILS_E2E_IDENTITY_SECRET='...'
$env:AILS_E2E_QDRANT_URL='http://127.0.0.1:6333'
$env:AILS_E2E_QDRANT_COLLECTION='ails_e2e_verify_hash-v1_256'
./mvnw.cmd -B clean verify
```

`mvn clean verify` 先跑 Surefire（`*Test` / `*Tests` / `*TestCase`），再跑 Failsafe
（`*IT`），因此本次 XML 全部来自同一次命令、同一组环境变量。

产物完整性核对：46 个 Surefire XML 的 mtime 落在 `19:20:12–19:21:03`，
Failsafe 的 `MemoryStateMachineConcurrencyIT` XML 为 `19:21:25`；此后未再运行任何
定向测试覆盖这些文件。汇总脚本读取的就是这一组文件。

### 2.3 Agent 全量运行

Java E2E 要求 Hash Embedding，knowledge E2E 要求真实 Embedding，二者不能同时
使用同一个 Agent 进程配置。因此 Agent 侧按以下方式单独启动真实 Embedding 后执行：

```powershell
cd agent
$env:AILS_KNOWLEDGE_E2E='true'
$env:AILS_E2E_BACKEND_URL='http://127.0.0.1:8080'
$env:AILS_QDRANT_IT='true'
$env:AILS_QDRANT_URL='http://127.0.0.1:6333'
$env:AILS_QDRANT_COLLECTION_PREFIX='ails_knowledge_e2e_verify'
$env:AILS_TEST_RABBITMQ_URL='amqp://guest:guest@127.0.0.1:5672/'
python -m pytest -q
```

同时启动的后端使用 `ails_knowledge_e2e_it`，Agent 使用 `agent/.env` 中的真实
Embedding 配置（`text-embedding-v4`，1024 维）与同一个
`ails_knowledge_e2e_verify` collection 前缀。

---

## 3. 后端结果

### 3.1 Surefire 汇总

```text
classes=46  tests=335  failures=0  errors=0  skipped=11
```

### 3.2 Failsafe 汇总

```text
classes=1  tests=4  failures=0  errors=0  skipped=0
```

### 3.3 11 条 skip 的完整解释

| 测试类 | tests | skipped | 门控 / 原因 |
| --- | ---: | ---: | --- |
| `BackendApplicationTests` | 1 | 1 | 类上永久 `@Disabled`，需要本地 MySQL 与已初始化库表 |
| `AnalysisMqIntegrationTest` | 10 | 10 | `DockerClientFactory.isDockerAvailable()` 为 false；Testcontainers Npipe 返回 HTTP 400。测试已改为逐条 assumption，skip 可见 |
| **合计** | **11** | **11** | — |

没有第三条静默不计入统计的测试类。`MemoryStateMachineConcurrencyIT` 不在
Surefire 统计中，但在 Failsafe 中计 4 条，见 §3.4。

### 3.4 关键测试类

| 测试类 | tests | failures | errors | skipped | 产物 |
| --- | ---: | ---: | ---: | ---: | --- |
| `MemoryStateMachineConcurrencyIT` | 4 | 0 | 0 | 0 | Failsafe XML |
| `DocumentServiceIntegrationTest` | 7 | 0 | 0 | 0 | Surefire XML |
| `DocumentIndexServiceIntegrationTest` | 15 | 0 | 0 | 0 | Surefire XML |
| `DocumentIndexE2EIntegrationTest` | 1 | 0 | 0 | 0 | Surefire XML |
| `DocumentIndexFencingIntegrationTest` | 4 | 0 | 0 | 0 | Surefire XML |
| `SecurityContextIntegrationTest` | 19 | 0 | 0 | 0 | Surefire XML |
| `InternalIdentityHttpIntegrationTest` | 5 | 0 | 0 | 0 | Surefire XML |
| `ChatSseIntegrationTest` | 9 | 0 | 0 | 0 | Surefire XML |
| `ChatSseOrderingProbeTest` | 1 | 0 | 0 | 0 | Surefire XML（诊断探针，不计入契约结论） |
| `Phase2RealStateIntegrationTest` | 6 | 0 | 0 | 0 | Surefire XML |
| `Phase2MqProcessorIntegrationTest` | 5 | 0 | 0 | 0 | Surefire XML |
| `Phase2RabbitMqBrokerTest` | 5 | 0 | 0 | 0 | Surefire XML |
| `AnalysisMqIntegrationTest` | 10 | 0 | 0 | 10 | Surefire XML |
| `BackendApplicationTests` | 1 | 0 | 0 | 1 | Surefire XML |

---

## 4. Phase 2 验收结果

### 4.1 文档索引链路 28/28

| 测试 | 结果 | 本次证据 |
| --- | --- | --- |
| `DocumentServiceIntegrationTest` | 7/7 PASS | 真实 MySQL `ails_document_it` |
| `DocumentIndexServiceIntegrationTest` | 15/15 PASS | 真实 MySQL `ails_document_index_it` + Fake Agent |
| `DocumentIndexE2EIntegrationTest` | 1/1 PASS | 真实 FastAPI Agent + 真实 Qdrant `ails_e2e_verify_hash-v1_256` |
| `agent/tests/test_qdrant_integration.py` | 4/4 PASS | 真实 Qdrant，随机临时 collection |
| `agent/tests/test_knowledge_e2e.py` | 1/1 PASS | 真实后端 + 真实 DashScope `text-embedding-v4` + Qdrant |
| **合计** | **28/28 PASS / 0 FAIL / 0 SKIP** | — |

### 4.2 RAG-01 fencing 回归

| 测试 | 结果 | 环境 |
| --- | --- | --- |
| `DocumentIndexFencingIntegrationTest` | 4/4 PASS | 真实 MySQL `ails_document_fencing_it` + 真实 Qdrant |

覆盖：

- `staleWorkerCleanupDoesNotDeleteNewWorkersActivePoints`
- `staleWorkerRollbackDoesNotDeleteNewWorkersActivePoints`
- `currentWorkerCleanupDeletesOnlyOlderVersions`
- `currentWorkerRollbackDeletesOnlyItsOwnAttempt`

### 4.3 D1 与 RAG-01 状态

- D1（`DocumentChunkMapper` 的 `&lt;&gt;` 未在 `<script>` 内）仍为已修复状态；
  本次 `DocumentIndexServiceIntegrationTest` 15/15、E2E 1/1 再次通过。
- RAG-01（回滚/清理缺少 attempt fencing）仍为已修复状态；本次 fencing IT 4/4 通过。
- RAG-02、RAG-03 未修复，仍是残余风险，见 Phase 2 报告第 10 节。

---

## 5. Agent 结果

```text
415 passed, 1 xfailed, 1 warning in 32.18s
```

- 0 skipped：`test_qdrant_integration.py`、`test_knowledge_e2e.py`、
  `test_worker_broker.py` 的门控均已设置并真实执行。
- 1 xfailed：`test_runtime_budget_contract.py` 的既有预期失败，不是环境跳过，
  也不是本轮新增失败。
- `test_qdrant_integration.py` 的 4 条包含 RAG-01 新增的精确版本删除测试。
- `test_knowledge_e2e.py` 的 1 条走完整链路：注册 → Space → 上传 → 真实
  Embedding/Qdrant → READY → 无 Concept 检索 → 引用来源 → 故障降级。

---

## 6. 残余验证缺口

1. **`AnalysisMqIntegrationTest` 10 条仍未真实执行。** 本机 Docker CLI 正常，
   但 Testcontainers 1.19.x 的 Npipe 客户端对当前 Docker Desktop 返回 HTTP 400。
   测试已正确计为 skip；若要补齐，需要升级/调整 Testcontainers 或提供可用的
   TCP Docker endpoint。不能把这 10 条写成通过。
2. **`BackendApplicationTests` 永久 `@Disabled`。** 它仍是 1 条 skip，建议后续
   改为环境门控或删除，不要长期用 `@Disabled` 表达“需要 MySQL”。
3. **knowledge E2E 依赖外部 Embedding 服务。** 本轮使用 DashScope
   `text-embedding-v4`，结果受网络与配额影响，不代表生产长期可用。
4. **测试库 schema 重建是本地操作。** 本次为消除漂移重建了 9 个独立测试库；
   开发库 `ails` 未动。其他机器复跑时仍应先确认测试库 schema 与 `schema.sql` 一致。
5. **`ChatSseOrderingProbeTest` 是诊断探针。** 它本轮 1/1 通过，但不应作为
   引用/SSE 契约结论的一部分，单独列出以免混淆。

---

## 7. 复现命令

```powershell
# 后端单次全量（Surefire + Failsafe）
cd backend/backend
$env:AILS_MEMORY_IT='true'; $env:AILS_PHASE2_IT='true'
$env:AILS_PHASE2_MQ_IT='true'; $env:AILS_PHASE2_MQ_PASSWORD='guest'
$env:AILS_SECURITY_IT='true'; $env:AILS_CHAT_SSE_IT='true'
$env:AILS_P8_ORDER_PROBE='true'
$env:AILS_DOCUMENT_IT='true'; $env:AILS_DOCUMENT_INDEX_IT='true'
$env:AILS_DOCUMENT_E2E_IT='true'; $env:AILS_DOCUMENT_FENCING_IT='true'
$env:AILS_IT_MYSQL_PASSWORD='...'
$env:AILS_E2E_AGENT_BASE_URL='http://127.0.0.1:8010'
$env:AILS_E2E_INTERNAL_TOKEN='...'; $env:AILS_E2E_IDENTITY_SECRET='...'
$env:AILS_E2E_QDRANT_URL='http://127.0.0.1:6333'
$env:AILS_E2E_QDRANT_COLLECTION='ails_e2e_verify_hash-v1_256'
./mvnw.cmd -B clean verify

# Agent 全量（真实 Embedding Agent + 后端 8080 + Qdrant）
cd ../../agent
$env:AILS_KNOWLEDGE_E2E='true'
$env:AILS_E2E_BACKEND_URL='http://127.0.0.1:8080'
$env:AILS_QDRANT_IT='true'; $env:AILS_QDRANT_URL='http://127.0.0.1:6333'
$env:AILS_QDRANT_COLLECTION_PREFIX='ails_knowledge_e2e_verify'
$env:AILS_TEST_RABBITMQ_URL='amqp://guest:guest@127.0.0.1:5672/'
python -m pytest -q
```

---

## 8. 边界遵守

| 要求 | 执行情况 |
| --- | --- |
| 不把未执行写成通过 | 遵守；11 条 skip 逐项列明，AnalysisMq 10 条明确未执行 |
| 不让整类测试静默消失 | 遵守；`AnalysisMqIntegrationTest` XML 为 10 tests / 10 skipped |
| 不削弱断言 | 遵守；E2E 只增加有界等待，最终仍断言 2 条 chunk + 2 个 Qdrant point |
| 不修改生产业务逻辑 | 遵守；本轮只改测试基础设施、测试等待、`pom.xml` 与文档 |
| 不自动提交 Git | 遵守；工作区未提交 |
| 不进入新阶段 | 遵守；本轮只做证据链与复核 |
