import json
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from gleaner.config import GithubIssueNotifyConfig
from gleaner.models import Digest, ScoredPaper
from gleaner.notify import GitHubClient, GitHubError, IssuePublisher, IssueRef

API = "https://api.github.com/repos/o/r"


def make_publisher(**config) -> IssuePublisher:
    github = GitHubClient("ghs_token", "o/r")
    return IssuePublisher(github, GithubIssueNotifyConfig(**config), pause_seconds=0)


def make_digest(count: int = 2) -> Digest:
    papers = [
        ScoredPaper(id=f"2610.0000{i}", title=f"Paper {i}", abstract="A.", llm_score=8)
        for i in range(1, count + 1)
    ]
    return Digest(day=date(2026, 10, 26), generated_at=datetime.now(UTC), papers=papers)


def issue(number: int, title: str, created_at: str = "2026-10-26T01:30:00Z", **extra) -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/o/r/issues/{number}",
        "created_at": created_at,
        **extra,
    }


@respx.mock
def test_ensure_label_tolerates_existing() -> None:
    route = respx.post(f"{API}/labels").mock(
        side_effect=[httpx.Response(201, json={}), httpx.Response(422, json={})]
    )
    github = GitHubClient("ghs_token", "o/r")

    github.ensure_label("gleaner", "1f6feb", "desc")
    github.ensure_label("gleaner", "1f6feb", "desc")

    assert route.call_count == 2
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer ghs_token"
    assert json.loads(request.content) == {
        "name": "gleaner",
        "color": "1f6feb",
        "description": "desc",
    }


@respx.mock
def test_errors_include_status_and_message() -> None:
    respx.post(f"{API}/labels").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible"})
    )
    with pytest.raises(GitHubError, match=r"HTTP 403.*Resource not accessible"):
        GitHubClient("t", "o/r").ensure_label("x", "000000", "")


@respx.mock
def test_find_existing() -> None:
    route = respx.get(f"{API}/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                issue(9, "📚 Paper Gleaner · 2026-10-26 · 3 篇", pull_request={}),
                issue(8, "📚 Paper Gleaner · 2026-10-26 · 10 篇"),
                issue(7, "📚 Paper Gleaner · 2026-10-23 · 10 篇"),
            ],
        )
    )
    publisher = make_publisher()

    assert publisher.find_existing(date(2026, 10, 26)) == IssueRef(
        8, "https://github.com/o/r/issues/8"
    )
    assert publisher.find_existing(date(2026, 10, 27)) is None
    params = route.calls[0].request.url.params
    assert params["labels"] == "gleaner,daily"
    assert params["state"] == "all"


@respx.mock
def test_publish_creates_issue_and_comments() -> None:
    respx.post(f"{API}/labels").mock(return_value=httpx.Response(201, json={}))
    create = respx.post(f"{API}/issues").mock(return_value=httpx.Response(201, json=issue(5, "t")))
    comments = respx.post(f"{API}/issues/5/comments").mock(
        side_effect=[httpx.Response(201, json={"id": 101}), httpx.Response(201, json={"id": 102})]
    )

    digest = make_publisher().publish(make_digest())

    payload = json.loads(create.calls[0].request.content)
    assert payload["title"] == "📚 Paper Gleaner · 2026-10-26 · 2 篇"
    assert payload["labels"] == ["gleaner", "daily"]
    assert "| 1 | [Paper 1]" in payload["body"]
    bodies = [json.loads(c.request.content)["body"] for c in comments.calls]
    assert bodies[0].startswith("### [1] Paper 1")
    assert bodies[1].startswith("### [2] Paper 2")
    assert digest.issue_number == 5
    assert digest.issue_url == "https://github.com/o/r/issues/5"
    assert digest.comment_ids == {"2610.00001": 101, "2610.00002": 102}


@respx.mock
def test_publish_continues_after_comment_failure() -> None:
    respx.post(f"{API}/labels").mock(return_value=httpx.Response(201, json={}))
    respx.post(f"{API}/issues").mock(return_value=httpx.Response(201, json=issue(5, "t")))
    respx.post(f"{API}/issues/5/comments").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(201, json={"id": 102}),
        ]
    )

    digest = make_publisher().publish(make_digest())

    assert digest.comment_ids == {"2610.00002": 102}


@respx.mock
def test_publish_inline_without_comments() -> None:
    respx.post(f"{API}/labels").mock(return_value=httpx.Response(201, json={}))
    create = respx.post(f"{API}/issues").mock(return_value=httpx.Response(201, json=issue(5, "t")))
    comments = respx.post(f"{API}/issues/5/comments")

    make_publisher(per_paper_comment=False).publish(make_digest())

    assert "### [1] Paper 1" in json.loads(create.calls[0].request.content)["body"]
    assert comments.call_count == 0


@respx.mock
def test_close_stale() -> None:
    respx.get(f"{API}/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                issue(3, "new", created_at="2026-10-25T01:30:00Z"),
                issue(2, "old", created_at="2026-10-16T01:30:00Z"),
            ],
        )
    )
    close = respx.patch(f"{API}/issues/2").mock(return_value=httpx.Response(200, json={}))

    closed = make_publisher(close_after_days=7).close_stale(datetime(2026, 10, 26, tzinfo=UTC))

    assert closed == [2]
    assert json.loads(close.calls[0].request.content) == {
        "state": "closed",
        "state_reason": "completed",
    }


@respx.mock
def test_close_stale_failure_is_not_fatal() -> None:
    respx.get(f"{API}/issues").mock(return_value=httpx.Response(500, json={}))
    assert make_publisher().close_stale() == []


def test_close_stale_disabled() -> None:
    # 没有 mock 任何请求：如果发出请求会因为网络不可用而失败
    assert make_publisher(close_after_days=None).close_stale() == []
