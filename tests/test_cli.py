from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gleaner import __version__
from gleaner.cli import app

runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个干净的工作目录，只含指定分类的 config.yaml。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    (tmp_path / "config.yaml").write_text("sources:\n  arxiv:\n    categories: [cs.CL, cs.LG]\n")
    return tmp_path


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_daily_requires_dry_run(workdir: Path) -> None:
    result = runner.invoke(app, ["daily"])
    assert result.exit_code == 2
    assert "--dry-run" in result.output


@respx.mock
def test_daily_dry_run_prints_papers(workdir: Path, rss_bytes: bytes) -> None:
    respx.get("https://rss.arxiv.org/rss/cs.CL+cs.LG").mock(
        return_value=httpx.Response(200, content=rss_bytes)
    )

    result = runner.invoke(app, ["daily", "--dry-run", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "抓取到 3 篇（new 2 · cross 1）" in result.output
    assert "[1] 2610.00001  LongKV" in result.output
    assert "Alice Zhang, Bob Li, Carol Wang 等 · cs.CL, cs.LG" in result.output
    assert "[3]" not in result.output
    assert "还有 1 篇未显示" in result.output


@respx.mock
def test_daily_dry_run_without_announcements(workdir: Path, empty_rss_bytes: bytes) -> None:
    respx.get("https://rss.arxiv.org/rss/cs.CL+cs.LG").mock(
        return_value=httpx.Response(200, content=empty_rss_bytes)
    )

    result = runner.invoke(app, ["daily", "--dry-run"])

    assert result.exit_code == 0
    assert "今天没有新公告" in result.output


@respx.mock
def test_daily_dry_run_network_error(workdir: Path) -> None:
    respx.get("https://rss.arxiv.org/rss/cs.CL+cs.LG").mock(return_value=httpx.Response(403))

    result = runner.invoke(app, ["daily", "--dry-run"])

    assert result.exit_code == 1
    assert "抓取 arXiv RSS 失败" in result.output


def test_config_error_is_reported(workdir: Path) -> None:
    (workdir / "config.yaml").write_text("ranking:\n  top_n: 5\n")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "ranking.top_n" in result.output


def test_check_reports_missing_items(workdir: Path) -> None:
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "✗ 兴趣描述" in result.output
    assert "✗ LLM_API_KEY" in result.output


def test_check_passes_when_complete(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (workdir / "profile").mkdir()
    (workdir / "profile" / "interests.md").write_text("长上下文推理")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")

    result = runner.invoke(app, ["check"])

    assert result.exit_code == 0, result.output
    assert "✓ 兴趣描述" in result.output
    assert "✓ LLM_API_KEY" in result.output
    assert "cs.CL, cs.LG" in result.output


def test_falls_back_to_example_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text("")

    result = runner.invoke(app, ["check"])

    assert "暂用 config.example.yaml" in result.output
