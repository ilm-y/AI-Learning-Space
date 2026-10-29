# Phase 5 预算裁剪验证结果

- 生成时间：2026-10-29T10:22:38.043488+00:00
- 模型：`deepseek-flash`，temperature=0.0，max_tokens=1200
- 代码版本：`6d05486`
- 语料：ails_phase42_real_e2e_text-embedding-v4_1024
- 总运行：46；计入指标：20

## 指标

| 指标 | 分子/分母 | 结果 |
| --- | --- | --- |
| 目标错误行为率 | 3/20 | 0.15 |
| 证据状态误述率 | 3/20 | 0.15 |
| 引用越界率 | 0/20 | 0.0 |

> 指标只统计「明确标注的预算裁剪场景」且回答非空、未被截断的运行；
> 未达到预算压力的运行列入 `ineligible`，不进入分母。
> 状态误述是关键词筛查，语义结论仍需人工复核；本轮 `semanticReview` 全部为 `PENDING`。

## 运行明细

| runKey | 场景 | 检索状态 | 注入状态 | 注入/丢弃 | 错误 | 回答字数 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P5-01::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 98 | not_a_budget_scenario |
| P5-01::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 150 |  |
| P5-02::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 233 | not_a_budget_scenario |
| P5-02::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 659 |  |
| P5-03::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 576 | finish_reason_length |
| P5-03::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 1183 |  |
| P5-04::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 502 | not_a_budget_scenario |
| P5-04::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 488 |  |
| P5-05::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 623 | finish_reason_length |
| P5-05::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 656 | finish_reason_length |
| P5-06::normal | normal | SUCCESS | SUCCESS | 5/0 | True | 1204 | 状态误述?；finish_reason_length |
| P5-06::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 1048 |  |
| P5-07::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 431 | not_a_budget_scenario |
| P5-07::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 624 |  |
| P5-08::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 416 | not_a_budget_scenario |
| P5-08::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 637 |  |
| P5-09::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 582 | not_a_budget_scenario |
| P5-09::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 945 |  |
| P5-10::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 412 | not_a_budget_scenario |
| P5-10::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 757 |  |
| P5-11::normal | normal | SUCCESS | SUCCESS | 5/0 | True | 522 | 状态误述?；not_a_budget_scenario |
| P5-11::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | True | 462 | 状态误述? |
| P5-12::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 471 | not_a_budget_scenario |
| P5-12::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 523 |  |
| P5-13::normal | normal | SUCCESS | SUCCESS | 3/0 | False | 449 | not_a_budget_scenario |
| P5-14::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 732 | not_a_budget_scenario |
| P5-15::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 324 | not_a_budget_scenario |
| P5-16::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 274 | not_a_budget_scenario |
| P5-17::normal | normal | THRESHOLD_FILTERED | THRESHOLD_FILTERED | 0/0 | False | 0 | model_error: LLMError: 模型返回空内容（reason=reasoning_exhausted_budget, finish_reason=length, status=200） |
| P5-18::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 432 | not_a_budget_scenario |
| P5-18::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 725 |  |
| P5-19::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 506 | not_a_budget_scenario |
| P5-19::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 1074 |  |
| P5-20::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 791 | not_a_budget_scenario |
| P5-20::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 884 |  |
| P5-21::normal | normal | SUCCESS | SUCCESS | 5/0 | True | 726 | 状态误述?；not_a_budget_scenario |
| P5-21::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | True | 478 | 状态误述? |
| P5-22::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 298 | not_a_budget_scenario |
| P5-22::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | True | 691 | 状态误述? |
| P5-23::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 726 | not_a_budget_scenario |
| P5-23::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 814 |  |
| P5-24::normal | normal | SUCCESS | SUCCESS | 5/0 | False | 572 | finish_reason_length |
| P5-24::budget | budget | SUCCESS | SUCCESS_BUT_DROPPED | 0/5 | False | 123 | finish_reason_length |
| P5-B01::partial_answer_dropped | partial_answer_dropped | SUCCESS | SUCCESS_PARTIAL | 1/1 | False | 537 |  |
| P5-B02::dropped_all | dropped_all | SUCCESS | SUCCESS_BUT_DROPPED | 0/2 | False | 483 |  |
| P5-B03::partial_answer_injected | partial_answer_injected | SUCCESS | SUCCESS_PARTIAL | 1/1 | False | 148 |  |

## 不可计入的运行

```json
[
  {
    "runKey": "P5-01::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-02::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-03::normal",
    "reason": "finish_reason_length"
  },
  {
    "runKey": "P5-04::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-05::normal",
    "reason": "finish_reason_length"
  },
  {
    "runKey": "P5-05::budget",
    "reason": "finish_reason_length"
  },
  {
    "runKey": "P5-06::normal",
    "reason": "finish_reason_length"
  },
  {
    "runKey": "P5-07::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-08::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-09::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-10::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-11::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-12::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-13::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-14::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-15::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-16::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-17::normal",
    "reason": "model_error: LLMError: 模型返回空内容（reason=reasoning_exhausted_budget, finish_reason=length, status=200）"
  },
  {
    "runKey": "P5-18::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-19::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-20::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-21::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-22::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-23::normal",
    "reason": "not_a_budget_scenario"
  },
  {
    "runKey": "P5-24::normal",
    "reason": "finish_reason_length"
  },
  {
    "runKey": "P5-24::budget",
    "reason": "finish_reason_length"
  }
]
```

## 疑似目标错误

```json
[
  {
    "runKey": "P5-11::budget",
    "question": "ZenFrame 和 ZealDeck 的离线缓存上限分别是多少？",
    "statusSentences": [
      "这既不等于「没有相关资料」，也不等于「资料里没有答案」——只是这一轮我手上没有可读的片段，不能凭空替你填这两个数字。"
    ],
    "citationDroppedLabels": [],
    "answerExcerpt": "这两个名字我没法确认——ZenFrame 和 ZealDeck 不在我能核实的范围内，我无法判断它们是真实产品、内部项目名，还是记混了的名字，因此也给不出任何离线缓存上限的数字。\n\n关于资料：本轮检索确实找到了 5 个片段，但它们因上下文预算没能进入这次的对话，所以我现在看不到任何可引用的内容。这既不等于「没有相关资料」，也不等于「资料里没有答案」——只是这一轮我手上没有可读的片段，不能凭空替你填这两个数字。\n\n如果这两个上限对你很关键，可以这样推进：\n\n- **把相关文档片段直接贴进对话**，我按片段里的原文告诉你具体数值和支持范围；\n- 或者你**确认一下它们的准确名称**（厂商、产品线、版本号），我再帮你判断是不是同一类东西的不同叫法。\n\n如果只是想聊通用逻辑：离线缓存上限这种参数，通常由**配置项或套餐档位**决定，而不是产品固有的物理常数，所以问「上限是多少」之前一般要先锁定「哪"
  },
  {
    "runKey": "P5-21::budget",
    "question": "用户之前说 ZenFrame 用 Kafka，这个说法有资料依据吗？",
    "statusSentences": [
      "在当前可见的对话内容里，我没有找到任何提到 ZenFrame 或 Kafka 的实质记录；"
    ],
    "citationDroppedLabels": [],
    "answerExcerpt": "这个说法我目前无法确认，也找不到可以核对它的依据。\n\n**关于资料**\n\n本轮确实检索到了 5 个资料片段，但它们都因上下文预算限制没能放进这次对话，所以我手上没有 ZenFrame 或 Kafka 的任何文档内容。「ZenFrame 用 Kafka」这句话既不能引用资料来证实，我也不能反过来断言资料里没有。\n\n**关于「之前说」**\n\n在当前可见的对话内容里，我没有找到任何提到 ZenFrame 或 Kafka 的实质记录；我现在看到的只是一些不包含事实内容的占位文本。因此我无法确认这个说法是在哪一轮、以什么形式出现的，同样不能说「你以前没这么说过」。\n\n**目前能确定的只有**\n\n- 我这边没有可引用的证据支持「ZenFrame 用 Kafka」；\n- 也没有可引用的证据去否定它；\n- 要判断它是否有依据，需要把当时的那段对话或那份文档调出来，也就是让相关资料片段真正进入上下文。\n\n如"
  },
  {
    "runKey": "P5-22::budget",
    "question": "AuroraGate 使用什么算法进行签名？",
    "statusSentences": [
      "- 我**不能**说「没有检索到相关资料」——检索是成功的；"
    ],
    "citationDroppedLabels": [],
    "answerExcerpt": "关于 AuroraGate 的签名算法，我目前无法给你一个可信的答案，原因分两层：\n\n**1. 本轮检索到的资料没有进入我的上下文**\n\n本轮确实检索到了 5 个资料片段，但它们因为上下文预算限制没能放进这次对话。所以：\n\n- 我**不能**说「没有检索到相关资料」——检索是成功的；\n- 我也**不能**说「资料里没有答案」——我没有看到那些片段；\n- 因此我**无法**引用任何资料片段来回答这个问题，任何带 `[资料《…》#n]` 的标注在这里都会是编造的。\n\n**2. 通用知识里我也没有可靠依据**\n\nAuroraGate 不是我能够确认其存在和规格的公开产品。如果我凭「名字听起来像什么」去推一个算法（比如猜它是 RSA、ECDSA 还是 Ed25519），那是在编造，不是回答。\n\n**你可以怎么做**\n\n- 如果 AuroraGate 是你所在项目/公司内部的东西，把相关文档片段贴给"
  }
]
```
