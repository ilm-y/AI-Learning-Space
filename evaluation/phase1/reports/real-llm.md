# Phase 1 Agent 评测结果

- runId: `20261027T125656Z`
- 生成时间: 2026-10-27T20:56:56.304984+08:00
- Python: 3.10.11
- 用例总数: 3

## 总览（状态口径）

| 状态 | 数量 |
| --- | ---: |
| PASS | 0 |
| FAIL | 0 |
| BLOCKED | 3 |
| SKIPPED | 0 |
| NOT_IMPLEMENTED | 0 |

实际执行（PASS+FAIL+BLOCKED）: 3；得出明确结论（PASS+FAIL）: 0；BLOCKED: 3；设计登记未执行（SKIPPED）：0；真实模型用例: 3。
统计自检：通过

## 分类

| 类别 | 名称 | 总数 | PASS | FAIL | BLOCKED | SKIPPED |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| B | 证据约束与回答可靠性 | 1 | 0 | 0 | 1 | 0 |
| D | RAG 检索与知识边界 | 1 | 0 | 0 | 1 | 0 |
| E | 教学任务与对话质量 | 1 | 0 | 0 | 1 | 0 |

## 执行类别（真实 / Mock / Fixture / Fallback）

| 类别 | 数量 | 说明 |
| --- | ---: | --- |
| REAL_LLM | 3 | fixture 数据 + 真实模型；以 RecordingLLMAdapter 的 HTTP 结果为准 |

## 缺陷映射

| 审计缺陷 | 用例 |
| --- | --- |

## 清理

- 清理失败: 无
- Fixture: {'users': 2, 'spaces': 3, 'documents': 5, 'userIds': [9001, 9002], 'spaceIds': [9101, 9104, 9102], 'documentNames': ['broken.md', 'redis.md', 'tcp.md', 'tlb.md'], 'nodes': [{'userId': 9001, 'spaceId': 9101, 'concepts': 3, 'documents': 3}, {'userId': 9001, 'spaceId': 9104, 'concepts': 1, 'documents': 1}, {'userId': 9002, 'spaceId': 9102, 'concepts': 1, 'documents': 1}]}

## 用例明细

| 用例 | 类别 | 状态 | 执行类别 | 关键失败 |
| --- | --- | --- | --- | --- |
| B08 | B | BLOCKED | REAL_LLM | 真实模型连续返回空流/调用失败；这是上游可用性问题，不作为 Agent 回答质量结论。 |
| D08 | D | BLOCKED | REAL_LLM | 真实模型连续返回空流/调用失败；这是上游可用性问题，不作为 Agent 回答质量结论。 |
| E07 | E | BLOCKED | REAL_LLM | 真实模型连续返回空流/调用失败；这是上游可用性问题，不作为 Agent 回答质量结论。 |

## 逐用例

### B08 真实模型证据问答冒烟

- 状态：**BLOCKED**（真实模型连续返回空流/调用失败；这是上游可用性问题，不作为 Agent 回答质量结论。）
- 执行类别：REAL_LLM；severity：P1
- 目标缺陷：无；合同状态：confirmed
- 耗时：14604 ms
- 清理：{'ok': True, 'error': None, 'retrieverClosed': True, 'mode': '进程内 fixture 深拷贝；执行后 aclose，无持久化副作用。'}

### D08 真实 RAG 引用冒烟

- 状态：**BLOCKED**（真实模型连续返回空流/调用失败；这是上游可用性问题，不作为 Agent 回答质量结论。）
- 执行类别：REAL_LLM；severity：P1
- 目标缺陷：无；合同状态：confirmed
- 耗时：15639 ms
- 清理：{'ok': True, 'error': None, 'retrieverClosed': True, 'mode': '进程内 fixture 深拷贝；执行后 aclose，无持久化副作用。'}

### E07 真实模型教学冒烟

- 状态：**BLOCKED**（真实模型连续返回空流/调用失败；这是上游可用性问题，不作为 Agent 回答质量结论。）
- 执行类别：REAL_LLM；severity：P1
- 目标缺陷：无；合同状态：confirmed
- 耗时：14966 ms
- 清理：{'ok': True, 'error': None, 'retrieverClosed': True, 'mode': '进程内 fixture 深拷贝；执行后 aclose，无持久化副作用。'}

## 口径说明

- PASS/FAIL 只由硬断言决定；语义评分单独列出，不进入通过率。
- BLOCKED 不计 PASS；SKIPPED 不计 PASS；designed_only 不计 PASS。
- 本轮所有 fixture 数据均为评测专用号段，不读取真实用户数据。
- 真实模型用例只保存脱敏、截断后的输出；不保存完整 context 或 Authorization。
