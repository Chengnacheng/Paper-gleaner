from datetime import UTC, datetime

import httpx
import pytest
import respx

from gleaner.config import ArxivConfig
from gleaner.sources import ArxivSource, SourceError
from gleaner.sources.arxiv import parse_rss

FEED_URL = "https://rss.arxiv.org/rss/cs.CL+cs.LG"


def test_parse_rss_fields(rss_bytes: bytes) -> None:
    papers = parse_rss(rss_bytes)
    assert [p.id for p in papers] == [
        "2610.00001",
        "2610.00002",
        "2610.00003",
        "2509.12345",
        "2508.54321",
    ]

    first = papers[0]
    assert first.title == "LongKV: Hierarchical KV Cache Compression for Million-Token Inference"
    assert first.abstract == (
        "We propose a hierarchical KV cache compression scheme "
        "that keeps perplexity nearly unchanged."
    )
    assert first.authors == ["Alice Zhang", "Bob Li", "Carol Wang", "David Chen"]
    assert first.categories == ["cs.CL", "cs.LG"]
    assert first.announce_type == "new"
    assert first.announced == datetime(2026, 10, 22, 4, 0, tzinfo=UTC)
    assert first.url == "https://arxiv.org/abs/2610.00001"
    assert first.pdf_url == "https://arxiv.org/pdf/2610.00001"

    assert [p.announce_type for p in papers[2:]] == ["cross", "replace", "replace-cross"]


def test_parse_rss_deduplicates(rss_bytes: bytes) -> None:
    text = rss_bytes.decode()
    first_item = text[text.index("<item>") : text.index("</item>") + len("</item>")]
    doubled = text.replace(first_item, first_item * 2, 1).encode()
    assert len(parse_rss(doubled)) == len(parse_rss(rss_bytes))


def test_parse_empty_feed(empty_rss_bytes: bytes) -> None:
    assert parse_rss(empty_rss_bytes) == []


@pytest.mark.parametrize(
    "content",
    [
        b"<html><body>Service Unavailable",
        b"<html><body><p>Down for maintenance</p></body></html>",
    ],
)
def test_parse_non_feed_raises(content: bytes) -> None:
    with pytest.raises(SourceError):
        parse_rss(content)


@respx.mock
def test_fetch_latest_default_filters(rss_bytes: bytes) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=rss_bytes))
    source = ArxivSource(ArxivConfig(categories=["cs.CL", "cs.LG"]))

    papers = source.fetch_latest()

    # 默认包含 new 和 cross，不包含 replace
    assert [p.announce_type for p in papers] == ["new", "new", "cross"]


@respx.mock
def test_fetch_latest_with_replace_without_cross(rss_bytes: bytes) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=rss_bytes))
    config = ArxivConfig(
        categories=["cs.CL", "cs.LG"], include_cross_list=False, include_replace=True
    )

    papers = ArxivSource(config).fetch_latest()

    assert [p.announce_type for p in papers] == ["new", "new", "replace", "replace-cross"]


@respx.mock
def test_fetch_latest_http_error() -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(503))
    source = ArxivSource(ArxivConfig(categories=["cs.CL", "cs.LG"]))

    with pytest.raises(SourceError, match="503"):
        source.fetch_latest()


@respx.mock
def test_fetch_latest_sends_user_agent(rss_bytes: bytes) -> None:
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=rss_bytes))

    ArxivSource(ArxivConfig(categories=["cs.CL", "cs.LG"])).fetch_latest()

    assert route.calls.last.request.headers["User-Agent"].startswith("paper-gleaner/")
