from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from gleaner.config import Config
from gleaner.filters import record_seen
from gleaner.models import Digest, Paper
from gleaner.notify import IssueRef
from gleaner.pipelines.daily import digest_path, run_daily
from gleaner.profile import ProfileError
from gleaner.sources.arxiv import parse_rss
from gleaner.store import DataStore
from tests.conftest import PIPELINE_VOCABULARY, KeywordEmbedder, fake_llm, pipeline_llm

TODAY = date(2026, 10, 26)
SCORES = {"2610.00001": 9, "2610.00002": 7, "2610.00003": 4}


class FakeSource:
    name = "fake"

    def __init__(self, papers: list[Paper], recent: list[Paper] | None = None) -> None:
        self.papers = papers
        self.recent = recent or []
        self.calls = 0
        self.recent_limits: list[int] = []

    def fetch_latest(self) -> list[Paper]:
        self.calls += 1
        return self.papers

    def fetch_recent(self, limit: int) -> list[Paper]:
        self.recent_limits.append(limit)
        return self.recent[:limit]


class FakePublisher:
    def __init__(self, existing: IssueRef | None = None) -> None:
        self.existing = existing
        self.published: list[Digest] = []
        self.repaired: list[Digest] = []
        self.closed = 0

    def find_existing(self, day: date) -> IssueRef | None:
        return self.existing

    def publish(self, digest: Digest) -> Digest:
        self.published.append(digest)
        return digest.model_copy(update={"issue_number": 7, "issue_url": "https://x/7"})

    def repair(self, digest: Digest) -> Digest:
        self.repaired.append(digest)
        ids = {p.id: 900 + i for i, p in enumerate(digest.papers)} | digest.comment_ids
        return digest.model_copy(update={"comment_ids": ids})

    def close_stale(self, now: datetime | None = None) -> list[int]:
        self.closed += 1
        return []


@pytest.fixture
def config(tmp_path: Path) -> Config:
    interests = tmp_path / "interests.md"
    interests.write_text("我研究长上下文推理和 RAG。", encoding="utf-8")
    return Config.model_validate({"profile": {"interests_file": str(interests)}})


@pytest.fixture
def papers(rss_bytes: bytes) -> list[Paper]:
    # 与 ArxivSource 默认过滤一致：new 和 cross，不含 replace
    return [p for p in parse_rss(rss_bytes) if p.announce_type in {"new", "cross"}]


def run(config: Config, store: DataStore, papers: list[Paper], scores=SCORES, **kwargs):
    llm, fake = fake_llm(pipeline_llm(scores))
    source = FakeSource(papers, recent=kwargs.pop("recent", None))
    result = run_daily(
        config,
        store=store,
        llm=llm,
        embedder=KeywordEmbedder(PIPELINE_VOCABULARY),
        today=kwargs.pop("today", TODAY),
        source=source,
        now=datetime(2026, 10, 26, 1, 30, tzinfo=UTC),
        **kwargs,
    )
    return result, fake, source


def test_preview_without_publisher(config: Config, papers: list[Paper], tmp_path: Path) -> None:
    store = DataStore(tmp_path / "data")

    result, fake, _ = run(config, store, papers)

    assert result.status == "preview"
    digest = result.digest
    assert [p.id for p in digest.papers] == ["2610.00001", "2610.00002"]
    assert [p.id for p in digest.others] == ["2610.00003"]
    assert digest.papers[0].reason == "2610.00001 与长上下文推理相关"
    assert digest.overview == "今天以长上下文推理为主，建议先读 [1]。"
    assert digest.issue_number is None

    stats = digest.stats
    assert (stats.fetched, stats.candidates, stats.selected) == (3, 3, 2)
    assert stats.llm_calls == len(fake.requests) == 3  # 兴趣拆解 + 1 批精排 + 概览
    assert stats.prompt_tokens == 300

    assert store.read_model(digest_path(TODAY), Digest) == digest
    assert [r["id"] for r in store.read_jsonl("history/seen/2026-10.jsonl")] == [
        "2610.00001",
        "2610.00002",
        "2610.00003",
    ]
    assert store.read_jsonl("stats/2026-10.jsonl")[0]["status"] == "preview"
    assert store.read_text("profile/anchors.json") is not None


def test_publishes_and_records_issue(config: Config, papers: list[Paper], tmp_path: Path) -> None:
    store = DataStore(tmp_path / "data")
    publisher = FakePublisher()

    result, _, _ = run(config, store, papers, publisher=publisher)

    assert result.status == "published"
    assert len(publisher.published) == 1
    assert publisher.closed == 1
    saved = store.read_model(digest_path(TODAY), Digest)
    assert saved.issue_number == 7
    assert store.read_jsonl("stats/2026-10.jsonl")[0]["issue"] == 7


def test_skips_when_already_published(config: Config, papers: list[Paper], tmp_path: Path) -> None:
    store = DataStore(tmp_path / "data")
    existing = IssueRef(3, "https://x/3")

    result, fake, source = run(config, store, papers, publisher=FakePublisher(existing))

    assert result.status == "already_published"
    assert result.existing == existing
    assert source.calls == 0
    assert fake.requests == []
    assert not (tmp_path / "data").exists()


def test_rerun_repairs_missing_comments(
    config: Config, papers: list[Paper], tmp_path: Path
) -> None:
    store = DataStore(tmp_path / "data")
    first, _, _ = run(config, store, papers, publisher=FakePublisher())
    saved = first.digest.model_copy(update={"comment_ids": {"2610.00001": 11}})
    store.write_model(digest_path(TODAY), saved)
    publisher = FakePublisher(IssueRef(7, "https://x/7"))

    result, fake, source = run(config, store, papers, publisher=publisher)

    assert result.status == "repaired"
    assert publisher.repaired == [saved]
    assert source.calls == 0
    assert fake.requests == []
    assert store.read_model(digest_path(TODAY), Digest).comment_ids == {
        "2610.00001": 11,
        "2610.00002": 901,
    }


def test_rerun_without_missing_comments(
    config: Config, papers: list[Paper], tmp_path: Path
) -> None:
    store = DataStore(tmp_path / "data")
    first, _, _ = run(config, store, papers, publisher=FakePublisher())
    complete = first.digest.model_copy(update={"comment_ids": {"2610.00001": 11, "2610.00002": 12}})
    store.write_model(digest_path(TODAY), complete)

    result, _, _ = run(config, store, papers, publisher=FakePublisher(IssueRef(7, "https://x/7")))

    assert result.status == "already_published"
    assert result.digest == complete


def test_rerun_ignores_digest_of_another_issue(
    config: Config, papers: list[Paper], tmp_path: Path
) -> None:
    store = DataStore(tmp_path / "data")
    run(config, store, papers, publisher=FakePublisher())  # 保存的 digest 对应 Issue #7
    publisher = FakePublisher(IssueRef(8, "https://x/8"))

    result, _, _ = run(config, store, papers, publisher=publisher)

    assert result.status == "already_published"
    assert publisher.repaired == []


def test_force_republishes(config: Config, papers: list[Paper], tmp_path: Path) -> None:
    publisher = FakePublisher(IssueRef(3, "https://x/3"))
    result, _, _ = run(config, DataStore(tmp_path), papers, publisher=publisher, force=True)
    assert result.status == "published"


def test_no_announcements(config: Config, tmp_path: Path) -> None:
    publisher = FakePublisher()
    result, fake, _ = run(config, DataStore(tmp_path / "data"), [], publisher=publisher)

    assert result.status == "no_announcements"
    assert fake.requests == []
    assert publisher.published == []
    assert not (tmp_path / "data").exists()


def test_nothing_relevant(config: Config, papers: list[Paper], tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    publisher = FakePublisher()
    low = {paper_id: 3 for paper_id in SCORES}

    result, fake, _ = run(config, store, papers, scores=low, publisher=publisher)

    assert result.status == "nothing_relevant"
    assert publisher.published == []
    assert len(result.digest.others) == 3
    assert len(fake.requests) == 2  # 没有入选论文时不生成概览
    assert store.read_model(digest_path(TODAY), Digest) is not None


def test_papers_are_not_repeated_on_later_days(
    config: Config, papers: list[Paper], tmp_path: Path
) -> None:
    store = DataStore(tmp_path)
    run(config, store, papers)

    same_day, _, _ = run(config, store, papers)
    next_day, _, _ = run(config, store, papers, today=date(2026, 10, 27))

    assert same_day.digest.stats.duplicates == 0  # 当天强制重跑不算重复
    assert next_day.status == "nothing_relevant"
    assert next_day.digest.stats.duplicates == 3
    assert next_day.digest.stats.candidates == 0


def test_missing_interests_fails_before_fetching(papers: list[Paper], tmp_path: Path) -> None:
    config = Config.model_validate({"profile": {"interests_file": str(tmp_path / "nope.md")}})
    with pytest.raises(ProfileError):
        run(config, DataStore(tmp_path), papers)


def test_dry_run_samples_recent_papers_without_announcements(
    config: Config, papers: list[Paper], tmp_path: Path
) -> None:
    store = DataStore(tmp_path)
    record_seen(store, papers, date(2026, 10, 23))  # 最近的论文都处理过，也照样参与试运行

    result, fake, source = run(config, store, [], recent=papers, sample_size=20)

    assert result.status == "preview"
    assert result.sampled is True
    assert source.recent_limits == [20]
    assert [p.id for p in result.digest.papers] == ["2610.00001", "2610.00002"]
    assert result.digest.stats.duplicates == 0
    assert len(fake.requests) == 3
    # 抽样的论文不记为已处理，不影响之后的去重
    assert store.read_jsonl("history/seen/2026-10.jsonl") == [
        {"id": p.id, "date": "2026-10-23"} for p in papers
    ]


def test_no_sampling_when_publishing(config: Config, papers: list[Paper], tmp_path: Path) -> None:
    result, _, source = run(
        config, DataStore(tmp_path), [], recent=papers, sample_size=20, publisher=FakePublisher()
    )

    assert result.status == "no_announcements"
    assert source.recent_limits == []
