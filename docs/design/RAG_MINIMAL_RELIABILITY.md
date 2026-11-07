# RAG 文档索引可靠性审查与最小修复设计

- 审查对象：`D:\learningMatiarials\AI-Learning-Space`
- 分支 / 提交：`main` / `81a983e`（审查时工作区仅有未跟踪的 `docs/audit/`、`docs/design/`）
- 审查日期：2026-11-07
- 本轮性质：**只读审查 + 设计**。未修改任何业务代码，未提交 Git，未连接 MySQL / Qdrant / Agent。
- 交付物：本文件 `docs/design/RAG_MINIMAL_RELIABILITY.md`

> **事实边界**：本文所有“当前实现”结论都来自逐文件阅读源码与 DDL，行号对应当前提交。
> 本轮没有重新执行测试，也没有启动真实依赖；PROJECT_BASELINE 中已有的测试数字只作为背景引用，
> 不作为本轮结论的证据。

---

## 0. 结论摘要

### 0.1 一句话结论

当前 RAG 主链路（上传 → 解析 → 切块 → Embedding → Qdrant → READY → 检索）**可以闭环**，
但索引生命周期只实现了“第一次成功”，没有实现“失败后可恢复、重建时不破坏旧版本、删除后能清理”。
其中最关键的三个事实是：

1. **MySQL chunk 的替换发生在 Qdrant 写入之前**，且是两个独立事务；中途失败时旧 chunk 已删、
   新 chunk 没有向量引用。
2. **Qdrant 侧同样是“先删旧 points，再分批 upsert”**；中途失败会留下“旧 points 已删、
   新 points 只写了一半”的状态。
3. **当前没有任何入口能重建一个 READY 文档**（`markProcessing` 只接受 `UPLOADING/FAILED`），
   所以上述破坏目前大多落在“本来就不参与检索的 FAILED 文档”上；一旦按直觉补一个“重建索引”按钮，
   它会立刻变成“正在服务的旧索引被摧毁”的生产事故。

因此推荐方案不是“直接加版本号”，而是：

> **A. 最小可靠方案：单活跃版本 + 分阶段构建 + 成功后原子切换 + 旧版本延迟清理。**
> 复用已有的 `document.index_version` 作为“活跃构建令牌”，给 `document_chunk` 增加同一个版本维度，
> 让 Qdrant point 的 ID 和 payload 也带版本；新版本构建期间旧版本继续服务，切换是一次条件 UPDATE；
> 失败时只清理新版本，旧版本原样保留。

完整版本化方案（B）在当前产品形态下**没有必要**：文档内容按 SHA-256 去重、没有“替换内容”入口、
单进程部署、单空间文档量很小，且没有历史版本查询需求。B 的成本会集中在“版本表 / 版本隔离 /
清理策略 / 巡检体系”上，而这些能力 A 已经用更小的代价覆盖了核心可靠性目标。

### 0.2 此前 7 条审计发现的逐条核对

| # | 原判断 | 本轮核对结果 | 精确事实 |
| --- | --- | --- | --- |
| 1 | `replaceChunks` 先删旧 chunks，再构建新索引 | **成立** | `DocumentIndexService.runIndex` 在 `:107` 调 `replaceChunks`，在 `:108` 才调 `agentClient.index`；`replaceChunks` 在 `:39-40` 先 `delete` 再逐条 `insert` |
| 2 | 新索引失败后旧索引已丢失 | **对“非服务中”的 chunk 成立；对“正在服务的 READY 索引”当前不可达** | `markProcessing` 只允许 `UPLOADING/FAILED → PROCESSING`（`DocumentIndexService.java:123-131`），READY 文档无法被重建；一旦新增 READY 重建入口，此条立刻成立 |
| 3 | 同内容文件重新上传被去重逻辑阻止 | **效果成立，但根因不是唯一键** | `findActive` 不筛状态（`DocumentService.java:168-173`），命中 FAILED/超时的 UPLOADING 时直接返回 `duplicate=true`，不会重新提交索引 |
| 4 | 文档删除接口或删除流程不完整 | **成立** | `DocumentController` 只有 upload/list/ready-ids/get（`:40-71`）；`DELETING/DELETED` 无任何写入方；`QdrantDocumentStore.delete_document` 只被测试调用（`qdrant_store.py:208`，全仓库无生产调用） |
| 5 | FAILED / UPLOADING 状态可能无法恢复 | **成立** | 无重试 / 重索引 API；`DocumentIndexRecoveryService` 只扫 `PROCESSING`（`DocumentMapper.java:65-78`）；进程在 insert 之后、`submit` 之前崩溃会永久停在 `UPLOADING` |
| 6 | Embedding 模型和维度变化可能导致旧向量被错误使用 | **部分成立，危险点与想象不同** | DB 只存 `embedding_model`（`Document.java:75`）；provider / dimension / 配置指纹都没存；ready-ids 只按模型名过滤（`DocumentMapper.java:47-49`）。同模型名换维度 → 旧文档仍进白名单但落到新 collection，表现为静默无结果；同模型名 + 同维度换 provider → 两套向量共用一个 collection，**会真的混用** |
| 7 | Qdrant 数据丢失缺少可靠检测 | **成立** | ready-ids 只在 MySQL 内校验 `chunk_count == vector_ref 数`（`DocumentMapper.java:40-46`）；`count_document_points` 只被测试使用（`qdrant_store.py:218`）；没有巡检、没有对账 |

### 0.3 推荐结论

| 问题 | 结论 |
| --- | --- |
| 选 A 还是 B | **选 A**。A 就能做到“失败不破坏旧索引、删除立即停止召回、失败可重试、配置变化可识别”；B 的额外收益在当前使用场景下几乎为零 |
| 是否需要 `index_version` | **需要，但只作为“活跃构建令牌”**，不需要独立的版本表 |
| 是否需要改 `document_chunk` | **需要**：增加 `index_version`，唯一键改为 `(document_id, index_version, chunk_index)`。这是“先构建、后切换”的最小代价 |
| 是否需要记录 provider/dimension/fingerprint | **需要**，而且检索阶段必须用 fingerprint 做过滤；只记录不校验等于没记 |
| 是否需要改 Qdrant collection 命名 | **不需要**。payload 里带 `doc_version` 与 `embedding_fingerprint`，检索按白名单 + 版本过滤即可；改命名会让所有旧数据失联 |
| 删除如何立即停止召回 | **先改状态为 `DELETING`**（ready-ids 只认真 `READY`），再异步删向量；状态切换是召回停止的唯一开关 |

---

## 1. 当前真实实现

### 1.1 分层与调用链

```text
HTTP 层（DocumentController.java:40-71）
  POST /api/spaces/{spaceId}/documents             upload
  GET  /api/spaces/{spaceId}/documents             list
  GET  /api/spaces/{spaceId}/documents/ready-ids   Agent 检索白名单
  GET  /api/documents/{id}                         detail
  （没有 DELETE / 没有 POST reindex）

Spring 写入链
  DocumentService.upload(:99-162)
    ├─ LearningSpaceService.ownedSpace
    ├─ DocumentStorageService.prepare / store       文件落盘
    ├─ findActive(spaceId, sha256)                  快速去重（不筛状态）
    ├─ INSERT document(status=UPLOADING)
    └─ DocumentIndexService.submit(documentId)
         ├─ markProcessing: UPDATE ... WHERE status IN ('UPLOADING','FAILED')
         └─ executor.execute(runIndex)

  DocumentIndexService.runIndex(:95-119)
    ├─ storageService.read
    ├─ POST /agent/documents/parse                  FastAPI 解析 + 切块
    ├─ DocumentChunkService.replaceChunks(:37-59)   ⚠ 独立事务：DELETE 旧块 + INSERT 新块
    ├─ POST /agent/documents/index                  FastAPI Embedding + Qdrant
    ├─ DocumentChunkService.updateVectorRefs(:65-77)⚠ 独立事务：逐条写 vector_ref
    └─ complete(:134-149)                           条件 UPDATE → READY, index_version+1

Agent 写入链（agent/app/documents/routes.py:66-136）
  POST /agent/documents/index
    ├─ provider.embed(texts)
    ├─ validate_vectors(count/dimension/finite)
    ├─ store.ensure_collection(model, dimension)
    └─ store.index_document
         ├─ Qdrant DELETE document_id 的全部 points   ⚠ 先删
         └─ 按 batch UPSERT（point_id = uuid5(docId, chunkIndex)）

Agent 读取链
  HttpBackendRetriever.get_knowledge(:144-183)
    ├─ GET ready-ids?embeddingModel=...
    └─ KnowledgeSearchService.search(:61-110)
         ├─ provider.embed([question])
         ├─ collection = prefix + model + dimension
         └─ store.search_knowledge(user_id, space_id, document_id IN allowed)
```

### 1.2 状态机：谁在写、写到哪

`document.status` 的枚举定义在 `DocumentStatus.java:15-20`：

```text
UPLOADING → PROCESSING → READY
                     ↘ FAILED
（DELETING / DELETED 只存在于枚举和迁移脚本，没有任何生产写入方）
```

| 状态 | 写入方 | 代码位置 | 条件 |
| --- | --- | --- | --- |
| `UPLOADING` | 上传 | `DocumentService.java:123` | 新记录固定值 |
| `PROCESSING` | `submit` 同步抢占 | `DocumentIndexService.java:123-131` | 仅 `UPLOADING/FAILED`，且 `deleted_at IS NULL` |
| `READY` | `complete` | `DocumentIndexService.java:134-149` | 仅 `PROCESSING`；同时写 `chunk_count/embedding_model/parser_version/indexed_at`，`index_version+1` |
| `FAILED` | `fail` / 线程池拒绝 / 超时回收 | `DocumentIndexService.java:151-162`、`:86-89`、`DocumentMapper.java:86-100` | 仅 `PROCESSING` |
| `DELETING` | **无** | — | 枚举存在，死代码 |
| `DELETED` | **无** | — | 枚举存在，死代码 |

**关键事实**：没有 `READY → 任何状态` 的条件更新。READY 文档一旦成功，除了软删（`deleted_at`）
之外没有别的状态出口。这既是当前“不会误伤服务中索引”的原因，也是“无法重建”的原因。

### 1.3 事务边界：哪些写入是原子的，哪些不是

| 步骤 | 事务范围 | 失败后已提交的副作用 |
| --- | --- | --- |
| 文件落盘 | 无（文件系统） | 失败时 `deleteQuietly` 清理；成功则文件存在 |
| `INSERT document` | 单条自动提交（`upload` 是 `NOT_SUPPORTED`） | 重复键时删文件；其他异常删文件 |
| `replaceChunks` | `@Transactional`（`DocumentChunkService.java:37`） | **已提交**：旧 chunk 已删，新 chunk 已插入，`vector_ref` 全为 NULL |
| `agentClient.index` | HTTP，无事务 | Agent 侧可能已删旧 points、已写部分新 points |
| `updateVectorRefs` | `@Transactional`（`:65-77`） | **已提交**：部分或全部 `vector_ref` 已写 |
| `complete` | 单条条件 UPDATE | 失败则文档停在 `PROCESSING`，由回收服务最终置 `FAILED` |

因此“部分成功”不是理论问题，而是当前代码的默认失败形态：

```text
MySQL:   旧 chunks 已删 / 新 chunks 存在 / vector_ref 可能为空
Qdrant:  旧 points 可能已删 / 新 points 可能只写了一半 / 或完全没有
状态:    PROCESSING（随后被回收成 FAILED）或 FAILED
文件:    始终存在
```

由于 ready-ids 要求 `status='READY'`，这些中间态**不会被检索到**。这是当前系统没有出现
“错误召回”的原因，但它换来的是“失败后没有可回退的旧索引”。

### 1.4 当前已经正确的部分（不要在修复中破坏）

| 机制 | 代码 | 为什么重要 |
| --- | --- | --- |
| 索引抢占 CAS | `DocumentIndexService.java:123-131` | 同一文档并发 submit 只有一个能进入 PROCESSING |
| 解析结果契约校验 | `:166-202` | 空块、序号不连续、负数 token 直接失败，不会写脏数据 |
| 索引响应契约校验 | `:222-262` | 向量数与 chunk 数不一致、未知/重复 chunkId、空 pointId 全部拒绝 |
| Qdrant point ID 确定性 | `qdrant_store.py:50-52` | 同一 `(document_id, chunk_index)` 重复索引是覆盖，不是追加 |
| 检索白名单 fail-closed | `http_backend.py:144-183`、`knowledge.py:61-110` | 白名单拿不到就降级，绝不退化成无过滤搜索 |
| 白名单完整性检查 | `DocumentMapper.java:28-56` | READY + `chunk_count == 有 vector_ref 的 chunk 数`，挡住一半脏数据 |
| 检索结果 payload 校验 | `knowledge.py:230-264` | 缺字段、白名单外 document_id、空内容都丢弃 |
| 依赖失败降级 | `knowledge.py:90-110` | Qdrant/Embedding 故障不会打挂对话 |

修复方案必须保留这些性质：条件更新、确定性 ID、契约校验、fail-closed。

---

## 2. 缺陷证据

按“会不会影响正在服务的索引 / 会不会导致永久不可恢复 / 是否会造成静默错误”排序。

### D1. chunk 替换先于 Qdrant 写入，失败后无法回退

**证据**

```java
// DocumentIndexService.java:107-110
List<DocumentChunk> saved = chunkService.replaceChunks(document, chunks);
AgentIndexResponse indexed = agentClient.index(document, indexRequest(document, saved));
Map<Long, String> refs = validateIndexResponse(saved, indexed);
chunkService.updateVectorRefs(documentId, refs);
```

```java
// DocumentChunkService.java:37-59
chunkMapper.delete(...documentId...);      // 旧块先删
for (AgentParsedChunk chunk : chunks) {    // 新块再插
    chunkMapper.insert(entity);
}
```

`replaceChunks` 是独立事务，方法返回时旧块已经物理删除。随后 `agentClient.index` 失败，
catch 只做 `fail(documentId, ...)`；MySQL 里留下的是“新块 + 空 vector_ref”，旧块不可恢复。

**触发条件**：Agent 不可用、Embedding 超时、Qdrant 写入失败、索引响应契约不合法。

**当前影响**：由于 READY 文档不能被重建，受损的只是 FAILED 文档的中间产物；用户看到的是
“重试又要重新解析”，不是“正在用的资料突然消失”。

**为什么仍然必须修**：一旦补上“重建 READY 文档”的入口（这是合理的产品需求），D1 立即变成
“重建失败 = 旧索引被删 = 该文档从检索中消失”。修复必须在加入重建入口之前或同时完成。

### D2. Qdrant 写入同样是“先删后写”

**证据**

```python
# agent/app/vectorstore/qdrant_store.py:169-174
# 先删除该文档的旧 points，再写入新版本
await self._client.delete(
    collection_name=collection_name,
    points_selector=qm.FilterSelector(filter=self._document_filter(document_id)),
    wait=True,
)
```

`index_document` 的异常包装成 `DocumentVectorStoreError`（`:205-206`），Spring 收到后置 FAILED。
如果失败发生在 delete 之后、upsert 完成之前，旧 points 已丢，新 points 不完整。
重试会再次 delete + upsert，最终可以收敛，但“旧索引”在失败窗口内不存在。

**与 D1 的区别**：D1 破坏 MySQL 的 chunk 行；D2 破坏 Qdrant 的向量。两者叠加时，
即使只修 D1，重建失败仍会让旧向量被删。

### D3. 同内容重新上传命中 FAILED 文档时不会重试

**证据**

```java
// DocumentService.java:105-108
Document existing = findActive(spaceId, prepared.sha256());
if (existing != null) {
    return new DocumentUploadResponse(DocumentResponse.from(existing), true);
}
```

```java
// DocumentService.java:168-173
private Document findActive(Long spaceId, String contentHash) {
    return documentMapper.selectOne(new LambdaQueryWrapper<Document>()
            .eq(Document::getLearningSpaceId, spaceId)
            .eq(Document::getContentHash, contentHash)
            .last("LIMIT 1"));
}
```

`findActive` 没有任何状态条件。以下场景都会返回“文件已存在”，且不会触发索引：

| 已存在记录状态 | 用户重新上传的预期 | 当前实际 |
| --- | --- | --- |
| `READY` | 已存在，直接可用 | 正确 |
| `PROCESSING` | 正在处理，稍后可用 | 正确（但不会提示进度） |
| `FAILED` | 重试索引 | **返回 duplicate，永远 FAILED** |
| `UPLOADING`（进程崩溃遗留） | 重新提交 | **返回 duplicate，永远 UPLOADING** |
| `DELETING` | 提示正在删除 | **返回 duplicate，状态为 DELETING** |

唯一键 `uk_doc_space_active_hash` 本身没有“阻止重传”；真正的问题是“命中后没有恢复动作”。

### D4. 删除能力不存在，`DELETING/DELETED` 是死状态

**证据**

- `DocumentController.java:40-71`：没有任何 `@DeleteMapping`。
- 全仓库检索 `DELETING` / `DELETED`：只有 `DocumentStatus.java:18-20`、`Document.java:84` 注释、
  迁移脚本和测试插入，没有生产写入。
- `QdrantDocumentStore.delete_document`（`qdrant_store.py:208-216`）只被
  `agent/tests/test_qdrant_store.py` 与 `agent/tests/test_qdrant_integration.py` 调用。
- `LearningSpaceService.delete`（`:77-80`）只软删空间行；`document`、`document_chunk`、文件、
  Qdrant points 全部保留。空间软删后 `ownedSpace` 查不到，所以 API 不可见，但向量与文件成为孤儿。

**影响**

1. 用户无法删除已上传资料；只能删整个空间（且不清理子资源）。
2. 即使未来加了删除接口，若只做 `documentMapper.deleteById`，`ready-ids` 会因 `deleted_at` 排除它，
   但 Qdrant points 和磁盘文件永久残留；同内容重新上传会创建新 document_id，旧 points 成为永久孤儿。
3. `DELETING/DELETED` 的存在说明设计意图是“先停召回、再异步清理”，但实现从未落地。

### D5. `UPLOADING` 没有回收路径

**证据**

- `DocumentIndexRecoveryService` 的查询只选 `status='PROCESSING'`（`DocumentMapper.java:65-78`）。
- `DocumentService.upload` 在 `:154-159` 吞掉 `indexService.submit` 的所有 RuntimeException：

```java
try {
    indexService.submit(document.getId());
} catch (RuntimeException e) {
    log.error("提交文档索引任务失败: documentId={}", document.getId(), e);
}
```

如果 `markProcessing` 本身因数据库闪断抛异常，或者进程在 `INSERT` 提交后、`submit` 调用前被杀，
文档会永久停在 `UPLOADING`。没有任何扫描器会再看它。

**用户可见后果**：列表里永远是“上传中”，重新上传同内容返回 duplicate，无法自愈。

### D6. `FAILED` 没有重试入口

**证据**

- `markProcessing` 的条件包含 `FAILED`（`DocumentIndexService.java:127`），说明状态机允许重试。
- 但全仓库没有第二个调用方：`submit` 只被 `DocumentService.upload` 调用。
- `DocumentIndexRecoveryService` 的错误文案写着“可重新提交索引”（`:76`），但没有任何 API 实现这句话。
- `index_attempts` 只在 `markProcessing` 自增（`:130`），没有第二次触发。

**结论**：`FAILED` 在当前产品里是终态；“可重新提交”是文档承诺，不是代码事实。

### D7. 超时回收与旧 worker 之间没有 fencing

**证据**

```java
// DocumentIndexService.java:123-131
int updated = documentMapper.update(null, new LambdaUpdateWrapper<Document>()
        .eq(Document::getId, documentId)
        .in(Document::getStatus, DocumentStatus.UPLOADING, DocumentStatus.FAILED)
        .set(Document::getStatus, DocumentStatus.PROCESSING)
        .setSql("index_attempts = index_attempts + 1"));
```

`runIndex` 在开始时 `selectById` 拿到文档，但之后**没有把 `index_attempts` 作为回归条件**。
`complete` / `fail` 只校验 `status='PROCESSING'`（`:138`、`:155`）。

**竞争序列**

```text
T0  索引线程 A 启动，markProcessing → PROCESSING，attempts=1
T1  A 卡在 Embedding / Qdrant（超过 15 分钟）
T2  回收服务把 PROCESSING → FAILED
T3  用户/新代码触发重试 B：FAILED → PROCESSING，attempts=2
T4  A 恢复，调用 complete：条件只有 status='PROCESSING' → 更新成功！
    A 用旧结果把文档置 READY，B 的结果被覆盖 / B 的 fail 又把它改回 FAILED
```

当前没有重试入口，所以 T3 不存在；一旦加入重试，D7 就会真实发生。修复必须引入 fencing token。

### D8. Embedding 身份只记录了模型名

**证据**

| 需要的信息 | 是否持久化 | 代码 |
| --- | --- | --- |
| `embedding_model` | 是 | `Document.java:75`、`DocumentIndexService.java:142` |
| `embedding_provider` | **否**（Agent 在响应里返回了 `embeddingProvider`，Spring 的 `AgentIndexResponse` 也接了字段，但 `complete` 只写 model） | `routes.py:128-133`、`AgentIndexResponse.java:7-12`、`DocumentIndexService.java:112` |
| `embedding_dimension` | **否** | 只存在于 Agent 配置 `config.py:114` 和 collection 名 |
| 配置指纹 | **否** | 无 |
| 实际 collection 名 | **否**（响应里有 `collection`，未持久化） | `routes.py:127`、`AgentIndexResponse.java:8` |

`ready-ids` 只按 `d.embedding_model = #{embeddingModel}` 过滤（`DocumentMapper.java:47-49`）。
`collection_name_for(prefix, model, dimension)`（`qdrant_store.py:55-59`）只用 model + dimension。

### D9. provider / dimension 变化时的实际行为

| 变化 | 当前会发生什么 | 是否错误召回 |
| --- | --- | --- |
| 同模型名，维度从 256 → 1024 | 旧文档 `embedding_model` 仍匹配 → 继续进白名单；查询 collection 变成 `prefix_model_1024`，旧 points 在 `prefix_model_256` | 不混用，但**静默 NO_MATCH**，用户以为“没检索到”，文档仍显示 READY |
| 同模型名、同维度，换 provider（例如两个 OpenAI 兼容网关） | 旧文档仍匹配；collection 名相同；查询向量来自新 provider，旧 points 来自旧 provider | **会混用**，相似度不可比，属于静默错误 |
| provider 从 hash → openai | `hash-v1` vs 真实模型名不同 → 旧文档被排除 | 正确 |
| 模型名变化（常规升级） | 旧文档被排除 | 正确，但没有“需要重建”的可见提示 |

**结论**：只记录 `embedding_model` 不足以识别不兼容向量；必须记录 provider、dimension 和
一个覆盖“provider + model + dimension + endpoint 身份”的指纹，并在检索白名单里做**精确过滤**，
不能只记录不校验。

### D10. Qdrant 数据丢失无法发现

**证据**

- `ready-ids` 的完整性检查完全在 MySQL 内（`DocumentMapper.java:40-46`）：
  `chunk_count == COUNT(document_chunk WHERE vector_ref IS NOT NULL AND vector_ref <> '')`。
  它只能发现“MySQL 写回不完整”，发现不了“Qdrant collection 被删 / points 被清 / 单文档 points 丢失”。
- `QdrantDocumentStore.count_document_points`（`:218-224`）存在，但只被测试使用。
- 没有对账任务、没有管理接口、没有指标。
- 如果 collection 不存在，`query_points` 抛异常 → `DocumentKnowledgeSearch.search` 捕获后返回 `DEGRADED`
  （`knowledge.py:103-110`），用户会看到“依赖不可用”；但如果 collection 存在、只是某个文档的
  points 没了，检索返回空候选 → `NO_MATCH`，与“资料里确实没有相关内容”无法区分。

### D11. 删除与正在运行的索引之间没有互斥

假设补上删除接口，按最直觉的实现（先删 Qdrant，再改状态），会出现：

```text
T0  索引线程 A 已通过 runIndex 的状态检查，正在调 Agent
T1  删除线程把状态改为 DELETING / DELETED，并删除 Qdrant points
T2  A 的 agentClient.index 完成，把 points 又写回去
T3  A 的 complete 因为 status != PROCESSING 失败（好），但 Qdrant 里留下已删文档的 points
```

这些 points 不会被召回（文档已不在白名单），但会成为孤儿；如果之后同内容重新上传，新文档用新的
`document_id`，旧 points 永远不会被查询，也无法被新文档的删除流程清掉。

**修复要点**：删除必须让索引线程的最终提交失败，并在提交失败时补偿删除“本次构建版本”的 points；
删除 worker 必须按 `document_id` 清掉**所有版本**。

### D12. 空间软删不级联，向量与文件成为孤儿

`LearningSpaceService.delete`（`:64-80`）明确只软删空间，子表不清理。对文档而言：

- `ready-ids` 先 `ownedSpace`，空间软删后查不到 → 不会召回（安全）。
- 但 `document` / `document_chunk` / 文件 / Qdrant points 全部保留，且没有任何对账。
- 用户重新创建同名空间不会复用旧数据，旧向量永久占用 Qdrant 空间。

这不是“错误召回”，但属于必须纳入清理策略的生命周期缺口。

### 2.13 缺陷严重度汇总

| # | 缺陷 | 影响 | 当前是否可触发 | 优先级 |
| --- | --- | --- | --- | --- |
| D1 | chunk 先删后建 | 失败后旧 chunk 丢失 | 是（FAILED 文档） | **P0**（加入重建入口后升级为服务中断） |
| D2 | Qdrant 先删后写 | 失败后旧 points 丢失 | 是（FAILED 文档） | **P0** |
| D3 | 同内容重传不重试 | 用户永远卡在失败/上传中 | 是 | **P0** |
| D4 | 无删除能力 | 无法删除、孤儿永久保留 | 是 | **P1** |
| D5 | UPLOADING 不回收 | 永久“上传中” | 是（崩溃/异常） | **P1** |
| D6 | FAILED 无重试入口 | 失败即终态 | 是 | **P1** |
| D7 | 无 fencing | 重试后旧 worker 覆盖新结果 | 加第 1 个重试入口后 | **P0**（与 D6 同时修） |
| D8/D9 | 只存模型名 | 换维度静默失效；换 provider 混用 | 是 | **P1** |
| D10 | 无 Qdrant 对账 | 数据丢失不可见 | 是 | **P1** |
| D11 | 删除与索引竞争 | 孤儿 points | 加删除接口后 | **P1** |
| D12 | 空间软删不清理 | 孤儿向量/文件 | 是 | **P2** |

---

## 3. 数据流图

### 3.1 当前写入链（含全部失败点）

```text
POST /api/spaces/{id}/documents
        │
        ├─ ownedSpace ─────────────── FAIL → 404/403（无副作用）
        ├─ prepare(file) ──────────── FAIL → 400/413（无副作用）
        ├─ findActive(hash) ───────── HIT  → 返回 duplicate（不重试）  ← D3
        ├─ store(file) ────────────── FAIL → 400/500（无副作用）
        ├─ INSERT document(UPLOADING) FAIL → 删文件
        └─ submit()
             ├─ markProcessing ────── FAIL/0 行 → 停 UPLOADING          ← D5
             └─ executor.execute
                  ├─ Rejected ───────→ FAILED
                  └─ runIndex
                       ├─ read(file) ──────── FAIL → FAILED（chunk 未动）
                       ├─ parse ───────────── FAIL → FAILED（chunk 未动）
                       ├─ replaceChunks ───── 提交：旧块已删、新块已插 ⚠ D1
                       ├─ index(Agent)
                       │    ├─ embed ──────── FAIL → FAILED（旧 points 未动）
                       │    ├─ ensure_collection FAIL → FAILED
                       │    └─ index_document
                       │         ├─ DELETE old points ── 提交 ⚠ D2
                       │         └─ UPSERT batches ───── FAIL → FAILED（旧已删、新不全）
                       ├─ updateVectorRefs ── 独立事务；部分失败 → 抛出
                       └─ complete ────────── 条件 UPDATE → READY
```

### 3.2 当前读取链

```text
Agent Runtime.prepare
  └─ HttpBackendRetriever.get_knowledge
       ├─ GET /api/spaces/{spaceId}/documents/ready-ids?embeddingModel=M
       │    └─ SQL: READY + 未删 + embedding_model=M
       │           + chunk_count == COUNT(chunk with vector_ref)
       │           （看不到 Qdrant）                                 ← D10
       ├─ provider.embed(question)
       ├─ collection = prefix_M_D
       └─ Qdrant query:
            filter: user_id AND space_id AND document_id IN allowed
            （没有 version / provider 过滤）                        ← D8/D9
            → Top-K → min_score → payload 校验 → KnowledgeChunk
```

### 3.3 部分成功矩阵（当前实现）

| 失败点 | MySQL chunks | MySQL vector_ref | Qdrant points | 文档状态 | 可检索 |
| --- | --- | --- | --- | --- | --- |
| 解析失败 | 旧（若有） | 旧 | 旧 | FAILED | 否 |
| `replaceChunks` 失败 | 回滚，旧保留 | 旧 | 旧 | FAILED | 否 |
| Qdrant 删除后、upsert 前失败 | 新，已提交 | 空 | **旧已删** | FAILED | 否 |
| upsert 部分批次失败 | 新，已提交 | 空 | **部分新** | FAILED | 否 |
| upsert 成功、`updateVectorRefs` 失败 | 新 | 部分 | 全新 | FAILED | 否 |
| `complete` 失败（状态变化） | 新 | 全 | 全新 | PROCESSING→FAILED | 否 |
| 全部成功 | 新 | 全 | 全新 | READY | 是 |

**结论**：现有设计用“READY 是唯一可检索状态”换取了正确性，代价是失败后没有可回退的旧版本，
且中间态会长期占用数据库和 Qdrant 空间。

---

## 4. 两套方案对比

### 4.1 方案 A：最小可靠方案（推荐）

核心机制：

1. **活跃构建令牌**：复用 `document.index_version`，含义从“成功次数”改为“当前活跃的构建令牌”。
   构建令牌由 `index_attempts` 在抢占租约时自增产生，单调递增、每次构建唯一。
2. **分阶段 chunk**：`document_chunk` 增加 `index_version`；唯一键改为
   `(document_id, index_version, chunk_index)`。新构建先写入新版本，旧版本不动。
3. **分阶段向量**：Qdrant point ID 包含版本：
   `uuid5(ns, "ails-document:{docId}:v{version}:{chunkIndex}")`；
   payload 增加 `index_version`、`doc_version`（`"{docId}:{version}"`）、`embedding_fingerprint`。
   **先 upsert 新版本，再清理旧版本**，不再 delete-first。
4. **原子切换**：全部成功后，一条带 `status/attempt` 条件的 `UPDATE document`
   把 `index_version` 指向新版本，同时写 `chunk_count/embedding_*/vector_collection/indexed_at`。
   这是唯一的“生效”动作。
5. **旧版本延迟清理**：切换提交后异步删除 `index_version <> active` 的 chunk 和 points；
   失败由清理扫描器重试。清理失败不影响召回，因为检索按 `doc_version` 过滤。
6. **删除**：`DELETE` 先把状态改为 `DELETING`（ready-ids 立即排除），异步按 `document_id`
   删除所有版本 points，成功后 `DELETED + deleted_at`；失败保留 `DELETING` 并重试。
7. **恢复**：`UPLOADING` / `PROCESSING` / `READY+index_started_at` / `DELETING` 四类扫描器；
   全部用条件更新 + fencing token 保证幂等。
8. **Embedding 身份**：记录 provider / model / dimension / fingerprint / collection；
   ready-ids 按 fingerprint 精确过滤。
9. **完整性巡检**：Agent 提供 `verify`（按 collection + document_id 计数）；
   Spring 滚动抽查 READY 文档，记录 `last_verified_at` / `verify_failures`；
   默认只告警、不自动改状态，避免 Qdrant 抖动造成批量下线。

### 4.2 方案 B：完整版本化索引

在 A 的基础上再引入：

- 独立 `document_index_version` 表：`document_id, version, status(BUILDING/ACTIVE/RETIRED/FAILED),
  collection, chunk_count, created_at, activated_at, retired_at, error`。
- 每次构建一个独立 collection（或独立 named vector），版本之间物理隔离。
- 版本保留策略（保留 N 个、按时间淘汰）、版本回滚 API、版本审计。
- 全量巡检体系：逐文档、逐版本核对 points / chunk / payload，产出可查询的对账报告。
- 多实例构建协调、构建队列、优先级、限流。

### 4.3 对比表

| 维度 | A（单活跃版本 + 分阶段构建） | B（完整版本化） |
| --- | --- | --- |
| **可靠性** | 覆盖“失败不破坏旧索引、删除立即停召回、重试幂等、配置变化可识别”；单集合内用 `doc_version` 过滤，正确性足够 | 额外覆盖“多版本并存、按版本回滚、版本级审计”；可靠性上限更高，但收益主要在极端场景 |
| **改造范围** | 1 个 chunk 列 + 唯一键；document 若干元数据列；写入链重排；检索白名单返回 `(id, version)`；删除/重试/巡检各 1 个入口 | A 的全部 + 新版本表 + 版本服务 + 清理/保留策略 + 巡检体系 + 管理 API |
| **数据库变化** | `document` 加 6-8 列；`document_chunk` 加 1 列并替换唯一键；加 1-2 个索引 | A 的全部 + 至少 1 张新表 + 版本状态机 + 更多索引 |
| **代码复杂度** | 中等：主要是 `DocumentIndexService` 重排、`DocumentChunkService` 版本化、Agent 写入/检索契约调整 | 高：新增版本领域模型、生命周期、保留策略、巡检、回滚；测试面显著扩大 |
| **测试成本** | 约 25-30 条（见第 8 节），核心是失败注入与并发 | A 的测试 + 版本切换/回滚/保留/多版本巡检，约 50+ 条 |
| **当前项目是否真的需要** | **需要**：它解决的是真实存在的数据丢失与不可恢复问题 | **不需要**：没有内容替换入口、没有版本历史查询、没有多租户/多实例强需求，B 的额外能力没有使用场景 |
| **升级路径** | 将来若出现“频繁重建 + 需要回滚到任意历史版本 + 合规审计”，可在 A 的版本令牌上平滑扩展出 B 的版本表 | 一开始就承担全部复杂度 |

### 4.4 结论

**推荐 A。** 理由不是“B 不好”，而是 B 解决的问题在当前产品里不存在：

- 上传按内容 SHA-256 去重（`uk_doc_space_active_hash`），没有“替换同一文档内容”的 API；
- 文档一旦 READY，内容不可变（重新上传同内容只会命中同一条记录）；
- 单进程部署，文档量以“每个学习空间几十份资料”计；
- 没有“回滚到上周三的索引版本”这种业务需求。

A 已经能把“失败不丢旧索引、删除能停召回、失败能重试、配置变化能识别、Qdrant 丢失能发现”
全部闭环；B 多出来的版本表与版本级巡检，在没有对应业务的前提下只是维护成本。

**升级触发条件（写清楚，避免以后争论）**：出现以下任意一条再考虑 B——
1. 需要“编辑/替换文档内容并保留历史版本”；
2. 需要在一次构建失败后回滚到任意历史版本（而不只是“上一个活跃版本”）；
3. 文档量或重建频率使单集合多版本共存造成明显成本/性能问题；
4. 合规要求索引版本审计。

---

## 5. 推荐的最小可靠方案（A）详细设计

### 5.1 不变量

修复后的系统必须始终满足：

| # | 不变量 | 违反后果 |
| --- | --- | --- |
| I1 | **MySQL 是事实源**，Qdrant 是可重建的派生存储 | 任何“Qdrant 成功但 MySQL 没记录”的状态都可通过 READY 白名单排除，并通过重建收敛 |
| I2 | **任何时刻至多一个活跃版本**（`document.index_version`） | 检索结果唯一、引用可解释 |
| I3 | **新版本在切换前不可检索**；旧版本在切换后不再被选中 | 重建期间用户仍看到旧内容，不会看到半成品 |
| I4 | **切换是一条条件 UPDATE**，失败则旧版本原样保留 | 不存在“切换了一半”的文档 |
| I5 | **删除状态先于向量清理**；只要不是 READY，就不参与召回 | Qdrant 清理再慢也不会造成已删文档被召回 |
| I6 | **每次构建有唯一 fencing token**；旧 worker 的任何写入都不能覆盖新结果 | 重试/回收与旧线程不互相污染 |
| I7 | **Embedding 不兼容的文档不进白名单** | 不会用新模型查询向量去检索旧模型/旧 provider 的 points |

### 5.2 构建令牌与租约

复用 `index_attempts` 作为单调递增的构建令牌：

```sql
-- 抢占租约（三种入口用不同条件）
-- 1) 首次索引：UPLOADING / FAILED → PROCESSING
UPDATE document
   SET index_attempts = index_attempts + 1,
       index_started_at = NOW(),
       status = 'PROCESSING',
       index_error = NULL
 WHERE id = ?
   AND deleted_at IS NULL
   AND status IN ('UPLOADING', 'FAILED');

-- 2) 重建 READY：状态保持 READY，只占租约（旧版本继续服务）
UPDATE document
   SET index_attempts = index_attempts + 1,
       index_started_at = NOW(),
       index_error = NULL
 WHERE id = ?
   AND deleted_at IS NULL
   AND status = 'READY'
   AND (index_started_at IS NULL OR index_started_at < ?);   -- cutoff

-- 3) 读取本次令牌
SELECT id, learning_space_id, user_id, status, index_attempts, index_version,
       embedding_model, embedding_provider, embedding_dimension,
       embedding_fingerprint, vector_collection, storage_uri, file_name, file_type, name
  FROM document WHERE id = ?;
```

`updated = 1` 的调用方是唯一合法 writer；`index_attempts` 就是本次构建的版本号。
**语义变更**：`index_version` 从“成功次数”改为“活跃构建令牌（不透明、单调递增）”。
它不再保证连续，也不应该被 UI 当作“第几次索引”展示；需要展示次数时用 `index_attempts`。

> 为什么不用 `active_version + 1`：两个并发 worker 会算出同一个版本号，point ID 相同，
> 后写的会覆盖先写的。用 `index_attempts` 作为版本号，每次构建天然唯一。

### 5.3 写入链（新）

```text
DELETE / POST reindex / upload
        │
        └─ submit(documentId)
             ├─ acquireLease()  ───────── 0 行 → 已有 writer / 状态不允许，直接返回
             ├─ capture attempt = document.index_attempts
             └─ executor.execute(runIndex(documentId, attempt))

runIndex(documentId, attempt)
  1. 重新读取 document，校验：
        status ∈ {PROCESSING (首次), READY (重建)}
        index_attempts == attempt
        index_started_at 未过期
     不满足 → 直接退出（不做任何写入）
  2. storageService.read(storage_uri)
  3. POST /agent/documents/parse
  4. 清理本次版本的残留：
        DELETE FROM document_chunk
         WHERE document_id = ? AND index_version = ?      -- ? = attempt
     （上次同版本构建可能因进程崩溃留下半成品；租约保证没有并发 writer）
  5. 插入分阶段 chunk：
        INSERT document_chunk(document_id, index_version=attempt, chunk_index, ...)
     这一事务失败 → 清租约，旧版本不动
  6. POST /agent/documents/index
        request 增加 indexVersion=attempt、embeddingFingerprint、embeddingProvider、dimension
        Agent：
          - point_id = uuid5(ns, "ails-document:{docId}:v{version}:{chunkIndex}")
          - **先 UPSERT 全部分批**，再删除该文档的旧版本 points
          - payload: document_id, chunk_id, chunk_index, index_version, doc_version,
                     embedding_fingerprint, user_id, space_id, content, metadata, ...
     失败 → 删除本版本 chunk，清租约；旧版本继续服务
  7. 校验响应：向量数、chunk 映射、pointId 非空、版本一致
  8. 单事务切换：
        BEGIN
          -- 再次确认租约/状态（防删除、防回收、防新构建）
          SELECT ... FROM document WHERE id = ? FOR UPDATE
          -- 必须仍然是本次 attempt，且状态允许
          UPDATE document_chunk SET vector_ref = ? WHERE id = ? AND document_id = ?
          UPDATE document
             SET status = 'READY',
                 index_version = attempt,
                 chunk_count = ?,
                 parser_version = ?,
                 embedding_model = ?,
                 embedding_provider = ?,
                 embedding_dimension = ?,
                 embedding_fingerprint = ?,
                 vector_collection = ?,
                 indexed_at = NOW(),
                 index_error = NULL,
                 index_started_at = NULL
           WHERE id = ?
             AND index_attempts = ?
             AND deleted_at IS NULL
             AND status IN ('PROCESSING', 'READY')
          -- 若 UPDATE 0 行 → ROLLBACK，旧版本保留
        COMMIT
  9. 异步清理（失败不影响召回）：
        - DELETE document_chunk WHERE document_id = ? AND index_version <> attempt
        - Agent delete points WHERE document_id = ? AND doc_version <> "{id}:{attempt}"
          （legacy: index_version 字段为空的 points 也要删）
```

**关键顺序变化**

| 当前 | 修复后 |
| --- | --- |
| 先删旧 chunk，再调 Qdrant | 先写新版本 chunk（旧版本不动），再调 Qdrant |
| Qdrant 先删后写 | 先写新版本 points，再删旧版本 |
| 三次独立提交（chunk / refs / status） | 一条事务完成 refs + 活跃版本切换 |
| 失败后旧索引已丢 | 失败后旧版本原样保留 |

**关于 chunk_id 与 payload**：新版本 chunk 在调用 Agent 前已经插入，因此 `chunk_id` 仍然可用，
不需要改现有 `chunkId → pointId` 契约；只需要在 request 里增加 `indexVersion`，
在 response 里增加 `embeddingDimension` / `embeddingFingerprint`（`embeddingProvider`、`collection` 已有）。

### 5.4 读取链（新）

```text
Agent
  ├─ GET /api/spaces/{spaceId}/documents/ready-ids
  │     ?embeddingModel=M
  │     &embeddingProvider=P
  │     &embeddingDimension=D
  │     &embeddingFingerprint=F
  │     &detail=versioned
  │
  │  Spring SQL（示意）：
  │    SELECT d.id, d.index_version, d.embedding_fingerprint
  │      FROM document d
  │     WHERE d.learning_space_id = ?
  │       AND d.user_id = ?
  │       AND d.status = 'READY'
  │       AND d.deleted_at IS NULL
  │       AND d.indexed_at IS NOT NULL
  │       AND d.chunk_count > 0
  │       AND d.chunk_count = (
  │             SELECT COUNT(*) FROM document_chunk c
  │              WHERE c.document_id = d.id
  │                AND c.index_version = d.index_version
  │                AND c.vector_ref IS NOT NULL AND c.vector_ref <> '')
  │       AND (d.embedding_fingerprint = ?             -- 新文档：精确匹配
  │            OR (d.embedding_fingerprint IS NULL     -- 迁移前的旧文档：仅按 model 兼容
  │                AND d.embedding_model = ?))
  │     ORDER BY d.id
  │
  ├─ 构造 doc_version 集合：["{id}:{version}", ...]
  └─ Qdrant query:
       filter:
         user_id = ?
         AND space_id = ?
         AND doc_version IN (白名单键集合)
       → Top-K → min_score → payload 校验
```

**为什么用 `doc_version` 而不是在 Qdrant 里按 `document_id + index_version` 两条件过滤**：
不同文档的活跃版本不同，Qdrant 的单个 `FieldCondition` 无法表达“每个文档各自的版本”；
把两者拼成一个字符串字段后，一个 `MatchAny` 就能表达整个白名单，过滤成本与文档数线性相关，
且比 `should` 嵌套更简单。

**兼容旧 points**：迁移前的 points 没有 `doc_version`。两种处理：
1. **推荐**：迁移时对所有 READY 文档执行一次重建（本项目的文档量很小），全部产生带版本的新 points；
2. 过渡期：`doc_version` 过滤只对“已有 version 的文档”生效，旧文档在 Agent 侧用
   `document_id IN allowed` 兜底，并在返回后按 `payload.index_version`（缺失视为 0）校验。
   这需要 `ready-ids` 同时告诉 Agent 哪些文档是 legacy。

**payload 校验（`knowledge.py:_parse_hit`）**：在现有字段校验之外增加
- `document_id` 必须在白名单内（已有）；
- `doc_version` 必须等于该文档的活跃版本（legacy 例外）；
- `index_version` 必须等于活跃版本（legacy 缺失视为 0）；
- 不满足的 hit 计入 `invalid`，不进入上下文。

### 5.5 删除链（新）

```text
DELETE /api/documents/{id}   （或 /api/spaces/{spaceId}/documents/{id}）
  1. ownedDocument(userId, documentId)
  2. 条件更新：
       UPDATE document
          SET status = 'DELETING',
              index_attempts = index_attempts + 1,   -- 立即 fence 正在跑的构建
              index_started_at = NULL,
              delete_error = NULL
        WHERE id = ? AND deleted_at IS NULL
          AND status IN ('READY', 'FAILED', 'UPLOADING', 'PROCESSING')
     -- 0 行：已删除/正在删除 → 幂等返回
  3. 立即返回 202/200：recall 已停止（ready-ids 只认 READY）
  4. 异步 worker（当前进程线程池 + 定时补扫）：
       a. Agent DELETE /agent/documents
            { documentId, collection: 已存的 vector_collection,
              model/dimension/fingerprint 兜底 }
          Agent 按 document_id 删除 collection 内所有版本 points
       b. 删除 document_chunk（FK 有 ON DELETE CASCADE，但软删不会触发；
          需要显式 DELETE 或保留行；推荐保留行直到最终删除，便于排查）
       c. 删除磁盘文件（deleteQuietly，幂等；失败仅告警）
       d. UPDATE document SET status='DELETED', deleted_at=NOW(),
              delete_error=NULL WHERE id=? AND status='DELETING'
  5. 失败：status 保持 DELETING，delete_attempts+1，delete_error=摘要；
     定时扫描器按 batch 重试。超过阈值只告警，不自动改成 DELETED
     （避免“向量没删干净却显示已删除”）。
```

**召回停止的证明**：`ready-ids` 的 SQL 条件是 `status='READY'`。`DELETE` 的第一步把状态改成
`DELETING`，因此从该 UPDATE 提交的那一刻起，该文档不再出现在任何 Agent 的白名单里。
Qdrant 里即使还有 points，也因为没有 `document_id` 在白名单中而不会被检索
（`qdrant_store.search_knowledge:245-248` 空白名单直接返回，`:251-265` 带 `document_id IN` 过滤）。

**与索引线程的竞争（D11）**：

- 删除的 UPDATE 自增 `index_attempts`，索引线程最终切换时的条件 `index_attempts = attempt` 会失败；
- 切换事务 ROLLBACK，旧版本保留（但文档已是 DELETING，不再服务）；
- 索引线程在切换失败后执行补偿：删除本次 `attempt` 版本的 Qdrant points；
- 删除 worker 按 `document_id` 删除所有版本，重复执行幂等；
- 若进程在“upsert 完成、补偿未执行”之间崩溃，删除 worker 的按文档删除仍会覆盖它；
- 若删除 worker 也在此时崩溃，`DELETING` 扫描器会重试。

**空间删除**：`LearningSpaceService.delete` 在同一事务里把该空间下
`status IN ('READY','FAILED','UPLOADING','PROCESSING')` 的文档批量改为 `DELETING`，然后交给
删除 worker。空间行软删后 API 已不可见；这一步只负责回收向量与文件，不阻塞删除响应。
若批量 UPDATE 失败，空间仍可删（召回已停止），由孤儿巡检兜底。

### 5.6 恢复与重试

| 场景 | 扫描条件 | 动作 | 结果 |
| --- | --- | --- | --- |
| 上传后未提交 | `status='UPLOADING' AND updated_at < now-60s AND index_started_at IS NULL` | 调 `submit`（CAS 抢占）；`index_attempts >= max` 时置 FAILED | 自愈或可诊断失败 |
| 首次索引卡死 | `status='PROCESSING' AND index_started_at < now-15min` | `index_attempts+1`（fence）、`status='FAILED'`、清租约、写错误 | 旧 worker 无法再提交；用户可重试 |
| READY 重建卡死 | `status='READY' AND index_started_at < now-15min` | `index_attempts+1`、`index_started_at=NULL`、写 `index_error='重建超时，旧索引继续服务'` | 文档仍 READY，旧版本继续服务 |
| 删除未完成 | `status='DELETING'` | 重试 Agent 删除 + 文件删除 + DELETED | 幂等收敛 |
| FAILED | 用户 `POST /api/documents/{id}/reindex` 或同内容重传 | CAS 抢占 → 正常构建 | 成功则 READY，失败则 FAILED |
| Qdrant 缺失 | READY 且 `last_verified_at < now-T` | verify → 记录/告警（默认不改状态） | 可发现、可人工重建 |

**Fencing 规则**：所有 `complete` / `fail` / 超时回收 / 删除的条件更新都必须带
`index_attempts = #{attempt}`（或 `index_attempts = index_attempts + 1` 的 fence 版本）。
任何“先读后写”的路径都不能只依赖 `status`。

**重试入口**：

```text
POST /api/documents/{id}/reindex
  - 归属校验
  - 允许状态：FAILED、READY（READY 走“保留旧版本”的重建路径）、UPLOADING
  - 不允许：PROCESSING（已在构建）、DELETING/DELETED
  - 冷却：同一文档 30s 内只接受一次（可用 index_started_at 判断）
  - 返回当前 status / indexAttempts / indexError
```

**同内容重传（D3）**：`upload` 的 `findActive` 之后按状态分支：

| 状态 | 行为 |
| --- | --- |
| READY | 返回 duplicate=true（当前行为） |
| PROCESSING | 返回 duplicate=true（正在处理） |
| FAILED | 调 `submit` 重试；返回 duplicate=true + 最新状态 |
| UPLOADING | 若超过阈值则调 `submit`；否则返回 duplicate=true |
| DELETING | 返回 409“文档正在删除，请稍后重试” |
| DELETED | `@TableLogic` 不会命中，正常新建 |

### 5.7 Qdrant 完整性巡检

**Agent 新接口**：

```text
POST /agent/documents/verify
{
  "items": [
    {"documentId": 101, "collection": "ails_documents_text-embedding-v4_1024",
     "expectedCount": 12, "docVersion": "101:7"}
  ]
}
→ 200
{
  "code": 0,
  "data": {
    "results": [
      {"documentId": 101, "present": true, "count": 12,
       "activeVersionCount": 12, "error": null}
    ]
  }
}
```

实现：`count_document_points(collection, document_id)`，如需要可按 `doc_version` 再 count 一次。
- `count == expectedCount` → 健康；
- `count == 0 && expected > 0` 且 Qdrant 可达 → 明确丢失；
- `error != null`（连接/超时）→ 依赖异常，不改状态。

**Spring 巡检**：

```text
每 verify-interval（默认 30 分钟）：
  SELECT id, vector_collection, index_version, chunk_count
    FROM document
   WHERE status='READY' AND deleted_at IS NULL
     AND (last_verified_at IS NULL OR last_verified_at < now-T)
   ORDER BY last_verified_at ASC
   LIMIT batch(50)
  → 调 Agent verify → 更新 last_verified_at / verify_failures
  → 连续 2 次 count=0 且无依赖错误：
       - verify.auto-mark-failed=false（默认）：只写日志/指标/告警
       - true：条件更新 status='FAILED', index_error='向量缺失，需要重建'
  → 记录指标：rag_integrity_missing_total / rag_integrity_error_total
```

**默认不自动下线的理由**：collection 被误删会让所有文档同时“丢失”；自动置 FAILED 会把
一次运维事故放大成“全空间资料不可用”。先做到“可发现 + 可重建”，再按运维成熟度打开自动降级。

### 5.8 Embedding 兼容性（必须记录 + 必须校验）

**记录**（`document` 表）：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `embedding_model` | 已有 | Agent `provider.model` |
| `embedding_provider` | 新增 | Agent `provider.name` |
| `embedding_dimension` | 新增 | Agent `provider.dimension` |
| `embedding_fingerprint` | 新增 | `v1:` + SHA-256(`provider|model|dimension|endpoint_identity`) |
| `vector_collection` | 新增 | 索引时实际使用的 collection 名 |

**指纹计算**（Agent 侧，`EmbeddingProvider` 增加 `fingerprint` 属性）：

```text
hash:              "v1:sha256('hash|hash-v1|<dim>|hash-v1')"
openai-compatible: "v1:sha256('openai-compatible|<model>|<dim>|<normalized_base_url>')"
```

`normalized_base_url` 只取 scheme + host + path，去掉用户信息、查询参数和尾部斜杠；
**绝不能把 API Key 放进指纹**（会进日志/DB）。如果同一网关后面可能切换底层模型，
建议额外配置一个显式的 `embedding_config_id`（例如 `bailian-v4-1024`）并纳入指纹；
这比“信任模型名”更稳。

**校验点**（不能只记录）：

1. **索引时**：Spring 校验 Agent 响应的 provider/model/dimension/fingerprint/collection 全部非空，
   且 dimension 与请求一致；任何不一致直接 FAILED，不写 READY。
2. **检索时**：Agent 把当前 provider 的 model/provider/dimension/fingerprint 传给 `ready-ids`；
   Spring 的 SQL 用 `embedding_fingerprint = ?` 精确过滤。指纹不匹配的旧文档**不进白名单**，
   因此不会被“新模型的查询向量”检索到。
3. **迁移兼容**：指纹为 NULL 的历史文档，只有在 `embedding_model` 与当前模型一致时才进白名单，
   并在日志里标记 `legacy_embedding_identity`；下一次重建会补齐指纹。
4. **collection 选择**：仍用 `prefix + model + dimension`。不把 provider 放进 collection 名，
   避免改名导致所有旧数据失联；provider 的隔离由 fingerprint 白名单完成。
   （如果将来要物理隔离，可在 A 的版本上单独加“collection 名包含 fingerprint”的迁移，
   但那需要全量重建，不属于本次最小方案。）

**回答“同模型名 + 同维度 + 换 provider 会混用吗”**：修复后不会。旧文档的 fingerprint 属于
provider A，新查询的 fingerprint 属于 provider B，`ready-ids` 不返回旧文档；旧文档在下次重建前
处于“不可检索”状态（而不是错误召回）。这是安全侧的正确取舍。

### 5.9 核心问题逐条回答

| # | 问题 | 回答 |
| --- | --- | --- |
| 1 | 当前索引更新为什么丢失旧 chunks | `replaceChunks` 在 Qdrant 写入前独立提交，先 `DELETE` 再 `INSERT`；失败后旧行已物理删除。Qdrant 侧 `index_document` 也是先删后写，两层叠加 |
| 2 | 是否可以先构建新 chunks，再切换 | 可以。给 chunk 加 `index_version`，新版本先写入；Qdrant point 也带版本；最后用一条条件 UPDATE 切换 `document.index_version`（5.3） |
| 3 | MySQL 与 Qdrant 不在同一事务，如何处理部分成功 | MySQL 是事实源；Qdrant 写入幂等（确定性 point ID + 先 upsert 后清理）；只有 Qdrant 成功且校验通过后才提交 MySQL 切换；任何部分成功都由“旧版本仍活跃 + 重试/清理”收敛（5.3、5.6） |
| 4 | 新索引失败时旧索引如何继续服务 | 新版本写入独立版本号，切换前不改变 `document.index_version`；READY 文档重建失败时状态仍为 READY，旧版本的 chunks/points 原样服务；只有切换 UPDATE 成功才生效（5.3、5.6） |
| 5 | 删除文档后如何阻止继续召回 | `DELETE` 第一步把状态改为 `DELETING`；ready-ids 只认 READY，所以召回立即停止；Qdrant 清理是异步的，清理慢不影响正确性（5.5） |
| 6 | Qdrant 清理失败如何重试 | `DELETING` 状态保留 + `delete_attempts/delete_error`；定时扫描按 batch 重试；Agent 删除接口幂等；超过阈值告警但不伪造 DELETED（5.5、5.6） |
| 7 | 同一文档重复触发重建如何避免竞争 | `index_attempts` 自增抢占租约（条件 UPDATE）；本次构建使用该值作为版本号/fencing token；`complete`、`fail`、回收、删除都带 `index_attempts = attempt`；旧 worker 的提交必然 0 行（5.2、5.6） |
| 8 | Embedding Provider/Model/Dimension 改变后如何识别不兼容向量 | 持久化 provider/model/dimension/fingerprint/collection；检索白名单按 fingerprint 精确过滤；索引响应逐字段校验；历史 NULL 指纹按 model 兼容并在重建后补齐（5.8） |
| 9 | 如何发现 MySQL 有记录但 Qdrant 丢失 | Agent `verify` 按 collection + document_id 计数；Spring 滚动抽查 READY 文档，记录 `last_verified_at/verify_failures`；默认告警，可配置自动置 FAILED；重建即修复（5.7） |

---

## 6. 数据模型变化

### 6.1 `document` 表新增列

| 列 | 类型 | 默认 | 用途 |
| --- | --- | --- | --- |
| `embedding_provider` | `VARCHAR(64) NULL` | NULL | Agent provider 名（`hash` / `openai-compatible`） |
| `embedding_dimension` | `INT NULL` | NULL | 向量维度；删除/校验时定位 collection |
| `embedding_fingerprint` | `VARCHAR(128) NULL` | NULL | 配置指纹（`v1:` + SHA-256） |
| `vector_collection` | `VARCHAR(255) NULL` | NULL | 实际写入的 Qdrant collection；删除/巡检直接使用 |
| `index_started_at` | `DATETIME NULL` | NULL | 构建租约开始时间；替代 `updated_at` 启发式 |
| `delete_attempts` | `INT NOT NULL` | 0 | 删除重试次数 |
| `delete_error` | `VARCHAR(1024) NULL` | NULL | 删除失败摘要 |
| `last_verified_at` | `DATETIME NULL` | NULL | 最近一次 Qdrant 巡检时间 |
| `verify_failures` | `INT NOT NULL` | 0 | 连续巡检失败次数 |

> `index_version`、`index_attempts` 已存在，不需要新增。
> `index_version` 语义改为“活跃构建令牌”，不再自增计数；`index_attempts` 是单调递增的构建序号。

### 6.2 `document_chunk` 表变更

| 变更 | 内容 |
| --- | --- |
| 新增列 | `index_version INT NOT NULL DEFAULT 0` |
| 删除唯一键 | `uk_chunk_doc_idx (document_id, chunk_index)` |
| 新增唯一键 | `uk_chunk_doc_ver_idx (document_id, index_version, chunk_index)` |
| 新增索引（可选） | `idx_chunk_doc_version (document_id, index_version)` 已由唯一键最左前缀覆盖，可不加 |
| 外键 | 不变（`fk_chunk_document ... ON DELETE CASCADE` 在物理删除 document 时仍有效） |

DDL 示意：

```sql
ALTER TABLE `document_chunk`
    ADD COLUMN `index_version` INT NOT NULL DEFAULT 0
        COMMENT '所属索引构建令牌；与 document.index_version 对应'
        AFTER `document_id`;

ALTER TABLE `document_chunk`
    DROP INDEX `uk_chunk_doc_idx`,
    ADD UNIQUE KEY `uk_chunk_doc_ver_idx`
        (`document_id`, `index_version`, `chunk_index`);
```

### 6.3 迁移与回填

推荐新增 `backend/backend/src/main/resources/schema-migration-rag-reliability.sql`，
沿用现有迁移脚本的幂等风格（查 `information_schema` 再 DDL，不删除业务数据）：

1. 加 `document` 的 9 个新列（逐列检查存在性）。
2. 加 `document_chunk.index_version`。
3. 回填：把现有 chunk 归到当前活跃版本：
   ```sql
   UPDATE `document_chunk` c
     JOIN `document` d ON d.id = c.document_id
      SET c.index_version = d.index_version;
   ```
   这样迁移后 `document.index_version` 与 chunk 的版本一致，旧数据立即可按活跃版本读取。
4. 替换唯一键（先确认新键无重复：`(document_id, index_version, chunk_index)` 在回填后必然唯一）。
5. 旧 READY 文档的 `embedding_provider/dimension/fingerprint/vector_collection` 保持 NULL，
   检索按“legacy + model 匹配”兼容；随后执行一次全量重建（脚本或管理接口）补齐。
6. 新增 `idx_doc_status_started (status, index_started_at)` 供租约/回收扫描；
   若已有 `idx_doc_space_status`，全局扫描仍需要按 `status + index_started_at`，建议新增。
7. 不新增表；不修改 `status` ENUM（`DELETING/DELETED` 已存在）。

> 迁移脚本不会被 `spring.sql.init` 执行（当前只加载 `schema.sql`），必须手工运行；
> `schema.sql` 同步更新，保证新库直接具备新列。

### 6.4 Qdrant payload 变化

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `index_version` | integer | 构建令牌；legacy points 缺失 |
| `doc_version` | keyword | `"{document_id}:{index_version}"`，检索主过滤键 |
| `embedding_fingerprint` | keyword | 诊断/巡检；不强制作为检索过滤（白名单已过滤） |

point ID：

```text
legacy / version 0: uuid5(ns, "ails-document:{docId}:{chunkIndex}")
version >= 1:       uuid5(ns, "ails-document:{docId}:v{version}:{chunkIndex}")
```

保留 version 0 的旧公式，是为了让迁移前的 points 在第一次重建前仍然可用；
第一次重建（version >= 1）成功后，旧 points 由清理任务删除。

### 6.5 接口变化

| 接口 | 变化 |
| --- | --- |
| `GET ready-ids` | 增加 `embeddingProvider/embeddingDimension/embeddingFingerprint` 参数；`detail=versioned` 时返回 `[{id, indexVersion}]` |
| `POST /agent/documents/index` | request 增加 `indexVersion`；response 增加 `embeddingDimension/embeddingFingerprint` |
| `POST /agent/documents/delete` | 新增；按 `documentId + collection` 删除所有版本 |
| `POST /agent/documents/verify` | 新增；批量计数 |
| `DELETE /api/documents/{id}` | 新增；触发 DELETING |
| `POST /api/documents/{id}/reindex` | 新增；FAILED/READY/UPLOADING 可重建 |
| `GET /api/spaces/{spaceId}/documents` | `DocumentResponse` 增加 provider/dimension/fingerprint/indexStartedAt/deleteError 等诊断字段（可选） |

---

## 7. 失败恢复机制

### 7.1 按失败点归类

| 失败点 | 已提交状态 | 恢复动作 | 用户可见 |
| --- | --- | --- | --- |
| 文件落盘失败 | 无 | 清理临时文件 | 上传失败 |
| `INSERT document` 失败 | 文件 | 删文件 | 上传失败 |
| `submit` 未执行（进程崩溃） | `UPLOADING` | UPLOADING 扫描器重新提交 | 稍后自动恢复 |
| `markProcessing` 数据库异常 | `UPLOADING` | 同上 | 稍后自动恢复 |
| 解析失败 | `PROCESSING` | `fail` → FAILED | 可重试 |
| 分阶段 chunk 插入失败 | 旧版本仍在 | 清租约 → FAILED（首次）/ READY（重建） | 旧索引仍可用 |
| Qdrant upsert 失败 | 新版本 chunk（非活跃） | 删除新版本 chunk + 清租约；旧版本继续 | 旧索引仍可用 |
| Qdrant 清理旧版本失败 | 新版本已活跃 | 清理扫描器重试；`doc_version` 过滤保证旧 points 不被选中 | 无影响 |
| MySQL 切换事务失败 | Qdrant 新版本 points | ROLLBACK；清租约；旧版本继续；补偿删除新版本 points | 旧索引仍可用 |
| 切换成功后进程崩溃（清理未跑） | 新版本活跃、旧版本残留 | 清理扫描器按版本清理 | 无影响 |
| 删除时 Agent 不可用 | `DELETING` | 删除扫描器重试 | 已停止召回 |
| 删除时进程崩溃 | `DELETING` | 同上 | 已停止召回 |
| Qdrant collection 丢失 | READY 但 points 0 | verify 发现 → 告警/可选 FAILED → 重建 | 巡检前可能静默 NO_MATCH |
| 空间软删 | 子资源仍在 | 批量 DELETING + 删除 worker | API 不可见 |

### 7.2 重试策略

| 任务 | 触发 | 退避 | 上限 |
| --- | --- | --- | --- |
| UPLOADING 重新提交 | 定时 60s | 每次扫描 | `index_attempts >= 5` 置 FAILED |
| PROCESSING 超时回收 | 定时 5min，阈值 15min | 固定 | 不自动重试，置 FAILED 等用户 |
| READY 重建超时 | 同上 | 固定 | 清租约，保持 READY |
| DELETING 清理 | 定时 5min | 固定间隔；记录 attempts | 不伪造 DELETED；超阈值告警 |
| Qdrant verify | 定时 30min | 滚动抽查 | 默认只告警 |
| 手动 reindex | 用户 | 30s 冷却 | 可配置每日上限 |

### 7.3 幂等性

| 操作 | 幂等依据 |
| --- | --- |
| `submit` | 条件 UPDATE；已 PROCESSING/租约未过期则 0 行 |
| 分阶段 chunk 写入 | 先删同版本残留，再插入；租约保证无并发 writer |
| Qdrant upsert | point ID = f(docId, version, chunkIndex)，重复写覆盖 |
| Qdrant 按版本清理 | 删除条件幂等；旧版本 points 不参与检索 |
| 切换 | `index_attempts = attempt` + 状态条件；重复执行 0 行 |
| 删除 | 状态条件 + 按 document_id 删除；重复执行幂等 |
| verify | 只读计数 |

### 7.4 不可自动恢复的残余状态

以下状态需要人工介入，设计上必须让它们“可见”而不是“静默”：

| 状态 | 检测方式 | 人工动作 |
| --- | --- | --- |
| `DELETING` 超过 N 次仍失败 | `delete_attempts` / 日志 / 指标 | 检查 Qdrant/网络；必要时手工删除 points 后置 DELETED |
| `FAILED` 且 `index_attempts >= max` | 文档详情 | 查看 `index_error`；修复依赖后 reindex |
| READY 但 verify 连续失败 | `verify_failures` / 指标 | 触发重建；检查 collection |
| 迁移后 legacy 文档未重建 | 日志/管理查询 | 跑全量重建脚本 |

---

## 8. 测试清单

> 实施时按此清单落地；本轮只读，未执行。标 **IT** 的需要 MySQL，标 **E2E** 的需要真实 Agent/Qdrant。

### 8.1 Spring 单元测试

| # | 测试 | 断言 |
| --- | --- | --- |
| 1 | `upload` 命中 FAILED 同内容 | 调用 `submit`；返回 duplicate=true 且状态推进 |
| 2 | `upload` 命中 READY 同内容 | 不调用 `submit`；返回 duplicate=true |
| 3 | `upload` 命中 DELETING | 返回 409，不调用 `submit` |
| 4 | `runIndex` 在 Agent index 失败 | 旧活跃版本 chunk 不变；新版本 chunk 被清理；首次索引置 FAILED |
| 5 | `runIndex` 在 READY 重建失败 | 文档仍 READY；`index_version` 不变；旧 chunk/points 仍可检索 |
| 6 | 切换事务失败（模拟 update 0 行） | ROLLBACK；旧版本完整；不出现半切换 |
| 7 | 旧 worker 使用过期 attempt 调 `complete` | 0 行；不能把文档置 READY |
| 8 | 删除把 `index_attempts+1` | 正在运行的构建切换失败；补偿删除新版本 points |
| 9 | `markProcessing` 并发两次 | 只有一次返回 true |
| 10 | `DocumentIndexRecoveryService` 扫 UPLOADING | 重新提交；超过上限置 FAILED |
| 11 | 回收 READY+`index_started_at` | 清租约、保持 READY、写 index_error |
| 12 | 删除成功 | status=DELETED、deleted_at 非空、文件删除、Agent delete 调用 |
| 13 | 删除失败 | status 保持 DELETING、delete_error 非空、下一轮重试 |
| 14 | `ready-ids` 版本完整性 | `chunk_count` 只统计活跃版本的 `vector_ref` |
| 15 | `ready-ids` fingerprint 过滤 | 指纹不匹配的 READY 文档不返回 |
| 16 | `ready-ids` legacy 兼容 | 指纹 NULL + model 匹配时返回（带标记） |
| 17 | 索引响应缺 provider/dimension/fingerprint | 拒绝，置 FAILED，不写 READY |
| 18 | `verify` 计数为 0 | 记录失败；默认不改状态；开关打开时置 FAILED |

### 8.2 Spring 集成测试（MySQL）

| # | 测试 | 断言 |
| --- | --- | --- |
| 19 | **IT** 迁移脚本重复执行 | 幂等；唯一键正确；回填版本一致 |
| 20 | **IT** 两个线程同时 submit | 只有一个 writer；另一个 0 行 |
| 21 | **IT** READY 重建期间查询 | 查询仍返回旧版本 chunk/point 映射 |
| 22 | **IT** 重建成功后查询 | 返回新版本；旧版本行/points 被清理 |
| 23 | **IT** 删除与索引并发 | 最终 status=DELETED；Qdrant 无该 document_id 的 points |
| 24 | **IT** 超时回收后再重试 | 旧 worker 无法提交；新 worker 正常完成 |
| 25 | **IT** `uk_chunk_doc_ver_idx` 冲突 | 同版本同序号拒绝；不同版本允许 |

### 8.3 Agent 单元 / 集成测试

| # | 测试 | 断言 |
| --- | --- | --- |
| 26 | `point_id_for(doc, version, idx)` | 同参数稳定；不同版本不同 ID；version=0 与旧公式一致 |
| 27 | `index_document` 中途失败 | 旧版本 points 仍在；新版本只写了一半也不会删旧 |
| 28 | `index_document` 成功后清理旧版本 | 只删除旧 `doc_version`，不影响其他文档 |
| 29 | `delete_document` | 只删指定 document_id，所有版本都删 |
| 30 | `verify` 计数 | 返回准确 count；Qdrant 不可用返回 error 而非 0 |
| 31 | `_parse_hit` 版本不匹配 | 丢弃；legacy 缺失版本按 0 处理 |
| 32 | `ready-ids` 解析 | 兼容 `[101,202]` 与 `[{id:101,indexVersion:3}]` |
| 33 | 查询 filter | 包含 `doc_version IN` 白名单；空白名单不查 Qdrant |
| 34 | fingerprint | 同 model/dim 不同 base_url 产生不同指纹；不含 API Key |

### 8.4 E2E（真实依赖）

| # | 测试 | 断言 |
| --- | --- | --- |
| 35 | **E2E** 上传 → READY → 检索 → 引用 | 全链路通过；payload 有 doc_version |
| 36 | **E2E** 重建时 Qdrant 故障 | 旧索引仍可检索；文档仍 READY；index_error 可见 |
| 37 | **E2E** 重建成功 | 检索命中新内容；旧 points 被清理 |
| 38 | **E2E** 删除 → 检索 | 删除后立即 NO_VALID_DOCUMENTS；Qdrant points=0；文件不存在 |
| 39 | **E2E** 手工删 Qdrant point | verify 发现；重建后恢复 |
| 40 | **E2E** 进程在索引中被杀 → 重启 | 回收收敛；可重试 |
| 41 | **E2E** 换 provider（同 model/dim） | 旧文档不进白名单；重建后恢复 |
| 42 | **E2E** 同内容重传 FAILED 文档 | 触发重建并最终 READY |

### 8.5 回归

- 现有 `DocumentServiceTest`、`DocumentIndexServiceTest`、`DocumentServiceIntegrationTest`、
  `DocumentIndexServiceIntegrationTest`、`DocumentMapperSqlTest`、`DocumentControllerTest` 全部保留；
  其中断言“chunk 先删后插顺序”的用例（`DocumentIndexServiceTest.java:153-159`）需要按新顺序调整，
  并在测试名/注释中明确“旧版本保留”的新语义。
- `agent/tests/test_qdrant_store.py` 的 `index_document` 幂等用例保留；新增“先写后删”断言。
- `agent/tests/test_knowledge_retrieval.py` 的 ready-ids 参数断言需要增加 fingerprint 参数；
  legacy 响应格式仍需兼容。

---

## 9. 修改文件清单

> 本轮不修改；以下为实现阶段清单。按依赖顺序排列。

### 9.1 数据库

| 文件 | 修改 |
| --- | --- |
| `backend/backend/src/main/resources/schema.sql` | `document` 新列；`document_chunk.index_version` 与新唯一键 |
| `backend/backend/src/main/resources/schema-migration-rag-reliability.sql` | 新增幂等迁移；回填 chunk 版本；替换唯一键；新增扫描索引 |

### 9.2 Spring

| 文件 | 修改 |
| --- | --- |
| `entity/Document.java` | 新字段：provider/dimension/fingerprint/vectorCollection/indexStartedAt/deleteAttempts/deleteError/lastVerifiedAt/verifyFailures |
| `entity/DocumentChunk.java` | `indexVersion` |
| `enums/DocumentStatus.java` | 不改（DELETING/DELETED 已存在）；补充注释说明写入方 |
| `mapper/DocumentMapper.java` | ready-ids 返回 `(id, indexVersion)`、fingerprint 过滤、活跃版本 chunk 统计；新增租约/回收/删除/巡检 SQL |
| `mapper/DocumentChunkMapper.java` | 活跃版本查询、按版本清理、按文档统计 |
| `service/DocumentService.java` | 去重按状态分支；删除编排；空间删除联动 |
| `service/DocumentIndexService.java` | 租约 + fencing；分阶段写入；Qdrant 成功后单事务切换；失败清理 |
| `service/DocumentChunkService.java` | `replaceChunks` 改为 `stageChunks`；`commitActiveVersion`；按版本清理；`listActiveByDocument` |
| `service/DocumentIndexRecoveryService.java` | UPLOADING / PROCESSING / READY 租约 / DELETING 四类扫描 |
| `service/DocumentDeletionService.java`（新增） | 异步删除 worker + 重试 |
| `service/DocumentIntegrityService.java`（新增） | 滚动 verify + 指标 + 可选自动降级 |
| `service/AgentDocumentClient.java` | index request 增版本；delete/verify 调用；新响应字段校验 |
| `controller/DocumentController.java` | `DELETE /api/documents/{id}`、`POST /api/documents/{id}/reindex` |
| `dto/document/agent/AgentIndexResponse.java` | `embeddingDimension`、`embeddingFingerprint` |
| `dto/document/agent/AgentIndexRequest.java` | `indexVersion`、`embeddingFingerprint` 等 |
| `dto/document/agent/AgentDeleteRequest/Response.java`（新增） | 删除契约 |
| `dto/document/agent/AgentVerifyRequest/Response.java`（新增） | 巡检契约 |
| `dto/document/DocumentResponse.java` | 诊断字段（可选） |
| `config/DocumentIndexProperties.java` | upload 超时、reindex 冷却、verify 间隔/批量、自动降级开关、删除重试上限 |
| `resources/application.yml` | 对应配置与注释 |
| `service/LearningSpaceService.java` | 空间删除时批量置 DELETING（或发事件） |

### 9.3 Agent

| 文件 | 修改 |
| --- | --- |
| `app/embedding/base.py` | `EmbeddingProvider.fingerprint`；文档说明 |
| `app/embedding/hash_provider.py` | `fingerprint` |
| `app/embedding/openai_compatible.py` | `fingerprint`（含 normalized base_url，不含 key） |
| `app/embedding/factory.py` | 透传配置（如 `embedding_config_id`） |
| `app/documents/schemas.py` | index request 增 `indexVersion`/fingerprint；delete/verify schema |
| `app/documents/routes.py` | index 写入版本/fingerprint；新增 delete/verify |
| `app/vectorstore/qdrant_store.py` | 版本化 point ID；先 upsert 后清理；payload 增 version/doc_version/fingerprint；按 collection 删除/计数 |
| `app/retriever/knowledge.py` | ready-ids 版本化解析；`doc_version` 过滤；payload 版本校验；legacy 兼容 |
| `app/retriever/http_backend.py` | 传递 provider/dimension/fingerprint；解析两种 ready-ids 形状 |
| `app/config.py` | 如新增 `embedding_config_id` / verify 超时 |

### 9.4 文档与测试

| 文件 | 修改 |
| --- | --- |
| `README.md` / `docs/delivery/KNOWN_LIMITATIONS.md` / `docs/delivery/TEST_MATRIX.md` | 更新删除、重试、版本、指纹、巡检的“已实现/未实现”口径 |
| `backend/backend/src/test/.../Document*Test.java` | 新增第 8.1/8.2 节用例；调整顺序断言 |
| `agent/tests/test_qdrant_store.py` / `test_document_api.py` / `test_knowledge_retrieval.py` / `test_knowledge_e2e.py` | 新增第 8.3/8.4 节用例 |
| `scripts/` | 新增“全量重建 / 指纹回填 / 孤儿巡检”手工脚本（可选） |

---

## 10. 风险与回滚方案

### 10.1 主要风险

| # | 风险 | 触发 | 缓解 |
| --- | --- | --- | --- |
| R1 | 唯一键替换失败 / 迁移中途失败 | 旧数据已有重复 `(document_id, version, chunk_index)` | 迁移前先查重并中止；分步 DDL；备份 `document_chunk`；脚本可重复执行 |
| R2 | 版本化 point ID 导致旧 points 失联 | 迁移后旧 points 用旧公式 | version 0 保留旧公式；旧文档在首次重建前仍可检索；全量重建后自然切换 |
| R3 | 角色/接口契约变化导致滚动部署不兼容 | 新后端 ↔ 旧 Agent，或反之 | Agent 同时接受“无 indexVersion”（走旧路径）与“有 indexVersion”；Spring 同时接受响应缺 fingerprint（降级并告警）；先部署 Agent，再部署 Spring |
| R4 | 重建期间检索质量下降 | 新版本 points 与旧版本共存；`doc_version` 过滤误伤 | 用 `doc_version` 精确过滤；legacy 过渡期 over-fetch；重建频率低 |
| R5 | 删除误删 / 无法恢复 | 用户误操作 | DELETE 仅软删 + 异步清向量；保留 `document`/`document_chunk` 行一段时间；恢复需重建向量（文件仍在时可行）；API 返回明确状态 |
| R6 | 巡检误报导致批量 FAILED | Qdrant 抖动 / collection 误删 | 默认只告警；连续 2 次 + 无依赖错误才考虑自动降级；开关默认关闭 |
| R7 | 清理任务误删新版本 | 版本条件写错 / 旧 worker | 清理只删 `index_version <> active` 且非活跃版本；所有清理带文档冻结条件；测试覆盖并发 |
| R8 | `index_attempts` 语义变化影响 UI/运维 | 旧代码把它当“尝试次数” | 保留 `index_attempts` 展示尝试次数；新增 `index_version` 为活跃令牌；文档写清 |
| R9 | 本地磁盘文件在多实例下不一致 | 多实例部署 | 当前单实例假设（KNOWN_LIMITATIONS）；多实例前必须换共享存储；删除/重建都依赖文件可读 |
| R10 | 指纹把 base_url 纳入后，网关迁移导致全量重建 | 换 endpoint | 这是预期行为；提供全量重建脚本/入口，并提前在文档中说明 |

### 10.2 分阶段上线

```text
阶段 0：只读设计（本轮）
阶段 1：schema 迁移 + Agent 兼容（接受无版本请求，按旧路径；新增 delete/verify）
阶段 2：Spring 写入链切换到“分阶段 + 切换”（用配置开关 versioned-index.enabled）
阶段 3：检索链切换（ready-ids 版本化 + fingerprint 过滤；保留 legacy 回退）
阶段 4：启用删除 / 重试 / 回收 / 巡检
阶段 5：对现有 READY 文档执行一次全量重建，补齐 fingerprint/collection/版本化 points
```

每一阶段都可独立回滚：

- 阶段 1：DDL 只增列，回滚 = 停止使用新列（保留列不影响旧代码）。
- 阶段 2：关闭 `versioned-index.enabled`，写入链回到旧顺序；已存在的新版本 chunk/points
  由清理脚本删除（`index_version <> active`），旧代码只读活跃版本。
- 阶段 3：关闭版本化检索开关，回到 `document_id IN` 过滤；新版本 points 仍带 `document_id`，
  可被旧逻辑检索到，但可能同时返回新旧版本 → 回滚前必须先清理非活跃版本。
- 阶段 4：删除/重试/巡检都是新增入口，停用即可；DELETING 文档由脚本补完。
- 阶段 5：全量重建是幂等操作，可重复执行。

### 10.3 回滚脚本要点

1. **停止所有索引/删除 worker**（配置开关或停实例）。
2. **清理非活跃版本**：
   ```sql
   DELETE c FROM document_chunk c
     JOIN document d ON d.id = c.document_id
    WHERE c.index_version <> d.index_version;
   ```
   Qdrant：删除 payload `index_version` 存在且 `doc_version` 不等于活跃键的 points；
   或按 `document_id` 重建。
3. **恢复唯一键**：
   ```sql
   ALTER TABLE document_chunk
       DROP INDEX uk_chunk_doc_ver_idx,
       ADD UNIQUE KEY uk_chunk_doc_idx (document_id, chunk_index);
   ```
4. **保留新列**（不 DROP，避免二次迁移风险）；旧代码忽略它们。
5. **若已产生版本化 points**：用旧代码对每个文档执行一次重建，旧代码的 `index_document`
   会按 `document_id` 删除全部 points 并用旧公式重写，收敛到旧格式。
6. **恢复 READY 文档的 `index_version`**：旧代码的 `complete` 会继续 `+1`；版本号只增不减，
   不影响旧代码的读取（旧代码不读 `index_version`）。

### 10.4 残余风险（接受并记录）

- Qdrant 被整体删除时，巡检发现前检索会返回 `NO_MATCH`（collection 存在但 points 空）；
  这是“可发现但非实时”的窗口，最小方案不引入每次查询的在线对账。
- `DELETED` 之后磁盘文件删除失败会留下孤儿文件；不影响召回，由运维清理。
- 正在运行的索引进程被强杀，分阶段 chunk 会残留；清理扫描器按“非活跃版本 + 年龄”删除。
- 多实例部署下，文件存储仍是本地磁盘；本方案只保证数据库层协调，不解决共享存储问题。

---

## 附录 A：当前关键代码索引

| 主题 | 位置 |
| --- | --- |
| 上传 / 去重 / 提交 | `backend/.../service/DocumentService.java:99-173` |
| 索引编排 / 状态 | `backend/.../service/DocumentIndexService.java:76-162` |
| chunk 替换 / 向量回写 | `backend/.../service/DocumentChunkService.java:37-83` |
| 白名单 SQL / 回收 SQL | `backend/.../mapper/DocumentMapper.java:28-100` |
| 删除接口缺失 | `backend/.../controller/DocumentController.java:40-71` |
| 状态枚举 | `backend/.../enums/DocumentStatus.java:8-21` |
| document 实体 | `backend/.../entity/Document.java:74-96` |
| document DDL | `backend/.../resources/schema.sql:399-424` |
| document_chunk DDL | `backend/.../resources/schema.sql:432-445` |
| RAG 迁移脚本 | `backend/.../resources/schema-migration-document-rag.sql` |
| Agent 索引路由 | `agent/app/documents/routes.py:66-136` |
| Qdrant store | `agent/app/vectorstore/qdrant_store.py:50-224` |
| 知识检索 | `agent/app/retriever/knowledge.py:61-264` |
| 白名单调用 | `agent/app/retriever/http_backend.py:144-183` |
| Embedding provider | `agent/app/embedding/{base,hash_provider,openai_compatible,factory}.py` |
| Agent 配置 | `agent/app/config.py:103-128` |

## 附录 B：本轮未做的事

- 未修改任何 `.java` / `.py` / `.sql` / `.yml` 业务文件。
- 未运行 `pytest` / `mvn test` / E2E。
- 未连接 MySQL / Qdrant / 真实 Embedding 服务。
- 未提交 Git。
- 未对历史数据执行迁移或重建。
