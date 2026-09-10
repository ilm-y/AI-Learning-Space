# AI Learning Space — Database & API Design

---

# 一、数据库设计

## 1. user

| 字段          | 类型         | 说明     |
| ------------- | ------------ | -------- |
| id            | BIGINT PK    | 用户 ID  |
| username      | VARCHAR(64)  | 用户名   |
| password_hash | VARCHAR(255) | 密码哈希 |
| email         | VARCHAR(255) | 邮箱     |
| created_at    | DATETIME     | 创建时间 |
| updated_at    | DATETIME     | 更新时间 |

---

# 2. learning_space

| 字段        | 类型         | 说明             |
| ----------- | ------------ | ---------------- |
| id          | BIGINT PK    | 空间 ID          |
| user_id     | BIGINT FK    | 所属用户         |
| name        | VARCHAR(128) | 空间名称         |
| description | TEXT         | 描述             |
| visibility  | ENUM         | PRIVATE / SHARED |
| created_at  | DATETIME     | 创建时间         |
| updated_at  | DATETIME     | 更新时间         |

---

# 3. conversation

| 字段              | 类型         | 说明              |
| ----------------- | ------------ | ----------------- |
| id                | BIGINT PK    | 对话 ID           |
| user_id           | BIGINT FK    | 用户              |
| learning_space_id | BIGINT FK    | 学习空间          |
| title             | VARCHAR(255) | 标题              |
| status            | ENUM         | ACTIVE / ARCHIVED |
| last_message_at   | DATETIME     | 最后消息时间      |
| created_at        | DATETIME     | 创建时间          |
| updated_at        | DATETIME     | 更新时间          |

INDEX:

```text
(user_id, learning_space_id, updated_at)
```

---

# 4. message

| 字段            | 类型      | 说明                             |
| --------------- | --------- | -------------------------------- |
| id              | BIGINT PK | 消息 ID                          |
| conversation_id | BIGINT FK | 对话                             |
| role            | ENUM      | USER / ASSISTANT / SYSTEM / TOOL |
| message_type    | ENUM      | TEXT / TEST / EVENT              |
| content         | LONGTEXT  | 消息内容                         |
| tokens          | INT       | token 数                         |
| sequence_no     | INT       | 对话顺序                         |
| created_at      | DATETIME  | 创建时间                         |

INDEX:

```text
(conversation_id, sequence_no)
```

---

# 5. highlight

| 字段          | 类型      | 说明                                                                             |
| ------------- | --------- | -------------------------------------------------------------------------------- |
| id            | BIGINT PK | 批注 ID                                                                          |
| user_id       | BIGINT FK | 用户                                                                             |
| message_id    | BIGINT FK | 消息                                                                             |
| selected_text | TEXT      | 选中的文字                                                                       |
| start_offset  | INT       | 起点                                                                             |
| end_offset    | INT       | 终点                                                                             |
| mark_type     | ENUM      | IMPORTANT / QUESTION / MY_UNDERSTANDING / MISCONCEPTION_CANDIDATE / CONCEPT_LINK |
| note          | TEXT      | 用户备注                                                                         |
| created_at    | DATETIME  | 创建时间                                                                         |

注意保存：

```text
start_offset
end_offset
```

以后才能可靠地定位原始消息中的选区。

---

# 6. concept

| 字段              | 类型         | 说明     |
| ----------------- | ------------ | -------- |
| id                | BIGINT PK    | Concept  |
| learning_space_id | BIGINT FK    | 所属空间 |
| canonical_name    | VARCHAR(255) | 标准名称 |
| display_name      | VARCHAR(255) | 显示名称 |
| description       | TEXT         | 描述     |
| difficulty_level  | INT          | 难度     |
| created_at        | DATETIME     | 创建     |
| updated_at        | DATETIME     | 更新     |

UNIQUE:

```text
(learning_space_id, canonical_name)
```

---

# 7. concept_relation

| 字段              | 类型      | 说明                                        |
| ----------------- | --------- | ------------------------------------------- |
| id                | BIGINT PK | ID                                          |
| source_concept_id | BIGINT FK | 源                                          |
| target_concept_id | BIGINT FK | 目标                                        |
| relation_type     | ENUM      | PREREQUISITE / RELATED / PART_OF / CONTRAST |
| weight            | FLOAT     | 关系权重                                    |
| created_at        | DATETIME  | 创建时间                                    |

UNIQUE:

```text
(source_concept_id, target_concept_id, relation_type)
```

---

# 8. learning_understanding

> 当前理解快照。

| 字段                    | 类型      | 说明                                                       |
| ----------------------- | --------- | ---------------------------------------------------------- |
| id                      | BIGINT PK | ID                                                         |
| user_id                 | BIGINT FK | 用户                                                       |
| concept_id              | BIGINT FK | Concept                                                    |
| summary                 | TEXT      | 当前理解总结                                               |
| known_parts             | JSON      | 已理解部分                                                 |
| misconceptions          | JSON      | 当前/历史误区                                              |
| uncertain_parts         | JSON      | 不确定部分                                                 |
| latest_user_explanation | TEXT      | 最近一次用户解释                                           |
| status                  | ENUM      | UNKNOWN / EXPOSED / PARTIAL / DEMONSTRATED / MISCONCEPTION |
| current_revision_id     | BIGINT    | 当前 Revision                                              |
| updated_at              | DATETIME  | 更新时间                                                   |

UNIQUE:

```text
(user_id, concept_id)
```

这里的重点：

> 它是当前快照，不承担历史记录。

---

# 9. understanding_revision

> 用户理解的历史版本。

| 字段                | 类型      | 说明                |
| ------------------- | --------- | ------------------- |
| id                  | BIGINT PK | Revision ID         |
| understanding_id    | BIGINT FK | Understanding       |
| revision_no         | INT       | 版本号              |
| summary             | TEXT      | 当时总结            |
| known_parts         | JSON      | 当时已知            |
| misconceptions      | JSON      | 当时误区            |
| uncertain_parts     | JSON      | 当时不确定          |
| user_explanation    | TEXT      | 当时用户解释        |
| status              | ENUM      | 当时状态            |
| trigger_evidence_id | BIGINT FK | 导致变化的 Evidence |
| created_at          | DATETIME  | 创建时间            |

UNIQUE:

```text
(understanding_id, revision_no)
```

---

# 10. learning_state

> 轻量状态，可从 Understanding 派生。

| 字段             | 类型         | 说明                                                       |
| ---------------- | ------------ | ---------------------------------------------------------- |
| user_id          | BIGINT PK/FK | 用户                                                       |
| concept_id       | BIGINT PK/FK | Concept                                                    |
| status           | ENUM         | UNKNOWN / EXPOSED / PARTIAL / DEMONSTRATED / MISCONCEPTION |
| next_review_at   | DATETIME     | 下次复习                                                   |
| review_priority  | INT          | 优先级                                                     |
| last_evidence_at | DATETIME     | 最近证据                                                   |
| updated_at       | DATETIME     | 更新时间                                                   |

第一版不保存：

```text
mastery
confidence
stability
```

---

# 11. learning_evidence

> 对用户认知判断的原子证据。

| 字段                      | 类型      | 说明                                    |
| ------------------------- | --------- | --------------------------------------- |
| id                        | BIGINT PK | Evidence                                |
| user_id                   | BIGINT FK | 用户                                    |
| concept_id                | BIGINT FK | Concept                                 |
| understanding_id          | BIGINT FK | 关联理解                                |
| observation               | TEXT      | 观察                                    |
| correctness               | ENUM      | CORRECT / INCORRECT / PARTIAL / UNKNOWN |
| reasoning_quality         | FLOAT     | 推理质量                                |
| specificity               | FLOAT     | 针对性                                  |
| ai_confidence             | FLOAT     | AI 置信度                               |
| need_follow_up            | BOOLEAN   | 是否需要跟进                            |
| follow_up_status          | ENUM      | NONE / PENDING / RESOLVED               |
| follow_up_conversation_id | BIGINT FK | 后续对话                                |
| created_at                | DATETIME  | 创建                                    |

---

# 12. evidence_source

> Evidence 的真实来源。

| 字段        | 类型      | 说明                              |
| ----------- | --------- | --------------------------------- |
| id          | BIGINT PK | ID                                |
| evidence_id | BIGINT FK | Evidence                          |
| source_type | ENUM      | MESSAGE / HIGHLIGHT / TEST_ANSWER |
| source_id   | BIGINT    | 对应源 ID                         |
| created_at  | DATETIME  | 创建                              |

UNIQUE:

```text
(evidence_id, source_type, source_id)
```

说明：

`source_id` 属于 polymorphic reference，由应用层验证真实对象存在。

---

# 13. memory

> Agent 长期记忆。

| 字段              | 类型               | 说明                          |
| ----------------- | ------------------ | ----------------------------- |
| id                | BIGINT PK          | Memory ID                     |
| user_id           | BIGINT FK          | 用户                          |
| learning_space_id | BIGINT FK nullable | 可选学习空间                  |
| concept_id        | BIGINT FK nullable | 可选 Concept                  |
| memory_type       | ENUM               | USER / LEARNING / PREFERENCE  |
| path              | VARCHAR(255)       | 层次路径                      |
| content           | TEXT               | Memory 内容                   |
| status            | ENUM               | CANDIDATE / ACTIVE / ARCHIVED |
| importance        | FLOAT              | 重要性                        |
| embedding_ref     | VARCHAR(255)       | 向量存储引用                  |
| created_at        | DATETIME           | 创建                          |
| updated_at        | DATETIME           | 更新                          |

---

# 14. memory_source

> Memory 的来源。

| 字段        | 类型      | 说明                                         |
| ----------- | --------- | -------------------------------------------- |
| id          | BIGINT PK | ID                                           |
| memory_id   | BIGINT FK | Memory                                       |
| source_type | ENUM      | MESSAGE / HIGHLIGHT / EVIDENCE / TEST_ANSWER |
| source_id   | BIGINT    | 来源                                         |
| created_at  | DATETIME  | 创建                                         |

UNIQUE:

```text
(memory_id, source_type, source_id)
```

---

# 15. test_task

| 字段              | 类型      | 说明                                      |
| ----------------- | --------- | ----------------------------------------- |
| id                | BIGINT PK | Task                                      |
| user_id           | BIGINT FK | 用户                                      |
| concept_id        | BIGINT FK | Concept                                   |
| conversation_id   | BIGINT FK | 对话                                      |
| question_type     | ENUM      | SINGLE_CHOICE / TRUE_FALSE / SHORT_ANSWER |
| question_text     | TEXT      | 问题                                      |
| options           | JSON      | 选项                                      |
| correct_answer    | TEXT      | 标准答案                                  |
| evaluation_rubric | JSON      | 简答评价标准                              |
| hint              | TEXT      | 提示                                      |
| status            | ENUM      | PENDING / ANSWERED / EXPIRED              |
| created_at        | DATETIME  | 创建                                      |
| expired_at        | DATETIME  | 过期                                      |

---

# 16. test_answer

| 字段        | 类型      | 说明     |
| ----------- | --------- | -------- |
| id          | BIGINT PK | Answer   |
| task_id     | BIGINT FK | Task     |
| user_answer | TEXT      | 用户答案 |
| is_correct  | BOOLEAN   | 是否正确 |
| evaluation  | JSON      | 评价信息 |
| answered_at | DATETIME  | 回答时间 |

---

# 17. learning_session

> 一次学习活动容器。

| 字段              | 类型         | 说明                           |
| ----------------- | ------------ | ------------------------------ |
| id                | BIGINT PK    | Session                        |
| user_id           | BIGINT FK    | 用户                           |
| learning_space_id | BIGINT FK    | 空间                           |
| title             | VARCHAR(255) | 标题                           |
| current_focus     | VARCHAR(255) | 当前关注点                     |
| status            | ENUM         | ACTIVE / COMPLETED / ABANDONED |
| started_at        | DATETIME     | 开始                           |
| last_active_at    | DATETIME     | 最近活动                       |
| completed_at      | DATETIME     | 完成                           |

注意：

Session 不承担长期记忆。

---

# 18. document

| 字段              | 类型          | 说明                                    |
| ----------------- | ------------- | --------------------------------------- |
| id                | BIGINT PK     | ID                                      |
| learning_space_id | BIGINT FK     | 空间                                    |
| name              | VARCHAR(255)  | 文档名                                  |
| file_name         | VARCHAR(255)  | 原文件                                  |
| storage_uri       | VARCHAR(1024) | 文件地址                                |
| file_type         | VARCHAR(64)   | 类型                                    |
| size              | BIGINT        | 大小                                    |
| status            | ENUM          | UPLOADING / PROCESSING / READY / FAILED |
| parser_version    | VARCHAR(64)   | 解析版本                                |
| created_at        | DATETIME      | 创建                                    |
| updated_at        | DATETIME      | 更新                                    |

---

# 19. document_chunk

| 字段          | 类型         | 说明       |
| ------------- | ------------ | ---------- |
| id            | BIGINT PK    | ID         |
| document_id   | BIGINT FK    | 文档       |
| chunk_index   | INT          | 序号       |
| content       | TEXT         | 内容       |
| page_number   | INT          | 页码       |
| token_count   | INT          | Token 数   |
| vector_ref    | VARCHAR(255) | 向量库引用 |
| metadata_json | JSON         | 元数据     |
| created_at    | DATETIME     | 创建       |

---

# 20. learning_plan

| 字段              | 类型         | 说明                          |
| ----------------- | ------------ | ----------------------------- |
| id                | BIGINT PK    | ID                            |
| user_id           | BIGINT FK    | 用户                          |
| learning_space_id | BIGINT FK    | 空间                          |
| title             | VARCHAR(255) | 标题                          |
| description       | TEXT         | 描述                          |
| goal              | TEXT         | 目标                          |
| status            | ENUM         | ACTIVE / COMPLETED / ARCHIVED |
| created_at        | DATETIME     | 创建                          |
| updated_at        | DATETIME     | 更新                          |

第一版只做目标清单，不做复杂排课。

---

# 21. import_task

| 字段              | 类型         | 说明                                      |
| ----------------- | ------------ | ----------------------------------------- |
| id                | BIGINT PK    | ID                                        |
| user_id           | BIGINT FK    | 用户                                      |
| learning_space_id | BIGINT FK    | 空间                                      |
| file_name         | VARCHAR(255) | 文件                                      |
| source_type       | ENUM         | CHATGPT / CLAUDE / GEMINI / OTHER         |
| status            | ENUM         | PENDING / PROCESSING / COMPLETED / FAILED |
| progress          | INT          | 进度                                      |
| total_messages    | INT          | 总消息数                                  |
| error_message     | TEXT         | 错误                                      |
| created_at        | DATETIME     | 创建                                      |
| updated_at        | DATETIME     | 更新                                      |

---

# 22. agent_run

| 字段            | 类型      | 说明                                       |
| --------------- | --------- | ------------------------------------------ |
| id              | BIGINT PK | Agent 执行 ID                              |
| user_id         | BIGINT FK | 用户                                       |
| conversation_id | BIGINT FK | 对话                                       |
| session_id      | BIGINT FK | Session                                    |
| run_type        | ENUM      | CHAT / EXTRACTION / UPDATE / TEST / MEMORY |
| status          | ENUM      | RUNNING / COMPLETED / FAILED               |
| input_summary   | TEXT      | 输入摘要                                   |
| output_summary  | TEXT      | 输出摘要                                   |
| duration_ms     | INT       | 执行时间                                   |
| error_stack     | TEXT      | 错误                                       |
| created_at      | DATETIME  | 创建                                       |
| finished_at     | DATETIME  | 完成                                       |

---

# 二、核心关系

```text
USER
 │
 └── LEARNING_SPACE
       │
       ├── CONVERSATION
       │      └── MESSAGE
       │             └── HIGHLIGHT
       │
       ├── CONCEPT
       │      └── CONCEPT_RELATION
       │
       ├── DOCUMENT
       │      └── DOCUMENT_CHUNK
       │
       └── LEARNING_SESSION


MESSAGE
HIGHLIGHT
TEST_ANSWER
       │
       ↓
    EVIDENCE
       │
       ↓
┌──────┴────────┐
↓               ↓
UNDERSTANDING   MEMORY
↓               ↓
REVISION       MEMORY_SOURCE

UNDERSTANDING
      ↓
LEARNING_STATE
```

---

# 三、核心业务接口

## Auth

### POST `/api/auth/register`

```json
{
  "username": "alice",
  "email": "alice@example.com",
  "password": "******"
}
```

### POST `/api/auth/login`

返回 JWT。

---

# Space

### GET `/api/spaces`

获取学习空间。

### POST `/api/spaces`

```json
{
  "name": "操作系统",
  "description": "OS 学习空间"
}
```

### GET `/api/spaces/{spaceId}`

---

# Conversation

### GET `/api/spaces/{spaceId}/conversations`

### POST `/api/spaces/{spaceId}/conversations`

```json
{
  "title": "TLB 与 Page Fault"
}
```

### GET `/api/conversations/{conversationId}/messages`

---

# Chat

## POST `/api/conversations/{conversationId}/messages`

请求：

```json
{
  "content": "TLB Miss 和 Page Fault 到底是什么关系？"
}
```

响应：

```text
text/event-stream
```

事件：

```text
message.start
message.delta
message.delta
message.end
```

---

# Highlight

## POST `/api/messages/{messageId}/highlights`

```json
{
  "selectedText": "TLB 是页表的缓存",
  "startOffset": 120,
  "endOffset": 130,
  "markType": "MY_UNDERSTANDING",
  "note": "我的理解"
}
```

---

# Understanding

## GET `/api/understandings`

支持：

```text
spaceId
conceptId
status
```

---

## GET `/api/understandings/{conceptId}`

返回：

```json
{
  "concept": "TLB",
  "summary": "用户理解 TLB 是页表缓存。",
  "knownParts": [],
  "misconceptions": [],
  "uncertainParts": [],
  "status": "PARTIAL",
  "currentRevision": 4
}
```

---

## GET `/api/understandings/{conceptId}/revisions`

查看历史理解变化。

---

# Evidence

## GET `/api/understandings/{conceptId}/evidence`

返回该 Concept 的证据。

---

## GET `/api/evidence/{evidenceId}`

返回：

```text
Evidence
↓
Sources
↓
Message / Highlight / Test Answer
```

---

# Memory

## GET `/api/memories`

支持：

```text
type
path
spaceId
conceptId
status
```

---

## POST `/api/memories`

允许用户手动创建长期 Memory。

---

## PATCH `/api/memories/{id}`

修改 Memory。

---

## DELETE `/api/memories/{id}`

删除 Memory。

---

# Test

## POST `/api/tests`

Agent 创建测试。

---

## POST `/api/tests/{testId}/answer`

```json
{
  "answer": "..."
}
```

---

# State

## GET `/api/learning-state/attention`

返回：

```text
需要复盘
存在误区
长期未验证
```

---

## GET `/api/learning-state/topic-coverage`

返回空间的话题覆盖度。

---

# Documents

## POST `/api/spaces/{spaceId}/documents`

上传文档。

---

## GET `/api/spaces/{spaceId}/documents`

---

## GET `/api/documents/{id}/status`

---

# Export

## POST `/api/exports/notes`

```json
{
  "spaceId": 1,
  "conceptIds": [10, 11],
  "format": "markdown"
}
```

---

## POST `/api/exports/mindmap`

```json
{
  "spaceId": 1,
  "rootConceptId": 10
}
```

---

# Import

## POST `/api/imports`

导入：

```text
ChatGPT
Claude
Gemini
```

---

## GET `/api/imports/{id}`

查看进度。

---

# 四、Agent Tools

Learning Agent 暴露以下 tools：

```text
retrieve_understanding
retrieve_memory
retrieve_evidence
retrieve_knowledge

create_evidence

update_understanding
create_understanding_revision

create_memory_candidate
update_memory
archive_memory

detect_evidence_gap
create_test

update_learning_state
```

---

# 五、Agent 的 Context Schema

最终给 Tutor 的上下文建议：

```json
{
  "current_conversation": [],
  "understandings": [],
  "memories": [],
  "evidence": [],
  "knowledge": []
}
```

其中：

```text
current_conversation
最高优先级

current user statement
最高优先级

historical understanding
中高优先级

memory
中优先级

knowledge
中优先级

old evidence
按需召回
```

---

# 六、重要数据规则

## Rule 1

当前用户明确表达：

> “不是，我现在已经明白了。”

优先于旧 Memory。

---

## Rule 2

新 Evidence 不能删除旧 Evidence。

---

## Rule 3

Understanding 更新必须产生 Revision。

---

## Rule 4

Memory 的删除不删除原始 Evidence。

---

## Rule 5

删除 Memory 不影响学习历史。

---

## Rule 6

删除 Conversation 时，不应该无条件删除 Evidence。

建议：

```text
Conversation
  ↓
soft delete
```

历史证据独立保存。

---

# 七、第一版不建议做

```text
复杂知识图谱推理
自动生成完整课程
复杂推荐算法
多 Agent 协作
Kafka
Kubernetes
复杂 RBAC
复杂社交系统
```

先证明：

```text
跨对话理解
+
Evidence
+
Understanding
+
Memory
```

真正成立。
