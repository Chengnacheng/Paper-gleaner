"""把日报发布成 GitHub Issue：正文是总览，每篇论文一条评论（方便逐篇点 👍/👎）。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from gleaner.config import GithubIssueNotifyConfig
from gleaner.models import Digest
from gleaner.render import issue_body, issue_title, issue_title_prefix, paper_section

LABELS = {
    "gleaner": ("1f6feb", "Paper Gleaner 自动生成"),
    "daily": ("0e8a16", "每日论文推送"),
}

logger = logging.getLogger(__name__)


class GitHubError(Exception):
    """调用 GitHub API 失败。"""


@dataclass
class IssueRef:
    number: int
    url: str


class GitHubClient:
    """GitHub REST API 的最小封装，只包含日报需要的几个接口。"""

    def __init__(
        self,
        token: str,
        repository: str,
        *,
        api_url: str = "https://api.github.com",
        client: httpx.Client | None = None,
    ) -> None:
        self.repository = repository
        self._client = client or httpx.Client(
            base_url=api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            transport=httpx.HTTPTransport(retries=3),
        )

    def ensure_label(self, name: str, color: str, description: str) -> None:
        response = self._client.post(
            f"/repos/{self.repository}/labels",
            json={"name": name, "color": color, "description": description},
        )
        if response.status_code == 422:  # 标签已存在
            return
        self._check(response, "创建标签")

    def list_issues(self, labels: list[str], *, state: str = "all") -> list[dict[str, Any]]:
        response = self._client.get(
            f"/repos/{self.repository}/issues",
            params={
                "labels": ",".join(labels),
                "state": state,
                "sort": "created",
                "direction": "desc",
                "per_page": 100,
            },
        )
        self._check(response, "查询 Issue")
        # 这个接口也会返回 Pull Request，需要排除
        return [item for item in response.json() if "pull_request" not in item]

    def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRef:
        response = self._client.post(
            f"/repos/{self.repository}/issues",
            json={"title": title, "body": body, "labels": labels},
        )
        data = self._check(response, "创建 Issue").json()
        return IssueRef(number=data["number"], url=data["html_url"])

    def create_comment(self, issue_number: int, body: str) -> int:
        response = self._client.post(
            f"/repos/{self.repository}/issues/{issue_number}/comments", json={"body": body}
        )
        return self._check(response, "发表评论").json()["id"]

    def close_issue(self, issue_number: int) -> None:
        response = self._client.patch(
            f"/repos/{self.repository}/issues/{issue_number}",
            json={"state": "closed", "state_reason": "completed"},
        )
        self._check(response, "关闭 Issue")

    @staticmethod
    def _check(response: httpx.Response, action: str) -> httpx.Response:
        if response.is_success:
            return response
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = response.text[:200]
        raise GitHubError(f"{action}失败（HTTP {response.status_code}）：{message}")


class IssuePublisher:
    def __init__(
        self,
        github: GitHubClient,
        config: GithubIssueNotifyConfig,
        *,
        pause_seconds: float = 1.0,
    ) -> None:
        self.github = github
        self.config = config
        # GitHub 建议连续创建内容时每次间隔约 1 秒，避免触发次级限流
        self.pause_seconds = pause_seconds

    def find_existing(self, day: date) -> IssueRef | None:
        prefix = issue_title_prefix(day)
        for issue in self.github.list_issues(list(LABELS)):
            if issue["title"].startswith(prefix):
                return IssueRef(number=issue["number"], url=issue["html_url"])
        return None

    def publish(self, digest: Digest) -> Digest:
        """创建当日 Issue 和每篇论文的评论，返回填好 Issue 信息的 digest。

        单条评论失败只记录日志，不中断发布（Issue 已经创建，重试会造成重复）。
        """
        for name, (color, description) in LABELS.items():
            self.github.ensure_label(name, color, description)

        body = issue_body(digest, per_paper_comment=self.config.per_paper_comment)
        issue = self.github.create_issue(issue_title(digest), body, list(LABELS))
        comment_ids: dict[str, int] = {}
        if self.config.per_paper_comment:
            for index, paper in enumerate(digest.papers, start=1):
                time.sleep(self.pause_seconds)
                try:
                    comment_ids[paper.id] = self.github.create_comment(
                        issue.number, paper_section(index, paper)
                    )
                except (GitHubError, httpx.HTTPError) as e:
                    logger.warning("第 %d 篇论文的评论发布失败：%s", index, e)
        return digest.model_copy(
            update={
                "issue_number": issue.number,
                "issue_url": issue.url,
                "comment_ids": comment_ids,
            }
        )

    def close_stale(self, now: datetime | None = None) -> list[int]:
        """关闭超过 close_after_days 天的旧日报，返回关闭的 Issue 编号。关闭后仍可评论。"""
        if self.config.close_after_days is None:
            return []
        cutoff = (now or datetime.now(UTC)) - timedelta(days=self.config.close_after_days)
        closed: list[int] = []
        try:
            for issue in self.github.list_issues(list(LABELS), state="open"):
                created = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
                if created < cutoff:
                    self.github.close_issue(issue["number"])
                    closed.append(issue["number"])
        except (GitHubError, httpx.HTTPError) as e:
            # 关闭旧日报只是整理，失败不影响当天的推送
            logger.warning("关闭旧日报失败：%s", e)
        return closed
