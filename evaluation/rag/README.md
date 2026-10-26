# Phase 4.2 / 4.3 RAG 评测

Phase 4.3 在**不改动 4.2 数据集与历史报告**的前提下增加了 v2 规则：
`dataset_v2.jsonl`、`scoring_v2.py`、`revalidate.py`。v1 结论仍由
`dataset.jsonl` + `scoring.py` 产生，两个版本不能混用。

这套评测回答三个不同的问题，不能混在一起看：

1. **检索对不对**：该被找到的文档片段有没有被 Qdrant 返回；
2. **上下文对不对**：返回的片段有没有真的进入模型看到的 system instruction；
3. **回答对不对**：回答是否包含人工标准答案要求的事实；
4. **引用对不对**：`[资料《文件名》#序号]` 是否精确对应本轮上下文，以及来源句子是否
   在词面上支持该说法。

第 3 层是词面规则，不是语义判分；第 4 层的“来源支持度”是启发式信号，不是事实正确性
证明。两者都不能替代人工抽样复核。

## 目录

```text
corpus/              固定评测文档（7 个 Markdown，人工编写）
learning_state.json  受控 Concept / Understanding / Memory / Evidence
dataset.jsonl        41 条用例：A 12、B 8、C 6、D 6、E 5、F 4
models.py            用例与观测的数据结构
scoring.py           四层评分与汇总
runner.py            真实 Spring + Agent + LLM 的进程内评测
spring_sse.py        真实 Spring SSE 契约验收
reports/             生成物（含模型原文与上下文，便于复核）
```

`dataset.jsonl` 不会整份发给模型。每轮只发送当前问题、检索到的文档片段，以及
`rag+state` 模式下受控的 Understanding / Memory / 按需 Evidence。

## 三种上下文模式

| 模式 | 文档 | 学习状态 | 用途 |
| --- | --- | --- | --- |
| `plain` | 否 | 否 | 纯模型基线 |
| `rag` | 是 | 否 | 只看检索文档 |
| `rag+state` | 是 | 受控 U/M，问题触发时含 E | 文档 + 学习状态 |

`plain` 模式下 D/F 类“无证据问题”的“资料里没有”不适用，评分层会标记为 N/A；
引用层仍然要求“没有上下文时不能编造引用”。

## 运行真实评测

前置条件：

- 测试数据库的 Spring Boot 已启动（不要指向生产库）；
- 测试 Qdrant 可用；`AILS_QDRANT_COLLECTION_PREFIX` 必须以 `ails_phase42` 开头；
- Agent 进程已启动，使用真实 Embedding 与真实 LLM，且与后端共享内部 Token；
- 真实 Key 只在 `agent/.env` 或进程环境里，不写进仓库、命令参数或报告。

```bash
# 在仓库根目录
AILS_LLM_PROVIDER=openai \
AILS_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
AILS_LLM_MODEL=qwen-plus \
AILS_LLM_TEMPERATURE=0.0 \
AILS_QDRANT_COLLECTION_PREFIX=ails_phase42_real_e2e \
python -m evaluation.rag.runner --backend-url http://127.0.0.1:18080
```

`AILS_LLM_API_KEY` 由启动脚本从本地 gitignored 配置注入；runner 只检查“是否为空”，
不会打印或写进报告。缺少 Key 时 runner 直接失败，不会退回 Mock。

调试时可以只跑一个类别：

```bash
python -m evaluation.rag.runner --backend-url http://127.0.0.1:18080 --categories A --max-cases 3
```

## 运行 Spring SSE 验收

需要 Spring Boot 与真实 Agent 都已启动：

```bash
python -m evaluation.rag.spring_sse --backend-url http://127.0.0.1:18080 \
  --embedding-model text-embedding-v4
```

它检查 `message.start` 首事件、`message.delta` 拼接、`message.end` 终态、引用标签格式，
以及无证据问题不伪造引用。

## Phase 4.3 v2 规则

v2 只改评分规则与证据约束，不重新定义检索基线（top_k / min_score 未调）：

- **答案同义词**：`requiredTermGroups` 表达“同一事实的已核验同义组”，例如
  `内存访问 / 访存 / 访问内存`；不再要求模型逐字复述某一个词。
- **无证据回答**：`admissionPatterns` 必须同时出现“否定 + 所问对象”，
  例如“资料中没有提到 SDK”，而不是只出现一个“没有”。
- **引用分类**：合法的对比引用（如 E05 的 ZealDeck 对照）不再自动判错，
  但会单独计入 `extra_valid_source_rate`；格式错误、非法标签、缺必需来源、
  禁止来源仍然失败。D02 这类「问题本身无答案、但回答引用了同一文档里的有据事实」
  也不判错；反过来，把「资料未提及」升级成「官方未提供任何语言 SDK」的绝对化断言
  仍判错（D04 的 forbidden pattern）。
- **来源支持度**：仍是词面启发式，只用于人工复核，不参与通过判定。

六个引用指标各用各的分母，不能横向比较百分比：

| 指标 | 分子 / 分母 |
| --- | --- |
| `illegal_label_rate` | 非法精确标签 / 精确标签总数 |
| `malformed_label_rate` | 格式错误标签 / 全部标签出现次数（精确 + 格式错误） |
| `required_source_missing_rate` | 缺必需来源的运行 / 有必需来源要求的运行（不含 plain） |
| `forbidden_source_rate` | 引用了禁止来源的运行 / 配置了禁止来源的运行 |
| `extra_valid_source_rate` | 含额外合法来源的运行 / 至少有一条合法引用的运行 |
| `citation_support_warning_rate` | 支持度为 none 或 numeric_conflict 的引用 / 合法引用数 |
| `citation_pass_rate` | 引用层通过的运行 / 全部运行 |

```bash
# 只跑 v2 目标用例（真实 LLM）
python -m evaluation.rag.runner --backend-url http://127.0.0.1:18080 \
  --rules-version v2 --cases D01,D04,D05,F01,F02,F03,F04 \
  --modes rag,rag+state --output-dir evaluation/rag/reports/<name>

# 离线重算已保存的模型输出，不调用模型、不改源报告
python -m evaluation.rag.revalidate \
  --source evaluation/rag/reports/real_20261026_full \
  --target evaluation/rag/reports/real_20261026_v2_offline

# S04 走真实 Spring SSE（判定器同样要求“否定 + 所问对象”）
python -m evaluation.rag.spring_sse --backend-url http://127.0.0.1:18080 \
  --cases S04 --output-dir evaluation/rag/reports/<name>
```

## Phase 4.4 非成功检索状态兜底

`agent/app/evidence_guard.py` 为四种状态（`NO_VALID_DOCUMENTS` / `NO_MATCH` /
`THRESHOLD_FILTERED` / `DEGRADED`）提供确定性兜底：本轮没有知识片段、问题看起来
是知识请求、且模型自己没有说明资料状态时，`AgentRuntime.generate` 会在模型输出
结束后追加一条固定说明。它是一个普通 delta，不缓冲、不删除、不覆盖模型输出，
SSE 事件类型不变。

兜底只在运行时生效，评分规则没有放宽。离线估算对已保存回答的影响：

```bash
python -m evaluation.rag.guard_offline \
  --source evaluation/rag/reports/real_20261026_v2_offline \
  --target evaluation/rag/reports/real_20261026_phase44_guard_offline
```

它不改源报告，也不调用模型。4.2 的 31/40 与 4.3 的 29/40 的口径差异见
`reports/baseline_audit_phase44/README.md`。

## 结果怎么读

- `runs.jsonl`：每条用例 × 模式一行，含 `retrieval`、`context`、`answer`、`citation`、
  `score` 与 `trace`。`context.text` 是实际发送给模型的消息序列。
- `summary.json`：按模式/类别聚合的通过率、非法引用率、来源支持不足率、平均延迟。
- `summary.md`：人读版；失败项会列出失败层与失败检查名。

硬指标：

- **非法引用率**：上下文里不存在的标签 / 全部标签；这是精确比较，不是启发式。
- **引用通过率**：标签合法、必需文档被引用、禁止文档未被引用、无 malformed。
- **检索/上下文/答案通过率**：分别定位“没检索到 / 被预算丢掉 / 模型答错”。

软信号：

- **来源支持不足率**：标注句与来源片段的词面重合为 `none` 或数字冲突。
  它提示人工复核，不直接判引用错误（同义改写会造成误报）。
