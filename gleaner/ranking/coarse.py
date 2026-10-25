"""向量粗筛：按与兴趣锚点的相似度给论文打分，保留 Top K（docs/architecture.md 3.1.1）。

    score(p) = w_interest · max_i cos(p, A1_i)
             + w_pos      · mean_top3_j (d_j · cos(p, A2_j))
             − w_neg      · max_k (d_k · cos(p, A3_k))

- A1：兴趣描述拆出的子方向；
- A2：正反馈论文（标题 + 摘要）。d_j 是它的权重：信号强度（一次 /like 为 1，一个 👍 为 0.5）
  × 时间衰减（每过 feedback_half_life_days 减半）。mean_top3 取加权相似度最高的 3 个求平均，
  不足 3 个按 0 计：一两次反馈只有部分影响，反馈越多、越一致，影响越大；
- A3：兴趣描述中明确不感兴趣的方向和屏蔽主题（权重 1，不衰减），以及负反馈论文（权重同 A2）。
  取最接近的一个作为惩罚。

没有任何反馈和屏蔽主题时（冷启动）只有 A1，结果与只用兴趣描述时完全相同。
Zotero（A4）还没有接入。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

import numpy as np

from gleaner.config import RankingWeights
from gleaner.models import Paper, ScoredPaper
from gleaner.profile import AnchorSet, Memory
from gleaner.ranking.embedder import Embedder

TOP_POSITIVE = 3  # mean_top3


def paper_text(paper: Paper) -> str:
    return f"{paper.title}. {paper.abstract}"


def coarse_rank(
    papers: list[Paper],
    anchors: AnchorSet,
    embedder: Embedder,
    *,
    top_k: int,
    weights: RankingWeights,
    forced_ids: Collection[str] = (),
    memory: Memory | None = None,
    half_life_days: float = 90,
    now: datetime | None = None,
) -> list[ScoredPaper]:
    """返回按分数从高到低排列的 Top K，命中 include_keywords 的论文无论分数都会附在后面。"""
    if not papers:
        return []
    now = now or datetime.now(UTC)
    positive, negative = anchors.positive, anchors.negative
    liked = memory.positive(now, half_life_days) if memory else []
    disliked = memory.negative(now, half_life_days) if memory else []
    muted = [topic.query for topic in memory.muted if topic.query] if memory else []

    # 所有文本一次嵌入；反馈锚点放在最后，冷启动时与只用兴趣描述时完全相同
    groups = [
        [a.query for a in positive],
        [a.query for a in negative],
        [paper_text(p) for p in papers],
        [paper_text(item.paper) for item, _ in liked],
        [paper_text(item.paper) for item, _ in disliked],
        muted,
    ]
    vectors = embedder.embed([text for group in groups for text in group])
    bounds = np.cumsum([0, *(len(group) for group in groups)])
    (
        positive_vectors,
        negative_vectors,
        paper_vectors,
        liked_vectors,
        disliked_vectors,
        muted_vectors,
    ) = (vectors[start:end] for start, end in zip(bounds[:-1], bounds[1:], strict=True))

    similarity = paper_vectors @ positive_vectors.T
    interest = weights.interest * similarity.max(axis=1)
    scores = interest
    positive_part = None
    if liked:
        contributions = (paper_vectors @ liked_vectors.T) * _weights(liked)
        top = np.sort(contributions, axis=1)[:, -TOP_POSITIVE:]
        positive_part = weights.positive * top.sum(axis=1) / TOP_POSITIVE
        scores = scores + positive_part
    negative_part = None
    penalties = []
    if negative:
        penalties.append(paper_vectors @ negative_vectors.T)
    if muted:
        penalties.append(paper_vectors @ muted_vectors.T)
    if disliked:
        penalties.append((paper_vectors @ disliked_vectors.T) * _weights(disliked))
    if penalties:
        negative_part = weights.negative * np.concatenate(penalties, axis=1).max(axis=1)
        scores = scores - negative_part
    best_match = similarity.argmax(axis=1)

    scored = [
        ScoredPaper(
            **paper.model_dump(),
            coarse_score=round(float(scores[i]), 4),
            coarse_match=positive[int(best_match[i])].name,
            interest_score=round(float(interest[i]), 4),
            positive_score=None if positive_part is None else round(float(positive_part[i]), 4),
            negative_score=None if negative_part is None else round(float(negative_part[i]), 4),
            forced=paper.id in forced_ids,
        )
        for i, paper in enumerate(papers)
    ]
    scored.sort(key=lambda p: p.coarse_score or 0.0, reverse=True)
    return scored[:top_k] + [p for p in scored[top_k:] if p.forced]


def _weights(pairs: list[tuple[object, float]]) -> np.ndarray:
    return np.asarray([weight for _, weight in pairs], dtype=np.float32)
