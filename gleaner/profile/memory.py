"""反馈与记忆（docs/architecture.md 3.4）。

所有反馈（表情、/like、/dislike、/deep、/compare）都按月追加到 gleaner-data 分支的
feedback/<YYYY-MM>.jsonl，只追加、不修改；并发追加靠 merge=union 合并。

读取时把一篇论文的全部记录聚合成一个「有效反馈」（PaperFeedback），规则如下：

- 表情只看这篇论文最新的一条表情记录：+1（👍）、-1（👎）或 0（没有或已撤销）。
  日报每天同步时只追加状态有变化的记录，即使重复写入了，也只有最新一条生效，不会重复计数；
- /like、/dislike 以最新一条为准：+2 或 -2，之后的 /dislike 覆盖之前的 /like；
- /deep、/compare 是隐式正反馈，每篇最多 +0.5，做多少次都只算一次；
- 三者相加：为正进入正反馈锚点（A2），为负进入负反馈锚点（A3），为 0 不参与。

记录按 created_at 排序后再聚合：union 合并后文件里的行序不可靠。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from gleaner.filters import recent_months
from gleaner.models import Feedback, FeedbackStats, Paper
from gleaner.profile.muted import MutedTopic, load_muted
from gleaner.store import DataStore

FEEDBACK_DIR = "feedback"
# 半衰期默认 90 天，一年前的反馈权重只剩约 6%，再往前的不读
FEEDBACK_LOOKBACK_MONTHS = 12
REACTION_WEIGHT = 1.0  # 👍 / 👎
COMMAND_WEIGHT = 2.0  # /like / /dislike
IMPLICIT_WEIGHT = 0.5  # /deep、/compare
MAX_NOTE_CHARS = 200  # /like、/dislike 附带的原因最多保留的字数
# 每类反馈锚点最多取权重最高的这么多篇，控制每天嵌入的计算量
MAX_FEEDBACK_ANCHORS = 200

CommandSignal = Literal["like", "dislike", "deep", "compare"]
_COMMAND_WEIGHTS: dict[str, float] = {
    "like": COMMAND_WEIGHT,
    "dislike": -COMMAND_WEIGHT,
    "deep": IMPLICIT_WEIGHT,
    "compare": IMPLICIT_WEIGHT,
}
_REACTION_ICONS = {1.0: "👍", -1.0: "👎"}

logger = logging.getLogger(__name__)


def feedback_path(created_at: datetime, timezone: str) -> str:
    """按配置的时区决定记录落在哪个月的文件里。"""
    return f"{FEEDBACK_DIR}/{created_at.astimezone(ZoneInfo(timezone)):%Y-%m}.jsonl"


def clean_note(note: str | None) -> str | None:
    """整理 /like、/dislike 附带的原因：合并空白，截断到 MAX_NOTE_CHARS，空的返回 None。"""
    if note is None:
        return None
    return " ".join(note.split())[:MAX_NOTE_CHARS] or None


def command_feedback(
    paper: Paper, signal: CommandSignal, *, now: datetime, note: str | None = None
) -> Feedback:
    """指令产生的反馈：/like、/dislike 为 ±2，/deep、/compare 为 +0.5。"""
    note = clean_note(note)
    return Feedback(
        paper_id=paper.id,
        title=paper.title,
        abstract=paper.abstract,
        signal=signal,
        weight=_COMMAND_WEIGHTS[signal],
        note=note,
        source="command",
        created_at=now,
    )


def record_feedback(store: DataStore, records: Iterable[Feedback], *, timezone: str) -> None:
    """追加反馈记录，按 created_at 所在的月份分片。"""
    by_path: dict[str, list[dict]] = {}
    for record in records:
        path = feedback_path(record.created_at, timezone)
        by_path.setdefault(path, []).append(record.model_dump(mode="json"))
    for path, rows in by_path.items():
        store.append_jsonl(path, rows)


def load_feedback(
    store: DataStore, today: date, months: int = FEEDBACK_LOOKBACK_MONTHS
) -> list[Feedback]:
    """读取最近几个月的反馈记录，按 created_at 从早到晚排列；格式不对的行跳过。"""
    records: list[Feedback] = []
    for month in reversed(recent_months(today, months)):
        path = f"{FEEDBACK_DIR}/{month}.jsonl"
        for raw in store.read_jsonl(path):
            try:
                records.append(Feedback.model_validate(raw))
            except ValidationError as e:
                logger.warning("跳过 %s 中格式不正确的反馈：%s", path, e)
    records.sort(key=lambda record: record.created_at)  # 稳定排序：同一时间的保持文件中的顺序
    return records


@dataclass
class PaperFeedback:
    """一篇论文的有效反馈，由它的全部反馈记录聚合而来（规则见模块说明）。"""

    paper_id: str
    title: str = ""
    abstract: str = ""
    reaction: float = 0.0  # 最新的表情状态：+1、-1 或 0
    command: float = 0.0  # 最新一条 /like（+2）或 /dislike（-2），没有时为 0
    implicit: float = 0.0  # /deep、/compare 过为 +0.5
    implicit_signals: list[str] = field(default_factory=list)  # 出现过的 deep、compare
    notes: list[str] = field(default_factory=list)  # 与当前 /like 或 /dislike 同向的原因
    updated_at: datetime | None = None  # 生效的信号中最新的时间，时间衰减从这里算起

    @property
    def total(self) -> float:
        return self.reaction + self.command + self.implicit

    @property
    def paper(self) -> Paper:
        return Paper(id=self.paper_id, title=self.title or self.paper_id, abstract=self.abstract)

    @property
    def has_text(self) -> bool:
        """有没有可以生成向量的内容：arXiv API 不可用时记下的反馈只有 ID。"""
        return bool(self.abstract) or bool(self.title and self.title != self.paper_id)

    @property
    def signals(self) -> list[str]:
        """生效的信号，用于展示，如 ["👍", "/like", "/deep"]。"""
        result = []
        if self.reaction:
            result.append(_REACTION_ICONS[self.reaction])
        if self.command:
            result.append("/like" if self.command > 0 else "/dislike")
        result += [f"/{signal}" for signal in self.implicit_signals]
        return result

    def strength(self) -> float:
        """信号强度，0–1：一次 /like 或 /dislike 为 1，一个表情为 0.5，只有 /deep 为 0.25。"""
        return min(abs(self.total) / COMMAND_WEIGHT, 1.0)

    def weight(self, now: datetime, half_life_days: float) -> float:
        """作为锚点的权重：信号强度 × 时间衰减（每过一个半衰期减半）。"""
        if self.updated_at is None:
            return self.strength()
        age_days = max((now - self.updated_at).total_seconds() / 86_400, 0.0)
        return self.strength() * 0.5 ** (age_days / half_life_days)


def aggregate_feedback(records: Iterable[Feedback]) -> list[PaperFeedback]:
    """把反馈记录按论文聚合，返回顺序为各论文第一次出现的先后。"""
    papers: dict[str, PaperFeedback] = {}
    # 每篇论文各分项最新一条记录的时间
    times: dict[str, dict[str, datetime]] = {}
    for record in sorted(records, key=lambda r: r.created_at):
        item = papers.setdefault(record.paper_id, PaperFeedback(record.paper_id))
        if record.abstract or not item.abstract:  # 优先用最新的、带摘要的元数据
            item.title, item.abstract = record.title, record.abstract
        parts = times.setdefault(record.paper_id, {})
        if record.signal in ("up", "down"):
            # 表情记录的 weight 是同步时的状态，按符号取 +1 / -1 / 0
            item.reaction = REACTION_WEIGHT * ((record.weight > 0) - (record.weight < 0))
            parts["reaction"] = record.created_at
        elif record.signal in ("like", "dislike"):
            value = _COMMAND_WEIGHTS[record.signal]
            if value != item.command:
                item.notes = []  # 态度变了，之前的原因不再适用
            item.command = value
            if record.note and record.note not in item.notes:
                item.notes.append(record.note)
            parts["command"] = record.created_at
        else:
            item.implicit = IMPLICIT_WEIGHT
            if record.signal not in item.implicit_signals:
                item.implicit_signals.append(record.signal)
            parts["implicit"] = record.created_at
        parts["any"] = record.created_at

    for paper_id, item in papers.items():
        parts = times[paper_id]
        active = [
            parts[name]
            for name, value in (
                ("reaction", item.reaction),
                ("command", item.command),
                ("implicit", item.implicit),
            )
            if value
        ]
        item.updated_at = max(active) if active else parts["any"]
    return list(papers.values())


@dataclass
class Memory:
    """粗筛用到的记忆：每篇论文的有效反馈和当前屏蔽的主题。"""

    feedback: list[PaperFeedback] = field(default_factory=list)
    muted: list[MutedTopic] = field(default_factory=list)

    def positive(self, now: datetime, half_life_days: float) -> list[tuple[PaperFeedback, float]]:
        """正反馈锚点（A2）及其权重，按权重从高到低。"""
        return self._anchors(lambda item: item.total > 0, now, half_life_days)

    def negative(self, now: datetime, half_life_days: float) -> list[tuple[PaperFeedback, float]]:
        """负反馈锚点（A3 中的论文）及其权重，按权重从高到低。"""
        return self._anchors(lambda item: item.total < 0, now, half_life_days)

    def stats(self, now: datetime, half_life_days: float) -> FeedbackStats:
        return FeedbackStats(
            up=sum(item.reaction > 0 for item in self.feedback),
            down=sum(item.reaction < 0 for item in self.feedback),
            like=sum(item.command > 0 for item in self.feedback),
            dislike=sum(item.command < 0 for item in self.feedback),
            implicit=sum(item.implicit > 0 for item in self.feedback),
            positive=len(self.positive(now, half_life_days)),
            negative=len(self.negative(now, half_life_days)),
            muted=len(self.muted),
        )

    def _anchors(
        self, keep: Callable[[PaperFeedback], bool], now: datetime, half_life_days: float
    ) -> list[tuple[PaperFeedback, float]]:
        weighted = [
            (item, item.weight(now, half_life_days))
            for item in self.feedback
            if keep(item) and item.has_text
        ]
        weighted.sort(key=lambda pair: pair[1], reverse=True)
        return weighted[:MAX_FEEDBACK_ANCHORS]


def load_memory(store: DataStore, today: date) -> Memory:
    return Memory(aggregate_feedback(load_feedback(store, today)), load_muted(store))


def latest_reactions(records: Sequence[Feedback]) -> dict[str, float]:
    """每篇论文最新一条表情记录的状态（+1 / -1 / 0），同步表情时据此判断有没有变化。"""
    return {item.paper_id: item.reaction for item in aggregate_feedback(_reactions(records))}


def _reactions(records: Iterable[Feedback]) -> list[Feedback]:
    return [record for record in records if record.signal in ("up", "down")]
