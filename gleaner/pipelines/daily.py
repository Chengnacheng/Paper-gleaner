"""每日推送流水线（docs/architecture.md 3.1）。

抓取 → 去重与关键词过滤 → 向量粗筛 → LLM 精排 → 生成概览 → 发布 → 写入 gleaner-data。
反馈同步（第 0 步）和自动深读（第 5 步）在后续阶段加入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, Protocol

from gleaner.config import Config
from gleaner.filters import apply_filters, load_seen, record_seen
from gleaner.llm import LLMClient
from gleaner.models import Digest, RunStats
from gleaner.notify import IssueRef
from gleaner.profile import get_anchors, load_interests
from gleaner.ranking import Embedder, coarse_rank, rerank, summarize
from gleaner.sources import ArxivSource, Source
from gleaner.store import DataStore

Status = Literal[
    "published",  # 已发布 Issue
    "preview",  # 没有发布渠道（dry-run），只生成了日报
    "already_published",  # 当天的 Issue 已存在，跳过
    "no_announcements",  # arXiv 今天没有公告
    "nothing_relevant",  # 没有达到 min_score 的论文，不发布
]


class Publisher(Protocol):
    def find_existing(self, day: date) -> IssueRef | None: ...

    def publish(self, digest: Digest) -> Digest: ...

    def close_stale(self, now: datetime | None = None) -> list[int]: ...


@dataclass
class DailyResult:
    status: Status
    digest: Digest | None = None
    existing: IssueRef | None = None


def digest_path(day: date) -> str:
    return f"history/digests/{day.isoformat()}.json"


def run_daily(
    config: Config,
    *,
    store: DataStore,
    llm: LLMClient,
    embedder: Embedder,
    today: date,
    source: Source | None = None,
    publisher: Publisher | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> DailyResult:
    started = time.monotonic()
    now = now or datetime.now(UTC)
    # 先读兴趣描述：没配置好就立刻失败，不白跑抓取
    interests = load_interests(config.profile.interests_file)

    existing = None if publisher is None or force else publisher.find_existing(today)
    if existing is not None:
        return DailyResult("already_published", existing=existing)

    papers = (source or ArxivSource(config.sources.arxiv)).fetch_latest()
    if not papers:
        return DailyResult("no_announcements")

    seen = load_seen(store, today)
    filtered = apply_filters(papers, seen=seen, config=config.filters)
    record_seen(store, [p for p in papers if p.id not in seen], today)

    anchors = get_anchors(store, interests, llm)
    ranking = config.ranking
    candidates = coarse_rank(
        filtered.papers,
        anchors,
        embedder,
        top_k=ranking.coarse_top_k,
        weights=ranking.weights,
        forced_ids=filtered.forced_ids,
    )
    reranked = rerank(
        candidates,
        interests=interests,
        anchors=anchors,
        llm=llm,
        top_n=ranking.final_top_n,
        min_score=ranking.min_score,
        language=config.language,
    )
    overview = summarize(reranked.selected, llm, config.language)

    usage = llm.tracker.total
    selected_ids = {p.id for p in reranked.selected}
    digest = Digest(
        day=today,
        generated_at=now,
        overview=overview,
        papers=reranked.selected,
        others=[p for p in reranked.ranked if p.id not in selected_ids],
        stats=RunStats(
            fetched=len(papers),
            duplicates=filtered.duplicates,
            excluded=filtered.excluded,
            candidates=len(candidates),
            selected=len(reranked.selected),
            failed_batches=reranked.failed_batches,
            degraded=reranked.degraded,
            llm_calls=usage.calls,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            duration_seconds=round(time.monotonic() - started, 1),
        ),
    )

    status: Status = "preview"
    if not digest.papers:
        status = "nothing_relevant"
    elif publisher is not None:
        digest = publisher.publish(digest)
        status = "published"
    if publisher is not None:
        publisher.close_stale(now)

    store.write_model(digest_path(today), digest)
    store.append_jsonl(
        f"stats/{today:%Y-%m}.jsonl",
        [
            {
                "day": today.isoformat(),
                "status": status,
                "issue": digest.issue_number,
                **digest.stats.model_dump(),
            }
        ],
    )
    return DailyResult(status, digest)
