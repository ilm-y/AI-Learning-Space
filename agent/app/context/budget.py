"""D4-3 上下文预算与 Manifest 的最小公共定义。

这一层只放**纯常量与纯计算**，不读配置、不接触 AgentState：

- 按类型的额度比例（默认值 + 理由）；
- 留给输出的余量；
- injection_status / reason 的稳定字符串；
- Manifest schema 版本。

为什么额度是「上限」而不是「切分」：各类上下文在真实请求里的长度分布差异很大。
如果按比例切分，某类没内容时它那份预算就浪费了；上限 + 剩余共享能保证
「有内容的类型可以借用空闲预算」，同时防止单一类型（尤其是 Knowledge / 历史）
挤掉当前认知。REQUIRED 先于 OPTIONAL 分配，OPTIONAL 只用剩余额度。

默认比例的理由：

- `history=0.35`：最近对话是理解指代与续接的基础，但旧轮次不应压过当前认知；
- `understanding=0.35`：当前快照决定从哪里讲起，是最该保留的弹性内容；
- `trajectory=0.35`：只在复盘出现，内容长但仅一类，给足空间；
- `memory=0.20`：长期事实用于调整讲法，通常短，且需给 Understanding 让位；
- `knowledge=0.40`：文档片段是最长的一类，但必须为认知内容留下剩余；
- `evidence=0.20` / `related_evidence=0.10`：历史判定依据是参考，不能挤掉当前状态；
- 状态说明（status）本身很短，单列小额度只用于 Manifest 展示，实际作为 protected 区块优先保留。

`output_reserve_ratio=0.10`：给模型输出与工具调用留出余量。`max_context_tokens`
约束的是动态输入上下文，不是完整 prompt；预留输出余量能避免「刚好塞满输入」
导致长回答被截断。该值可配置，默认 0.10。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

MANIFEST_SCHEMA_VERSION = 1

# ---------- injection_status ----------
INJECTION_SELECTED = "SELECTED"
INJECTION_EXCLUDED = "EXCLUDED"
INJECTION_TRUNCATED = "TRUNCATED"
INJECTION_OMITTED = "OMITTED"
INJECTION_PARTIAL = "PARTIAL"

# ---------- 稳定原因 ----------
REASON_WITHIN_BUDGET = "within_budget"
REASON_POLICY_SKIP = "policy_skip"
REASON_NOT_REVIEW_INTENT = "intent_not_review"
REASON_DUPLICATE_SOURCE = "duplicate_source"
REASON_TYPE_BUDGET = "type_budget"
REASON_GLOBAL_BUDGET = "global_budget"
REASON_SEMANTIC_REDUNDANCY = "redundant_with_understanding"
REASON_CHAR_TRUNCATED = "char_truncated"
REASON_TIER_PREFIX = "tier:"

# ---------- 压缩档位 ----------
TIER_FULL = "full"
TIER_STRUCTURED = "structured"
TIER_ONE_LINE = "one_line"

# 弹性内容类型。顺序即同组内的分配优先级（与 builder 的 PRIORITY_* 一致）：
# Understanding > Trajectory > Memory > Knowledge > Evidence > Related Evidence。
ELASTIC_KIND_ORDER = (
    "understanding",
    "trajectory",
    "memory",
    "knowledge",
    "evidence",
    "related_evidence",
)

# protected 区块：先占预算，不参与类型额度竞争。它们解释「这次查没查、查成没成」，
# 一旦被预算丢掉，模型就会把「没看到」误说成「没有」。
PROTECTED_KINDS = (
    "cognitive_status",
    "concept_status",
    "knowledge_status",
    "evidence_status",
)
PROTECTED_SECTION_NAMES = ("session",)

DEFAULT_TYPE_CAP_RATIOS: Mapping[str, float] = {
    "history": 0.35,
    "session": 0.10,
    "understanding": 0.35,
    "trajectory": 0.35,
    "memory": 0.20,
    "knowledge": 0.40,
    "evidence": 0.20,
    "related_evidence": 0.10,
    "cognitive_status": 0.08,
    "concept_status": 0.08,
    "knowledge_status": 0.08,
    "evidence_status": 0.08,
}

DEFAULT_OUTPUT_RESERVE_RATIO = 0.10


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """一份可配置的预算策略。刻意不包含任何状态。"""

    output_reserve_ratio: float = DEFAULT_OUTPUT_RESERVE_RATIO
    type_cap_ratios: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_TYPE_CAP_RATIOS)
    )

    def validate(self) -> None:
        if not 0 <= self.output_reserve_ratio < 1:
            raise ValueError("output_reserve_ratio 必须在 [0, 1) 内")
        for kind, ratio in self.type_cap_ratios.items():
            if not 0 < ratio <= 1:
                raise ValueError(f"类型额度比例非法：{kind}={ratio}，必须在 (0, 1] 内")

    def effective_total(self, total: int) -> int:
        """扣除输出余量后的可分配输入预算（至少 1，避免除零式空预算）。"""
        reserve = self.reserve_tokens(total)
        return max(1, total - reserve)

    def reserve_tokens(self, total: int) -> int:
        return max(0, int(total * self.output_reserve_ratio))

    def cap(self, kind: str, effective_total: int) -> int:
        """某类型的额度上限。未声明的类型不设上限（返回全额）。"""
        ratio = self.type_cap_ratios.get(kind)
        if ratio is None:
            return effective_total
        return max(1, int(effective_total * ratio))
