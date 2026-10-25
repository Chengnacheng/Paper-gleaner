from datetime import UTC, datetime, timedelta

import pytest

from gleaner.config import RankingWeights
from gleaner.models import Paper
from gleaner.profile import Anchor, AnchorSet, Memory, MutedTopic, PaperFeedback
from gleaner.ranking import coarse_rank
from tests.conftest import KeywordEmbedder

VOCABULARY = ["kv cache", "retrieval", "medical", "speech"]


def make_anchors(*, with_negative: bool = True) -> AnchorSet:
    anchors = [
        Anchor(name="长上下文", query="KV cache compression"),
        Anchor(name="RAG", query="retrieval augmented generation"),
    ]
    if with_negative:
        anchors.append(Anchor(name="医学影像", query="medical imaging", negative=True))
    return AnchorSet(source_hash="h", model="m", generated_at=datetime.now(UTC), anchors=anchors)


def paper(id: str, title: str, abstract: str = "") -> Paper:
    return Paper(id=id, title=title, abstract=abstract)


def rank(anchors: AnchorSet, top_k: int) -> list[str]:
    result = coarse_rank(
        PAPERS, anchors, KeywordEmbedder(VOCABULARY), top_k=top_k, weights=RankingWeights()
    )
    return [p.id for p in result]


PAPERS = [
    paper("speech", "Speech recognition"),
    paper("kv", "KV cache tricks", "We compress the KV cache."),
    paper("rag", "Retrieval for QA"),
    paper("weak", "Retrieval of speech segments"),
    paper("medical-rag", "Retrieval for medical reports", "Retrieval."),
]


def test_ranks_by_anchor_similarity() -> None:
    embedder = KeywordEmbedder(VOCABULARY)
    anchors = make_anchors(with_negative=False)
    ranked = coarse_rank(PAPERS, anchors, embedder, top_k=5, weights=RankingWeights())

    assert {p.id for p in ranked[:2]} == {"kv", "rag"}
    assert ranked[-1].id == "speech"
    assert ranked == sorted(ranked, key=lambda p: p.coarse_score, reverse=True)
    by_id = {p.id: p for p in ranked}
    assert by_id["kv"].coarse_match == "长上下文"
    assert by_id["rag"].coarse_match == "RAG"
    assert all(p.coarse_score is not None for p in ranked)


def test_negative_anchor_penalizes() -> None:
    without = rank(make_anchors(with_negative=False), top_k=5)
    with_negative = rank(make_anchors(), top_k=5)

    # 不加负向锚点时，medical-rag 与 RAG 更相关，排在 weak 前面；加上后被压到后面
    assert without.index("medical-rag") < without.index("weak")
    assert with_negative.index("medical-rag") > with_negative.index("weak")


def test_top_k_keeps_forced_papers() -> None:
    ranked = coarse_rank(
        PAPERS,
        make_anchors(),
        KeywordEmbedder(VOCABULARY),
        top_k=2,
        weights=RankingWeights(),
        forced_ids={"speech"},
    )

    assert {p.id for p in ranked[:2]} == {"kv", "rag"}
    assert ranked[2].id == "speech"
    assert [p.forced for p in ranked] == [False, False, True]


def test_embeds_anchors_and_papers_in_one_call() -> None:
    embedder = KeywordEmbedder(VOCABULARY)
    coarse_rank(PAPERS, make_anchors(), embedder, top_k=2, weights=RankingWeights())

    assert len(embedder.calls) == 1
    texts = embedder.calls[0]
    assert texts[:3] == [
        "KV cache compression",
        "retrieval augmented generation",
        "medical imaging",
    ]
    assert texts[4] == "KV cache tricks. We compress the KV cache."


def test_empty_papers() -> None:
    embedder = KeywordEmbedder(VOCABULARY)
    assert coarse_rank([], make_anchors(), embedder, top_k=5, weights=RankingWeights()) == []
    assert embedder.calls == []


# --- 反馈锚点（A2 / A3） ---------------------------------------------------------

NOW = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)


def liked(n: int, *, days_ago: float = 0, signal: str = "like") -> list[PaperFeedback]:
    """n 篇讲语音的正反馈论文。"""
    return [
        PaperFeedback(
            paper_id=f"2609.0000{i}",
            title=f"Speech model {i}",
            abstract="Speech recognition with speech encoders.",
            command=2.0 if signal == "like" else 0.0,
            reaction=1.0 if signal == "up" else 0.0,
            updated_at=NOW - timedelta(days=days_ago),
        )
        for i in range(n)
    ]


def disliked_medical() -> PaperFeedback:
    return PaperFeedback(
        paper_id="2609.00100",
        title="Medical imaging",
        abstract="Medical segmentation.",
        command=-2.0,
        updated_at=NOW,
    )


def ranked(memory: Memory | None, *, anchors: AnchorSet | None = None, embedder=None):
    result = coarse_rank(
        PAPERS,
        anchors or make_anchors(),
        embedder or KeywordEmbedder(VOCABULARY),
        top_k=5,
        weights=RankingWeights(),
        memory=memory,
        half_life_days=90,
        now=NOW,
    )
    return {p.id: p for p in result}, [p.id for p in result]


def test_cold_start_memory_changes_nothing() -> None:
    without, order = ranked(None)
    empty, empty_order = ranked(Memory())
    no_query, _ = ranked(Memory(muted=[MutedTopic("医学影像", None, NOW)]))  # 还没有英文描述

    assert empty_order == order
    for result in (empty, no_query):
        assert {i: p.coarse_score for i, p in result.items()} == {
            i: p.coarse_score for i, p in without.items()
        }
    kv = without["kv"]
    assert kv.positive_score is None
    assert kv.coarse_score == pytest.approx(kv.interest_score - kv.negative_score, abs=1e-4)


def test_liked_direction_rises() -> None:
    before, before_order = ranked(Memory())
    after, after_order = ranked(Memory(feedback=liked(3)))

    # 喜欢了三篇语音论文：同时涉及检索和语音的 weak 升到第一，纯语音的 speech 也往前了
    assert before_order.index("weak") == 2
    assert after_order[0] == "weak"
    assert after_order.index("speech") < before_order.index("speech")
    assert after["weak"].positive_score > 0.4
    assert after["kv"].positive_score == pytest.approx(0.0, abs=0.01)
    assert after["kv"].interest_score == before["kv"].interest_score
    for paper in after.values():
        total = paper.interest_score + paper.positive_score - paper.negative_score
        assert paper.coarse_score == pytest.approx(total, abs=1e-3)


def test_positive_term_grows_with_more_feedback() -> None:
    one, _ = ranked(Memory(feedback=liked(1)))
    three, _ = ranked(Memory(feedback=liked(3)))
    thumbs, _ = ranked(Memory(feedback=liked(1, signal="up")))

    # mean_top3：不足 3 个按 0 计；一个 👍 的强度是 /like 的一半
    assert one["speech"].positive_score == pytest.approx(three["speech"].positive_score / 3, 1e-3)
    assert thumbs["speech"].positive_score == pytest.approx(one["speech"].positive_score / 2, 1e-3)


def test_dislike_and_mute_push_similar_papers_down() -> None:
    anchors = make_anchors(with_negative=False)
    _, before = ranked(Memory(), anchors=anchors)
    disliked, after_dislike = ranked(Memory(feedback=[disliked_medical()]), anchors=anchors)
    muted = Memory(muted=[MutedTopic("医学影像", "Medical imaging", NOW)])
    _, after_mute = ranked(muted, anchors=anchors)

    assert before.index("medical-rag") < before.index("weak")
    assert after_dislike.index("medical-rag") > after_dislike.index("weak")
    assert after_mute.index("medical-rag") > after_mute.index("weak")
    assert disliked["medical-rag"].negative_score > 0.3
    assert disliked["kv"].negative_score < 0.01


def test_feedback_decays_with_half_life() -> None:
    fresh, _ = ranked(Memory(feedback=liked(3)))
    old, _ = ranked(Memory(feedback=liked(3, days_ago=90)))
    older, _ = ranked(Memory(feedback=liked(3, days_ago=180)))

    score = fresh["speech"].positive_score
    assert old["speech"].positive_score == pytest.approx(score / 2, rel=1e-3)
    assert older["speech"].positive_score == pytest.approx(score / 4, rel=1e-3)


def test_feedback_texts_are_embedded_after_papers() -> None:
    embedder = KeywordEmbedder(VOCABULARY)
    bare = PaperFeedback(paper_id="2609.09999", title="2609.09999", command=2.0, updated_at=NOW)
    memory = Memory(
        feedback=[*liked(1), bare, disliked_medical()],
        muted=[MutedTopic("语音", "Speech processing.", NOW)],
    )

    ranked(memory, embedder=embedder)

    [texts] = embedder.calls
    assert texts[3] == "Speech recognition. "  # 论文的位置不变
    assert texts[-3:] == [
        "Speech model 0. Speech recognition with speech encoders.",
        "Medical imaging. Medical segmentation.",
        "Speech processing.",
    ]  # 只有 ID、没有标题和摘要的反馈不参与
