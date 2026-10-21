# Bad Case 数据集

这里记录**真实发生过**的问题，而不是「可能会发生」的问题。每条 Bad Case 都必须
形成一条可执行的回归用例，否则修完之后没人能证明它没再回来。

## 格式

```text
BC-xxx 标题

发现日期 / 影响范围

复现输入
    ↓
错误行为（用户 / 数据库看到什么）
    ↓
预期行为
    ↓
根因（代码层面的那一行 / 那个假设）
    ↓
修复
    ↓
回归用例（evaluation 里的 case id）
```

## 与用例的关系

- Bad Case 解释**为什么**要这条用例，用例负责**持续证明**它；
- 一条用例可以覆盖多条 Bad Case（同一个根因的多个表现），但一条 Bad Case
  至少要指向一条用例；
- 用例失败时，`reports/latest.md` 给出期望 / 实际，Bad Case 文档给出历史背景，
  排查从这两份材料开始。

## 索引

| 编号 | 标题 | 回归用例 |
| --- | --- | --- |
| [BC-001](BC-001-question-only-turn-stays-unknown.md) | 纯提问轮次没有推进理解状态 | `live-revision-question-exposes-concept` |
| [BC-002](BC-002-concurrent-revision-conflict.md) | 并发分析撞 `uk_revision_no` | `live-cross-session-attaches` |
| [BC-003](BC-003-sse-async-dispatch-unauthorized.md) | SSE 流结束时被安全过滤链硬断 | 所有含 `sendMessage` 的 live 用例 |
