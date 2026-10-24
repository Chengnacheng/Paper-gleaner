"""把日报渲染成 GitHub Issue 的 Markdown。

标题、摘要和 LLM 输出都是外部内容，渲染前统一做转义：
避免 @某人 触发通知、#123 关联到其它 Issue，以及 HTML 标签破坏版式。
"""

from __future__ import annotations

import html
import re
from datetime import date

from gleaner.models import Digest, RunStats, ScoredPaper

TITLE_PREFIX = "📚 Paper Gleaner"
_ZERO_WIDTH_SPACE = "​"


def issue_title_prefix(day: date) -> str:
    return f"{TITLE_PREFIX} · {day.isoformat()}"


def issue_title(digest: Digest) -> str:
    return f"{issue_title_prefix(digest.day)} · {len(digest.papers)} 篇"


def issue_body(digest: Digest, *, per_paper_comment: bool = True) -> str:
    parts: list[str] = []
    if digest.overview:
        parts.append("> " + sanitize(digest.overview).replace("\n", "\n> "))
    if digest.stats.degraded:
        parts.append("⚠️ 今天的 LLM 精排没有成功，以下按向量相似度排序，没有相关理由。")

    rows = ["| # | 论文 | 分数 | 匹配兴趣 |", "| --: | --- | :-: | --- |"]
    for index, paper in enumerate(digest.papers, start=1):
        score = "—" if paper.llm_score is None else str(paper.llm_score)
        interests = "、".join(paper.matched_interests) or paper.coarse_match or ""
        rows.append(f"| {index} | {_link(paper)} | {score} | {_cell(interests)} |")
    parts.append("\n".join(rows))

    if per_paper_comment:
        parts.append("每篇论文的详细信息见下方评论。")
    else:
        parts.extend(paper_section(i, p) for i, p in enumerate(digest.papers, start=1))

    parts.append(
        f"<details><summary>本次运行</summary>\n\n{stats_line(digest.stats)}\n\n</details>"
    )
    parts.append(f"<!-- gleaner:digest day={digest.day.isoformat()} -->")
    return "\n\n".join(parts) + "\n"


def paper_section(index: int, paper: ScoredPaper) -> str:
    """单篇论文的详细信息，作为一条评论发布（或在不分评论时写进正文）。"""
    authors = ", ".join(paper.authors[:3]) + (" 等" if len(paper.authors) > 3 else "")
    meta = [f"**作者**：{sanitize(authors)}"] if authors else []
    if paper.categories:
        meta.append(f"**分类**：{', '.join(paper.categories)}")
    if paper.llm_score is not None:
        meta.append(f"**分数**：{paper.llm_score}/10")

    lines = [
        f"### [{index}] {sanitize(paper.title)}",
        "",
        " · ".join(meta),
        f"[arXiv]({paper.url}) · [PDF]({paper.pdf_url})",
        "",
    ]
    if paper.reason:
        lines += [f"🎯 **为什么和你相关**：{sanitize(paper.reason)}", ""]
    elif paper.coarse_match:
        lines += [f"🎯 **最接近的兴趣方向**：{sanitize(paper.coarse_match)}", ""]
    if paper.tldr:
        lines += [f"📝 **TL;DR**：{sanitize(paper.tldr)}", ""]
    else:
        lines += [f"📄 **摘要**：{sanitize(_truncate(paper.abstract, 400))}", ""]
    if paper.tags:
        lines += ["🏷️ " + " ".join(f"`{tag.replace('`', '')}`" for tag in paper.tags), ""]
    lines.append(f"<!-- gleaner:paper id={paper.id} index={index} -->")
    return "\n".join(lines)


def stats_line(stats: RunStats) -> str:
    funnel = (
        f"抓取 {stats.fetched} 篇 · 重复 {stats.duplicates} · 黑名单 {stats.excluded} · "
        f"精排 {stats.candidates} · 推送 {stats.selected}"
    )
    tokens = stats.prompt_tokens + stats.completion_tokens
    minutes, seconds = divmod(round(stats.duration_seconds), 60)
    usage = (
        f"LLM 调用 {stats.llm_calls} 次 · token {_kilo(tokens)}"
        f"（输入 {_kilo(stats.prompt_tokens)} / 输出 {_kilo(stats.completion_tokens)}）"
        f" · 耗时 {minutes} 分 {seconds} 秒"
    )
    if stats.failed_batches:
        usage += f" · 精排失败 {stats.failed_batches} 批"
    return f"{funnel}<br>\n{usage}"


def sanitize(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"@(?=\w)", "@" + _ZERO_WIDTH_SPACE, text)
    return re.sub(r"#(?=\d)", "#" + _ZERO_WIDTH_SPACE, text)


def _cell(text: str) -> str:
    return " ".join(sanitize(text).split()).replace("|", "\\|")


def _link(paper: ScoredPaper) -> str:
    title = _cell(paper.title).replace("[", "\\[").replace("]", "\\]")
    return f"[{title}]({paper.url})"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _kilo(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)
