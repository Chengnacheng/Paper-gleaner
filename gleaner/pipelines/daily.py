"""每日推送流水线（docs/architecture.md 3.1）。

同步表情 → 抓取 → 去重与关键词过滤 → 向量粗筛 → LLM 精排 → 生成概览 → 发布 Issue
→ 推送到邮件、飞书、企业微信 → 写入 gleaner-data。自动深读（第 5 步）还没有接入。

推送渠道只在日报发布成功时发送（没有启用 GitHub Issue 时照样发送，只是不带链接）；
没有达到 min_score 的论文、没有新公告和 dry-run 都不发送。各渠道的结果记在 digest 里，
兜底运行只补发失败的渠道。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal, Protocol

from pydantic import ValidationError

from gleaner.config import Config
from gleaner.filters import apply_filters, load_seen, record_seen
from gleaner.llm import LLMClient
from gleaner.models import ChannelResult, Digest, RunStats
from gleaner.notify import IssueRef
from gleaner.notify.base import Notifier
from gleaner.notify.dispatch import failed_channels, failure_warnings, send_all
from gleaner.pipelines.feedback import (
    REACTION_SYNC_DAYS,
    ReactionReader,
    SyncResult,
    sync_reactions,
)
from gleaner.profile import ensure_queries, get_anchors, load_interests, load_memory
from gleaner.ranking import Embedder, coarse_rank, rerank, summarize
from gleaner.sources import ArxivSource, Source
from gleaner.store import DataStore

Status = Literal[
    "published",  # 已发布 Issue
    "preview",  # 没有发布渠道（dry-run），只生成了日报
    "already_published",  # 当天的日报已发布，跳过
    "repaired",  # 当天的日报已发布，补发了上次没发出去的评论或推送
    "no_announcements",  # arXiv 今天没有公告
    "nothing_relevant",  # 没有达到 min_score 的论文，不发布
]

logger = logging.getLogger(__name__)


class Publisher(Protocol):
    def find_existing(self, day: date) -> IssueRef | None: ...

    def publish(self, digest: Digest) -> Digest: ...

    def repair(self, digest: Digest) -> Digest: ...

    def close_stale(self, now: datetime | None = None) -> list[int]: ...


@dataclass
class DailyResult:
    status: Status
    digest: Digest | None = None
    existing: IssueRef | None = None
    sampled: bool = False  # 没有新公告，改用了最近提交的论文
    sync: SyncResult = field(default_factory=SyncResult)  # 第 0 步：表情同步
    warnings: list[str] = field(default_factory=list)  # 不影响日报的问题，如表情同步失败
    notified: dict[str, ChannelResult] = field(default_factory=dict)  # 这次发送的渠道及结果
    repaired_comments: int = 0  # 兜底运行补发的论文评论数


DIGEST_DIR = "history/digests"


def digest_path(day: date) -> str:
    return f"{DIGEST_DIR}/{day.isoformat()}.json"


def load_recent_digests(store: DataStore, today: date, days: int) -> list[Digest]:
    """最近 days 天（含今天）保存的日报，从早到晚；格式不对的跳过。"""
    digests = []
    for offset in reversed(range(days)):
        day = today - timedelta(days=offset)
        try:
            digest = store.read_model(digest_path(day), Digest)
        except ValidationError as e:
            logger.warning("%s 格式不正确，跳过：%s", digest_path(day), e)
            continue
        if digest is not None:
            digests.append(digest)
    return digests


def run_daily(
    config: Config,
    *,
    store: DataStore,
    llm: LLMClient,
    embedder: Embedder,
    today: date,
    source: Source | None = None,
    publisher: Publisher | None = None,
    reactions: ReactionReader | None = None,
    owner: str | None = None,
    notifiers: Sequence[Notifier] = (),
    force: bool = False,
    sample_size: int | None = None,
    now: datetime | None = None,
) -> DailyResult:
    """运行一次日报流水线。

    reactions、owner：从哪里读取论文评论的表情、只统计谁的；没有时跳过表情同步。
    notifiers：发布后推送摘要的渠道（邮件、飞书、企业微信），dry-run 时为空。
    sample_size：没有新公告时，改用最近提交的这么多篇论文（不去重、不记录为已处理）。
    只在没有发布渠道时生效，用于周末等时候试运行、检查配置。
    """
    started = time.monotonic()
    now = now or datetime.now(UTC)
    # 先读兴趣描述：没配置好就立刻失败，不白跑抓取
    interests = load_interests(config.profile.interests_file)

    # 第 0 步放在最前面：当天日报已经发布（兜底运行）或没有新公告时也同步
    sync = SyncResult()
    if reactions is not None and owner:
        digests = load_recent_digests(store, today, REACTION_SYNC_DAYS)
        sync = sync_reactions(
            store, digests, reactions, owner=owner, today=today, now=now, timezone=config.timezone
        )
    warnings = _sync_warnings(sync)

    existing = None if publisher is None or force else publisher.find_existing(today)
    # 没有启用 GitHub Issue 时，按保存的 digest 判断今天是否已经推送过
    pushed = publisher is None and bool(notifiers) and not force and _was_notified(store, today)
    if existing is not None or pushed:
        result = _repair(store, publisher, existing, today, notifiers, now)
        result.sync, result.warnings = sync, warnings + result.warnings
        return result

    source = source or ArxivSource(config.sources.arxiv)
    papers = source.fetch_latest()
    sampled = not papers and bool(sample_size) and publisher is None and not notifiers
    if sampled and sample_size:
        papers = source.fetch_recent(sample_size)
    if not papers:
        return DailyResult("no_announcements", sync=sync, warnings=warnings)

    seen = set() if sampled else load_seen(store, today)
    filtered = apply_filters(papers, seen=seen, config=config.filters)
    if not sampled:
        record_seen(store, [p for p in papers if p.id not in seen], today)

    anchors = get_anchors(store, interests, llm)
    # 反馈锚点、屏蔽主题和学到的偏好；手改过的屏蔽主题缺少英文描述时在这里补上
    memory = load_memory(store, today)
    warnings += ensure_queries(store, memory.muted, llm)
    ranking = config.ranking
    candidates = coarse_rank(
        filtered.papers,
        anchors,
        embedder,
        top_k=ranking.coarse_top_k,
        weights=ranking.weights,
        forced_ids=filtered.forced_ids,
        memory=memory,
        half_life_days=ranking.feedback_half_life_days,
        now=now,
    )
    reranked = rerank(
        candidates,
        interests=interests,
        anchors=anchors,
        llm=llm,
        top_n=ranking.final_top_n,
        min_score=ranking.min_score,
        language=config.language,
        preferences=memory.preferences,
        muted_topics=[topic.topic for topic in memory.muted],
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
            feedback=memory.stats(now, ranking.feedback_half_life_days).model_copy(
                update={"synced": sync.records, "sync_error": sync.error}
            ),
        ),
    )

    status: Status = "preview"
    notified: dict[str, ChannelResult] = {}
    if not digest.papers:
        status = "nothing_relevant"
    else:
        if publisher is not None:
            digest = publisher.publish(digest)
            status = "published"
        if notifiers:
            # 发布 Issue 之后再推送，消息里才有 Issue 链接；某个渠道失败不影响其它渠道
            notified = send_all(digest, notifiers, now=now)
            digest = digest.model_copy(update={"notifications": notified})
            warnings += failure_warnings(notified)
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
    return DailyResult(
        status, digest, sampled=sampled, sync=sync, warnings=warnings, notified=notified
    )


def _sync_warnings(sync: SyncResult) -> list[str]:
    if sync.error is None:
        return []
    written = f"已写入 {sync.records} 条，其余下次再同步"
    return [f"表情同步没有完成（{written}）：{sync.error}"]


def _saved_digest(store: DataStore, today: date) -> Digest | None:
    try:
        return store.read_model(digest_path(today), Digest)
    except ValidationError:
        return None


def _was_notified(store: DataStore, today: date) -> bool:
    saved = _saved_digest(store, today)
    return saved is not None and bool(saved.notifications)


def _repair(
    store: DataStore,
    publisher: Publisher | None,
    existing: IssueRef | None,
    today: date,
    notifiers: Sequence[Notifier],
    now: datetime,
) -> DailyResult:
    """当天日报已发布：按保存的 digest 补发上次没发出去的论文评论，以及推送失败的渠道。

    已经发送成功的渠道不再发送；上次没有启用的渠道也不补发。
    """
    saved = _saved_digest(store, today)
    if saved is None or (existing is not None and saved.issue_number != existing.number):
        return DailyResult("already_published", existing=existing)

    repaired = publisher.repair(saved) if publisher is not None else saved
    retry = failed_channels(repaired, notifiers)
    notified = send_all(repaired, retry, now=now) if retry else {}
    if notified:
        notifications = {**repaired.notifications, **notified}
        repaired = repaired.model_copy(update={"notifications": notifications})
    if repaired == saved:
        return DailyResult("already_published", saved, existing)
    store.write_model(digest_path(today), repaired)
    return DailyResult(
        "repaired",
        repaired,
        existing,
        warnings=failure_warnings(notified),
        notified=notified,
        repaired_comments=len(repaired.comment_ids) - len(saved.comment_ids),
    )
