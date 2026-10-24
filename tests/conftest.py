from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rss_bytes() -> bytes:
    return (FIXTURES / "arxiv_rss.xml").read_bytes()


@pytest.fixture
def empty_rss_bytes() -> bytes:
    return (FIXTURES / "arxiv_rss_empty.xml").read_bytes()
