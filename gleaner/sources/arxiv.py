"""arXiv 来源：通过 RSS 获取当天公告的论文。

arXiv 的 RSS（rss.arxiv.org）每个公告日更新一次，条目里已包含标题、摘要、作者、
分类和公告类型（new / cross / replace / replace-cross），日报所需的元数据都在其中。
周末和节假日没有公告时，RSS 不含任何条目。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from time import struct_time
from typing import Any

import feedparser
import httpx

from gleaner import __version__
from gleaner.config import ArxivConfig
from gleaner.models import Paper
from gleaner.sources.base import SourceError

RSS_URL = "https://rss.arxiv.org/rss/{categories}"
USER_AGENT = f"paper-gleaner/{__version__} (+https://github.com/stanGSA/Paper-gleaner)"

_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
# 摘要前缀形如 "arXiv:2410.12345v1 Announce Type: new \nAbstract: ..."
_DESCRIPTION_RE = re.compile(
    r"^\s*arXiv:\S+\s+Announce Type:\s*(?P<type>\S+)\s*Abstract:\s*(?P<abstract>.*)$",
    re.DOTALL,
)
_REPLACE_TYPES = {"replace", "replace-cross"}


class ArxivSource:
    name = "arxiv"

    def __init__(
        self,
        config: ArxivConfig,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.config = config
        self._client = client
        self._timeout = timeout

    @property
    def feed_url(self) -> str:
        return RSS_URL.format(categories="+".join(self.config.categories))

    def fetch_latest(self) -> list[Paper]:
        papers = parse_rss(self._download(self.feed_url))
        return [paper for paper in papers if self._wanted(paper)]

    def _download(self, url: str) -> bytes:
        try:
            if self._client is not None:
                response = self._client.get(url)
            else:
                with httpx.Client(
                    headers={"User-Agent": USER_AGENT},
                    timeout=self._timeout,
                    follow_redirects=True,
                    transport=httpx.HTTPTransport(retries=3),
                ) as client:
                    response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceError(f"抓取 arXiv RSS 失败：{e}") from e
        return response.content

    def _wanted(self, paper: Paper) -> bool:
        if paper.announce_type == "cross":
            return self.config.include_cross_list
        if paper.announce_type in _REPLACE_TYPES:
            return self.config.include_replace
        return True


def parse_rss(content: bytes) -> list[Paper]:
    """解析 arXiv RSS，按出现顺序返回去重后的论文。"""
    feed = feedparser.parse(content)
    # 维护页等非 RSS 内容不能当作"今天没有公告"
    if not feed.entries and (feed.bozo or not feed.version):
        raise SourceError(f"无法解析 arXiv RSS：{feed.get('bozo_exception') or '不是 RSS 内容'}")

    papers: dict[str, Paper] = {}
    for entry in feed.entries:
        paper = _parse_entry(entry)
        if paper is not None and paper.id not in papers:
            papers[paper.id] = paper
    return list(papers.values())


def _parse_entry(entry: Any) -> Paper | None:
    match = _ID_RE.search(entry.get("link", "")) or _ID_RE.search(entry.get("id", ""))
    if match is None:
        return None

    description = entry.get("summary", "")
    announce_type = entry.get("arxiv_announce_type")
    if parsed := _DESCRIPTION_RE.match(description):
        description = parsed["abstract"]
        announce_type = announce_type or parsed["type"]

    return Paper(
        id=match.group(1),
        title=_squash(entry.get("title", "")),
        abstract=_squash(description),
        authors=[name for name in (_squash(n) for n in entry.get("author", "").split(",")) if name],
        categories=[tag["term"] for tag in entry.get("tags", []) if tag.get("term")],
        announce_type=announce_type,
        announced=_to_datetime(entry.get("published_parsed")),
    )


def _squash(text: str) -> str:
    return " ".join(text.split())


def _to_datetime(value: struct_time | None) -> datetime | None:
    if value is None:
        return None
    # feedparser 已把时间换算为 UTC
    return datetime(*value[:6], tzinfo=UTC)
