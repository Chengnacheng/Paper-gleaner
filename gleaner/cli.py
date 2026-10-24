"""命令行入口。GitHub Actions、MCP Server 和 Claude Code Skill 都通过它调用核心能力。"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated

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
from gleaner.models import Paper
from gleaner.pipelines.daily import run_daily
from gleaner.sources import SourceError

app = typer.Typer(
    help="Paper Gleaner：fork 即用、零服务器的论文追踪 Agent。",
    no_args_is_help=True,
    add_completion=False,
)

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


@app.command()
def daily(
    config_path: ConfigOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只抓取并打印，不发布")] = False,
    limit: Annotated[int, typer.Option(min=1, help="dry-run 时最多打印的篇数")] = 20,
) -> None:
    """运行每日推送流水线。"""
    if not dry_run:
        typer.echo("目前只支持 --dry-run：发布日报将在 Phase 1 实现。", err=True)
        raise typer.Exit(2)

    _, config = _load(config_path)
    try:
        result = run_daily(config)
    except SourceError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from e

    if not result.papers:
        typer.echo("今天没有新公告（arXiv 周末和节假日不更新）。")
        return

    typer.echo(_summary(result.papers))
    for index, paper in enumerate(result.papers[:limit], start=1):
        typer.echo(_format_paper(index, paper))
    if (hidden := len(result.papers) - limit) > 0:
        typer.echo(f"… 还有 {hidden} 篇未显示，可用 --limit 调整")


def _load(config_path: Path | None) -> tuple[Path, Config]:
    try:
        path = resolve_config_path(config_path)
        if path == EXAMPLE_CONFIG_PATH and config_path is None:
            typer.echo(f"! 未找到 config.yaml，暂用 {EXAMPLE_CONFIG_PATH}", err=True)
        return path, load_config(path)
    except ConfigError as e:
        typer.echo(f"✗ 配置错误：{e}", err=True)
        raise typer.Exit(1) from e


def _summary(papers: list[Paper]) -> str:
    counts = Counter(paper.announce_type or "unknown" for paper in papers)
    detail = " · ".join(f"{kind} {count}" for kind, count in counts.most_common())
    return f"从 arXiv 抓取到 {len(papers)} 篇（{detail}）\n"


def _format_paper(index: int, paper: Paper) -> str:
    authors = ", ".join(paper.authors[:3]) + (" 等" if len(paper.authors) > 3 else "")
    meta = " · ".join(part for part in (authors, ", ".join(paper.categories)) if part)
    return f"[{index}] {paper.id}  {paper.title}\n    {meta}"
