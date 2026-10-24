"""论文来源的统一接口，方便以后接入 OpenReview、ACL Anthology 等。"""

from __future__ import annotations

from typing import Protocol

from gleaner.models import Paper


class SourceError(Exception):
    """从论文来源获取数据失败。"""


class Source(Protocol):
    name: str

    def fetch_latest(self) -> list[Paper]:
        """返回最近一个公告周期的新论文。没有新公告时返回空列表。"""
        ...
