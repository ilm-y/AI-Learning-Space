"""Phase 1 断言与语义评分。

两层刻意分开：

- **hard assertions** 决定 PASS / FAIL。它们只检查可核对事实：状态、计数、
  trace 事件、最终回答是否包含某个由记录支撑的短语、是否出现禁止内容。
- **semantic rubric** 只输出 0/1/2 与理由，不改变通过率。它是给人看的辅助，
  不是 LLM Judge 的替代品，也不能替代权限/状态/路径断言。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evaluation.runner.checks import CheckOutcome, evaluate

from .models import EvalCase, RubricCriterion


@dataclass
class RubricOutcome:
    criterion_id: str
    score: int
    max_score: int = 2
    reason: str = ""
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    forbidden_hit: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterionId": self.criterion_id,
            "score": self.score,
            "maxScore": self.max_score,
            "reason": self.reason,
            "matched": list(self.matched),
            "missing": list(self.missing),
            "forbiddenHit": list(self.forbidden_hit),
        }


def run_hard_assertions(case: EvalCase, actual: dict[str, Any]) -> list[CheckOutcome]:
    # 逐条执行，不能先塞进 dict：同一个 path 上的两条断言会互相覆盖。
    outcomes: list[CheckOutcome] = []
    for item in case.hard_assertions:
        outcomes.extend(
            evaluate({item.path: {"op": item.op, "value": item.value}}, actual)
        )
    return outcomes


def score_rubric(case: EvalCase, actual: dict[str, Any]) -> list[RubricOutcome]:
    text = _rubric_text(actual)
    if not text.strip():
        # BLOCKED / 只跑检索的用例没有用户可见回答，不给 0 分冒充「语义失败」。
        return []
    return [_score_one(criterion, text) for criterion in case.rubric]


def _score_one(criterion: RubricCriterion, text: str) -> RubricOutcome:
    forbidden_hit = [term for term in criterion.forbidden if term and term in text]
    if forbidden_hit:
        return RubricOutcome(
            criterion.id,
            0,
            reason=f"命中禁止表述：{'、'.join(forbidden_hit[:3])}",
            forbidden_hit=forbidden_hit,
        )

    if criterion.signals2:
        matched: list[str] = []
        missing: list[str] = []
        for group in criterion.signals2:
            hit = next((term for term in group if term and term in text), None)
            if hit:
                matched.append(hit)
            else:
                missing.append(" / ".join(group))
        if not missing:
            return RubricOutcome(
                criterion.id,
                2,
                reason="命中全部正向信号：" + "、".join(matched[:5]),
                matched=matched,
            )
        if missing:
            fallback = next(
                (term for term in criterion.signals1 if term and term in text), None
            )
            if fallback:
                return RubricOutcome(
                    criterion.id,
                    1,
                    reason=(
                        "部分正向信号缺失（" + "、".join(missing[:3]) + "），"
                        f"但命中弱信号：{fallback}"
                    ),
                    matched=matched,
                    missing=missing,
                )
            return RubricOutcome(
                criterion.id,
                0,
                reason="正向信号缺失：" + "、".join(missing[:3]),
                matched=matched,
                missing=missing,
            )

    if criterion.signals1:
        hit = [term for term in criterion.signals1 if term and term in text]
        if hit:
            return RubricOutcome(
                criterion.id, 2, reason="命中正向信号：" + "、".join(hit[:5]), matched=hit
            )
        return RubricOutcome(criterion.id, 0, reason="没有命中任何正向信号")

    # 只有 forbidden、没有正向信号的评分项：没有命中禁止表述即满分。
    return RubricOutcome(criterion.id, 2, reason="没有命中禁止表述")


def _rubric_text(actual: dict[str, Any]) -> str:
    # 语义评分只看**用户可见回答**。Context 里本身就有「不要说『你错了』」
    # 这类规则文本，把它拼进来会让禁止词永远命中，等于给所有用例判 0。
    return str((actual.get("answer") or {}).get("text") or "")
