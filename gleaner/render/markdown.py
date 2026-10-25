"""把日报渲染成 GitHub Issue 的 Markdown。

标题、摘要和 LLM 输出都是外部内容，渲染前统一做转义：
避免 @某人 触发通知、#123 关联到其它 Issue，以及 HTML 标签破坏版式。
"""

from __future__ import annotations

import html
import re
from datetime import date

from gleaner.models import Digest, FeedbackStats, Paper, RunStats, ScoredPaper

TITLE_PREFIX = "📚 Paper Gleaner"
_ZERO_WIDTH_SPACE = "​"
# GitHub 评论正文最多 65536 个字符；拆分时留出余量给「续」的提示
COMMENT_LIMIT = 65_536
SPLIT_LIMIT = 60_000


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
        rows.append(f"| {index} | {paper_link(paper)} | {score} | {_cell(interests)} |")
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
        f"LLM 调用 {stats.llm_calls} 次 · token {kilo(tokens)}"
        f"（输入 {kilo(stats.prompt_tokens)} / 输出 {kilo(stats.completion_tokens)}）"
        f" · 耗时 {minutes} 分 {seconds} 秒"
    )
    if stats.failed_batches:
        usage += f" · 精排失败 {stats.failed_batches} 批"
    lines = [funnel, usage]
    if not stats.feedback.empty:
        lines += feedback_lines(stats.feedback)
    return "<br>\n".join(lines)


def feedback_lines(feedback: FeedbackStats) -> list[str]:
    """反馈概况：各类反馈的论文数、参与粗筛的锚点数，以及表情同步的情况。"""
    signals = (
        f"反馈：👍 {feedback.up} · 👎 {feedback.down} · /like {feedback.like} · "
        f"/dislike {feedback.dislike} · /deep、/compare {feedback.implicit}"
    )
    anchors = (
        f"粗筛锚点：正反馈 {feedback.positive} 篇 · 负反馈 {feedback.negative} 篇 · "
        f"屏蔽主题 {feedback.muted} 个"
    )
    if feedback.synced:
        anchors += f" · 本次同步表情 {feedback.synced} 条"
    lines = [signals, anchors]
    if feedback.sync_error:
        lines.append(f"⚠️ 表情同步没有完成：{sanitize(_truncate(feedback.sync_error, 200))}")
    return lines


def sanitize(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"@(?=\w)", "@" + _ZERO_WIDTH_SPACE, text)
    return re.sub(r"#(?=\d)", "#" + _ZERO_WIDTH_SPACE, text)


def sanitize_markdown(text: str) -> str:
    """转义模型生成的 Markdown：与 sanitize 相同，但保留 ">"，引用块才能正常显示。

    只要 "<" 被转义，就不会出现 HTML 标签或伪造的 gleaner 标记注释。
    """
    return sanitize(text).replace("&gt;", ">")


def split_comment(body: str, limit: int = SPLIT_LIMIT) -> list[str]:
    """把超长的回复拆成多条评论，每条不超过 limit 个字符。

    尽量在空行处断开；必须在代码块或 <details> 折叠块中间断开时，先把它们闭合，
    下一条评论开头再重新打开。
    """
    if len(body) <= limit:
        return [body]
    budget = limit - 200  # 留给闭合标记和「续」的提示
    chunks: list[list[str]] = []
    current: list[str] = []
    breaks: list[int] = []  # current 中可以安全断开的位置（空行之后、不在代码块或折叠块内）
    size = 0
    fence: str | None = None  # 所在代码块的开头一行，如 "```python"
    depth = 0  # 未闭合的 <details> 层数

    for line in _wrap_lines(body.split("\n"), budget // 2):
        while size + len(line) + 1 > budget and current:
            if breaks:
                cut = breaks[-1]
                chunks.append(current[:cut])
                current = current[cut:]
            else:
                closers = (["```"] if fence else []) + ["</details>"] * depth
                chunks.append(current + closers)
                current = ["<details><summary>（续）</summary>", ""] * depth
                current += [fence] if fence else []
            size = sum(len(item) + 1 for item in current)
            breaks = []
        current.append(line)
        size += len(line) + 1
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = None if fence else stripped
        elif fence is None:
            depth = max(depth + stripped.count("<details") - stripped.count("</details>"), 0)
        if fence is None and depth == 0 and not stripped:
            breaks.append(len(current))
    chunks.append(current)

    parts = ["\n".join(chunk).strip("\n") for chunk in chunks]
    parts = [part for part in parts if part.strip()]
    total = len(parts)
    for index in range(total):
        if index > 0:
            parts[index] = f"<sub>（续 {index + 1}/{total}）</sub>\n\n{parts[index]}"
        if index < total - 1:
            parts[index] += "\n\n<sub>（未完，见下一条评论）</sub>"
    return parts


def _wrap_lines(lines: list[str], width: int) -> list[str]:
    """把超长的单行（如巨大的表格行）硬切成几段，保证每段都能放进一条评论。"""
    wrapped: list[str] = []
    for line in lines:
        while len(line) > width:
            wrapped.append(line[:width])
            line = line[width:]
        wrapped.append(line)
    return wrapped


def paper_link(paper: Paper) -> str:
    """标题链接到 arXiv 摘要页；标题经过转义，可以放进表格单元格。"""
    title = _cell(paper.title).replace("[", "\\[").replace("]", "\\]")
    return f"[{title}]({paper.url})"


def _cell(text: str) -> str:
    return " ".join(sanitize(text).split()).replace("|", "\\|")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def kilo(value: int) -> str:
    """把 token 数写成 12.3k 这样的形式。"""
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)
