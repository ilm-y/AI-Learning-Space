"""学习轨迹的选取与证据回查。

这里解决的是阶段 5.1 暴露的具体问题：复盘问题只按 `createdAt DESC` 取最近
几条 Evidence，早期错误会被近期观察挤出；Revision 虽然完整保留在数据库里，
却从未进入上下文。

做法不是「把全部历史塞给模型」，而是：

1. 只在显式复盘意图下读取 Revision；
2. 只保留发生真实变化的关键版本（首次、状态变化、误区出现/解除、最新）；
3. 用 Revision 已有的 `trigger_evidence_id` 和误区里的 `reversal_evidence_ids`
   精确回查原始证据，而不是按时间猜；
4. 把「历史」与「当前」分开交给渲染层。

本模块是纯函数，不访问网络/数据库，便于回归。
"""

from __future__ import annotations

from datetime import datetime

from app.schemas import (
    EvidenceItem,
    LearningTrajectory,
    MisconceptionStatus,
    Understanding,
    UnderstandingRevision,
)

# 每次复盘最多保留的版本数。超过时优先保留首次/最新/误区变化，而不是简单截断。
MAX_REVISIONS_PER_CONCEPT = 8
# 由 Revision 关联出的证据 id 上限。典型场景只有个位数；上限只防异常数据。
MAX_EVIDENCE_IDS = 20


def build_trajectories(
    understandings: list[Understanding],
    revisions_by_concept: dict[int, list[UnderstandingRevision]],
) -> list[LearningTrajectory]:
    """按概念组装历史轨迹；没有 Revision 的概念跳过。"""
    trajectories: list[LearningTrajectory] = []
    for understanding in understandings:
        revisions = revisions_by_concept.get(understanding.concept_id) or []
        if not revisions:
            continue
        selected = select_revisions(revisions)
        trajectories.append(
            LearningTrajectory(
                concept_id=understanding.concept_id,
                concept_name=understanding.concept_name,
                current_status=understanding.status,
                current_misconceptions=[
                    item.text for item in understanding.misconceptions
                ],
                revisions=selected,
            )
        )
    return trajectories


def select_revisions(
    revisions: list[UnderstandingRevision],
    limit: int = MAX_REVISIONS_PER_CONCEPT,
) -> list[UnderstandingRevision]:
    """选出足以解释「错在哪、后来怎么纠正」的关键版本。

    - 第一次与最近一次版本必留；
    - Understanding 状态变化必留；
    - 误区集合或误区状态（ACTIVE/PENDING）变化必留；
    - 其余同优先级按时间倒序保留，最后仍按 revision_no 正序输出。
    """
    ordered = sorted(revisions, key=lambda item: item.revision_no)
    if not ordered:
        return []
    if len(ordered) <= limit:
        return ordered

    scored: dict[int, tuple[int, UnderstandingRevision]] = {}

    def keep(revision: UnderstandingRevision, score: int) -> None:
        current = scored.get(revision.revision_no)
        if current is None or score > current[0]:
            scored[revision.revision_no] = (score, revision)

    keep(ordered[0], 4)
    keep(ordered[-1], 4)

    previous: UnderstandingRevision | None = None
    for revision in ordered:
        if previous is not None:
            if revision.status is not previous.status:
                keep(revision, 3)
            if _misconception_signature(revision) != _misconception_signature(previous):
                keep(revision, 3)
        previous = revision

    if len(scored) < limit:
        for revision in reversed(ordered):
            if revision.revision_no in scored:
                continue
            keep(revision, 1)
            if len(scored) >= limit:
                break

    chosen = sorted(scored.values(), key=lambda item: item[0], reverse=True)[:limit]
    return sorted((revision for _, revision in chosen), key=lambda item: item.revision_no)


def collect_evidence_ids(
    trajectories: list[LearningTrajectory],
    understandings: list[Understanding] | None = None,
    limit: int = MAX_EVIDENCE_IDS,
) -> list[int]:
    """从关键 Revision 与当前误区里收集需要精确回查的证据 id。

    顺序保持「先历史、后当前」，便于上游按上限裁剪时保留最早与最新两端。
    """
    ids: list[int] = []

    def add(value: int | None) -> None:
        if value is not None and value not in ids:
            ids.append(value)

    for trajectory in trajectories:
        for revision in trajectory.revisions:
            add(revision.trigger_evidence_id)
            for misconception in revision.misconceptions:
                for evidence_id in misconception.reversal_evidence_ids:
                    add(evidence_id)

    for understanding in understandings or []:
        for misconception in understanding.misconceptions:
            for evidence_id in misconception.reversal_evidence_ids:
                add(evidence_id)

    return _bounded(ids, limit)


def attach_evidences(
    trajectories: list[LearningTrajectory], evidences: list[EvidenceItem]
) -> None:
    """按 concept 归属证据；不跨概念串线。"""
    by_concept: dict[int, list[EvidenceItem]] = {}
    for evidence in evidences:
        by_concept.setdefault(evidence.concept_id, []).append(evidence)
    for trajectory in trajectories:
        trajectory.evidences = sorted(
            by_concept.get(trajectory.concept_id, []),
            key=lambda item: item.created_at or _MIN_TIME,
        )


def historical_misconceptions(trajectory: LearningTrajectory) -> list[str]:
    """历史版本里出现过、但当前快照已不存在的误区文本（按首次出现排序）。"""
    current = set(trajectory.current_misconceptions)
    seen: set[str] = set()
    result: list[str] = []
    for revision in trajectory.revisions:
        for misconception in revision.misconceptions:
            if misconception.text in current or misconception.text in seen:
                continue
            seen.add(misconception.text)
            result.append(misconception.text)
    return result


def misconception_first_seen(
    trajectory: LearningTrajectory, text: str
) -> UnderstandingRevision | None:
    for revision in trajectory.revisions:
        if any(item.text == text for item in revision.misconceptions):
            return revision
    return None


def misconception_last_status(
    trajectory: LearningTrajectory, text: str
) -> MisconceptionStatus | None:
    status: MisconceptionStatus | None = None
    for revision in trajectory.revisions:
        for item in revision.misconceptions:
            if item.text == text:
                status = item.status
    return status


def _misconception_signature(
    revision: UnderstandingRevision,
) -> tuple[tuple[str, MisconceptionStatus], ...]:
    return tuple(
        sorted(
            (item.text, item.status) for item in revision.misconceptions
        )
    )


def _bounded(values: list[int], limit: int) -> list[int]:
    if len(values) <= limit:
        return values
    half = limit // 2
    return values[:half] + values[-(limit - half):]


_MIN_TIME = datetime.min
