# Phase 1B — Memory 状态机最小修复实施记录

- 实施日期：2026-11-07
- 分支：`main`
- 基线提交：`4732e40 chore: 忽略不属于本项目文件`
- 设计依据：[`docs/design/MEMORY_MINIMAL_FIX.md`](../design/MEMORY_MINIMAL_FIX.md)
- 本轮范围：只修复 Memory 状态机缺陷；不进入 Phase 2，不处理设计文档中明确排除的四个相邻问题，不提交 Git。

> **测试数字模式说明（2026-11-07 补记）**：本报告中的后端数字分两种模式，不可混用。
> **默认模式** = 不设置任何 `AILS_*_IT`，执行 `mvn -B test`，Failsafe 不执行；
> **门控全量模式** = 打开全部 `AILS_*_IT`，执行 `mvn clean verify`。
> 最新实测、产物与逐项差异见 [`TEST_EVIDENCE_MODES.md`](TEST_EVIDENCE_MODES.md)。
> 文中未标注模式的历史数字只作基线。

## 0. 结论摘要

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 普通 `PATCH` 不能再任意改 `status` | 通过 | 状态只经 `applyStatusTransition`；字段白名单不含 `status` |
| `ARCHIVED → ACTIVE` / `ACTIVE → CANDIDATE` 返回 409 且不写库 | 通过 | T1、T2、T6、T7 |
| 合法迁移 `CANDIDATE → ACTIVE`、`CANDIDATE → ARCHIVED`、`ACTIVE → ARCHIVED` | 通过 | T3、T4、T5 |
| 同态重复请求幂等 | 通过 | T12、T13 |
| 并发更新不静默覆盖、冲突可识别 | 通过（真实 MySQL 并发验证） | T14、T15、T21；见 §4.4 |
| 所有应用层状态写入收敛到唯一入口 | 通过 | `memory.setStatus` 仅剩 `MemoryService.java:602`；§3 |
| 跨用户仍为 404 | 通过（服务层） | T10、T11 |
| Agent 路径不产生非法迁移 | 通过 | T16–T21 |
| Memory 读取 / Agent 流程无回归 | 通过 | **门控全量模式**：后端 **339 tests**（Surefire 335 + Failsafe 4）0 失败 / 11 skipped；Agent **415 passed / 0 skipped / 1 xfailed**（历史声明，无本轮原始产物）；Phase 1 相关测试无回归 |
| 新增 skip 不超过基线 | 通过 | **门控全量模式** 11 skipped，均为既有门控（`BackendApplicationTests` 1 + `AnalysisMqIntegrationTest` 10）；Phase 1 新增 22 条与并发 IT 4 条均为真实执行通过。**默认模式**下同一套 Surefire 为 335 / 0 / 0 / **88 skipped**（247 passed），Failsafe 不执行 |
| 未自动提交 Git | 通过 | `git log -1` 仍为 `4732e40` |

Phase 1B 初版历史结果（**非最终全量**）：**Tests run: 277, Failures: 0, Errors: 0, Skipped: 73**（基线 255 / 0 / 0 / 73，新增 22 条全部执行）。

> **2026-11-07 最终单次全量复核（门控全量模式）**：在真实 MySQL 8.0.37 上，`MemoryStateMachineConcurrencyIT`（4 条）
> 已通过 Maven Failsafe 执行并产出 `target/failsafe-reports/TEST-*.xml`，结果 **4 PASS / 0 FAIL / 0 SKIP**。
> `mvn clean verify` 的 Surefire 汇总为 **335 tests / 0 failures / 0 errors / 11 skipped**，
> 后端合计 **339 条 / 328 通过 / 11 跳过**；Agent 门控全量为 **415 passed / 0 skipped / 1 xfailed**
> （历史文档声明；本轮未保留该次 pytest 原始产物）。
> **默认模式**（`mvn -B test`，2026-11-07 19:56 实测）为 Surefire **335 / 0 / 0 / 88**
> （247 passed），Failsafe 不执行；原始输出见
> [`artifacts/maven-default-test-20261107T195424.txt`](artifacts/maven-default-test-20261107T195424.txt)。
> 详见 §4.4、§4.5、[`TEST_EVIDENCE_MODES.md`](TEST_EVIDENCE_MODES.md) 与 [`INTEGRATION_VERIFICATION.md`](INTEGRATION_VERIFICATION.md)。

---

## 1. 修复前后代码证据对比

### 1.1 状态枚举：从“只有三个值”到“矩阵是唯一真相”

修复前（`HEAD:backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java`）：

```java
public enum MemoryStatus {
    CANDIDATE,
    ACTIVE,
    ARCHIVED
}
```

修复后（`backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java:20-44`）：

```java
public enum MemoryStatus {
    CANDIDATE,
    ACTIVE,
    ARCHIVED;

    public boolean canTransitionTo(MemoryStatus target) {
        if (target == null) {
            return false;
        }
        if (this == target) {
            return true;
        }
        return switch (this) {
            case CANDIDATE -> target == ACTIVE || target == ARCHIVED;
            case ACTIVE -> target == ARCHIVED;
            case ARCHIVED -> false;
        };
    }
}
```

矩阵与设计 §4.1 完全一致：

| from \ to | CANDIDATE | ACTIVE | ARCHIVED |
| --- | --- | --- | --- |
| CANDIDATE | ✅ 同态 | ✅ | ✅ |
| ACTIVE | ❌ | ✅ 同态 | ✅ |
| ARCHIVED | ❌ | ❌ | ✅ 同态（仅 DELETE 短路） |

### 1.2 普通 `update`：删除任意状态写入，改为唯一入口 + 白名单

修复前（`HEAD:MemoryService.java:408-410`）：

```java
if (request.status() != null) {
    memory.setStatus(request.status());   // 任意值直接落库
}
```

修复后（`backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:394-424`）：

```java
@Transactional
public MemoryResponse update(Long userId, Long memoryId, MemoryRequest request) {
    Memory memory = ownedMemory(userId, memoryId);

    // 状态迁移先走唯一入口：ARCHIVED → 非 ARCHIVED 会得到精确的转换错误。
    // 字段编辑是否允许取决于迁移前的状态：ARCHIVED 整体拒绝，CANDIDATE / ACTIVE 允许。
    MemoryStatus statusBeforeUpdate = memory.getStatus();
    if (request.status() != null) {
        applyStatusTransition(memory, request.status());
    }
    requireEditable(statusBeforeUpdate);

    // ...原有 scope / identity 解析...

    // 普通字段白名单：status 不在其中，只能由上面的 applyStatusTransition 修改。
    memory.setMemoryType(request.memoryType());
    memory.setContent(request.content().trim());
    memory.setImportance(request.importance());
    requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
    return MemoryResponse.from(memory);
}
```

关键点：

- `status` 不再出现在普通字段赋值区；唯一的状态写入在 `applyStatusTransition` 内。
- `requireEditable` 接收的是**迁移前**状态。不能接收迁移后状态：`CANDIDATE → ARCHIVED` 是合法 PATCH，迁移后对象已是 `ARCHIVED`，用迁移后状态判断会把合法请求误拒。
- 实现顺序与设计 §4.4 伪代码略有不同：设计伪代码先 `requireEditable(memory)` 再迁移；实现先迁移、再用迁移前状态做可编辑校验。这样 `ARCHIVED → ACTIVE` 会得到精确的 `"非法的记忆状态转换: ARCHIVED → ACTIVE"`，而不是笼统的“已归档不可修改”，满足“响应体包含可区分原因”的验收要求；同时 `CANDIDATE → ARCHIVED` 仍被正确允许。

### 1.3 `archive`：增加终态短路，重复 DELETE 不再写库

修复前（`HEAD:MemoryService.java:419-424`）：

```java
@Transactional
public void archive(Long userId, Long memoryId) {
    Memory memory = ownedMemory(userId, memoryId);
    memory.setStatus(MemoryStatus.ARCHIVED);
    requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
}
```

修复后（`backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:430-439`）：

```java
@Transactional
public void archive(Long userId, Long memoryId) {
    Memory memory = ownedMemory(userId, memoryId);
    if (memory.getStatus() == MemoryStatus.ARCHIVED) {
        // DELETE 幂等：已经归档，不重复写库，也不刷新 updated_at / version。
        return;
    }
    applyStatusTransition(memory, MemoryStatus.ARCHIVED);
    requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
}
```

行为变化：`CANDIDATE → ARCHIVED`、`ACTIVE → ARCHIVED` 不变；`ARCHIVED → ARCHIVED` 从“再次 UPDATE”变为“直接成功、不写库”。

### 1.4 创建路径：保留初始状态语义，但同样经过唯一入口

修复前：

```java
// persist
memory.setStatus(status);                       // HEAD:MemoryService.java:488
// insertCandidate
memory.setStatus(MemoryStatus.CANDIDATE);       // HEAD:MemoryService.java:172
```

修复后：

```java
// persist（create / createCandidate 的公共实现）
applyStatusTransition(memory, status);          // MemoryService.java:503
// insertCandidate
applyStatusTransition(memory, MemoryStatus.CANDIDATE); // MemoryService.java:173
```

创建路径的实体尚未落库，`memory.getStatus()` 为 `null`；`applyStatusTransition` 把 `null` 视为 CANDIDATE 基线。因此：

- `create`（用户自己创建）传入 `ACTIVE`，等价于 `CANDIDATE → ACTIVE`，最终仍为 `ACTIVE`；
- `createCandidate` 传入 `CANDIDATE`，同态，最终仍为 `CANDIDATE`；
- `insertCandidate` 固定 `CANDIDATE`，最终仍为 `CANDIDATE`。

这是对设计 §4.5 “创建路径可保留直接 `setStatus`” 的有意收紧：本轮要求“所有状态写入必须经过唯一的 `applyStatusTransition`”，因此创建路径也统一入口；最终状态与修复前一致。

### 1.5 唯一入口与可编辑校验

`backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:574-603`：

```java
private static void requireEditable(MemoryStatus statusBeforeUpdate) {
    if (statusBeforeUpdate == MemoryStatus.ARCHIVED) {
        throw BusinessException.conflict("已归档的记忆不可修改");
    }
}

private static void applyStatusTransition(Memory memory, MemoryStatus target) {
    MemoryStatus current = memory.getStatus() == null
            ? MemoryStatus.CANDIDATE
            : memory.getStatus();
    if (!current.canTransitionTo(target)) {
        throw BusinessException.conflict(
                "非法的记忆状态转换: " + current + " → " + target);
    }
    memory.setStatus(target);
}
```

`Memory.setStatus` 由 Lombok `@Data` 生成，编译期无法禁止其他类调用；本轮通过代码收敛 + 注释 + 测试保证。生产代码中 Memory 相关 `setStatus` 只剩这一处。

### 1.6 测试先行的红 / 绿证据

先加 22 条测试、不改生产代码时：

```text
Tests run: 22, Failures: 6, Errors: 0, Skipped: 0
```

失败项正好覆盖缺陷：

| 失败用例 | 旧实现表现 | 预期 |
| --- | --- | --- |
| T1 `archivedToActiveReturns409` | HTTP 200 | 409 |
| T2 `activeToCandidateReturns409` | HTTP 200 | 409 |
| T6 `archivedToCandidateRejected` | 未抛 409 | 409 |
| T7 `activeToCandidateRejected` | 未抛 409 | 409 |
| T8 `archivedContentEditRejected` | 未抛 409 | 409 |
| T13 `repeatDeleteIsIdempotent` | 第二次仍调用 `updateById` | 只写 1 次 |

修复后同一组测试：

```text
Tests run: 22, Failures: 0, Errors: 0, Skipped: 0
```

---

## 2. 修改文件清单与行为变化

### 2.1 文件清单

| # | 文件 | 类型 | 变更 |
| --- | --- | --- | --- |
| 1 | `backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java` | 修改 | 新增 `canTransitionTo` 3×3 矩阵与矩阵 Javadoc |
| 2 | `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java` | 修改 | 新增 `applyStatusTransition` / `requireEditable`；`update` 白名单排除 `status`；`archive` 终态短路；创建路径与 Agent 路径收敛 |
| 3 | `backend/backend/src/test/java/com/ails/backend/service/MemoryStateMachineTest.java` | 新增 | T3–T21，共 19 条 Mockito 单测 |
| 4 | `backend/backend/src/test/java/com/ails/backend/controller/MemoryControllerTest.java` | 新增 | T1、T2、T22，共 3 条 MockMvc 测试 |

生产代码 diff 统计：2 个文件，93 insertions / 8 deletions。另新增本审计文档 `docs/audit/PHASE1_MEMORY_FIX.md`（交付物，不属于生产代码）。

未修改：`MemoryController`、`MemoryRequest`、`Memory` 实体、`MemoryMapper`、`schema.sql`、`MybatisPlusConfig`、Agent 代码、Flutter、既有测试。没有表结构变更、没有数据迁移、没有新增并发机制。

### 2.2 行为变化

| 场景 | 修复前 | 修复后 | 是否预期 |
| --- | --- | --- | --- |
| `PATCH` 归档记忆 + `status=ACTIVE/CANDIDATE` | 200，记忆复活/降级 | 409，不写库 | ✅ 核心修复 |
| `PATCH` 归档记忆 + 不带 `status`（只改内容） | 200，内容被改 | 409 `"已归档的记忆不可修改"`，不写库 | ✅ 终态不可编辑 |
| `PATCH` 生效记忆 + `status=CANDIDATE` | 200，降级为候选 | 409，不写库 | ✅ 核心修复 |
| `PATCH` 候选记忆 + `status=ACTIVE` | 200 | 200，仍为 ACTIVE | ✅ 不变 |
| `PATCH` 候选记忆 + `status=ARCHIVED` | 200 | 200，仍为 ARCHIVED（经矩阵） | ✅ 不变 |
| `PATCH` 同状态（如 ACTIVE→ACTIVE） | 200，再次写库 | 200，再次写库，最终状态不变 | ✅ 幂等 |
| `DELETE` 已归档记忆 | 200，再次写库 | 200，不写库 | ✅ DELETE 幂等 |
| `POST` 用户创建 | 落 ACTIVE | 落 ACTIVE（经 CANDIDATE 基线矩阵） | ✅ 不变 |
| Agent ADD / UPDATE 候选 | 落/保持 CANDIDATE | 落/保持 CANDIDATE | ✅ 不变 |
| Agent UPDATE 生效/归档记忆 | `SKIPPED` | `SKIPPED`（新增防御性 guard） | ✅ 不变 |
| 成功响应结构 | `{"code":0,"message":"success","data":...}` | 不变 | ✅ 不变 |
| 非法迁移错误 | 无 | HTTP 409，`{"code":409,"message":"非法的记忆状态转换: ARCHIVED → ACTIVE","data":null}` | ✅ 新增契约 |
| 非法枚举 | 400 | 400（`HttpMessageNotReadableException`） | ✅ 不变 |

错误响应由既有 `GlobalExceptionHandler` 生成（`backend/backend/src/main/java/com/ails/backend/common/GlobalExceptionHandler.java:22-27`），未改响应结构。`409` 是稳定契约；`message` 文案用于人类阅读和测试断言，不建议外部客户端依赖精确文本。

---

## 3. 六个应用层状态写值点的收敛情况

设计文档 §1.2 的编号中，`1/2/3/4/6/7` 是 6 个应用层写值点；`5` 本身不写 `status`，`8` 是数据库默认值。逐项对照：

| 设计编号 | 入口 | 修复前写入方式 | 修复后写入方式 | 收敛结论 |
| --- | --- | --- | --- | --- |
| 1 | `create` | `persist(..., ACTIVE)` → `setStatus(status)` | `persist(..., ACTIVE)` → `applyStatusTransition(memory, ACTIVE)` | ✅ 经唯一入口；最终 ACTIVE |
| 2 | `createCandidate` | `persist(..., CANDIDATE)` → `setStatus(status)` | `persist(..., CANDIDATE)` → `applyStatusTransition(memory, CANDIDATE)` | ✅ 经唯一入口；最终 CANDIDATE |
| 3 | `persist` | `memory.setStatus(status)` | `applyStatusTransition(memory, status)` | ✅ 唯一 `setStatus` 所在方法内部 |
| 4 | `insertCandidate` | `memory.setStatus(CANDIDATE)` | `applyStatusTransition(memory, CANDIDATE)` | ✅ 经唯一入口 |
| 5 | `updateCandidate` | 不写 `status` | 不写 `status`；新增 `existing.status != CANDIDATE → SKIPPED` 防御性 guard | ✅ 现状不变，未来调用点也不能绕过 Agent 边界 |
| 6 | `update` | `if (request.status() != null) setStatus(request.status())` | 仅 `applyStatusTransition(memory, request.status())`；普通字段白名单不含 `status` | ✅ 缺陷入口已关闭 |
| 7 | `archive` | 无条件 `setStatus(ARCHIVED)` | 已归档直接短路；否则 `applyStatusTransition(memory, ARCHIVED)` | ✅ 经唯一入口；DELETE 幂等 |
| 8 | 数据库默认值 | `CANDIDATE` | `CANDIDATE` | ✅ 未改，仅作裸 INSERT 兜底 |

收敛证据：

```text
$ grep -rn "setStatus(" backend/backend/src/main/java --include=*.java
...（其余命中均为 AgentRunStatus / DocumentStatus / SessionStatus 等其他领域）
backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:602: memory.setStatus(target);
```

- Memory 生产代码中只有 `MemoryService.java:602` 一处 `memory.setStatus`，位于 `applyStatusTransition` 内部。
- 四个调用点：`MemoryService.java:173`（insertCandidate）、`:402`（update）、`:437`（archive）、`:503`（persist）。
- `MemoryMapper` 只继承 `BaseMapper`，无手写 XML；`resources/mapper` 不存在；没有 `UPDATE memory SET status=...` 的直接 SQL。
- `MemoryController.update` 是 `MemoryService.update` 的唯一生产调用方（`MemoryController.java:71`）。
- `create` / `createCandidate` 都走 `persist`，没有第二条创建路径。
- 数据库 `status ENUM` 和 `version` 未改；`@Version` + `OptimisticLockerInnerInterceptor` 仍是唯一并发机制。

---

## 4. 22 条测试逐条结果

### 4.1 测试类与执行命令

```bash
# 专项测试（22 条）
cd backend/backend
./mvnw -B -Dtest=MemoryStateMachineTest,MemoryControllerTest test

# 完整 Spring Boot 测试
./mvnw -B clean test

# Agent 相关后端回归
./mvnw -B -Dtest='*Agent*Test,*Analysis*Test,MemorySourceConcurrencyTest,TurnAnalysisServiceTest' test
```

- T1、T2、T22：`MemoryControllerTest`（MockMvc + 真实 `MemoryService` + Mock mapper + 真实 `GlobalExceptionHandler`）。
- T3–T21：`MemoryStateMachineTest`（Mockito，无数据库、无 Spring 容器）。

### 4.2 逐条结果

| ID | 测试方法 | 场景 | 关键断言 | 结果 |
| --- | --- | --- | --- | --- |
| T1 | `t1_archivedToActiveReturns409` | PATCH 归档记忆 + `status=ACTIVE` | HTTP 409；`$.code=409`；message 含 `ARCHIVED → ACTIVE`；`updateById` 从未调用 | **PASS** |
| T2 | `t2_activeToCandidateReturns409` | PATCH 生效记忆 + `status=CANDIDATE` | HTTP 409；`$.code=409`；message 含 `ACTIVE → CANDIDATE`；`updateById` 从未调用 | **PASS** |
| T3 | `t3_candidateToActiveSucceeds` | 候选确认 | 返回 ACTIVE；捕获的更新对象 status=ACTIVE | **PASS** |
| T4 | `t4_candidateToArchivedByDeleteSucceeds` | 候选拒绝（DELETE） | 捕获的更新对象 status=ARCHIVED | **PASS** |
| T5 | `t5_activeToArchivedByDeleteSucceeds` | 生效记忆归档（DELETE） | 捕获的更新对象 status=ARCHIVED | **PASS** |
| T6 | `t6_archivedToCandidateRejected` | 归档降级为候选 | 409；message 含 `ARCHIVED → CANDIDATE`；不写库 | **PASS** |
| T7 | `t7_activeToCandidateRejected` | 生效降级为候选 | 409；message 含 `ACTIVE → CANDIDATE`；不写库 | **PASS** |
| T8 | `t8_archivedContentEditRejected` | 归档记忆只改内容 | 409；message 含“已归档”；不写库 | **PASS** |
| T9 | `t9_missingMemoryReturns404` | 不存在的 Memory，update/archive | 两者均 404；不写库 | **PASS** |
| T10 | `t10_crossUserUpdateReturns404` | 用户 A 操作用户 B 的 Memory | update/archive 均 404；不写库 | **PASS** |
| T11 | `t11_crossUserSpaceReturns404` | PATCH 挂载他人 space | 404；不写库 | **PASS** |
| T12 | `t12_repeatSameStatePatchIsIdempotent` | 连续两次 PATCH ACTIVE→ACTIVE | 两次成功；`updateById` 2 次；最终 ACTIVE | **PASS** |
| T13 | `t13_repeatDeleteIsIdempotent` | 连续两次 DELETE | `updateById` 仅 1 次；最终 ARCHIVED | **PASS**（另有真实 MySQL 并发版，§4.4） |
| T14 | `t14_concurrentConfirmAndArchiveDoNotSilentlyOverwrite` | 并发“确认” vs “归档” | 恰好 1 个 409；提交状态为 ACTIVE 或 ARCHIVED；两次更新尝试；`OptimisticLockerInnerInterceptor` 已注册 | **PASS**（单测）；**PASS**（真实 MySQL 合法 vs 非法并发，§4.4） |
| T15 | `t15_concurrentDoubleConfirmKeepsActive` | 并发两次确认 | 恰好 1 个 409；提交状态 ACTIVE；不会回到 CANDIDATE | **PASS**（单测）；**PASS**（真实 MySQL 并发确认，§4.4） |
| T16 | `t16_agentAddPersistsCandidate` | Agent ADD | 结果 ADDED；插入对象 status=CANDIDATE | **PASS** |
| T17 | `t17_agentUpdateCandidateKeepsCandidate` | Agent UPDATE 候选 | 结果 UPDATED；内容更新；status 仍 CANDIDATE | **PASS** |
| T18 | `t18_agentUpdateActiveIsSkipped` | Agent UPDATE 生效记忆 | 结果 SKIPPED；不写库 | **PASS** |
| T19 | `t19_agentUpdateArchivedIsSkipped` | Agent UPDATE 归档记忆 | 结果 SKIPPED；不写库；不复活 | **PASS** |
| T20 | `t20_createCandidateIgnoresRequestedActive` | Agent 创建时请求 status=ACTIVE | 落库对象 status=CANDIDATE | **PASS** |
| T21 | `t21_agentUpdateLosesOptimisticLockRace` | Agent 更新遇到版本冲突 | `updateById` 返回 0 → 结果 SKIPPED，不抛异常 | **PASS**（单测）；**PASS**（真实 MySQL Agent UPDATE vs 用户确认，§4.4） |
| T22 | `t22_invalidStatusEnumReturns400` | PATCH `status="RESURRECTED"` | HTTP 400；`$.code=400`；未进入 Service/Mapper | **PASS** |

汇总：**22 / 22 PASS，0 Failures，0 Errors，0 Skipped**。另有真实 MySQL 并发 IT **4 / 4 PASS**（§4.4）。

### 4.3 补充回归

> 本节前三行为 Phase 1B 初版/集成验证轮的历史命令与结果（**非最终单次全量**）。
> 最终全量结果见末行与 §4.5。

| 回归范围 | 命令 | 结果 |
| --- | --- | --- |
| 完整 Spring Boot（历史） | `./mvnw -B clean test` | **277 run / 0 fail / 0 error / 73 skipped** |
| Agent 相关后端（历史） | `./mvnw -B -Dtest='*Agent*Test,*Analysis*Test,MemorySourceConcurrencyTest,TurnAnalysisServiceTest' test` | **71 run / 0 fail / 0 error / 0 skipped** |
| Python Agent（历史） | `cd agent && python -m pytest -q` | **383 passed / 5 skipped / 1 xfailed** |
| **门控全量模式（历史）** | `./mvnw.cmd -B clean verify` + `python -m pytest -q`（全部 `AILS_*_IT` 打开） | 后端 **339 / 0 fail / 0 error / 11 skipped**（Surefire 335 + Failsafe 4）；Agent **415 passed / 0 skipped / 1 xfailed**（历史声明，无本轮原始产物） |
| **默认模式（2026-11-07 19:56 实测）** | `mvn -B test`（不设置 `AILS_*_IT`） | Surefire **335 / 0 fail / 0 error / 88 skipped**（247 passed）；Failsafe 不执行；Agent `python -m pytest -q` 为 **408 passed / 7 skipped / 1 xfailed** |

Agent 后端回归覆盖：`HttpPythonAgentAnalysisGatewayTest`（10）、`HttpPythonAgentGatewayTest`（13）、`AnalysisResultConsumerTest`（9）、`AnalysisResultProcessorTest`（7）、`AnalysisStaleRunCompensatorTest`（5）、`AnalysisTaskPublisherTest`（6）、`AnalysisTaskServiceTest`（8）、`AgentAnalysisWriteServiceTest`（3）、`AnalysisDispatchServiceTest`（4）、`MemorySourceConcurrencyTest`（3）、`TurnAnalysisServiceTest`（3）。历史记录中 `AnalysisMqIntegrationTest` 在未启用 Docker/RabbitMQ 时运行 0 条；**门控全量模式**中该类的 10 条已作为 **10 skipped** 进入 Surefire XML，不再静默消失，但仍未真实执行（Docker/Testcontainers Npipe HTTP 400）。

### 4.4 真实 MySQL 并发集成验证（2026-11-07 补跑）

测试类：`backend/backend/src/test/java/com/ails/backend/service/MemoryStateMachineConcurrencyIT.java`（新增）。

执行方式：

```powershell
cd backend/backend
$env:AILS_MEMORY_IT='true'; $env:AILS_IT_MYSQL_PASSWORD='...'
./mvnw.cmd -q -Dtest=MemoryStateMachineConcurrencyIT test
```

环境：MySQL 8.0.37，`127.0.0.1:3306`，独立库 `ails_memory_it`（不连接开发库 `ails`）。
HTTP 层使用 standalone MockMvc + 真实 `MemoryController`；数据层使用真实 `MemoryService`、
真实 MyBatis Mapper、真实 MySQL 事务与 `@Version` 乐观锁。所有断言直接读数据库最终行。

结果：**4 PASS / 0 FAIL / 0 SKIP**。

| 场景 | 方法 | 关键断言（数据库最终状态） | 结果 |
| --- | --- | --- | --- |
| T15 并发两次确认 | `concurrentDoubleConfirmOnCandidateKeepsActive` | 两个响应 ∈ {200,409}；成功次数 1–2；`version == 成功次数`；最终 `status=ACTIVE`、内容为确认值；若仅 1 次成功则必为 200+409 | PASS |
| T14 合法 vs 非法并发 | `illegalCandidatePatchIsRejectedWhileLegalActivePatchSucceeds` | 响应恰为 200+409；最终 `status=ACTIVE`、内容为“合法更新”；内容不是“非法降级”；`version = 原版本 + 1`（非法请求未写库） | PASS |
| T13 重复 DELETE 并发 | `concurrentDeleteOfArchivedMemoryIsIdempotent` | 两个请求均 200；最终 `status=ARCHIVED`、内容不变、`version` 不变、`updated_at` 不变 | PASS |
| T21 Agent UPDATE vs 用户确认 | `agentUpdateRacingUserConfirmNeverOverwritesConfirmedContent` | 用户响应 ∈ {200,409}；Agent 结果 ∈ {UPDATED, SKIPPED}；用户 200 时最终必为 ACTIVE + 用户内容（版本 1 或 2）；用户 409 时 Agent 必须 UPDATED 且最终为 CANDIDATE + Agent 内容（版本 1）；不会出现“ACTIVE 却保留 Agent 内容” | PASS |

真实乐观锁证据（测试日志）：

```text
# T21 本次交错：Agent 先提交 version 0→1，用户读回 version=1 后提交 version 1→2
[pool-5-thread-2] MemoryMapper.updateById ... version=1 ... WHERE id=? AND version=0 : <== Updates: 1
[pool-5-thread-1] MemoryMapper.updateById ... version=2 ... WHERE id=? AND version=1 : <== Updates: 1

# T15 并发两次确认：一个线程先提交，另一个的 version 条件未命中
[pool-6-thread-1] MemoryMapper.updateById : <== Updates: 1
[pool-6-thread-2] MemoryMapper.updateById : <== Updates: 0
```

`Updates: 0` 表示条件 `WHERE id=? AND version=?` 未命中，冲突方按设计返回 409（HTTP）或
`SKIPPED`（Agent），而不是覆盖另一方已提交的数据。T21 的结果取决于线程交错：
本次是 Agent 先写、用户后写，因此 Agent 结果为 `UPDATED`、最终版本 2、内容为用户确认值；
测试同时覆盖并断言另一种交错（用户先写 → Agent 得到 `SKIPPED`）的不变量。

#### 改造前后对照

| 维度 | 改造前（Phase 1B 初版） | 改造后（集成验证轮） |
| --- | --- | --- |
| 测试类型 | Mockito 单测，`updateById` 用返回值 1/0 模拟冲突 | 真实 MySQL + 真实事务 + 真实 `@Version` 乐观锁 |
| 断言对象 | 捕获的更新对象 / `never().updateById` | 数据库最终行（status/content/version/updated_at） |
| 并发执行 | 单线程顺序调用，模拟两次结果 | 两个线程 `CountDownLatch` 同时发起 |
| 409 来源 | 手工构造的 `updateById=0` | MySQL 条件更新真实返回 0 行 |
| T14 | 单测覆盖“确认 vs 归档” | 真实 MySQL 覆盖“合法 PATCH ACTIVE vs 非法 PATCH CANDIDATE” |
| T15 | 单测覆盖并发两次确认 | 真实 MySQL 覆盖并发两次 CANDIDATE→ACTIVE |
| T13 | 单测覆盖连续两次 DELETE | 真实 MySQL 覆盖两个线程同时 DELETE 已归档记忆 |
| T21 | 单测覆盖 Agent 更新版本冲突 | 真实 MySQL 覆盖 Agent `reconcileCandidate` 与用户 PATCH 的真实竞争 |
| 结果 | 22/22 PASS（单测） | 22/22 PASS（单测）+ 4/4 PASS（真实 MySQL IT） |

没有为通过而削弱断言：单测保留；IT 断言的是数据库最终状态，不是响应码。
最终证据链修复后，`*IT` 由 Maven Failsafe 在 `mvn clean verify` 中统一执行，
`MemoryStateMachineConcurrencyIT` 不再依赖 `-Dtest=...` 的临时定向命令，
而是产出 `target/failsafe-reports/TEST-com.ails.backend.service.MemoryStateMachineConcurrencyIT.xml`
（`tests=4 / failures=0 / errors=0 / skipped=0`）。该结果与 Surefire 的 335 条来自
同一次 `clean verify`，可直接复核。

### 4.5 最终单次全量证据（2026-11-07 19:20，门控全量模式）

命令：`backend/backend` 下 `./mvnw.cmd -B clean verify`（全部 `AILS_*_IT` 门控开启）。
产物（已归档到 `docs/audit/artifacts/maven-gated-full-20261107-xml.zip`）：

```text
target/surefire-reports/  classes=46  tests=335  failures=0  errors=0  skipped=11
target/failsafe-reports/  classes=1   tests=4    failures=0  errors=0  skipped=0
```

- Phase 1 相关 `MemoryStateMachineTest` 19/19、`MemoryControllerTest` 3/3、
  `MemorySourceConcurrencyTest` 3/3，均在 Surefire 中通过。
- `MemoryStateMachineConcurrencyIT` 4/4 在 Failsafe 中通过。
- 11 条 skip 只有 `BackendApplicationTests` 1 条（永久 `@Disabled`）与
  `AnalysisMqIntegrationTest` 10 条（Docker 不可见，显式 skip）。
- Agent 门控全量 **415 passed / 0 skipped / 1 xfailed**（历史声明；本轮无原始产物）。

**默认模式对照（2026-11-07 19:56 实测）**：`mvn -B test` 为 Surefire
**335 / 0 / 0 / 88**（247 passed），Failsafe 不执行；Agent 默认模式为
**408 passed / 7 skipped / 1 xfailed**（原始产物在 `docs/audit/artifacts/`）。
因此上面的 335/11 只能称为“门控全量”，不能称为默认 `mvn test` 的结果。

---

## 5. skip 数量对比（Phase 1B 初版历史）

> 本节是 Phase 1B 初版的历史对比，**不是最终单次全量**。最终门控全量为
> Surefire **335 / 0 / 0 / 11** + Failsafe **4 / 0 / 0 / 0**；skip 从历史 73 降至 11，
> 原因是门控全量显式开启了所有可执行的门控，而不是删除了测试。
> 默认模式下同一套 Surefire 为 **335 / 0 / 0 / 88**（247 passed），Failsafe 不执行；
> 两种模式不能混用，详见 [`TEST_EVIDENCE_MODES.md`](TEST_EVIDENCE_MODES.md)。

| 指标 | 改造前基线 | 改造后 | 差值 |
| --- | --- | --- | --- |
| Tests run | 255 | 277 | +22（全部为新增 Memory 测试） |
| Failures | 0 | 0 | 0 |
| Errors | 0 | 0 | 0 |
| Skipped | 73 | 73 | **0** |

新增 22 条测试的 skip 数：**0**。没有新增 skip，因此无需逐条解释新增 skip；既有 73 条 skip 的分布逐类一致：

| 跳过类 | 改造前 | 改造后 | 原因（未变） |
| --- | ---: | ---: | --- |
| `BackendApplicationTests` | 1 | 1 | Spring 上下文集成测试门控 |
| `Phase2MqProcessorIntegrationTest` | 5 | 5 | 需要 RabbitMQ / 环境门控 |
| `Phase2RabbitMqBrokerTest` | 5 | 5 | 需要 RabbitMQ / 环境门控 |
| `InternalIdentityHttpIntegrationTest` | 5 | 5 | 需要真实 HTTP / 内部身份环境 |
| `SecurityContextIntegrationTest` | 19 | 19 | 需要 Spring Security 集成环境 |
| `ChatSseIntegrationTest` | 8 | 8 | 需要 SSE / 集成环境 |
| `ChatSseOrderingProbeTest` | 1 | 1 | 集成探针环境 |
| `DocumentIndexE2EIntegrationTest` | 1 | 1 | 需要真实 MySQL / 索引环境 |
| `DocumentIndexServiceIntegrationTest` | 15 | 15 | 需要真实 MySQL |
| `DocumentServiceIntegrationTest` | 7 | 7 | 需要真实 MySQL |
| `Phase2RealStateIntegrationTest` | 6 | 6 | 需要真实 MySQL（`AILS_PHASE2_IT=true`） |
| **合计** | **73** | **73** | — |

说明：Phase 1B 初版没有把 T14/T15/T21 写成真实 MySQL 集成测试，以避免新增门控 skip；
2026-11-07 集成验证轮新增 `MemoryStateMachineConcurrencyIT`（4 条），使用 `AILS_MEMORY_IT`
门控并**显式执行**，结果 4/4 PASS（§4.4）。最终证据链修复后，该类由 Failsafe 在
`mvn clean verify` 中执行，计入 **4 条 / 0 skip**，不再被 Surefire 默认 include 排除。
**门控全量模式**下仍是 **11 skipped**（`BackendApplicationTests` 1 + `AnalysisMqIntegrationTest` 10），
没有因为 Phase 1 新增测试而增加 skip，也没有削弱原有 22 条单测。
**默认模式**下另有 77 条环境门控测试计入 skip（合计 88），见
[`TEST_EVIDENCE_MODES.md`](TEST_EVIDENCE_MODES.md)。

---

## 6. PATCH 拒绝归档记忆的影响面

### 6.1 生产代码调用方

| 调用方 | 路径 | 是否受影响 |
| --- | --- | --- |
| HTTP 客户端 | `PATCH /api/memories/{id}` → `MemoryController.update` → `MemoryService.update` | **是**：对 `ARCHIVED` 记忆的任何 PATCH 现在返回 409 |
| Controller 其他方法 | `GET` / `POST` / `DELETE` | 否；`DELETE` 对已归档改为幂等成功 |
| Agent 后端写入 | `AgentAnalysisWriteService.reconcileMemories` → `MemoryService.reconcileCandidate` | 否；不走 `update`，且只写 CANDIDATE |
| Agent 内部 HTTP | `InternalServiceAuthenticationFilter` 只建立 `GET` 身份 | 否；Agent 无法调用 PATCH/DELETE |
| Python Agent 检索 | `agent/app/retriever/http_backend.py` 只调用 `GET /api/memories/search` | 否；只读 |
| Flutter | 仓库内未发现调用 `/api/memories` 的代码 | 否 |

### 6.2 文档 / 使用指南中的调用说明

以下文档描述了 PATCH 更新记忆，但没有说明“归档记忆整体拒绝”，属于文档债务，本轮未改：

| 文档 | 位置 | 现状 |
| --- | --- | --- |
| `backend/backend/POSTMAN_API_GUIDE.md` | §12.5 更新记忆 | 仍写“可选传 `status` 推进生命周期”，未提归档 409 |
| `backend/backend/README.md` | `:574` | 建议用 PATCH 更新，未提归档 409 |
| `docs/database&API_design.md` | `:2356` | 建议用 PATCH 更新，未提归档 409 |
| `docs/audit/PROJECT_BASELINE.md` | 缺陷 4.17 | 已记录原缺陷，本轮修复后应视为已关闭（该文档未在本轮修改） |

### 6.3 兼容性结论

- 仓库内没有自动化客户端 PATCH 归档记忆，因此没有已知的代码级破坏。
- 外部调用方如果曾依赖“PATCH 复活归档记忆”，现在会收到 409；这是有意的生命周期收紧。若产品需要恢复归档，应新增显式恢复动作（设计文档建议 `POST /api/memories/{id}/restore`），不要塞回通用 PATCH。本轮不实现。
- 成功响应结构未变；仅新增 409 错误响应，属于兼容性收紧而非破坏性结构变更。

---

## 7. 未覆盖场景与已知限制

1. **真实 MySQL 并发已补验（T14/T15/T13/T21），仍有下列未覆盖边界**
   - 2026-11-07 集成验证轮新增 `MemoryStateMachineConcurrencyIT`（4 条），在真实 MySQL 8.0.37
     上执行 **4/4 PASS**；断言直接读取数据库最终 `status / content / version / updated_at`，
     并观察到真实条件更新返回 `Updates: 0`。详见 §4.4。
   - 仍未覆盖：原 T14 的“确认 vs 归档”精确交错（新 IT 按本轮验证规格覆盖“合法 PATCH ACTIVE
     vs 非法 PATCH CANDIDATE”）；真实 MySQL 下的双用户 HTTP 归属场景（T10/T11 仍是服务层单测）；
     用户 `create` 路径；`PATCH` 归档记忆 + `status=ARCHIVED` 的精确文案。
   - T21 的真实竞争结果取决于线程交错；测试断言的是不变量（不会出现“ACTIVE 却保留 Agent 内容”、
     版本与成功写入次数一致），而不是每次强制 Agent 进入 `SKIPPED`。本次 T21 的交错是
     Agent 先提交、用户后读新版本再提交（Agent `UPDATED`，最终版本 2、用户内容）；
     同一次类运行中 T15 的第二个线程得到 `Updates: 0`，证明真实条件更新冲突路径被触发
     （HTTP 409）。
   - `Phase2RealStateIntegrationTest` 6 条真实 MySQL 用例本轮未执行（不属于本轮指定测试清单），
     其覆盖不计入本次结论。

2. **跨用户用例是服务层单测，不是双用户 HTTP 集成**
   - T10/T11 直接构造不同 `userId` 的 Memory / 让 `ownedSpace` 抛 404，验证的是 `ownedMemory` / `ownedSpace` 既有逻辑；未走真实 JWT、过滤器、HTTP 层。
   - 归属校验代码本轮未改，风险较低，但“跨用户 404”的端到端证据仍来自既有集成测试（本轮被跳过）而非新测试。

3. **用户创建路径没有新增无门控测试**
   - `create` 从直接 `setStatus(ACTIVE)` 改为 `persist → applyStatusTransition(ACTIVE)`。矩阵分支与 T3 相同（`null` 视为 CANDIDATE，CANDIDATE→ACTIVE 允许），代码审查可确认最终仍为 ACTIVE；但 22 条新测试中没有直接调用 `MemoryService.create`。
   - 既有 `Phase2RealStateIntegrationTest.B5` 覆盖“用户 Memory 直接 ACTIVE，Agent 候选只能 CANDIDATE”，
     但本轮未执行该测试类，因此用户 `create` 路径仍没有直接的真实库断言。

4. **`PATCH` 归档记忆 + `status=ARCHIVED` 的精确文案未单测**
   - 当前行为：`applyStatusTransition` 允许同态，随后 `requireEditable(ARCHIVED)` 抛 409 `"已归档的记忆不可修改"`。符合设计 §5.1 “PATCH 对归档记忆整体拒绝（包括字段编辑）”，但错误文案不是 `"ARCHIVED → ARCHIVED"`。
   - T8 覆盖了“归档 + 不带 status”，未覆盖“归档 + status=ARCHIVED”。这是文案层面的小缺口，不影响 409 和“不写库”。

5. **错误优先级：状态校验先于作用域归属校验**
   - `update` 在 `ownedMemory` 之后先做状态迁移校验，再做 `resolveScope` / `ownedSpace`。如果请求同时包含“非法状态迁移”和“他人的 spaceId”，会先返回 409 而不是 404。
   - 这不造成跨用户数据泄露（Memory 本身属于当前用户），但与“跨用户一律 404”的严格表述存在顺序差异。T11 使用的是合法状态迁移，因此仍为 404。若后续要求 404 绝对优先，可调整校验顺序；本轮未改。

6. **没有状态变更历史 / 审计日志**
   - 设计明确排除，本轮未实现。409 和最终状态可观测，但无法追溯“谁在何时把状态从 A 改到 B”。

7. **四个明确排除的相邻问题仍未处理**
   - scope / memoryKey 身份迁移竞态：未改。
   - `MemoryRequest` 不是真正的 PATCH DTO（`memoryType` / `content` 仍为必填）：未改。
   - 没有向量召回：未改。
   - 没有状态历史：未改。

8. **并发冲突的错误识别依赖 HTTP 409 + message**
   - 409 是稳定契约；message 文案可能随产品措辞调整。外部客户端应判断 `code=409`，不要依赖 `"非法的记忆状态转换"` 的精确文本。
   - 乐观锁冲突与非法迁移都返回 409，当前通过 message 区分（“记忆已被其他请求修改” vs “非法的记忆状态转换”）。如果客户端需要机器可读的细分原因，后续可增加错误子码；本轮未改响应结构。

9. **数据库层仍允许直接写任意合法 ENUM**
   - `status ENUM` 只保证不出现第四种值，不约束迁移方向；绕过应用层直接 UPDATE 仍可复活归档记忆。本轮依靠应用层唯一入口，未加数据库触发器或权限限制（设计排除）。

---

## 8. 回滚方法

本轮没有提交 Git、没有数据库迁移、没有数据修复，回滚是纯源码操作。

### 8.1 回滚生产代码

```bash
cd D:/learningMatiarials/AI-Learning-Space

git checkout -- \
  backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java \
  backend/backend/src/main/java/com/ails/backend/service/MemoryService.java
```

### 8.2 回滚新增测试

新增测试是未跟踪文件；回滚生产代码后它们会失败，需要一并移除（或保留在独立分支）：

```bash
rm backend/backend/src/test/java/com/ails/backend/service/MemoryStateMachineTest.java
rm backend/backend/src/test/java/com/ails/backend/controller/MemoryControllerTest.java
```

### 8.3 回滚审计文档（可选）

```bash
rm docs/audit/PHASE1_MEMORY_FIX.md
```

### 8.4 验证回滚

```bash
cd backend/backend
./mvnw -B clean test
```

预期回到 Phase 1B 基线：**Tests run: 255, Failures: 0, Errors: 0, Skipped: 73**。
（当前工作区已包含 Phase 2/3 与 Failsafe 配置；**门控全量模式**为
Surefire 335 / 0 / 0 / 11 + Failsafe 4 / 0 / 0 / 0；**默认模式**为
Surefire 335 / 0 / 0 / 88，Failsafe 不执行。回滚 Phase 2/3 后才会回到 255。）

### 8.5 如果未来已经提交

若后续把本轮改动提交到 Git，回滚应使用：

```bash
git revert <phase1b-commit>
```

而不是 `git checkout --`，以避免覆盖后续提交。

### 8.6 数据影响

- 无 schema 变更，无需回滚 DDL。
- 无数据迁移；已存在的 `CANDIDATE` / `ACTIVE` / `ARCHIVED` 数据在回滚后仍合法。
- 回滚后旧的 `update` 会重新接受任意 `status`，归档记忆可再次被 PATCH 复活；这是回滚的已知副作用。

---

## 附录 A：验证环境

- OS：Windows 11，Git Bash / PowerShell 5.1
- JDK：17
- Spring Boot：3.3.5
- MyBatis-Plus：3.5.7
- Maven Surefire：3.2.5
- Phase 1B 初版环境未启动 MySQL、RabbitMQ、Docker；相关集成测试按既有门控跳过（73 条）。
- 2026-11-07 门控全量运行启动了真实 MySQL 8.0.37（`127.0.0.1:3306`）、Qdrant
  （`ails-qdrant`，`6333`）、RabbitMQ（`ails-rabbitmq`，`5672`）与 FastAPI Agent（`8010`）；
  `mvn clean verify` 结果为 Surefire **335 / 0 / 0 / 11** + Failsafe
  **4 / 0 / 0 / 0**，`MemoryStateMachineConcurrencyIT` 4/4 PASS 且有独立 XML。
  同日默认模式 `mvn -B test` 实测为 Surefire **335 / 0 / 0 / 88**（247 passed），
  Failsafe 不执行。
- 本地运行日志（未提交）：`/tmp/phase1b/phase1b-baseline.log`、`phase1b-red.log`、`phase1b-green.log`、`phase1b-full.log`、`phase1b-agent.log`、`phase1b-agent-pytest.log`。

## 附录 B：本轮未做事项

- 未自动提交 Git。
- 未进入 Phase 2。
- 未处理四个明确排除项（scope/memoryKey 竞态、PATCH DTO、向量召回、状态历史）。
- 未修改既有 API 成功响应结构。
- 未新增表结构、未新增并发机制、未引入工作流引擎。
- 未修改 `MemoryController`、`MemoryRequest`、`Memory` 实体、`MemoryMapper`、`schema.sql`、`MybatisPlusConfig`、Agent 代码、Flutter 代码。
