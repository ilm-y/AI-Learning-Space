# Phase 5 pre-Step3：统一数据快照与 C 策略验证脚本

本目录提供**快照准备、冻结检查与批次 3 的 C 策略执行器**。A/B 执行仍不在本目录。

## 前置

1. `start_prestep3_services.py` 拉起隔离 Agent + Spring，并生成 STATE_FILE；
   该脚本固定 `AILS_CONTEXT_STRATEGY=C`，并使用带只读上下文哈希诊断的
   `run_agent_prestep3.py`；
2. 分析消费者关闭（Understanding / Evidence / Memory 写回），补偿关闭；
3. 本机 `application-local.yml` 与 `agent/.env` 可用（脚本只读取，不打印密钥）。

## 运行顺序（批次 3：只跑 C）

```bash
# 0) 拉起隔离服务（固定 C），记录 STATE_FILE
python scripts/phase5/pre-step3/start_prestep3_services.py

# 1) 一条命令完成：快照 + Knowledge 索引 + 两组基线 + 3 次 C 验证 + 清理
python scripts/phase5/pre-step3/run_c_verification.py \
    --state <STATE_FILE> --cleanup

# 也可分步：
# 1a) 建统一快照（user/space/concept/session/单 conversation/历史/U/E/R/M）
python scripts/phase5/pre-step3/setup_snapshot.py <STATE_FILE> --apply --index-knowledge
# 1b) MySQL 冻结基线（15 表行哈希 + 生成参数指纹）
python scripts/phase5/pre-step3/freeze_check.py <snapshot> --write-baseline --assert-generation
# 1c) Qdrant 只读冻结基线（point 总数 + 每点 id/payload 哈希）
python scripts/phase5/pre-step3/qdrant_freeze_check.py <snapshot> --write-baseline
# 1d) 每次运行前：恢复快照并确认未漂移
python scripts/phase5/pre-step3/restore_snapshot.py <snapshot> --apply --prune-run-messages
python scripts/phase5/pre-step3/freeze_check.py <snapshot> --check
python scripts/phase5/pre-step3/qdrant_freeze_check.py <snapshot> --check
# 1e) 对同一 conversationId 发送同一个 NEUTRAL_QUESTION（3 次，串行）
# 1f) 每次运行后：再次 restore + 两组冻结检查（必须 PASS）
```

## 批次 3 的四项决策

1. **单会话 + 每组前恢复**：`restore_snapshot.py --apply` 在事务内做
   「删除前冻结校验 → 恢复 → 删除后冻结校验」；commit 后再独立计算并比对，
   任一不一致立即非零退出。每次运行后再 restore 一次。
2. **`--prune-run-messages` 的硬前提**：删除前冻结通过；删除范围严格为
   `conversation_id=<快照会话> AND created_at > <snapshotTime> AND id NOT IN
   (<快照历史 id>)`；删除后再次冻结；任一失败回滚并停止。
3. **前缀严格相等**：`AILS_QDRANT_COLLECTION_PREFIX` 必须逐字等于
   `AILS_TEST_ENV_ID`，`AILS_MEMORY_COLLECTION_PREFIX` 必须逐字等于
   `AILS_TEST_ENV_ID + "-memory"`；不接受任何后缀。
4. **Qdrant 只读冻结**：`qdrant_freeze_check.py` 只发 GET 与 scroll POST，
   不发任何写接口；`--write-baseline` / `--check` 的 point 数、id 集合或
   payload 任一变化即失败。

## 默认行为

| 脚本 | 默认 | 说明 |
|---|---|---|
| `setup_snapshot.py` | dry-run | 只校验隔离并打印计划；`--apply` 才写隔离 MySQL |
| `freeze_check.py` | `--check` | 只读（`SET SESSION TRANSACTION READ ONLY`），从不 commit |
| `restore_snapshot.py` | dry-run | 只打印 upsert；`--apply` 才写；默认不删除任何行 |
| `qdrant_freeze_check.py` | `--check` | 只读 GET/scroll；从不写入或删除 point |
| `run_c_verification.py` | 3 次 | 只跑 C；任一失败判据命中立即停止并落盘结果 |

## 冻结范围

- 15 张表：`user`、`learning_space`、`concept`、`learning_session`、`conversation`、
  `message`、`learning_understanding`、`learning_evidence`、`evidence_source`、
  `understanding_revision`、`memory`、`memory_source`、`document`、`document_chunk`、
  `document_index_version`；
- 排除运行期可变列：会话游标/最后活跃时间、文档索引重试与对账状态、生成列；
- 生成参数指纹：provider/model/temperature/max_tokens、top_k/min_score、预算、
  history limit、embedding provider/model/dimension、`BASE_SYSTEM_INSTRUCTION` SHA-256。

## 注意

- `restore_snapshot.py --prune-run-messages` 只删除**同一 conversation 内、
  `snapshotTime` 之后、且不属于快照历史消息列表**的消息，即实验自己写出的消息；
  三个条件缺一不可，不加该参数则不会删除任何行。
- 它不会清理分析写回产生的 Understanding/Evidence/Memory 新行——分析消费者必须关闭。
- Qdrant 冻结由 `qdrant_freeze_check.py` 独立完成；默认 Memory collection 不写入
  基线，执行器另做前后只读比对。
- `run_c_verification.py` 不输出任何密钥、token、连接串密码或完整 prompt；
  只保存 hash、id、状态与计数。
- 快照账号仅存在于隔离库，口令常量在 `snapshot_common.py`，不得用于任何真实环境。
