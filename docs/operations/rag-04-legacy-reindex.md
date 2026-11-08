# RAG-04 阶段 A：legacy 文档重建 Runbook

> 适用范围：`AI-Learning-Space` 后端 + Agent。
> 对应审计项：`docs/audit/KNOWN_LIMITATIONS.md` 的 **RAG-04**。
> 本阶段只提供“默认关闭”的严格指纹开关、只读观测与重建流程；**不自动重建、
> 不修改 collection 命名、不引入 DDL、不引入 deployment_id / model_revision**。

## 1. 背景

Embedding 指纹为 `v1:sha256(provider|model|dimension|endpoint_identity)`。
Qdrant collection 名只由 `<prefix>_<model>_<dimension>` 决定，**不含 endpoint**。
因此“同 model、同 dimension、不同 endpoint”的两批向量可能进入同一个 collection：

- 新文档写入 `embedding_fingerprint`；
- 迁移前的 legacy 文档 / points 没有 fingerprint；
- 默认（开关 false）下，后端白名单对 fingerprint 为 NULL 的 legacy 文档按
  model/provider/dimension 兼容放行，Agent 也接受没有 fingerprint 的 payload。

结果不是报错，而是 **不同向量空间被当作同一空间**，召回质量静默下降。
阶段 A 的目标是先用观测确认该风险是否真实存在，再通过重建让 legacy 文档获得
fingerprint，最后才考虑把严格开关翻转为 true。

## 2. 前置条件（必须按顺序确认）

1. **RAG-02 已完成**：跨 collection 清理/旧 collection 回收方案已落地。
2. **RAG-03 已完成**：MySQL chunk 与 Qdrant points 的反向对账已落地。
3. **严格开关已实现且默认 false**：
   - `ails.retrieval.require-embedding-fingerprint`
     （环境变量 `AILS_RETRIEVAL_REQUIRE_EMBEDDING_FINGERPRINT`，默认 `false`）；
   - 后端 `ready-ids` 严格分支、Agent payload 校验、Runtime 调用契约三处均已生效。
4. **产品明确签字**：同意在开启严格模式后，**未重建的 legacy 文档会暂时不可检索**
   （不是报错，而是从白名单中消失）。需要提前告知受影响用户或准备公告。
5. **维护窗口与回滚方案已确认**：
   - 窗口内允许重建期间的检索质量波动；
   - 回滚手段明确：把开关改回 `false` 并重启后端（Agent 同步恢复非严格行为）。

## 3. 观测：先确认风险是否真实存在

严格模式关闭时，`ready-ids` 路径会额外执行一次**只读**聚合（失败只记 warning，
不影响检索）：

```text
legacyReadyDocs=<n>, spaceId=<id>, model=<model>, legacyModels=<distinct>,
legacyCollections=<distinct>
```

- 仅当日志级别允许 INFO 且 `n > 0` 时输出；`n = 0` 时降级为 DEBUG，避免刷屏。
- 仓库没有引入 Micrometer 等指标框架，因此本阶段**只打日志，不新增依赖**。
- 观测查询与检索查询分离；它不改变白名单结果，也不会被 Agent 消费。

需要重点判断：同一 `model` 下是否出现**多个 collection**，或日志里出现
“同 model 但 endpoint 不同”的迹象。这正是 RAG-04 场景 2 是否真实发生的信号。

### 3.1 只读诊断端点（本阶段选择）

本阶段选择**内部诊断端点**而不是直接提供原始 SQL，原因是：

- 端点强制走 Space 所有权校验（`LearningSpaceService.ownedSpace`），
  跨用户访问统一 404，不会泄露其他用户文档；
- 返回结构化明细，便于重建脚本与审计；
- 原始 SQL 依赖操作者自觉加 `user_id + learning_space_id` 过滤，容易误查全表。

```http
GET /api/spaces/{spaceId}/documents/diagnostics/legacy?embeddingModel=<optional>
Authorization: Bearer <owner JWT>
```

响应（`ApiResponse.data`）：

```json
{
  "legacyReadyDocs": 12,
  "legacyModels": ["text-embedding-v4"],
  "legacyCollections": ["ails_documents_text-embedding-v4_1024"],
  "items": [
    {
      "id": 42,
      "userId": 7,
      "learningSpaceId": 9,
      "embeddingModel": "text-embedding-v4",
      "vectorCollection": "ails_documents_text-embedding-v4_1024",
      "indexedAt": "2026-10-01T12:00:00"
    }
  ]
}
```

**Agent 的正常检索从不调用该端点**。诊断结果只供 owner/运维排查和重建清单使用，
不能作为检索资格判定。代码路径与测试均守护这一点。

### 3.2 等价的只读 SQL（运维直查时使用）

> 仅供只读排查；执行时必须替换 `:userId`、`:spaceId`，不要跨用户查询。
> 该 SQL 统计 `embedding_fingerprint IS NULL` 的 READY 文档，与观测日志口径一致；
> 严格白名单还会额外排除空字符串 fingerprint（正常写入路径不会产生空串）。

```sql
SELECT d.id,
       d.user_id,
       d.learning_space_id,
       d.embedding_model,
       d.vector_collection,
       d.indexed_at
FROM document d
WHERE d.learning_space_id = :spaceId
  AND d.user_id = :userId
  AND d.status = 'READY'
  AND d.deleted_at IS NULL
  AND d.indexed_at IS NOT NULL
  AND d.embedding_model IS NOT NULL
  AND d.embedding_model <> ''
  AND d.embedding_fingerprint IS NULL
  AND d.chunk_count > 0
  AND d.chunk_count = (
      SELECT COUNT(*)
      FROM document_chunk c
      WHERE c.document_id = d.id
        AND c.index_version = d.index_version
        AND c.vector_ref IS NOT NULL
        AND c.vector_ref <> ''
  )
ORDER BY d.id;
```

## 4. 逐文档重建

对清单中的每个文档，由 **文档所属 Space 的 owner** 调用：

```http
POST /api/documents/{id}/reindex
Authorization: Bearer <owner JWT>
```

- 非 owner / 不存在 / 已软删：统一 404，不泄露存在性。
- READY 文档重建期间保持 READY，旧活跃版本继续服务；
- 重建成功后原子切换到新 `index_version`，新版本带非空 `embedding_fingerprint`；
- 重建失败时旧版本与 READY 状态原样保留，不会让文档“消失”。

### 4.1 批量执行建议

```bash
# 伪代码：不要并发轰炸，按文档串行或小并发执行
for id in $(cat legacy-document-ids.txt); do
  curl -fsS -X POST \
    -H "Authorization: Bearer $OWNER_JWT" \
    "https://api.example.com/api/documents/${id}/reindex"
  sleep 2
done
```

必须遵守现有索引配置：

- `ails.document.index.max-index-attempts=3`
  （`AILS_DOCUMENT_MAX_INDEX_ATTEMPTS`，默认 3）：
  同一文档最多自动重试 3 次，超过后置 FAILED；不要用脚本无限重试。
- `ails.document.index.reindex-cooldown-ms=30000`
  （`AILS_DOCUMENT_REINDEX_COOLDOWN_MS`，默认 30000 ms）：
  同一文档两次构建之间至少间隔 30 秒，冷却窗口内重复提交会被忽略。

建议每批处理后重新调用诊断端点，确认待重建数量单调下降。

## 5. 失败处理（必须作为产品决策显式确认）

重建失败不会自动让 legacy 文档继续可用；严格模式下失败的文档会从白名单消失。
因此需要产品在开启严格模式前明确选择：

| 选项 | 行为 | 代价 |
| --- | --- | --- |
| A. 阻塞上线 | 全部 legacy 文档重建成功后才开严格模式 | 窗口长，但无“暂时不可检索” |
| B. 分批放行 | 只对已完成重建的 Space/用户开启，其余保持 false | 需要按 Space 灰度能力 |
| C. 接受暂时不可检索 | 到窗口结束仍未成功的文档保持 legacy，严格模式下不可检索 | 必须公告，且要有补建计划 |

**本阶段不替产品做选择**。Runbook 只要求：选定选项、记录在变更单、明确补建负责人。
失败文档可通过 `index_error` 与 `GET /api/documents/{id}` 排查；修复依赖后重新
`POST /api/documents/{id}/reindex`，并注意 30 秒冷却与 3 次上限。

## 6. 开启严格模式

前置条件全部满足、且产品签字后：

```yaml
ails:
  retrieval:
    require-embedding-fingerprint: true
```

或：

```bash
AILS_RETRIEVAL_REQUIRE_EMBEDDING_FINGERPRINT=true
```

Agent 侧同步设置：

```bash
AILS_REQUIRE_EMBEDDING_FINGERPRINT=true
```

两侧都开启后：

- 后端 `ready-ids` 要求 `embeddingModel` + 非空 `embeddingFingerprint`，
  SQL 精确匹配 fingerprint，legacy 文档整体退出白名单；
- Agent 发送当前 fingerprint，并丢弃 payload 中缺失/空白/不匹配的 point；
- 正常检索不再存在“仅 model 匹配”的降级分支；诊断端点仍然是独立路径。

## 7. 验证

1. **诊断端点 / 观测日志**：目标 Space 的 `legacyReadyDocs` 降至 `0`。
2. **数据库抽查**：重建成功的文档满足
   `embedding_fingerprint IS NOT NULL AND embedding_fingerprint <> ''`。
3. **命中率对比**：开启严格模式前后，用同一批问题对比
   `KnowledgeStatus.SUCCESS` 比例、`candidates`、`invalid` 与空召回率。
   预期：短期内可能下降（legacy 文档退出），重建完成后应恢复或优于基线。
4. **Agent 日志**：严格模式下若出现“字段不完整”或 fingerprint mismatch 告警，
   说明仍有未重建 point 被 Qdrant 返回；按文档清单继续重建。
5. **回滚演练**：确认把开关改回 `false` 并重启后，legacy 文档重新进入白名单
   （行为与历史一致）。

## 8. 回滚

1. 后端：`AILS_RETRIEVAL_REQUIRE_EMBEDDING_FINGERPRINT=false` 并重启；
2. Agent：`AILS_REQUIRE_EMBEDDING_FINGERPRINT=false` 并重启；
3. 用诊断端点确认 legacy 文档重新可见（`legacyReadyDocs` 回升属于预期）；
4. 回滚不删除已重建版本，也不改变 collection 命名；已重建文档继续正常服务。

## 9. 明确不在本阶段范围

- 不自动翻转严格开关（默认 false）；
- 不自动重建任何 legacy 文档；
- 不修改 collection 命名规则；
- 不引入 `deployment_id` / `model_revision`；
- 不新增 DDL、不做数据迁移；
- 不做 RAG-02（跨 Collection 清理）与 RAG-03（反向对账）。

## 10. 指纹身份边界（务必知晓）

`v1:sha256(provider|model|dimension|endpoint_identity)` 能区分：

- provider 不同；
- model 名不同；
- dimension 不同；
- endpoint 身份不同（规范化后的 scheme + host + path）。

它**不能**保证：

- 模型权重绝对一致：供应商静默升级权重时指纹不变，无法检测；
- 同一 URL 背后按 API key / tenant 路由到不同后端：指纹相同但向量空间可能不同；
- 本批未引入 `deployment_id` / `model_revision`，无法表达部署级差异。

因此严格指纹是“显著降低混用概率”，不是“数学上证明向量空间一致”。
若未来需要更强保证，应在阶段 B 评估 `deployment_id` / `model_revision`
以及 collection 名加入 endpoint 身份。
