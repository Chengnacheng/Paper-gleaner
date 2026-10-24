"""向量粗筛：按与兴趣锚点的相似度给论文打分，保留 Top K。

目前只使用兴趣描述拆出的锚点（docs/architecture.md 3.1.1 中的 A1，以及其中的负向锚点）；
反馈论文（A2/A3）和 Zotero（A4）在后续阶段加入。
"""

from __future__ import annotations

from collections.abc import Collection

from gleaner.config import RankingWeights
from gleaner.models import Paper, ScoredPaper
from gleaner.profile import AnchorSet
from gleaner.ranking.embedder import Embedder


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
) -> list[ScoredPaper]:
    """返回按分数从高到低排列的 Top K，命中 include_keywords 的论文无论分数都会附在后面。"""
    if not papers:
        return []
    positive, negative = anchors.positive, anchors.negative

    texts = [a.query for a in positive] + [a.query for a in negative]
    texts += [paper_text(p) for p in papers]
    vectors = embedder.embed(texts)
    positive_vectors = vectors[: len(positive)]
    negative_vectors = vectors[len(positive) : len(positive) + len(negative)]
    paper_vectors = vectors[len(positive) + len(negative) :]

    similarity = paper_vectors @ positive_vectors.T
    scores = weights.interest * similarity.max(axis=1)
    if negative:
        scores -= weights.negative * (paper_vectors @ negative_vectors.T).max(axis=1)
    best_match = similarity.argmax(axis=1)

    scored = [
        ScoredPaper(
            **paper.model_dump(),
            coarse_score=round(float(score), 4),
            coarse_match=positive[int(match)].name,
            forced=paper.id in forced_ids,
        )
        for paper, score, match in zip(papers, scores, best_match, strict=True)
    ]
    scored.sort(key=lambda p: p.coarse_score or 0.0, reverse=True)
    return scored[:top_k] + [p for p in scored[top_k:] if p.forced]
