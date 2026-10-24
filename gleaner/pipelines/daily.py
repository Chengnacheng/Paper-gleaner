"""每日推送流水线。

完整流程见 docs/architecture.md 3.1。目前（Phase 0）只实现抓取；
去重、粗筛、精排、发布和持久化在 Phase 1 中补上。
"""

from __future__ import annotations

from dataclasses import dataclass

from gleaner.config import Config
from gleaner.models import Paper
from gleaner.sources import ArxivSource, Source


@dataclass
class DailyResult:
    papers: list[Paper]


def run_daily(config: Config, *, source: Source | None = None) -> DailyResult:
    source = source or ArxivSource(config.sources.arxiv)
    return DailyResult(papers=source.fetch_latest())
