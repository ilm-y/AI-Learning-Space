# Memory 最小状态机修复设计

- 审查对象：`D:\learningMatiarials\AI-Learning-Space`
- 分支 / 提交：`main` / `4732e40`
- 审查日期：2026-11-07
- 本轮性质：**只读审查 + 设计**。未修改任何业务代码，未提交 Git。
- 交付物：本文件 `docs/design/MEMORY_MINIMAL_FIX.md`

---

## 0. 结论摘要

**此前审计发现的问题仍然存在，且可以在 HTTP 层直接复现。**

`PATCH /api/memories/{id}` 会把请求体里的 `status` 原样写进数据库，`MemoryService.update` 没有任何状态迁移校验：

```java
// MemoryService.java:408-410
if (request.status() != null) {
    memory.setStatus(request.status());
}
```

因此以下请求都会成功：

| 请求 | 当前结果 | 违反的业务语义 |
| --- | --- | --- |
| `ARCHIVED` 记忆 `status=ACTIVE` | 复活为 `ACTIVE` | 归档是用户决定，不能被普通更新复活 |
| `ACTIVE` 记忆 `status=CANDIDATE` | 降级为候选 | 已确认的记忆被降级，且立刻从 Agent 检索中消失 |
| `CANDIDATE` 记忆 `status=ARCHIVED` | 归档 | 结果本身合法，但绕过了专用归档入口 |
| `ARCHIVED` 记忆 `status=CANDIDATE` | 复活为候选 | 归档状态被绕过 |

**Agent 自动写入路径本身是安全的**：`reconcileCandidate` 只会写 `CANDIDATE`，且拒绝更新非 `CANDIDATE` 记忆；内部服务凭据也只允许 `GET`，Agent 无法调用 `PATCH/DELETE`。真正的问题集中在用户侧的通用 `update`。

**最小修复方向**：不引入工作流引擎，只在 `MemoryStatus` + `MemoryService` 增加一张 3×3 转换矩阵和一个统一的状态迁移方法；数据库、接口路径、请求字段都不变。

---

## 1. 当前实现

### 1.1 分层与调用链

```text
HTTP 层
  POST   /api/memories              MemoryController.create   → MemoryService.create
  PATCH  /api/memories/{id}         MemoryController.update   → MemoryService.update      ← 问题入口
  DELETE /api/memories/{id}         MemoryController.archive  → MemoryService.archive
  GET    /api/memories              MemoryController.list     → MemoryService.list
  GET    /api/memories/search       MemoryController.search   → MemoryService.search
  GET    /api/memories/{id}         MemoryController.get      → MemoryService.get

Agent 写入链
  MQ 分析结果 → AgentAnalysisWriteService.reconcileMemories
              → MemoryService.reconcileCandidate
              → insertCandidate(CANDIDATE) / updateCandidate(content only)

持久化
  MemoryMapper extends BaseMapper<Memory>   （无自定义 SQL）
  MyBatis-Plus OptimisticLockerInnerInterceptor
  MySQL memory 表（status ENUM + version INT）
```

关键代码：

| 层 | 文件 | 说明 |
| --- | --- | --- |
| Controller | `backend/backend/src/main/java/com/ails/backend/controller/MemoryController.java:67-79` | `PATCH` 注释写“传 status 推进生命周期”，但只做参数绑定 |
| Service | `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:389-424` | `update` 直接写 `status`；`archive` 只写 `ARCHIVED` |
| Service（Agent） | `MemoryService.java:137-206` | `reconcileCandidate` / `insertCandidate` / `updateCandidate` |
| Repository | `backend/backend/src/main/java/com/ails/backend/mapper/MemoryMapper.java:6` | 仅继承 `BaseMapper`，无手写 SQL |
| Entity | `backend/backend/src/main/java/com/ails/backend/entity/Memory.java:69-81` | `status` 字段 + `@Version` |
| DTO | `backend/backend/src/main/java/com/ails/backend/dto/memory/MemoryRequest.java:43-44` | `status` 由客户端传入 |
| Enum | `backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java:9-13` | 只有三个枚举值，没有转换规则 |
| DDL | `backend/backend/src/main/resources/schema.sql:306-332` | `status ENUM(...) NOT NULL DEFAULT 'CANDIDATE'`、`version`、`uk_memory_identity` |
| 乐观锁 | `backend/backend/src/main/java/com/ails/backend/config/MybatisPlusConfig.java:28-34` | 已注册 `OptimisticLockerInnerInterceptor` |
| Agent 接线 | `backend/backend/src/main/java/com/ails/backend/service/AgentAnalysisWriteService.java:339-387` | 分析结果逐条调用 `reconcileCandidate` |

### 1.2 所有会改变 `memory.status` 的代码点

| # | 入口 | 代码位置 | 写入值 | 是否有转换校验 | 性质 |
| --- | --- | --- | --- | --- | --- |
| 1 | `create` | `MemoryService.java:77-80` | `ACTIVE` | 固定值，无 | 初始状态 |
| 2 | `createCandidate` | `MemoryService.java:92-102` | `CANDIDATE` | 固定值，无 | 初始状态 |
| 3 | `persist` | `MemoryService.java:478-498` | 由调用方传入 | 调用方固定 | 创建公共路径 |
| 4 | `insertCandidate` | `MemoryService.java:164-189` | `CANDIDATE` | 固定值，无 | 初始状态 |
| 5 | `updateCandidate` | `MemoryService.java:191-206` | **不写 `status`** | 上游已限定 `CANDIDATE` | Agent 更新内容 |
| 6 | `update` | `MemoryService.java:389-413` | **`request.status()` 任意值** | **无** | **缺陷入口** |
| 7 | `archive` | `MemoryService.java:419-424` | `ARCHIVED` | 固定值，无（不检查原状态） | 归档 |
| 8 | 数据库默认值 | `schema.sql:316` | `CANDIDATE` | 仅约束合法枚举 | 裸 INSERT 兜底 |

结论：应用层共有 **6 个状态写值点**（1/2/4/6/7 是直接写，5 不写，3 是 1/2 的公共实现），其中 **只有第 6 个接受外部输入**，且没有任何校验。

### 1.3 现有并发保护

- `Memory.version` 带 `@Version`，且 `MybatisPlusConfig` 已注册 `OptimisticLockerInnerInterceptor`。
- `update` / `archive` 都通过 `memoryMapper.updateById(memory)` 写入，再用 `requireUpdated(...)` 检查影响行数；版本不匹配时返回 0，服务抛 `BusinessException.conflict`（409）。
- `updateCandidate` 在影响行数为 0 时返回 `SKIPPED`，刻意不抛异常，避免整份分析事务回滚（`MemoryService.java:197-203`）。

所以**并发丢失更新已经被乐观锁挡住**，缺的是“状态迁移是否合法”的判断。

### 1.4 现有业务规则的实际载体

| 规则 | 载体 | 是否强制执行 |
| --- | --- | --- |
| `CANDIDATE → ACTIVE → ARCHIVED` | `MemoryStatus.java:3-8`、`MemoryService.java:38-43`、`Memory.java:22-24` | 仅注释 |
| Agent 推断只能停在 `CANDIDATE` | `MemoryService.java:131-133`、`MemoryCandidateDraft.java:8-24` | **是**（代码固定） |
| 归档不物理删除 | `MemoryService.java:415-424`、`MemoryController.java:74-79` | 是 |
| 用户确认后升 `ACTIVE` | `MemoryController.java:67`、`POSTMAN_API_GUIDE.md:12.5` | 否（`update` 接受任意值） |
| 更新走乐观锁 | `docs/database&API_design.md` §三十、`Memory.java:73-81` | 是 |

**当前没有明确、可执行的状态转换规则；规则只存在于注释和文档里。**

---

## 2. 确认存在的问题

逐条回答审查要求中的 7 个问题。

| # | 问题 | 结论 | 证据 / 说明 |
| --- | --- | --- | --- |
| 1 | 普通 `update` 是否可以直接修改 `status`？ | **是** | `MemoryService.java:408-410` 直接 `setStatus(request.status())`，随后 `:411` `updateById` 落库 |
| 2 | 是否存在多个状态修改入口？ | **是** | 见 §1.2，应用层 6 个写值点；用户侧手动入口 2 个（`PATCH`、`DELETE`），Agent 侧 3 个 |
| 3 | Agent 是否可以绕过正常状态转换规则？ | **否** | `reconcileCandidate` 只写 `CANDIDATE`，非 `CANDIDATE` 一律 `SKIPPED`（`:150-155`）；`updateCandidate` 不写 `status`（`:191-206`）；内部凭据只建立 `GET` 身份（`InternalServiceAuthenticationFilter.java:82-100`），Agent 无法调用 `PATCH/DELETE`。但 `createCandidate` 是 `public`，且 `MemoryService.java:87` 的注释已陈旧（声称“当前还没有 Agent 往这里写”） |
| 4 | 是否存在并发更新导致状态覆盖？ | **状态覆盖不会发生**，但并发重复请求会得到 409 | `@Version` + `requireUpdated` 保证丢失更新返回 409；`updateCandidate` 版本冲突返回 `SKIPPED`。没有“状态 CAS”，但乐观锁已足够防止静默覆盖 |
| 5 | 是否存在不同用户之间的 Memory 越权修改？ | **未发现** | `ownedMemory` 先按 id 查再比对 `userId`，非本人一律 404（`MemoryService.java:552-558`）；改挂到别人的空间 / 知识点会走 `ownedSpace` / `accessibleConcept`，同样 404（`:448-462`、`LearningSpaceService.java:85-90`、`ConceptService.java:136-143`） |
| 6 | 是否存在重复请求导致状态异常？ | **顺序重复不会异常；并发重复会 409** | 同状态 `PATCH` 当前会再次写入；顺序重复 `DELETE` 会再次写 `ARCHIVED`。并发重复由版本号挡成 409。没有幂等语义定义 |
| 7 | 当前状态转换是否有明确的业务规则？ | **没有可执行的规则** | 只有注释 / 文档；枚举无 `canTransitionTo`，Service 无转换校验。`MemoryController.java:67` 的“CANDIDATE → ACTIVE”只是注释 |

### 2.1 缺陷的可复现路径

前置条件：当前用户拥有 id=42 的 Memory，且其 `status=ARCHIVED`。

```http
PATCH /api/memories/42
Content-Type: application/json

{
  "memoryType": "LEARNING",
  "content": "用户已掌握虚拟内存基本概念",
  "status": "ACTIVE"
}
```

执行结果：

1. `MemoryController.update` 把请求交给 `MemoryService.update`（`MemoryController.java:68-72`）。
2. `ownedMemory` 确认归属（`:391`）。
3. `resolveScope` / `requireIdentityFree` / `applyScope` 正常执行（`:396-404`）。
4. `memory.setContent(...)`、`memory.setStatus(ACTIVE)`（`:405-410`）。
5. `memoryMapper.updateById(memory)` 成功（版本号匹配时），返回 `ACTIVE`（`:411-412`）。

数据库最终状态：`ARCHIVED → ACTIVE`，且该记忆立即重新进入 `GET /api/memories/search` 的结果（检索只查 `ACTIVE`，`MemoryService.java:311`）。

换成 `"status": "CANDIDATE"` 则相反：已确认记忆被降级，并从 Agent 检索结果中**静默消失**。

### 2.2 影响面

| 影响 | 说明 |
| --- | --- |
| 归档语义失效 | 用户“删除 / 归档”的记忆可以被普通更新复活，等于没有真正退役 |
| 确认语义失效 | 已确认的 `ACTIVE` 记忆可被降级为 `CANDIDATE`，前端 / Agent 看到的长期上下文会无提示地减少 |
| 检索结果不可预测 | `search` 只查 `ACTIVE`（`:311`），状态被任意改写会直接改变 Agent 上下文 |
| 审计困难 | 没有状态变更历史，事后无法区分“正常确认”和“绕过更新” |

---

## 3. 代码证据

### 3.1 缺陷本体

`backend/backend/src/main/java/com/ails/backend/service/MemoryService.java:389-413`：

```java
@Transactional
public MemoryResponse update(Long userId, Long memoryId, MemoryRequest request) {
    Memory memory = ownedMemory(userId, memoryId);

    Long spaceId = request.learningSpaceId() != null ? request.learningSpaceId() : memory.getLearningSpaceId();
    Long conceptId = request.conceptId() != null ? request.conceptId() : memory.getConceptId();
    Scope scope = resolveScope(userId, spaceId, conceptId);
    String memoryKey = StringUtils.hasText(request.memoryKey())
            ? request.memoryKey().trim()
            : memory.getMemoryKey();

    requireIdentityFree(userId, scope, memoryKey, memoryId);
    applyScope(memory, scope, memoryKey);
    memory.setMemoryType(request.memoryType());
    memory.setContent(request.content().trim());
    memory.setImportance(request.importance());
    if (request.status() != null) {
        memory.setStatus(request.status());      // ← 任意值直接写入
    }
    requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
    return MemoryResponse.from(memory);
}
```

`backend/backend/src/main/java/com/ails/backend/dto/memory/MemoryRequest.java:43-44`：

```java
/** 仅更新时生效；创建时忽略，固定为 ACTIVE */
MemoryStatus status) {
```

`backend/backend/src/main/java/com/ails/backend/controller/MemoryController.java:67-72`：

```java
/** 更新内容，或传 status 推进生命周期（CANDIDATE → ACTIVE） */
@PatchMapping("/{id}")
public ApiResponse<MemoryResponse> update(@PathVariable Long id,
                                          @Valid @RequestBody MemoryRequest request) {
    return ApiResponse.ok(memoryService.update(SecurityUtil.currentUserId(), id, request));
}
```

注释承诺“推进生命周期”，实现没有校验。

### 3.2 归档入口没有转换校验

`MemoryService.java:419-424`：

```java
@Transactional
public void archive(Long userId, Long memoryId) {
    Memory memory = ownedMemory(userId, memoryId);
    memory.setStatus(MemoryStatus.ARCHIVED);
    requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
}
```

- `CANDIDATE → ARCHIVED`：允许（用户拒绝候选），合理。
- `ACTIVE → ARCHIVED`：允许，合理。
- `ARCHIVED → ARCHIVED`：再次写入，顺序重复请求不会报错，但会多一次 UPDATE。
- 没有“归档是终态”的显式判断。

### 3.3 Agent 写入路径是封闭的

`MemoryService.java:137-162`：

```java
public ReconcileOutcome reconcileCandidate(...) {
    ...
    Memory existing = findByScopeAndKey(userId, scope.scopeKey(), memoryKey);
    if (existing != null) {
        return draft.action() == MemoryReconcileAction.UPDATE
                && existing.getStatus() == MemoryStatus.CANDIDATE
                ? updateCandidate(existing, draft, sourceType, sourceId)
                : ReconcileOutcome.SKIPPED;
    }
    if (draft.action() == MemoryReconcileAction.UPDATE) {
        return ReconcileOutcome.SKIPPED;
    }
    return insertCandidate(userId, scope, memoryKey, draft, sourceType, sourceId);
}
```

- `UPDATE` 只有在目标状态是 `CANDIDATE` 时才执行。
- `insertCandidate` 固定 `CANDIDATE`（`:172`）。
- `updateCandidate` 只改 `memoryType/content/importance`，不改 `status`（`:191-206`）。
- `MemoryCandidateDraft` 根本没有 `status` 字段（`MemoryCandidateDraft.java:25-33`），Agent 无法表达状态变更。

内部鉴权边界（`InternalServiceAuthenticationFilter.java:82-100`）：

```java
boolean readOnly = "GET".equalsIgnoreCase(request.getMethod());
if (readOnly && ...) {
    // 建立内部用户上下文
}
```

`POST/PUT/PATCH/DELETE` 不会由内部凭据建立身份，Agent 没有用户 JWT，因此不能调用 Memory 的写接口。

### 3.4 并发保护确实存在

`Memory.java:73-81`：

```java
@Version
private Integer version;
```

`MybatisPlusConfig.java:28-34`：

```java
@Bean
public MybatisPlusInterceptor mybatisPlusInterceptor() {
    MybatisPlusInterceptor interceptor = new MybatisPlusInterceptor();
    interceptor.addInnerInterceptor(new OptimisticLockerInnerInterceptor());
    interceptor.addInnerInterceptor(new PaginationInnerInterceptor(DbType.MYSQL));
    return interceptor;
}
```

`MemoryService.java:546-550`：

```java
private static void requireUpdated(int rows, String message) {
    if (rows == 0) {
        throw BusinessException.conflict(message);
    }
}
```

### 3.5 数据库只能约束“值”，不能约束“转换”

`schema.sql:306-332`：

```sql
`status` ENUM ('CANDIDATE', 'ACTIVE', 'ARCHIVED') NOT NULL DEFAULT 'CANDIDATE',
`version` INT NOT NULL DEFAULT 0,
UNIQUE KEY `uk_memory_identity` (`user_id`, `scope_key`, `memory_key`),
```

ENUM 只能保证不出现第四种值；它无法知道上一行的状态，因此 `ARCHIVED → ACTIVE` 在数据库层是合法 UPDATE。状态转换必须在应用层校验。

### 3.6 归属校验证据

`MemoryService.java:552-558`：

```java
private Memory ownedMemory(Long userId, Long memoryId) {
    Memory memory = memoryMapper.selectById(memoryId);
    if (memory == null || !Objects.equals(memory.getUserId(), userId)) {
        throw BusinessException.notFound("记忆不存在");
    }
    return memory;
}
```

`MemoryService.java:448-462` 的 `resolveScope` 对 `learningSpaceId` / `conceptId` 分别调用 `ownedSpace` / `accessibleConcept`，跨用户资源一律 404。

---

## 4. 最小修复方案

目标：**只修状态机，不改接口路径、不改数据库、不引入工作流引擎。**

### 4.1 状态含义

| 状态 | 含义 | 是否可被 Agent 检索 | 是否允许用户编辑内容 | 是否允许离开该状态 |
| --- | --- | --- | --- | --- |
| `CANDIDATE` | Agent 推断、等待用户确认的候选记忆 | 否（`search` 只查 `ACTIVE`） | 是（用户可改内容后再确认） | 是：`ACTIVE` / `ARCHIVED` |
| `ACTIVE` | 用户已确认、当前生效的长期上下文 | 是 | 是 | 是：`ARCHIVED` |
| `ARCHIVED` | 用户归档、已退役的记忆 | 否 | **否**（终态，不可编辑） | 否（终态） |

`ARCHIVED` 是终态：不物理删除，不自动复活，不允许通过通用更新改回 `CANDIDATE/ACTIVE`。

### 4.2 合法状态转换矩阵

| from \ to | `CANDIDATE` | `ACTIVE` | `ARCHIVED` |
| --- | --- | --- | --- |
| `CANDIDATE` | ✅ 同态（幂等，不改变状态） | ✅ 用户确认 | ✅ 用户拒绝 / 归档 |
| `ACTIVE` | ❌ 409 | ✅ 同态（幂等，不改变状态） | ✅ 用户归档 |
| `ARCHIVED` | ❌ 409 | ❌ 409 | ✅ 仅 `DELETE` 幂等无操作；`PATCH` 因终态不可编辑而 409 |

说明：

- **同态视为合法但无操作**，用于吸收重复请求（例如连续两次确认）。
- `DELETE` 对已归档记忆返回成功且不写库，保证 HTTP DELETE 的幂等语义。
- `PATCH` 对已归档记忆整体拒绝（包括字段编辑），保证终态不可变。
- 任何 `ARCHIVED → 非 ARCHIVED` 都是 409，这是本修复的核心。

状态图：

```text
                   PATCH status=ACTIVE
        CANDIDATE ─────────────────────▶ ACTIVE
            │                              │
            │ DELETE                     │ DELETE
            │ 或 PATCH status=ARCHIVED   │ 或 PATCH status=ARCHIVED
            ▼                              ▼
                 ARCHIVED（终态）
                     │
                     └── DELETE：幂等成功，不写库
                     └── 其它：409
```

### 4.3 非法状态转换处理

| 场景 | 处理 |
| --- | --- |
| `ARCHIVED → ACTIVE` / `ARCHIVED → CANDIDATE` | `BusinessException.conflict` → HTTP 409；不写库；事务回滚 |
| `ACTIVE → CANDIDATE` | 409；不写库 |
| 对 `ARCHIVED` 做任意字段更新 | 409；不写库 |
| 请求里的枚举值不是三个合法值之一 | Jackson 反序列化失败 → 现有 `GlobalExceptionHandler` 返回 400（`HttpMessageNotReadableException`） |
| 同态重复请求 | 200；`PATCH` 可继续更新内容，`DELETE` 直接无操作返回 |

错误信息建议包含 `from → to`，但**不要拼接 `content`**（避免把用户记忆内容写进日志 / 响应）：

```text
非法的记忆状态转换: ARCHIVED → ACTIVE
```

选择 409 而不是 400：请求体本身格式正确，冲突来自资源当前状态；这与现有 `requireUpdated` / 身份冲突的 409 语义一致。

### 4.4 普通字段更新的允许字段

| 字段 | 是否允许客户端更新 | 说明 |
| --- | --- | --- |
| `memoryType` | 是 | 当前 DTO 的 `@NotNull` 字段，PATCH 必须携带 |
| `content` | 是 | 当前 DTO 的 `@NotBlank` 字段，PATCH 必须携带 |
| `importance` | 是 | 可空 |
| `learningSpaceId` / `conceptId` | 是（维持现状） | 由 `resolveScope` 重新推导作用域并校验归属；本次不改身份迁移语义 |
| `memoryKey` | 是（维持现状） | 同样会触发 `requireIdentityFree`；本次不改 |
| `status` | **仅按转换矩阵** | 只允许 `CANDIDATE→ACTIVE`、`CANDIDATE→ARCHIVED`、`ACTIVE→ARCHIVED`、同态 |
| `userId` | 否 | 服务端从登录态取 |
| `scopeType` / `scopeKey` | 否 | 由两个外键推导 |
| `version` | 否 | 乐观锁字段 |
| `embeddingRef` | 否 | 向量引用，服务端维护 |
| `createdAt` / `updatedAt` | 否 | 自动填充 |

`ARCHIVED` 记忆：以上字段全部拒绝（409）。

> 注意：当前 `MemoryRequest` 同时用于创建与更新，`memoryType` / `content` 带 `@NotNull` / `@NotBlank`，所以 PATCH 实际上不是“只传 content 的局部更新”，而是必须带全这两个字段。本次不拆分 DTO，避免扩大改动面；拆分 `MemoryCreateRequest` / `MemoryUpdateRequest` 可作为后续独立改进。

### 4.5 状态转换的统一入口

不新增 Controller，不新增工作流引擎。在 `MemoryStatus` 放矩阵，在 `MemoryService` 放唯一迁移方法。

**`MemoryStatus.java`（新增）**：

```java
public boolean canTransitionTo(MemoryStatus target) {
    if (target == null) {
        return false;
    }
    if (this == target) {
        return true; // 同态：由调用方决定是否真的写库
    }
    return switch (this) {
        case CANDIDATE -> target == ACTIVE || target == ARCHIVED;
        case ACTIVE -> target == ARCHIVED;
        case ARCHIVED -> false;
    };
}
```

**`MemoryService.java`（新增两个私有方法）**：

```java
/** 已归档记忆是终态：不参与任何字段更新。 */
private static void requireEditable(Memory memory) {
    if (memory.getStatus() == MemoryStatus.ARCHIVED) {
        throw BusinessException.conflict("已归档的记忆不可修改");
    }
}

/** 运行时状态迁移的唯一入口；初始化状态不走这里。 */
private static void applyStatusTransition(Memory memory, MemoryStatus target) {
    MemoryStatus current = memory.getStatus();
    if (!current.canTransitionTo(target)) {
        throw BusinessException.conflict(
                "非法的记忆状态转换: " + current + " → " + target);
    }
    memory.setStatus(target);
}
```

**`update` 的改法**（保持其余逻辑不变）：

```java
Memory memory = ownedMemory(userId, memoryId);
requireEditable(memory);
if (request.status() != null) {
    applyStatusTransition(memory, request.status());
}
// ...原有 scope / identity / 字段赋值逻辑...
requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
```

**`archive` 的改法**：

```java
Memory memory = ownedMemory(userId, memoryId);
if (memory.getStatus() == MemoryStatus.ARCHIVED) {
    return; // DELETE 幂等：已经归档，不重复写库
}
applyStatusTransition(memory, MemoryStatus.ARCHIVED);
requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
```

**创建路径**：`create` / `createCandidate` / `insertCandidate` 设置的是**初始状态**，不是迁移，仍可直接 `setStatus`；建议在方法上注明“初始状态，不经过转换矩阵”。这样“唯一入口”只约束运行时迁移，不会为了形式统一把创建逻辑也绕一圈。

**约束**：`Memory.setStatus` 由 Lombok `@Data` 生成，无法在编译期禁止其他类调用。最小方案靠代码约定 + 测试 + 注释；如果后续要强约束，可加 ArchUnit 规则，限制 `setStatus` 只在 `MemoryService` 中出现，但不在本轮范围。

### 4.6 并发控制方案

继续使用现有乐观锁，不新增悲观锁。

```text
T1: SELECT memory (status=S, version=V)
T1: 校验 S → target 合法
T2: 另一个请求先提交，version 变成 V+1
T1: UPDATE ... WHERE id=? AND version=V  → 影响 0 行
T1: requireUpdated(0) → 409
```

好处：

- **不会基于过期状态提交迁移**：版本冲突直接失败，而不是把 `ARCHIVED` 当成 `CANDIDATE` 去确认。
- **不会丢失更新**：内容与状态在同一个 `updateById` 里提交，要么都成功，要么都失败。
- **Agent 路径不回滚分析**：`updateCandidate` 继续把版本冲突转成 `SKIPPED`（`:197-203`），不抛异常。

幂等语义：

| 场景 | 行为 |
| --- | --- |
| 顺序重复 `PATCH status=ACTIVE` | 第二次读到 `ACTIVE`，同态合法，可继续更新内容，最终仍 `ACTIVE` |
| 顺序重复 `DELETE` | 第二次读到 `ARCHIVED`，直接返回成功，不写库 |
| 并发重复 `PATCH` / `DELETE` | 一个成功，另一个版本冲突 409；客户端重试后按顺序幂等处理 |
| 并发“确认” vs “归档” | 二者都基于 `CANDIDATE`；先提交者赢，后者 409；最终状态不是 `ACTIVE` 就是 `ARCHIVED`，不会出现中间态 |

不采用“版本冲突后自动重读并返回成功”的原因：`update` 同时携带内容，自动吞掉冲突会掩盖内容写入失败；让调用方刷新重试更诚实。

### 4.7 Agent 写入权限边界

| 边界 | 规则 | 现有实现 |
| --- | --- | --- |
| Agent 能写什么状态 | 只能写 `CANDIDATE` | `insertCandidate` 固定 `:172`；`createCandidate` 固定 `:95` |
| Agent 能更新什么 | 只能更新 `CANDIDATE` 的 `content/type/importance` | `reconcileCandidate` 状态判断 `:150-155`；`updateCandidate` 不碰 `status` `:191-206` |
| Agent 能改状态吗 | 不能 | `MemoryCandidateDraft` 无 `status` 字段 |
| Agent 能调用 PATCH/DELETE 吗 | 不能 | 内部凭据只建立 `GET` 身份 `InternalServiceAuthenticationFilter.java:82-100` |
| 版本冲突时 | 放弃本次写入，不回滚整份分析 | `updateCandidate` 返回 `SKIPPED` `:197-203` |
| `ACTIVE` / `ARCHIVED` 被 Agent 重放 | 跳过，不覆盖、不复活 | `:150-155` |

建议在修复时补一条注释，明确 `createCandidate` 仅供 Agent / 内部调用，不在 Controller 暴露；并修正 `MemoryService.java:87` 的陈旧注释（它声称“当前还没有 Agent 往这里写”，与 `AgentAnalysisWriteService.java:361` 的调用事实相反）。

---

## 5. 状态转换矩阵（汇总）

### 5.1 状态 × 操作

| 当前状态 | `PATCH` 改内容 | `PATCH status=ACTIVE` | `PATCH status=ARCHIVED` | `PATCH status=CANDIDATE` | `DELETE` |
| --- | --- | --- | --- | --- | --- |
| `CANDIDATE` | ✅ | ✅ → `ACTIVE` | ✅ → `ARCHIVED` | ✅ 同态，无变化 | ✅ → `ARCHIVED` |
| `ACTIVE` | ✅ | ✅ 同态，无变化 | ✅ → `ARCHIVED` | ❌ 409 | ✅ → `ARCHIVED` |
| `ARCHIVED` | ❌ 409 | ❌ 409 | ❌ 409 | ❌ 409 | ✅ 幂等成功，不写库 |

### 5.2 迁移合法性（不含同态）

| from | to | 允许 | 触发方式 |
| --- | --- | --- | --- |
| `CANDIDATE` | `ACTIVE` | 是 | `PATCH status=ACTIVE` |
| `CANDIDATE` | `ARCHIVED` | 是 | `PATCH status=ARCHIVED` 或 `DELETE` |
| `ACTIVE` | `ARCHIVED` | 是 | `PATCH status=ARCHIVED` 或 `DELETE` |
| `ACTIVE` | `CANDIDATE` | 否 | — |
| `ARCHIVED` | `ACTIVE` | 否 | — |
| `ARCHIVED` | `CANDIDATE` | 否 | — |

### 5.3 非法转换的错误契约

| 项 | 值 |
| --- | --- |
| HTTP 状态 | `409 Conflict` |
| 响应体 | `ApiResponse.fail(409, "非法的记忆状态转换: {from} → {to}")` |
| 数据库 | 不写入；`version` 不变；事务不产生半更新 |
| 是否自动修复 | 否；不自动升 / 降级，不自动归档 |

---

## 6. 修改文件清单

> 以下是**建议改动**，本轮未执行。按最小改动排序。

| # | 文件 | 改动 | 类型 |
| --- | --- | --- | --- |
| 1 | `backend/backend/src/main/java/com/ails/backend/enums/MemoryStatus.java` | 新增 `canTransitionTo(MemoryStatus)`，承载 3×3 矩阵 | 业务代码 |
| 2 | `backend/backend/src/main/java/com/ails/backend/service/MemoryService.java` | 新增 `requireEditable` / `applyStatusTransition`；`update` 在写库前校验；`archive` 增加终态幂等短路；修正 `:87` 陈旧注释 | 业务代码 |
| 3 | `backend/backend/src/main/java/com/ails/backend/controller/MemoryController.java` | 仅更新 `update` / `archive` 的 Javadoc，写明 409 规则（无逻辑改动） | 注释 |
| 4 | `backend/backend/src/test/java/com/ails/backend/service/MemoryStateMachineTest.java` | **新增**：Mockito 单测，覆盖矩阵、404、跨用户、幂等 | 测试 |
| 5 | `backend/backend/src/test/java/com/ails/backend/service/Phase2RealStateIntegrationTest.java` | **扩展**：真实 MySQL 上的并发状态更新、版本号断言 | 测试 |
| 6 | `backend/backend/src/test/java/com/ails/backend/controller/MemoryControllerTest.java` | **新增**：MockMvc，验证 409 / 400 / 404 的 HTTP 映射 | 测试 |
| 7 | `backend/backend/src/test/java/com/ails/backend/service/MemorySourceConcurrencyTest.java` | **扩展**：Agent 路径不能改状态、版本冲突仍 `SKIPPED` | 测试 |
| 8 | `backend/backend/POSTMAN_API_GUIDE.md` §12.5 | 补充：`status` 只接受合法迁移；非法迁移 409；归档终态 | 文档 |
| 9 | `backend/backend/README.md` 记忆章节 | 同步生命周期规则（可选） | 文档 |
| 10 | `docs/database&API_design.md` | 在 §三十后补一张转换矩阵（可选） | 文档 |

**不需要改动**：

- `schema.sql` / 迁移脚本：`status` 仍是三值 ENUM，无需加列、加索引、加触发器。
- `MemoryRequest` / `MemoryResponse`：接口字段不变。
- `MemoryMapper`：继续使用 `BaseMapper` + 乐观锁插件。
- `AgentAnalysisWriteService`：无需改动，Agent 边界已经正确。
- `MybatisPlusConfig`：乐观锁插件已注册。

---

## 7. 测试清单

### 7.1 必测项（对应审查要求）

| ID | 场景 | 前置状态 | 操作 | 预期 | 层级 |
| --- | --- | --- | --- | --- | --- |
| T1 | 普通 update 修改 status | `ARCHIVED` | `PATCH` 完整 body + `status=ACTIVE` | **409**；DB 仍 `ARCHIVED`；`version` 不变 | 单测 + Controller |
| T2 | 普通 update 修改 status | `ACTIVE` | `PATCH` + `status=CANDIDATE` | **409**；DB 仍 `ACTIVE` | 单测 + Controller |
| T3 | 合法转换：候选确认 | `CANDIDATE` | `PATCH` + `status=ACTIVE` | 200；DB `ACTIVE`；`version+1` | 单测 + 集成 |
| T4 | 合法转换：候选拒绝 | `CANDIDATE` | `DELETE` | 200；DB `ARCHIVED` | 单测 + 集成 |
| T5 | 合法转换：生效归档 | `ACTIVE` | `DELETE` | 200；DB `ARCHIVED` | 单测 + 集成 |
| T6 | 非法转换：归档降级 | `ARCHIVED` | `PATCH` + `status=CANDIDATE` | 409；DB 不变 | 单测 |
| T7 | 非法转换：生效降级 | `ACTIVE` | `PATCH` + `status=CANDIDATE` | 409；DB 不变 | 单测 |
| T8 | 归档记忆编辑内容 | `ARCHIVED` | `PATCH` 不带 status，只改 content | 409；DB 不变 | 单测 |
| T9 | 不存在的 Memory | — | `update` / `archive` 随机 id | 404 `记忆不存在` | 单测 |
| T10 | 跨用户更新 | 用户 B 的 Memory | 用户 A 调 `PATCH` / `DELETE` | 404；DB 不变 | 集成（两用户） |
| T11 | 跨用户挂载 | 用户 B 的 space/concept | 用户 A 在 `PATCH` 里填 B 的 id | 404；DB 不变 | 集成 |
| T12 | 重复 PATCH 同状态 | `ACTIVE` | 连续两次 `PATCH status=ACTIVE` | 两次 200；最终 `ACTIVE`；无非法中间态 | 单测 |
| T13 | 重复 DELETE | `ACTIVE` | 连续两次 `DELETE` | 两次 200；第一次写库，第二次无写入；最终 `ARCHIVED` | 单测 + 集成 |
| T14 | 并发确认 / 归档 | `CANDIDATE` | 两线程同时 `PATCH status=ACTIVE` / `DELETE` | 恰好一个成功、一个 409；最终状态是 `ACTIVE` 或 `ARCHIVED`；`version` 只 +1 | 真实 MySQL 集成 |
| T15 | 并发同状态确认 | `CANDIDATE` | 两线程同时 `PATCH status=ACTIVE` | 一个 200、一个 409；最终 `ACTIVE`；不会变成 `CANDIDATE` | 真实 MySQL 集成 |
| T16 | Agent ADD | 不存在 | `reconcileCandidate(ADD)` | `ADDED`；DB `CANDIDATE` | 单测 |
| T17 | Agent UPDATE 候选 | `CANDIDATE` | `reconcileCandidate(UPDATE)` | `UPDATED`；内容变；状态仍 `CANDIDATE` | 单测 |
| T18 | Agent UPDATE 生效 | `ACTIVE` | `reconcileCandidate(UPDATE)` | `SKIPPED`；内容 / 状态均不变 | 单测 |
| T19 | Agent UPDATE 归档 | `ARCHIVED` | `reconcileCandidate(UPDATE)` | `SKIPPED`；不复活 | 单测 |
| T20 | Agent 创建忽略 status | — | `createCandidate` 的 `MemoryRequest.status=ACTIVE` | DB 仍 `CANDIDATE` | 单测 |
| T21 | Agent 并发更新遇到用户确认 | `CANDIDATE` | 用户先确认，Agent 再 `updateById` | Agent `SKIPPED`；DB `ACTIVE` | 真实 MySQL 集成 |
| T22 | 非法枚举值 | — | `PATCH` body `status="RESURRECTED"` | 400；DB 不变 | Controller |

### 7.2 测试实现建议

**单测（Mockito，无需数据库）**：新增 `MemoryStateMachineTest`

- 构造 `MemoryService(memoryMapper, memorySourceMapper, spaceService, conceptService)`。
- `ownedMemory` 走 `memoryMapper.selectById`；GLOBAL 作用域不会调用 `spaceService/conceptService`。
- `requireIdentityFree` 走 `memoryMapper.selectCount`，默认返回 0。
- `updateById` 默认返回 1；模拟版本冲突时返回 0。
- 断言 `BusinessException.getCode() == 409/404`；用 `verify(memoryMapper, never()).updateById(any())` 证明非法迁移不写库。

**Controller 单测（根据 `DocumentControllerTest` 的风格）**：新增 `MemoryControllerTest`

- `MockMvcBuilders.standaloneSetup(new MemoryController(memoryService))`
  `.setControllerAdvice(new GlobalExceptionHandler())`。
- 直接向 `SecurityContextHolder` 写入 `AuthUser`，模拟 JWT 已认证。
- 覆盖 409 / 404 / 400 的 JSON `code` 与 HTTP status。

**真实 MySQL 集成测试**：扩展 `Phase2RealStateIntegrationTest`

- 该测试已使用真实 `MemoryService` + `JdbcTemplate`，并在 `@BeforeEach` 清理 `memory` / `memory_source`。
- 并发用例用 `ExecutorService` + `CountDownLatch` 同时发起两个请求，断言“一成一败”。
- 直接查 `SELECT status, version FROM memory WHERE id=?` 验证最终状态与版本号。
- 默认由 `AILS_PHASE2_IT=true` 门控；本地不设置时跳过，与现有集成测试一致。

**Agent 路径测试**：扩展现有 `MemorySourceConcurrencyTest` 或新建 `MemoryAgentBoundaryTest`

- 复用 `MemoryService` + Mockito；重点断言 `updateCandidate` 在 `existing.status != CANDIDATE` 时不被调用。
- `createCandidate` 传入 `status=ACTIVE` 的请求，断言落库前 `setStatus(CANDIDATE)`。

**回归命令**：

```bash
# 纯单测
cd backend/backend && mvn -B test

# 真实 MySQL 状态机 / 并发
cd backend/backend && AILS_PHASE2_IT=true AILS_IT_MYSQL_PASSWORD=... \
  mvn -B -Dtest=Phase2RealStateIntegrationTest test
```

### 7.3 修复前的“红测试”

建议先只加 T1 / T2 两条测试、不改业务代码，确认它们**失败**：

- 当前 T1 会得到 200 且 DB 变 `ACTIVE`，不是 409。
- 当前 T2 会得到 200 且 DB 变 `CANDIDATE`。

这能证明测试确实覆盖了本次缺陷，而不是在修复后才“补一条永远通过的断言”。

---

## 8. 兼容性和回滚风险

### 8.1 兼容性

| 维度 | 影响 | 说明 |
| --- | --- | --- |
| HTTP 路径 / 方法 | 不变 | `GET/POST/PATCH/DELETE /api/memories` 全部保留 |
| 请求 / 响应字段 | 不变 | `MemoryRequest.status` 仍在；不新增必填字段 |
| 合法请求 | 行为不变 | `CANDIDATE→ACTIVE`、`ACTIVE→ARCHIVED`、重复确认仍成功 |
| 非法请求 | **200 → 409** | 这是本次修复的预期行为变化；调用方若依赖“复活归档”会失败 |
| 数据库结构 | 不变 | 无迁移、无新表、无新列 |
| Agent 写入 | 不变 | 继续只能写 `CANDIDATE` |
| 现有测试 | 预期全部通过 | `Phase2RealStateIntegrationTest.memoryLifecyclePersistsRealStatus` 不涉及非法迁移；`MemorySourceConcurrencyTest` 不涉及用户 update |
| 前端 | 无已知业务前端 | Flutter 仍是脚手架；Postman 示例需同步说明 409 |
| 监控 / 日志 | 轻微 | 非法迁移会多出 409；如需可加一条 `warn`，只记录 `memoryId/userId/from/to`，不记录内容 |

### 8.2 需要明确接受的产品后果

**归档是终态，且 `uk_memory_identity` 包含归档行。** 这意味着：

```text
用户归档了 (user=1, scope=GLOBAL, memoryKey=profile)
→ 该身份永久存在 ARCHIVED 行
→ 再次 POST 同 memoryKey 会命中唯一约束，返回 409
→ 本次修复不提供“恢复归档”或“物理删除”
```

最小方案下，重新记录同类偏好需要换一个 `memoryKey`。如果产品确实需要“恢复归档”，应当新增**显式的用户动作**（例如 `POST /api/memories/{id}/restore`），并为它单独定义规则；不要把它塞回通用 `PATCH`，否则“归档不可被普通更新复活”的边界又会模糊。

### 8.3 回滚

| 项 | 说明 |
| --- | --- |
| 回滚方式 | 直接 revert `MemoryStatus.java` / `MemoryService.java` 的小改动；无数据库迁移需要回滚 |
| 数据影响 | 修复本身不改变任何历史数据；已归档行保持归档 |
| 已发生的非法数据 | 无法自动识别。修复前通过 `PATCH` 复活 / 降级的行，`updated_at` 可能晚于确认时间，但没有状态变更历史，不能可靠反推；本次不做数据修复 |
| 测试回滚 | 新增测试可保留；删除测试不影响生产代码 |
| 风险等级 | 低：改动集中在一个 Service + 一个 Enum，接口和表结构不变 |

### 8.4 不在本次范围

以下问题与本缺陷相邻，但不属于“最小状态机修复”，建议单独立项：

1. **作用域 / `memoryKey` 在 `update` 中可改**：这是身份迁移，不是状态迁移。`requireIdentityFree` 是 check-then-act，并发下可能以 `DuplicateKeyException` 形式冒成 500，而不是 409。
2. **`MemoryRequest` 不是真正的 PATCH DTO**：`memoryType` / `content` 强制必填，无法只改 `importance` 或只改 `status`。拆分创建 / 更新 DTO 属于接口演进。
3. **Memory 向量召回**：`embedding_ref` 仍未接 Qdrant。
4. **状态变更审计**：没有 `memory_status_history` 表；如需追踪“谁在什么时候确认 / 归档”，应单独立项。
5. **`MemoryService.java:87` 陈旧注释**：建议随手修正，但它不是状态机缺陷。
6. **`MemorySourceConcurrencyTest` 之外的 Agent 端到端**：现有 MQ 集成测试已覆盖 CANDIDATE 落库，但未覆盖“用户确认与 Agent 并发”的真实数据库竞争。

---

## 附录：审查范围对照

| 审查要求 | 是否覆盖 | 证据位置 |
| --- | --- | --- |
| Memory Controller | 是 | `MemoryController.java:36-79` |
| Memory Service | 是 | `MemoryService.java:77-558` |
| Memory Repository | 是 | `MemoryMapper.java:6`（仅 `BaseMapper`，无自定义 SQL） |
| Memory Entity | 是 | `Memory.java:26-91` |
| Memory DTO | 是 | `MemoryRequest.java:21-45`、`MemoryResponse.java:17-39` |
| Memory 状态枚举 | 是 | `MemoryStatus.java:9-13` |
| Agent 自动写入 Memory 的调用路径 | 是 | `AgentAnalysisWriteService.java:339-387` → `MemoryService.java:137-206` |
| 所有涉及 Memory 状态修改的代码 | 是 | 全仓库 `setStatus` / `update memory` 检索；应用层 6 个写值点见 §1.2 |
| 追踪到最终数据库写入位置 | 是 | `memoryMapper.updateById/insert` → MyBatis-Plus → `schema.sql:306-332` |

本轮只读结论：**缺陷确认存在；修复方案为应用层 3×3 矩阵 + 乐观锁，不动数据库和接口。**
