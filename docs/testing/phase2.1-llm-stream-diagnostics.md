# Phase 2.1 真实 LLM 空流诊断

日期：2026-10-27  
对象：`agent/app/llm/openai_compatible.py`（OpenAI 兼容适配器）  
真实服务：`deepseek-flash`，通过本机 gitignored `.env` 注入，报告不记录 Key、Authorization 或用户内容。

## 1. 结论先行

| 问题 | 结论 | 证据等级 |
| --- | --- | --- |
| 非流式 `complete()` 空内容被静默当成 `""` | **已确认并修复**：现在抛 `LLMError`，可区分 `reasoning_exhausted_budget` / `length_limited` / `finished_without_content` | 离线契约测试 + 真实 API 实测 |
| `max_tokens=64` 时空内容的主因 | **已确认**：`reasoning_content` 消耗预算，`finish_reason=length`，用户可见 `content` 为空 | 真实 API 对照实测（64 vs 1024） |
| 流式首轮只返回 reasoning | **已确认可发生**；有界重试可恢复，仍无内容则返回可识别失败 | 真实 API 实测 + 离线测试 |
| 服务端格式不兼容 / HTTP 错误 / 流内 error 被静默吞掉 | **已覆盖**，分别归类为 `format_incompatible` / `http_error` / `model_error` | 离线测试 |
| Agent 语义质量 | **BLOCKED**：本次只验证“是否有可识别输出”，没有跑完整真实模型质量评测 | 无足够证据 |

> HTTP 200 与 `[DONE]` 只证明传输层完成，**不证明模型完成了有效生成**。本报告不把
> “真实 API 有返回”写成“Agent 质量通过”。

## 2. 适配器诊断字段

流式调用（`last_diagnostics.as_dict()`）：

- `requestId`、`model`、`attempt`、`maxAttempts`
- `httpStatus`、`providerRequestId`、`contentType`
- `dataEventCount`、`emptyChoiceCount`、`contentDeltaCount`、`reasoningDeltaCount`
- `finishReason`、`usage`、`sawDone`
- `errorCategory`：`none` / `empty_stream` / `http_error` / `model_error` / `format_incompatible` / `timeout` / `connection_error`
- `emptyReason`：`reasoning_only` / `no_data_events` / `format_incompatible` / `empty_choices` / `finished_without_content` / `content_empty`
- `retryable`、`latencyMs`、`contentChars`
- `attemptHistory`：每次尝试的 outcome、HTTP 状态、finish reason、本次 content/reasoning 增量与空流原因

非流式调用（`last_completion_diagnostics.as_dict()`）：

- `requestId`、`model`、`httpStatus`、`providerRequestId`、`contentType`
- `finishReason`、`usage`
- `contentChars`、`reasoningChars`
- `errorCategory`、`emptyReason`、`latencyMs`

诊断不保存 prompt、回答正文、Key、Authorization 或用户身份。

## 3. 离线确定性测试

命令：

```bash
cd agent
python -m pytest tests/test_llm_openai_compatible.py -q
```

结果：**24 passed**（2026-10-27）。覆盖：

1. 正常 content delta、多 delta、finish 事件、usage、provider request id；
2. `empty_choices`、`reasoning_only`、`format_incompatible`、`no_data_events`；
3. 空流重试一次后成功，且成功后 `retryable=False`、不残留首轮 `emptyReason`；
4. 重试有界（最多 2 次）；
5. 流内 `error` payload 归类 `model_error`，不再静默吞掉；
6. HTTP 错误、超时、非法 JSON、结构不符；
7. `complete()` 的 `null` / `""` / 纯空白内容均抛 `LLMError`；
8. `reasoning_content` 占满预算时归类 `reasoning_exhausted_budget`。

全量 Agent 回归：`296 passed, 5 skipped`（2026-10-27，同一工作区）。

## 4. 真实 API 对照实测

### 4.1 试验设置

- 模型：`deepseek-flash`
- 同一 prompt：`用一句话说明 TLB 是什么。`
- 流式与完成为独立请求；仅显式传入 `max_tokens` 作为诊断对照
- **没有修改** `.env`、默认 `AILS_LLM_MAX_TOKENS`（1024）、模型名或流式协议

### 4.2 `max_tokens=64`

| 轮次 | 方式 | 结果 |
| --- | --- | --- |
| 1 | stream | 尝试 1 成功，63 字符，`finish_reason=length`，content delta 24，reasoning delta 39 |
| 1 | complete | `LLMError`，`reasoning_exhausted_budget`，`finish_reason=length`，reasoning 138 字符，content 0 |
| 2 | stream | 尝试 1 **空**：`reasoning_only`，content delta 0，reasoning delta 64；尝试 2 成功 46 字符，`finish_reason=length` |
| 2 | complete | `LLMError`，`reasoning_exhausted_budget`，reasoning 155 字符，content 0 |

### 4.3 `max_tokens=1024`（当前配置值）

| 方式 | 结果 |
| --- | --- |
| stream | 尝试 1 成功，56 字符，`finish_reason=stop`，content delta 33，reasoning delta 44 |
| complete | 成功，80 字符，`finish_reason=stop`，reasoning 197 字符 |

另一次同配置冒烟：stream 51 字符（attempt 1，`stop`），complete 49 字符（`stop`）。

### 4.4 解释

`deepseek-flash` 会先输出大量 `reasoning_content`。当 `max_tokens` 很小（64）时，
预算可能在 reasoning 阶段耗尽，`finish_reason=length`，而 `delta.content` 始终为空。
旧实现把这种情况：

- 流式：当成“空流”，但错误信息只有笼统的一行；
- 非流式：`content is None` 时直接 `return ""`，**调用方无法区分“模型没说话”和“成功返回空串”**。

现在两种情况都会返回可识别失败，并保留按尝试拆分的诊断。`max_tokens=1024` 的
对照说明本次真实端点、模型名与流式解析本身可用；不能据此推断 64 是合理业务配置，
也不能据此断言回答质量合格。

## 5. 有界重试契约

- 只对**尚未向调用方输出任何字符**的空流或可重试 HTTP 状态（429/500/502/503/504）重试；
- 默认最多 2 次，退避 0.5 秒 × 尝试次数，可在构造器覆盖；
- 已经输出过字符的流不重放，避免拼接重复文本；
- 最终仍无内容：抛 `LLMError`，`status_code=502`，消息包含 `reason`、`status`、`attempts`；
- 成功重试后 `errorCategory=none`、`emptyReason=null`、`retryable=false`。

## 6. 仍未确认 / 风险

1. **真实 Agent 语义质量仍为 BLOCKED。** 本次只跑了适配器级小样本，不覆盖 40 条评测集，
   也没有人工/规则质量评分；不得计入“真实 LLM 质量通过”。
2. 当前真实服务未返回 `x-request-id` 等头（`providerRequestId=null`），无法与服务商侧日志关联。
3. 64 与 1024 的对照是**诊断样本**，不是吞吐或成功率指标；没有生成任何性能数字。
4. 未修改模型、`max_tokens`、流式协议或关键依赖。是否调整生产预算需要产品/成本决策，
   并配合完整真实模型评测，而不是为了本次修复改配置。
5. `complete()` 现在对空白内容直接失败；调用方若原本依赖“空串继续”，会改为走降级/错误路径，
   这是有意的 fail-closed 行为，已有回归测试覆盖。

## 7. Phase 2.1 用例台账

### 7.1 离线确定性解析测试（24/24 PASS）

执行：`cd agent && python -m pytest tests/test_llm_openai_compatible.py -q`
真实依赖：无网络、无真实模型；使用 `httpx.MockTransport` 构造确定性响应。
这些结果只证明**解析/诊断契约**，不证明模型质量。

| case_id | 测试 | 覆盖 |
| --- | --- | --- |
| LLM-2.1-01 | `test_complete_parses_content_and_sends_payload` | 非流式内容与请求体 |
| LLM-2.1-02 | `test_stream_parses_deltas_and_ignores_keepalive` | 多 delta、keepalive |
| LLM-2.1-03 | `test_stream_diagnostics_capture_finish_reason_usage_and_provider_id` | finish/usage/request id |
| LLM-2.1-04–07 | `test_empty_stream_is_identifiable[empty_choices / reasoning_only / format_incompatible / no_data_events]` | 四类空流分类 |
| LLM-2.1-08 | `test_empty_stream_retries_once_then_succeeds` | 有界重试后成功 |
| LLM-2.1-09 | `test_empty_complete_is_an_identifiable_failure` | 非流式空内容失败 |
| LLM-2.1-10 | `test_complete_reasoning_exhausting_token_budget_is_classified` | `reasoning_exhausted_budget` |
| LLM-2.1-11–13 | `test_complete_null_or_blank_content_never_returns_empty_string[None / "" / 空白]` | 空串不再静默成功 |
| LLM-2.1-14 | `test_empty_stream_retry_is_bounded` | 最多 2 次 |
| LLM-2.1-15 | `test_stream_error_payload_is_not_silently_swallowed` | 流内 `error` → `model_error` |
| LLM-2.1-16 | `test_http_error_maps_to_llm_error` | HTTP 错误 |
| LLM-2.1-17 | `test_timeout_maps_to_llm_error_for_complete_and_stream` | 超时 |
| LLM-2.1-18 | `test_invalid_json_response_maps_to_llm_error` | 非法 JSON |
| LLM-2.1-19–22 | `test_malformed_complete_payload_maps_to_llm_error[4 组]` | 结构不符 |
| LLM-2.1-23 | `test_factory_requires_key_for_openai_instead_of_falling_back_to_mock` | 缺 Key 不偷偷降级 Mock |
| LLM-2.1-24 | `test_factory_defaults_to_mock_and_builds_openai_with_key` | 工厂选择 |

### 7.2 真实 API 小样本（可识别输出，8/8 有结果）

| case_id | 设置 | 实际 | 结论 |
| --- | --- | --- | --- |
| LLM-REAL-01 | stream，`max_tokens=64`，第 1 轮 | 63 字符，`finish_reason=length`，content delta 24 / reasoning delta 39 | 有输出，但预算被 reasoning 消耗 |
| LLM-REAL-02 | complete，64，第 1 轮 | `LLMError(reasoning_exhausted_budget)`，content 0 | 可识别失败 |
| LLM-REAL-03 | stream，64，第 2 轮 | 尝试 1 `reasoning_only` 空；尝试 2 成功 46 字符，`finish_reason=length` | 有界重试可恢复 |
| LLM-REAL-04 | complete，64，第 2 轮 | `LLMError(reasoning_exhausted_budget)`，content 0 | 可识别失败 |
| LLM-REAL-05 | stream，1024 | 56 字符，`finish_reason=stop` | 当前配置可产出内容 |
| LLM-REAL-06 | complete，1024 | 80 字符，`finish_reason=stop` | 当前配置可产出内容 |
| LLM-REAL-07 | stream，1024，第二次冒烟 | 51 字符，attempt 1，`stop` | 可产出内容 |
| LLM-REAL-08 | complete，1024，第二次冒烟 | 49 字符，`stop` | 可产出内容 |

以上只证明“真实端点、模型名、解析和重试契约可用”，**不是**质量评分、成功率或性能指标。

### 7.3 明确未通过的能力

| case_id | 项目 | 状态 | 原因 |
| --- | --- | --- | --- |
| LLM-QUALITY-01 | 真实模型回答质量 | **BLOCKED** | 未跑 40 条评测集，没有人工/规则质量评分 |
| LLM-QUALITY-02 | 真实模型成功率 / 延迟 | **NOT_MEASURED** | 小样本不是统计指标，不生成数字 |
| LLM-QUALITY-03 | provider 侧请求关联 | **PARTIAL** | 当前端点未返回 `x-request-id`，`providerRequestId=null` |
