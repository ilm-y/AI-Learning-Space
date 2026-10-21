# BC-002 并发分析撞 `uk_revision_no`

- **发现日期**：2026-10-21
- **影响范围**：同一个概念在极短时间内被两条链路同时推进理解
  （对话结束后的后台分析 + 用户立即批注触发的同步分析）
- **严重程度**：高 —— 其中一条链路直接 500，且它已经写了一半的数据

## 复现输入

```text
1. POST /api/conversations/{id}/messages   "我理解 TLB 就是 Page Table 的缓存"
2. 收到 message.end 后立刻
   POST /api/messages/{id}/highlights      markType=MY_UNDERSTANDING
```

第 2 步返回 `HTTP 500`：

```text
Duplicate entry '17-1' for key 'understanding_revision.uk_revision_no'
```

## 错误行为

- 后台分析先插入 `understanding_revision(revision_no = 1)`；
- 批注触发的分析在同一瞬间也认为当前只有 0 条 Revision，算出 `revision_no = 1`；
- 后者撞唯一索引，事务回滚，批注接口 500。

两条链路都先拿 `learning_understanding` 的行锁，理论上应当串行 —— 但实际上
**串行只保证了「不会同时读改写」，没有保证「读得到对方已提交的 Revision」**。

## 根因

`UnderstandingService.nextRevisionNo` 用的是普通 `SELECT COUNT(*)`。

InnoDB 在 `REPEATABLE READ` 下，一致性读视图是在事务里**第一次普通 SELECT**
时建立的。分析事务在调用 `apply / applyProposal / markExposed` 之前，已经做过
普通 SELECT（读证据、读概念、查重），视图停在了对方提交之前。等它拿到行锁时，
锁是最新的，但视图没变 —— `COUNT(*)` 仍然返回旧值。

注释里「全程持有行锁所以 COUNT 安全」这个推论只覆盖了并发，没有覆盖
**读视图的时点**。

## 修复

`UnderstandingRevisionMapper.selectMaxRevisionNoForUpdate`：

```sql
SELECT COALESCE(MAX(revision_no), 0)
FROM understanding_revision
WHERE understanding_id = ?
FOR UPDATE
```

加锁读是「当前读」，不受事务视图影响；`FOR UPDATE` 同时在该索引范围上取
next-key 锁。行的写入顺序仍是「先 understanding 行锁 → 再 revision 范围锁」，
没有引入新的等待环。

## 回归用例

`live-cross-session-attaches`（`evaluation/cases/live/cross_session.json`）

它的第 1、2 步就是「发消息 → 立刻批注」，正好把两条分析链路叠在一起；
修复前该用例偶发 `step2/highlight: 服务器内部错误`，修复后稳定通过。
