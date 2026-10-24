import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import openai

from gleaner.models import ScoredPaper
from gleaner.profile import Anchor, AnchorSet
from gleaner.ranking import rerank, summarize
from tests.conftest import fake_llm, user_prompt

ANCHORS = AnchorSet(
    source_hash="h",
    model="m",
    generated_at=datetime.now(UTC),
    anchors=[
        Anchor(name="长上下文推理", query="KV cache compression"),
        Anchor(name="检索增强生成", query="retrieval augmented generation"),
        Anchor(name="医学影像", query="medical imaging", negative=True),
    ],
)


def candidate(n: int, coarse: float = 0.5, abstract: str = "Abstract.") -> ScoredPaper:
    return ScoredPaper(
        id=f"2610.{n:05d}",
        title=f"Paper {n}",
        abstract=abstract,
        categories=["cs.CL"],
        coarse_score=coarse,
        coarse_match="长上下文推理",
    )


def scorer(scores: dict[str, int], *, fail_containing: str | None = None):
    """根据 prompt 中的论文 ID 返回预设分数；prompt 含 fail_containing 时模拟网络错误。"""

    def handler(request: dict[str, Any]) -> str:
        prompt = user_prompt(request)
        if fail_containing and fail_containing in prompt:
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://x"))
        ids = re.findall(r'<paper id="([^"]+)">', prompt)
        results = [
            {
                "id": f"arXiv:{paper_id}v1",
                "score": scores.get(paper_id, 0),
                "matched_interests": ["长上下文推理"],
                "reason": f"{paper_id} 的理由",
                "tldr": f"{paper_id} 的摘要",
                "tags": ["kv-cache"],
            }
            for paper_id in ids
            if paper_id in scores
        ]
        return json.dumps({"results": results}, ensure_ascii=False)

    return handler


def run(candidates: list[ScoredPaper], llm, **kwargs: Any):
    options = {"top_n": 3, "min_score": 6, "language": "zh"} | kwargs
    return rerank(candidates, interests="我研究长上下文推理。", anchors=ANCHORS, llm=llm, **options)


def test_batches_and_prompt() -> None:
    candidates = [candidate(i) for i in range(23)]
    llm, fake = fake_llm(scorer({}))

    run(candidates, llm)

    assert len(fake.requests) == 3  # 10 + 10 + 3
    system = fake.requests[0]["messages"][0]["content"]
    assert "我研究长上下文推理。" in system
    assert "长上下文推理、检索增强生成" in system
    assert "医学影像" in system
    assert "简体中文" in system
    first_batch = user_prompt(fake.requests[0])
    assert first_batch.count("<paper id=") == 10
    assert '<paper id="2610.00000">' in first_batch
    assert fake.requests[0]["temperature"] == 0.2


def test_selects_by_score_threshold_and_top_n() -> None:
    candidates = [candidate(1, 0.9), candidate(2, 0.8), candidate(3, 0.7), candidate(4, 0.6)]
    candidates.append(candidate(5, 0.95))
    scores = {"2610.00001": 5, "2610.00002": 8, "2610.00003": 9, "2610.00004": 8}
    llm, _ = fake_llm(scorer(scores))

    result = run(candidates, llm, top_n=2)

    # 同分时按粗筛分数排序；低于 min_score 和没有打分的论文不入选
    assert [p.id for p in result.selected] == ["2610.00003", "2610.00002"]
    assert [p.id for p in result.ranked] == [
        "2610.00003",
        "2610.00002",
        "2610.00004",
        "2610.00001",
        "2610.00005",
    ]
    top = result.selected[0]
    assert top.llm_score == 9
    assert top.reason == "2610.00003 的理由"
    assert top.tldr == "2610.00003 的摘要"
    assert top.matched_interests == ["长上下文推理"]
    assert top.tags == ["kv-cache"]
    assert result.degraded is False
    assert result.failed_batches == 0


def test_failed_batch_is_skipped() -> None:
    candidates = [candidate(i, coarse=1 - i / 100) for i in range(12)]
    scores = {c.id: 7 for c in candidates}
    llm, _ = fake_llm(scorer(scores, fail_containing="2610.00010"))

    result = run(candidates, llm, top_n=20)

    assert result.failed_batches == 1
    assert result.degraded is False
    assert len(result.selected) == 10
    assert all(p.llm_score == 7 for p in result.selected)


def test_all_batches_failed_falls_back_to_coarse_order() -> None:
    candidates = [candidate(1, 0.2), candidate(2, 0.9), candidate(3, 0.5)]
    llm, _ = fake_llm(scorer({}, fail_containing="paper"))

    result = run(candidates, llm, top_n=2)

    assert result.degraded is True
    assert result.failed_batches == 1
    assert [p.id for p in result.selected] == ["2610.00002", "2610.00003"]
    assert all(p.llm_score is None for p in result.selected)


def test_budget_exceeded_degrades() -> None:
    candidates = [candidate(i) for i in range(15)]
    llm, fake = fake_llm(scorer({c.id: 8 for c in candidates}), daily_token_budget=50)

    result = run(candidates, llm, top_n=20)

    assert len(fake.requests) == 1  # 第一批用掉预算后，第二批不再发出请求
    assert result.failed_batches == 1
    assert len(result.selected) == 10


def test_long_abstract_is_truncated() -> None:
    llm, fake = fake_llm(scorer({}))
    run([candidate(1, abstract="x" * 5000)], llm)
    assert "x" * 1500 + "…" in user_prompt(fake.requests[0])
    assert "x" * 1501 not in user_prompt(fake.requests[0])


def test_empty_candidates() -> None:
    llm, fake = fake_llm(scorer({}))
    result = run([], llm)
    assert result.selected == []
    assert result.degraded is False
    assert fake.requests == []


def test_summarize() -> None:
    papers = [candidate(1).model_copy(update={"tldr": "压缩 KV cache"}), candidate(2)]
    llm, fake = fake_llm(lambda request: "  今天主要是长上下文方向，建议先读 [1]。 \n")

    assert summarize(papers, llm) == "今天主要是长上下文方向，建议先读 [1]。"
    prompt = user_prompt(fake.requests[0])
    assert "[1] Paper 1 —— 压缩 KV cache" in prompt
    assert "[2] Paper 2 —— Abstract." in prompt


def test_summarize_failure_and_empty() -> None:
    def fail(request: dict[str, Any]) -> str:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://x"))

    llm, fake = fake_llm(fail)
    assert summarize([candidate(1)], llm) is None
    assert summarize([], llm) is None
    assert len(fake.requests) == 1
