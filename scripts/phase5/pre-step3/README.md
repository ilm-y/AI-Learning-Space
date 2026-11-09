# Phase 5 pre-Step3：统一数据快照脚本

本目录只提供**快照准备与冻结检查**，不实现 A/B/C 执行调度（执行留批次 3）。

## 前置

1. `scripts/phase5/pilot/start_services.py` 已拉起隔离 Agent + Spring，并生成 STATE_FILE；
2. 分析消费者关闭（Understanding / Evidence / Memory 写回），补偿关闭；
3. 本机 `application-local.yml` 与 `agent/.env` 可用（脚本只读取，不打印密钥）。

## 运行顺序（批次 3）

```bash
# 1) 建统一快照（user/space/concept/session/单 conversation/历史/U/E/R/M）
#    --index-knowledge 会经 Spring 上传并索引文档（调用 Embedding）
python scripts/phase5/pre-step3/setup_snapshot.py <STATE_FILE> --apply --index-knowledge

# 2) 冻结基线（行数 + SHA-256 + 生成参数指纹）
python scripts/phase5/pre-step3/freeze_check.py <tempDir>/prestep3-snapshot.json \
    --write-baseline --assert-generation

# 3) 每组运行前：恢复快照并确认未漂移
python scripts/phase5/pre-step3/restore_snapshot.py <snapshot> --apply --prune-run-messages
python scripts/phase5/pre-step3/freeze_check.py <snapshot> --check

# 4) 用 AILS_CONTEXT_STRATEGY=A|B|C 重启 Agent（Spring/MySQL/Qdrant 不动），
#    对同一 conversationId 发送同一个 NEUTRAL_QUESTION

# 5) 每组运行后：再次冻结检查（必须 PASS）
python scripts/phase5/pre-step3/freeze_check.py <snapshot> --check
```

## 默认行为

| 脚本 | 默认 | 说明 |
|---|---|---|
| `setup_snapshot.py` | dry-run | 只校验隔离并打印计划；`--apply` 才写隔离 MySQL |
| `freeze_check.py` | `--check` | 只读（`SET SESSION TRANSACTION READ ONLY`），从不 commit |
| `restore_snapshot.py` | dry-run | 只打印 upsert；`--apply` 才写；默认不删除任何行 |

## 冻结范围

- 15 张表：`user`、`learning_space`、`concept`、`learning_session`、`conversation`、
  `message`、`learning_understanding`、`learning_evidence`、`evidence_source`、
  `understanding_revision`、`memory`、`memory_source`、`document`、`document_chunk`、
  `document_index_version`；
- 排除运行期可变列：会话游标/最后活跃时间、文档索引重试与对账状态、生成列；
- 生成参数指纹：provider/model/temperature/max_tokens、top_k/min_score、预算、
  history limit、embedding provider/model/dimension、`BASE_SYSTEM_INSTRUCTION` SHA-256。

## 注意

- `restore_snapshot.py --prune-run-messages` 只删除**同一 conversation 内、不属于快照
  历史消息列表**的消息，即三组实验自己写出的消息；不加该参数则不会删除任何行。
- 它不会清理分析写回产生的 Understanding/Evidence/Memory 新行——分析消费者必须关闭。
- Qdrant point/payload 哈希未包含在本脚本内；如需严格冻结，批次 3 另行核对。
- 快照账号仅存在于隔离库，口令常量在 `snapshot_common.py`，不得用于任何真实环境。
