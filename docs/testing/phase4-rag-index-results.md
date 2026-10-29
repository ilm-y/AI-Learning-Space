# Phase 4 索引与生命周期实测结果

- 日期：2026-10-28
- 运行批次：`20261027_172403`
- 真实服务：Qdrant `127.0.0.1:6333`、Embedding `text-embedding-v4`（1024 维）、
  Spring `127.0.0.1:8081`、Agent `127.0.0.1:8000`
- 判定：索引/生命周期硬检查 **7/7 PASS**；真实调用全部来自上述服务，没有
  hash embedding 或 Mock。

## 0. 前置配置审查与最小安全修复（PASS）

### 0.1 问题

Phase 3.3 已证明：正常启动的 Agent 进程没有 `AILS_INTERNAL_DELEGATION_SECRET`，
但额外注入该变量后进程仍会启动。也就是说，“正常配置不持有密钥”成立，
“配置层 fail closed”不成立。

### 0.2 修复

`agent/app/config.py` 增加一个只用于启动校验的字段
`internal_delegation_secret`（`exclude=True`、`repr=False`），并加
`field_validator`：

- 缺失：字段为 `""`，正常启动；
- 空白：视为未配置，正常启动；
- 非空（来自进程环境、`.env` 或 kwargs）：`ValidationError`，HTTP/Worker
  都无法启动；
- 错误信息只说明检测到该变量，不回显值；
- `describe()` 不包含该字段。

这不是身份协议的一部分，也**不能**阻止拥有修改代码或启动参数权限的人。
它只覆盖“运行配置里被误注入该密钥”的情况。

### 0.3 验证

| 验证 | 结果 |
| --- | --- |
| `agent/tests/test_config_security.py` | **6 passed** |
| Agent 全量 | **337 passed / 5 skipped / 1 warning** |
| 注入密钥真实启动 | exit code **3**；日志出现 `AILS_INTERNAL_DELEGATION_SECRET`；密钥值出现 **0** 次；`Application startup failed` |
| 不注入密钥真实启动 | `/health` **200**；停止后 8011 端口关闭 |
| 密钥值泄漏检查 | 单元测试与启动日志均未出现密钥值 |

真实启动验证使用独立端口 8011，没有干扰 8000 上的 Phase 4 Agent。注入的
测试密钥是临时合成值，未写入报告或 Git。

## 1. 环境与配置

| 项 | 实测值 |
| --- | --- |
| Embedding provider | `openai`（OpenAI 兼容） |
| Embedding 模型 | `text-embedding-v4` |
| Embedding 维度 | 1024 |
| `embedding_batch_size` | 10（本轮每文档 1 chunk，未触发多批） |
| Qdrant collection | `ails_phase4_e2e_text-embedding-v4_1024` |
| collection 维度 | 1024（与 Embedding 一致） |
| `knowledge_top_k` | 5 |
| `knowledge_min_score` | 0.35（未临时调整） |
| LLM | `deepseek-flash`（真实调用，见 Agent 报告） |
| 隔离 DB | `ails_phase4_e2e` |

## 2. 文档生命周期

### 2.1 READY 文档

| 角色 | document_id | chunk_id | 等待方式 | 最终状态 | chunk_count | indexed_at | embedding_model |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A1 目标 | 20 | 18 | wait_ready 轮询至 READY | READY | 1 | 2026-10-28T01:24:04 | text-embedding-v4 |
| A1 干扰 | 21 | 19 | wait_ready 轮询至 READY | READY | 1 | 2026-10-28T01:24:06 | text-embedding-v4 |
| A2 同用户其他空间 | 22 | 20 | wait_ready 轮询至 READY | READY | 1 | 2026-10-28T01:24:08 | text-embedding-v4 |
| B1 其他用户 | 23 | 21 | wait_ready 轮询至 READY | READY | 1 | 2026-10-28T01:24:10 | text-embedding-v4 |

本批次只记录最终 READY，没有逐文档记录中间态耗时；早期探针曾观察到
`PROCESSING → READY`，但不把该观察套到这四个文档上。每个 READY 文档都满足：

- `vector_ref` 非空；
- MySQL chunk 数 = Qdrant point 数 = `document.chunk_count`；
- Qdrant payload 的 `user_id`、`space_id`、`document_id`、`chunk_id`、
  `chunk_index`、`document_name`、`file_name`、`content` 与 MySQL/文档一致；
- collection 的向量维度 = Embedding 维度 = 1024。

**没有把“DB 里有 chunk”或“Qdrant collection 存在”当成 RAG 成功**；上述检查
逐项比对了两边的 ID、正文与来源元数据。

### 2.2 FAILED 文档

| 项 | 值 |
| --- | --- |
| document_id | 2 |
| 名称 | `phase4_failed_20261028_011700` |
| 状态 | `FAILED` |
| `index_attempts` | 1 |
| `index_error` | `Agent 服务未启动或无法连接` |
| `chunk_count` | 0 |
| MySQL chunk | 0 |
| Qdrant point | 0 |
| `embedding_model` / `indexed_at` | NULL / NULL |
| 进 ready-ids | 否 |

这是**故障注入**：先停 Agent，再上传。后端把失败落在文档状态上，没有伪造
READY，也没有留下 chunk/point。

### 2.3 PROCESSING 文档（中断态）

| 项 | 值 |
| --- | --- |
| document_id | 3 |
| 名称 | `phase4_processing` |
| 状态 | `PROCESSING` |
| `index_attempts` | 1 |
| `index_error` | NULL |
| `chunk_count` | 0 |
| MySQL chunk | 0 |
| Qdrant point | 0 |
| 进 ready-ids | 否 |

这是**中断注入**：用一个假 TCP 服务挂住 parse 请求，再杀掉后端；后端重启后
该行仍停在 PROCESSING。它不是“索引正在慢慢跑”的正常状态，而是没有租约/
超时回收的孤儿状态。风险见 [phase4-failure-analysis.md](phase4-failure-analysis.md)。

### 2.4 READY 白名单

后端 `/ready-ids?embeddingModel=text-embedding-v4` 实际返回：

| 空间 | ready IDs | 是否精确 |
| --- | --- | --- |
| A1（user 3, space 3） | `[20, 21]` | 是；不含 2、3、22、23 |
| A2（user 3, space 12） | `[22]` | 是 |
| B1（user 8, space 13） | `[23]` | 是 |

同时验证：

- FAILED 2 不在 A1；
- PROCESSING 3 不在 A1；
- A2 的 22 不在 A1；
- B1 的 23 不在 A1。

`selectReadyIndexedIds` 的 SQL 还要求 `chunk_count ==` 有 `vector_ref` 的
chunk 数，因此“只写了一部分 chunk”的文档不会进入白名单。

## 3. 检查汇总

`index_results.json` 共 7 项：

| 项 | 结果 |
| --- | --- |
| 4 个 READY 文档的 DB/Qdrant 一致性 | 4/4 PASS |
| FAILED 状态与零 chunk/point | PASS |
| PROCESSING 状态与零 chunk/point | PASS |
| ready-ids 空间/用户/状态过滤 | PASS |
| **合计** | **7/7 PASS** |

## 4. 证据

- `evaluation/phase4/reports/20261027_172403/index_results.json`
- `evaluation/phase4/reports/20261027_172403/manifest.json`
- `evaluation/phase4/run_rag_e2e.py`

## 5. 边界

- 每个文档只有 1 个 chunk，未验证多 chunk 文档的切块边界、批量 Embedding
  请求边界（`batch_size=10` 未被真正触发）、部分写入失败后的回滚；
- 没有验证真实生产规模的点数、索引耗时或并发上传；
- Qdrant collection 在报告生成后已随本轮隔离资源清理；原有三个 collection
  未删除。
