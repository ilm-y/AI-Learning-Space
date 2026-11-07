# Phase 2 — Agent 向量化接口与同步通道实施报告

- 实施日期：2026-11-07
- 分支：`main`
- 基线提交：`058d417 feat(memory): 使用 MySQL 全文索引 (FULLTEXT) 替代 LIKE 进行关键词检索`
- 设计依据：[`docs/design/MEMORY_HYBRID_SEARCH_DESIGN.md`](../design/MEMORY_HYBRID_SEARCH_DESIGN.md) §4.4、§5.3
- 本轮范围：Python Agent 的记忆同步 / 检索接口 + Qdrant 存储 + Spring `AgentMemoryClient`
- **未做**：Outbox Relay、RRF、`MemoryService` 主流程改造、HybridSearch 端点

---

## 0. 结论摘要

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| `/agent/memories/sync` 支持 UPSERT / DELETE | 通过 | `test_memories_api.py` 17 条 |
| UPSERT 写入 Qdrant，payload 含 `memory_id/user_id/model/dimension/content_hash` | 通过 | 同上 + 真实 Qdrant IT |
| DELETE 按 `memory_id` 删除且幂等 | 通过 | 同上 |
| `/agent/memories/search` 按 `user_id` 过滤，只返回 id / score | 通过 | 同上；响应不含 `content` |
| 内部鉴权复用现有 token + 签名身份 | 通过 | 401 / 403 / 503 用例 |
| Spring `AgentMemoryClient` 请求体与异常处理 | 通过 | `AgentMemoryClientTest` 11 条 |
| 真实 Qdrant 集成测试保留在门控中 | 通过 | `AILS_QDRANT_IT=true`：430 passed |
| 默认测试不被真实依赖污染 | 通过 | 默认 Agent：425 passed / 8 skipped；后端 Failsafe 12 skipped |
| 未修改记忆检索主流程 / 未写 Relay / 未提交 Git | 通过 | `git status` 仅本轮文件 |

---

## 1. 接口契约

### 1.1 鉴权

两个路由都依赖 `require_internal_user`（`agent/app/internal_auth.py`）：

| 请求头 | 作用 | 缺失时 |
| --- | --- | --- |
| `X-Internal-Token` | 证明调用方是受信内部服务 | 503（未配置）/ 401（不匹配） |
| `X-Internal-Identity` | Spring 用 HMAC 签发的短期用户声明 | 503（未配置）/ 401（无效） |
| `X-Internal-User-Id` | 交叉核对，不单独构成身份 | 缺失可接受，不一致 403 |

请求体里的 `userId` 必须等于签名身份中的 `sub`，否则返回 403。共享 token
泄露不能冒充任意用户。

### 1.2 `POST /agent/memories/sync`

请求字段（camelCase；同时接受 snake_case）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `eventId` | 否 | 仅用于日志串联 |
| `operation` | 是 | `UPSERT` / `DELETE`；兼容字段名 `action`，两者只能出现一个 |
| `memoryId` | 是 | 正整数，决定确定性 point id |
| `userId` | 是 | 必须与签名身份一致 |
| `content` | UPSERT 必填 | 正文；不会写入 Qdrant payload |
| `contentHash` | 否 | 缺省时 Agent 计算 SHA-256 |
| `memoryVersion` | 否 | 写入 payload，便于排查索引落后 |
| `scopeType` | 否 | `GLOBAL` / `SPACE` / `CONCEPT`；缺省按可用字段推导 |
| `learningSpaceId` | 否 | SPACE/CONCEPT 必填 |
| `conceptId` | 否 | CONCEPT 必填 |
| `memoryType` | 否 | 写入 payload |
| `importance` | 否 | `[0,1]`，写入 payload |
| `status` | 否 | 默认 `ACTIVE`；检索只召回 `ACTIVE` |

示例：

```json
{
  "eventId": 9001,
  "operation": "UPSERT",
  "memoryId": 42,
  "userId": 7,
  "content": "用户偏好用递归解释分治",
  "contentHash": "9f2c...",
  "memoryVersion": 3,
  "scopeType": "SPACE",
  "learningSpaceId": 9,
  "memoryType": "LEARNING",
  "importance": 0.8,
  "status": "ACTIVE"
}
```

响应：

```json
{
  "code": 0,
  "data": {
    "pointId": "0b1e...",
    "collection": "ails_memories_hash-v1_256",
    "skipped": false
  }
}
```

- `skipped=true`：payload 的全部过滤字段与 `contentHash` 都未变化，没有重新嵌入；
- DELETE 时 `pointId` 为 `null`，且**不会创建 collection**。

### 1.3 `POST /agent/memories/search`

请求：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `requestId` | 否 | 日志串联 |
| `userId` | 是 | 强制过滤 |
| `spaceId` | 否 | 为空时不做作用域过滤（与旧 `MemoryService.search` 一致） |
| `conceptId` | 否 | 需要同时给 `spaceId` |
| `query` | 是 | 1–2000 字符，去空白后不能为空 |
| `recallK` | 否 | 默认取 `AILS_MEMORY_SEARCH_TOP_K`（20），上限 200 |

响应：

```json
{
  "code": 0,
  "data": {
    "collection": "ails_memories_hash-v1_256",
    "warnings": [],
    "hits": [
      {"memoryId": 42, "score": 0.87, "contentHash": "9f2c..."}
    ]
  }
}
```

**响应里没有正文**。Spring 必须回 MySQL 复核 `status='ACTIVE'` 与可见性。

### 1.4 错误语义

| 场景 | HTTP / code | Spring 侧分类 |
| --- | --- | --- |
| 缺 token / 身份无效 | 401 | 永久失败 |
| `userId` 与签名身份不一致 | 403 | 永久失败 |
| 请求字段不合法 | 422 | 永久失败 |
| Embedding 失败 | 502 / 503 | 可重试 |
| Qdrant 失败 / 超时 | 503 / 504 | 可重试 |
| 响应契约漂移 | 502 | **永久失败**（重试不会改字段名） |
| collection 未建立（检索） | 200 + `warnings` | 正常空结果 |

---

## 2. Agent 侧实现

### 2.1 新增 / 修改文件

| 文件 | 作用 |
| --- | --- |
| `agent/app/memories/__init__.py` | 模块入口 |
| `agent/app/memories/errors.py` | 带 HTTP 状态码的显式错误 |
| `agent/app/memories/schemas.py` | `MemorySyncRequest` / `MemorySearchRequest` |
| `agent/app/memories/indexer.py` | `MemoryIndexer`：hash → embedding → upsert / delete |
| `agent/app/memories/search.py` | `MemoryVectorSearch`：query → embedding → 检索 + 指纹过滤 |
| `agent/app/memories/routes.py` | 两个内部路由 |
| `agent/app/vectorstore/memory_store.py` | `QdrantMemoryStore`、`point_id_for_memory` |
| `agent/app/vectorstore/qdrant_store.py` | 抽出 `assert_vector_config` / `ensure_payload_indexes`，文档与记忆共用（行为不变） |
| `agent/app/config.py` | `memory_collection_prefix`、`memory_search_top_k`、`memory_search_min_score`、`memory_search_timeout_seconds` |
| `agent/app/main.py` | lifespan 构建存储与索引器，挂载路由，关闭连接 |

### 2.2 Collection、point、payload

```text
collection = <memory_collection_prefix>_<model>_<dimension>
例如       ails_memories_hash-v1_256
           ails_memories_text-embedding-v4_1024
```

- point id 由 UUIDv5 根据 `ails-memory:<memoryId>` 确定性生成；
- 单条 upsert，`wait=True`；
- 删除按 `memory_id` 过滤，`wait=True`，collection/point 不存在时视为成功。

payload：

```json
{
  "memory_id": 42,
  "user_id": 7,
  "status": "ACTIVE",
  "scope_type": "SPACE",
  "space_id": 9,
  "concept_id": null,
  "memory_type": "LEARNING",
  "importance": 0.8,
  "content_hash": "9f2c...",
  "memory_version": 3,
  "embedding_fingerprint": "v1:sha256(...)"
}
```

- **`content` 不写入 payload**；
- payload 索引：`memory_id`、`user_id`、`status`、`scope_type`、`space_id`、`concept_id`；
- 维度或距离函数（COSINE）不一致时拒绝写入。

### 2.3 检索过滤

```text
must:   user_id = ? AND status = 'ACTIVE'
should (仅当 space_id 非空):
        scope_type = 'GLOBAL'
        OR (scope_type = 'SPACE'   AND space_id = ?)
        OR (scope_type = 'CONCEPT' AND space_id = ? AND concept_id = ?)
```

- `space_id` 为空时 `should=None`（不过滤作用域），不传空数组；
- 命中后校验 `embedding_fingerprint`，不匹配的候选丢弃并给出 warning；
- `min_score` 默认 0，不做阈值过滤（RRF 只用排名）；
- collection 不存在时返回空列表 + warning，不在检索路径隐式建集合；
- 检索总时限由 `memory_search_timeout_seconds`（默认 2s）约束。

---

## 3. Spring Boot 侧实现

### 3.1 新增 / 修改文件

| 文件 | 作用 |
| --- | --- |
| `backend/.../service/AgentMemoryClient.java` | `sync` / `search` 内部 HTTP 客户端 |
| `backend/.../service/AgentMemoryException.java` | 状态码 + 永久/可重试分类 |
| `backend/.../dto/memory/agent/MemorySyncRequest.java` | 同步请求 |
| `backend/.../dto/memory/agent/MemorySyncResponse.java` | 同步响应 |
| `backend/.../dto/memory/agent/MemoryVectorSearchRequest.java` | 检索请求 |
| `backend/.../dto/memory/agent/MemoryVectorHit.java` | 检索命中（无正文） |
| `backend/.../src/main/resources/application.yml` | 新增两个超时配置 |
| `backend/.../src/test/java/.../AgentMemoryClientTest.java` | 11 条契约测试 |

### 3.2 鉴权与超时

- 复用 `ails.internal.token` 与 `InternalIdentitySigner.sign(userId)`；
- 请求头：`X-Internal-Token`、`X-Internal-Identity`、`X-Internal-User-Id`；
- 未配置 token / 身份密钥时 fail closed（503），不降级成匿名调用；
- HTTP/1.1、禁止重定向，与现有 `AgentDocumentClient` 一致；
- 新增配置：

```yaml
ails:
  agent:
    memory-sync-timeout-ms: ${AILS_AGENT_MEMORY_SYNC_TIMEOUT_MS:5000}
    memory-search-timeout-ms: ${AILS_AGENT_MEMORY_SEARCH_TIMEOUT_MS:2000}
```

### 3.3 异常分类

| 情况 | 状态码 | `isPermanent()` |
| --- | ---: | --- |
| 请求参数不合法 | 422 | true |
| Agent 返回 401 / 403 / 422 | 原样 | true |
| Agent 返回 408 / 429 | 原样 | false |
| Agent 返回 5xx | 原样 | false |
| 连接失败 / 超时 | 503 / 504 | false |
| 响应 JSON 非法、缺 `data`/`hits`、字段类型错误 | 502 | **true**（契约漂移） |

本轮**没有**把客户端接入 `MemoryService`，也没有实现 Outbox Relay；
`AgentMemoryClient` 目前只被测试使用。

---

## 4. 测试结果

### 4.1 Python Agent

| 模式 | 命令 | 结果 | 原始产物 |
| --- | --- | --- | --- |
| 默认 | `cd agent && python -m pytest -q -rs` | **425 passed, 8 skipped, 1 xfailed** | [`agent-pytest-memory-phase2-20261107T2113.txt`](artifacts/agent-pytest-memory-phase2-20261107T2113.txt) |
| Qdrant 门控 | `AILS_QDRANT_IT=true AILS_QDRANT_URL=http://127.0.0.1:6333 python -m pytest -q -rs` | **430 passed, 3 skipped, 1 xfailed** | [`agent-pytest-memory-phase2-gated-20261107T2113.txt`](artifacts/agent-pytest-memory-phase2-gated-20261107T2113.txt) |

新增用例：

- `tests/test_memories_api.py`：**17 passed**。覆盖 UPSERT payload、`action`/`operation` 别名、
  跳过重嵌入、DELETE 幂等、用户隔离、鉴权失败、Embedding/Store 失败、指纹过滤、空查询。
- `tests/test_memory_qdrant_integration.py`：**1 passed**（仅门控）。覆盖真实 collection
  建索引、payload 索引、用户/状态/作用域过滤、覆盖写、重复删除。

默认模式新增 1 条 skip 就是真实 Qdrant 用例，符合“不污染默认测试”的要求。
门控模式剩余的 3 条 skip 是既有的知识 E2E（1）与 RabbitMQ（2）。

### 4.2 Spring Boot

| 模式 | 命令 | 结果 | 原始产物 |
| --- | --- | --- | --- |
| 默认 | `cd backend/backend && ./mvnw -B clean verify` | **BUILD SUCCESS**；Surefire 346 / 0 / 0 / **88 skipped**；Failsafe 12 / 0 / 0 / **12 skipped** | [`maven-memory-phase2-default-20261107T2113.txt`](artifacts/maven-memory-phase2-default-20261107T2113.txt) |
| Memory IT 门控 | `AILS_MEMORY_IT=true AILS_IT_MYSQL_PASSWORD=*** ./mvnw -B clean verify` | **BUILD SUCCESS**；Surefire 346 / 0 / 0 / 88 skipped；Failsafe **12 / 0 / 0 / 0** | [`maven-memory-phase2-gated-20261107T2113.txt`](artifacts/maven-memory-phase2-gated-20261107T2113.txt) |

- 新增 `AgentMemoryClientTest`：**11 / 11 通过**（默认模式即执行）；
- 门控 Failsafe 仍为 Phase 1 的 12 条：`MemoryFulltextSearchIT` 8 + `MemoryStateMachineConcurrencyIT` 4，
  证明本轮没有回归 Phase 1；
- 默认模式的 88 个 Surefire skip 与 Phase 1 完全一致，均为既有环境门控。

### 4.3 HTTP 端到端冒烟（真实进程 + 真实 Qdrant）

以 `uvicorn` 启动 Agent（Hash Embedding、8 维、临时 collection 前缀），用签名身份调用：

```text
sync   → code=0, pointId=f6ef1c34-..., skipped=false
search → hits=[{memoryId: 501, score: -0.658..., contentHash: 0bf677...}]
delete → code=0, pointId=null
search → hits=[]
```

验证了 FastAPI lifespan、路由、鉴权、Qdrant 写入/过滤/删除的完整链路。
冒烟结束已删除临时 collection。Hash 向量分数可以为负，属于余弦点积的正常范围；
默认 `min_score=0` 不做阈值过滤，因此仍然返回。

---

## 5. 契约决策与偏差说明

1. **`operation` vs `action`**：设计文档使用 `operation`，本轮需求文字使用 `action`。
   Agent 两个都接受且只能出现一个，Spring 客户端发送 `operation`。两条契约都可工作。
2. **`status`**：设计文档只在 Qdrant payload 中出现；本轮同步请求也接受 `status`，
   默认 `ACTIVE`。检索固定过滤 `ACTIVE`。
3. **`scopeType` 缺省推导**：最小请求只要求 `memoryId / userId / content / operation`。
   缺 `scopeType` 时按 `conceptId → CONCEPT`、`learningSpaceId → SPACE`、否则 `GLOBAL`
   推导。Spring Relay 应显式传入，避免依赖推导。
4. **`contentHash` 缺省计算**：调用方未提供时，Agent 计算 SHA-256。生产链路应由
   Spring 提供，便于回源比对。
5. **契约错误按永久失败**：响应缺字段/类型错误抛 502 但 `isPermanent()=true`，
   与设计 R15“契约错误进入 DEAD”一致。
6. **Qdrant 存储签名**：设计给出的 `upsert_memory(model, dimension, ...)` 在本轮实现为
   `upsert_memory(collection_name, ...)`，collection 由 `ensure_collection` 显式返回。
   语义不变，避免每次写入重复计算/校验 collection。

---

## 6. 明确未做

- 未实现 `OutboxRelayWorker`、事件表读写、重试/租约/DEAD；
- 未修改 `MemoryService.search` 或任何检索主流程；
- 未实现 `HybridSearchService`、RRF、回源复核；
- 未新增 Spring 对外端点；
- 未提交 Git。

## 7. 如何复现

```bash
# Agent 单元测试（不需要 Qdrant）
cd agent && python -m pytest -q -rs

# 真实 Qdrant 集成测试
AILS_QDRANT_IT=true AILS_QDRANT_URL=http://127.0.0.1:6333 \
  python -m pytest tests/test_memory_qdrant_integration.py -v

# 后端默认测试
cd backend/backend && ./mvnw -B clean verify

# 后端 Memory IT（Phase 1 回归 + 本轮客户端）
AILS_MEMORY_IT=true AILS_IT_MYSQL_PASSWORD=*** ./mvnw -B clean verify
```
