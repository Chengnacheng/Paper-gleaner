"""命令行入口。GitHub Actions、MCP Server 和 Claude Code Skill 都通过它调用核心能力。"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
import typer

from gleaner import __version__
from gleaner.config import (
    EXAMPLE_CONFIG_PATH,
    Config,
    ConfigError,
    load_config,
    resolve_config_path,
)
from gleaner.llm import LLMClient, LLMError
from gleaner.models import ScoredPaper
from gleaner.notify import GitHubClient, GitHubError, IssuePublisher
from gleaner.pipelines.daily import DailyResult, run_daily
from gleaner.profile import ProfileError
from gleaner.ranking import EmbeddingError, build_embedder
from gleaner.render import stats_line
from gleaner.sources import SourceError
from gleaner.store import DataBranch, DataBranchError

app = typer.Typer(
    help="Paper Gleaner：fork 即用、零服务器的论文追踪 Agent。",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_DATA_DIR = Path(".gleaner-data")

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config", "-c", help="配置文件路径，默认依次查找 config.yaml、config.example.yaml"
    ),
]


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="输出调试日志")] = False,
) -> None:
    _configure_stdio()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _configure_stdio() -> None:
    """Use UTF-8 on Windows consoles while preserving redirected streams."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(OSError, ValueError):
                stream.reconfigure(encoding="utf-8")


@app.command()
def version() -> None:
    """显示版本号。"""
    typer.echo(__version__)


@app.command()
def check(
    config_path: ConfigOption = None,
    ping_llm: Annotated[
        bool, typer.Option("--ping-llm", help="向两档模型各发一次极短请求，验证 API Key 和模型名")
    ] = False,
) -> None:
    """检查配置是否完整、可用。"""
    path, config = _load(config_path)
    problems = 0

    typer.echo(f"✓ 配置文件：{path}")

    interests = config.profile.interests_file
    if interests.is_file():
        typer.echo(f"✓ 兴趣描述：{interests}")
    else:
        typer.echo(
            f"✗ 兴趣描述：找不到 {interests}，请从 profile/interests.example.md 复制一份并改写"
        )
        problems += 1

    if config.llm.api_key is not None:
        typer.echo("✓ LLM_API_KEY：已设置")
    else:
        typer.echo(
            "✗ LLM_API_KEY：未设置，请在仓库 Settings → Secrets and variables → Actions 中添加"
        )
        problems += 1

    models = config.llm.models
    typer.echo(f"· LLM：{config.llm.base_url}（fast={models.fast}，deep={models.deep}）")
    typer.echo(f"· arXiv 分类：{', '.join(config.sources.arxiv.categories)}")

    if ping_llm and config.llm.api_key is not None:
        client = LLMClient(config.llm)
        for tier in ("fast", "deep"):
            model = client.model_for(tier)
            try:
                client.chat([{"role": "user", "content": "请只回复 OK"}], tier=tier)
            except LLMError as e:
                typer.echo(f"✗ LLM {tier}（{model}）：{e}")
                problems += 1
            else:
                typer.echo(f"✓ LLM {tier}（{model}）：可用")

    if problems:
        raise typer.Exit(1)


# 日报运行中可预期的失败：打印原因后以退出码 1 结束，不打印调用栈
_DAILY_ERRORS = (
    ConfigError,
    ProfileError,
    LLMError,
    EmbeddingError,
    SourceError,
    GitHubError,
    DataBranchError,
    httpx.HTTPError,
)


@app.command()
def daily(
    config_path: ConfigOption = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="完整运行，但不发布 Issue、不提交数据分支")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="当天日报已存在时仍重新生成")] = False,
    data_dir: Annotated[
        Path, typer.Option("--data-dir", help="gleaner-data 分支的本地 worktree 目录")
    ] = DEFAULT_DATA_DIR,
) -> None:
    """运行每日推送流水线：抓取、筛选、精排，并发布到 GitHub Issue。"""
    _, config = _load(config_path)
    today = datetime.now(ZoneInfo(config.timezone)).date()
    branch = DataBranch(Path.cwd(), data_dir)
    try:
        publisher = None if dry_run else _build_publisher(config)
        llm = LLMClient(config.llm)
        result = run_daily(
            config,
            store=branch.checkout(),
            llm=llm,
            embedder=build_embedder(config),
            today=today,
            publisher=publisher,
            force=force,
        )
        if not dry_run and result.status != "already_published":
            branch.commit_and_push(f"data: daily digest {today.isoformat()}")
    except _DAILY_ERRORS as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from e

    missing = _missing_comments(result, config)
    report = _report(result, missing)
    typer.echo(report)
    if missing and os.environ.get("GITHUB_ACTIONS") == "true":
        # 在 Actions 运行页面显示为黄色警告
        typer.echo(f"::warning::{missing} 篇论文的评论没有发出去，{_REPAIR_HINT}")
    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"## Paper Gleaner · {today.isoformat()}\n\n{report}\n")


def _load(config_path: Path | None) -> tuple[Path, Config]:
    try:
        path = resolve_config_path(config_path)
        if path == EXAMPLE_CONFIG_PATH and config_path is None:
            typer.echo(f"! 未找到 config.yaml，暂用 {EXAMPLE_CONFIG_PATH}", err=True)
        return path, load_config(path)
    except ConfigError as e:
        typer.echo(f"✗ 配置错误：{e}", err=True)
        raise typer.Exit(1) from e


def _build_publisher(config: Config) -> IssuePublisher | None:
    if not config.notify.github_issue.enabled:
        return None
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repository:
        raise ConfigError(
            "发布 Issue 需要 GITHUB_TOKEN 和 GITHUB_REPOSITORY 环境变量"
            "（GitHub Actions 中自动提供）；本地试运行请加 --dry-run"
        )
    api_url = os.environ.get("GITHUB_API_URL", "").strip() or "https://api.github.com"
    github = GitHubClient(token, repository, api_url=api_url)
    return IssuePublisher(github, config.notify.github_issue)


_REPAIR_HINT = "不加 --force 重新运行（或等 11:30 的兜底运行）会自动补发"


def _missing_comments(result: DailyResult, config: Config) -> int:
    """已发布但评论没发出去的论文数。"""
    digest = result.digest
    if digest is None or digest.issue_number is None:
        return 0
    if not config.notify.github_issue.per_paper_comment:
        return 0
    return sum(1 for paper in digest.papers if paper.id not in digest.comment_ids)


def _report(result: DailyResult, missing: int = 0) -> str:
    warning = f"! 还有 {missing} 篇论文的评论没有发出去，{_REPAIR_HINT}。" if missing else ""
    url = result.existing.url if result.existing else ""
    if result.status == "already_published":
        lines = [f"今天的日报已经发布：{url}", warning, "如需重新生成，请加 --force。"]
        return "\n".join(line for line in lines if line)
    if result.status == "repaired":
        return "\n".join(line for line in [f"✓ 已补发缺失的论文评论：{url}", warning] if line)
    if result.status == "no_announcements":
        return "今天没有新公告（arXiv 周末和节假日不更新）。"

    digest = result.digest
    assert digest is not None
    lines = []
    if result.status == "published":
        lines.append(f"✓ 已发布：{digest.issue_url}")
        if warning:
            lines.append(warning)
    elif result.status == "nothing_relevant":
        lines.append("今天没有达到 min_score 的论文，未发布日报。")
    if digest.stats.degraded:
        lines.append("! LLM 精排没有成功，以下按向量相似度排序。")
    if digest.overview:
        lines.append(f"\n{digest.overview}")
    for index, paper in enumerate(digest.papers, start=1):
        lines.append("\n" + _format_paper(index, paper))
    lines.append("\n" + stats_line(digest.stats).replace("<br>", ""))
    return "\n".join(lines).lstrip("\n")


def _format_paper(index: int, paper: ScoredPaper) -> str:
    score = "—" if paper.llm_score is None else f"{paper.llm_score}/10"
    lines = [f"[{index}] {score}  {paper.id}  {paper.title}"]
    if paper.reason:
        lines.append(f"    🎯 {paper.reason}")
    if paper.tldr:
        lines.append(f"    📝 {paper.tldr}")
    return "\n".join(lines)
