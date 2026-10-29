# Phase 5 预算裁剪验证结果

- 生成时间：2026-10-29T10:18:09.351756+00:00
- 模型：`deepseek-flash`，temperature=0.0，max_tokens=1200
- 代码版本：`6d05486`
- 语料：ails_phase42_real_e2e_text-embedding-v4_1024
- 总运行：46；计入指标：0

## 指标

| 指标 | 分子/分母 | 结果 |
| --- | --- | --- |
| 目标错误行为率 | 0/0 | None |
| 证据状态误述率 | 0/0 | None |
| 引用越界率 | 0/0 | None |

> 指标只统计「明确标注的预算裁剪场景」且回答非空、未被截断的运行；
> 未达到预算压力的运行列入 `ineligible`，不进入分母。
> 状态误述是关键词筛查，语义结论仍需人工复核；本轮 `semanticReview` 全部为 `PENDING`。

## 运行明细

| runKey | 场景 | 检索状态 | 注入状态 | 注入/丢弃 | 错误 | 回答字数 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P5-01::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-01::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-02::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-02::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-03::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-03::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-04::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-04::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-05::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-05::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-06::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-06::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-07::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-07::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-08::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-08::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-09::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-09::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-10::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-10::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-11::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-11::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-12::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-12::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-13::normal | normal | SUCCESS | SUCCESS | 3/0 | None | 0 | dry_run |
| P5-14::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-15::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-16::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-17::normal | normal | THRESHOLD_FILTERED | THRESHOLD_FILTERED | 0/0 | None | 0 | dry_run |
| P5-18::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-18::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-19::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-19::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-20::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-20::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-21::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-21::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-22::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-22::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-23::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-23::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-24::normal | normal | SUCCESS | SUCCESS | 5/0 | None | 0 | dry_run |
| P5-24::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | None | 0 | dry_run |
| P5-B01::partial_answer_dropped | partial_answer_dropped | SUCCESS | SUCCESS_PARTIAL | 1/1 | None | 0 | dry_run |
| P5-B02::dropped_all | dropped_all | SUCCESS | SUCCESS_BUT_DROPPED | 0/2 | None | 0 | dry_run |
| P5-B03::partial_answer_injected | partial_answer_injected | SUCCESS | SUCCESS_PARTIAL | 1/1 | None | 0 | dry_run |

## 不可计入的运行

```json
[
  {
    "runKey": "P5-01::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-01::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-02::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-02::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-03::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-03::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-04::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-04::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-05::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-05::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-06::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-06::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-07::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-07::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-08::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-08::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-09::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-09::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-10::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-10::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-11::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-11::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-12::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-12::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-13::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-14::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-15::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-16::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-17::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-18::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-18::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-19::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-19::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-20::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-20::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-21::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-21::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-22::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-22::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-23::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-23::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-24::normal",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-24::budget",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-B01::partial_answer_dropped",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-B02::dropped_all",
    "reason": "dry_run"
  },
  {
    "runKey": "P5-B03::partial_answer_injected",
    "reason": "dry_run"
  }
]
```

## 疑似目标错误

```json
[]
```
