from datetime import UTC, datetime

from gleaner.config import RankingWeights
from gleaner.models import Paper
from gleaner.profile import Anchor, AnchorSet
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
