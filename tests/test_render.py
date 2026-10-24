from datetime import UTC, date, datetime

from gleaner.models import Digest, RunStats, ScoredPaper
from gleaner.render import issue_body, issue_title, paper_section, sanitize, stats_line


def scored(n: int, **fields) -> ScoredPaper:
    values = {
        "id": f"2610.0000{n}",
        "title": f"Paper {n}",
        "abstract": "We study things.",
        "authors": ["Alice", "Bob"],
        "categories": ["cs.CL"],
        "coarse_score": 0.8,
        "coarse_match": "长上下文推理",
        "llm_score": 8,
        "matched_interests": ["长上下文推理"],
        "reason": "压缩 KV cache，与你的长上下文推理方向直接相关。",
        "tldr": "显存降低 4 倍。",
        "tags": ["kv-cache", "efficiency"],
    } | fields
    return ScoredPaper(**values)


def make_digest(**fields) -> Digest:
    values = {
        "day": date(2026, 10, 26),
        "generated_at": datetime(2026, 10, 26, 1, 30, tzinfo=UTC),
        "overview": "今天以长上下文为主。\n建议先读 [1]。",
        "papers": [scored(1), scored(2, llm_score=None, matched_interests=[])],
        "stats": RunStats(fetched=600, candidates=50, selected=2, llm_calls=7),
    } | fields
    return Digest(**values)


def test_issue_title() -> None:
    assert issue_title(make_digest()) == "📚 Paper Gleaner · 2026-10-26 · 2 篇"


def test_issue_body() -> None:
    body = issue_body(make_digest())

    assert body.startswith("> 今天以长上下文为主。\n> 建议先读 [1]。")
    assert "| 1 | [Paper 1](https://arxiv.org/abs/2610.00001) | 8 | 长上下文推理 |" in body
    # 没有 LLM 分数时显示 —，匹配兴趣退回到粗筛最接近的方向
    assert "| 2 | [Paper 2](https://arxiv.org/abs/2610.00002) | — | 长上下文推理 |" in body
    assert "每篇论文的详细信息见下方评论" in body
    assert "### [1]" not in body
    assert "<details><summary>本次运行</summary>" in body
    assert "抓取 600 篇" in body
    assert "<!-- gleaner:digest day=2026-10-26 -->" in body
    assert "⚠️" not in body


def test_issue_body_degraded_and_inline_papers() -> None:
    digest = make_digest(overview=None, stats=RunStats(degraded=True))
    body = issue_body(digest, per_paper_comment=False)

    assert not body.startswith(">")
    assert "⚠️ 今天的 LLM 精排没有成功" in body
    assert "### [1] Paper 1" in body
    assert "### [2] Paper 2" in body
    assert "下方评论" not in body


def test_paper_section() -> None:
    paper = scored(3, authors=["A", "B", "C", "D"])
    text = paper_section(3, paper)

    assert text.startswith("### [3] Paper 3\n")
    assert "**作者**：A, B, C 等 · **分类**：cs.CL · **分数**：8/10" in text
    assert (
        "[arXiv](https://arxiv.org/abs/2610.00003) · [PDF](https://arxiv.org/pdf/2610.00003)"
        in text
    )
    assert "🎯 **为什么和你相关**：压缩 KV cache" in text
    assert "📝 **TL;DR**：显存降低 4 倍。" in text
    assert "🏷️ `kv-cache` `efficiency`" in text
    assert text.endswith("<!-- gleaner:paper id=2610.00003 index=3 -->")


def test_paper_section_without_llm_output() -> None:
    paper = scored(4, llm_score=None, reason="", tldr="", tags=[], abstract="x" * 500)
    text = paper_section(1, paper)

    assert "**分数**" not in text
    assert "🎯 **最接近的兴趣方向**：长上下文推理" in text
    assert "📄 **摘要**：" + "x" * 400 + "…" in text
    assert "🏷️" not in text


def test_external_text_is_sanitized() -> None:
    assert sanitize("ping @octocat about #12") == "ping @​octocat about #​12"
    assert sanitize("<img src=x>") == "&lt;img src=x&gt;"
    assert sanitize("C# and #rust") == "C# and #rust"

    paper = scored(5, title="A | B [draft] <b>", reason="cc @someone")
    body = issue_body(make_digest(papers=[paper]))
    assert "[A \\| B \\[draft\\] &lt;b&gt;](https://arxiv.org/abs/2610.00005)" in body
    assert "@​someone" in paper_section(1, paper)


def test_stats_line() -> None:
    stats = RunStats(
        fetched=612,
        duplicates=3,
        excluded=1,
        candidates=50,
        selected=10,
        llm_calls=7,
        prompt_tokens=31_200,
        completion_tokens=850,
        duration_seconds=133,
        failed_batches=1,
    )
    line = stats_line(stats)
    assert "抓取 612 篇 · 重复 3 · 黑名单 1 · 精排 50 · 推送 10" in line
    assert "token 32.0k（输入 31.2k / 输出 850）" in line
    assert "耗时 2 分 13 秒" in line
    assert "精排失败 1 批" in line
