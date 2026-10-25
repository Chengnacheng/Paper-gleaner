import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from typer.testing import CliRunner

from gleaner import __version__, cli
from gleaner.cli import _report, app
from gleaner.llm import LLMClient
from gleaner.models import Digest, ScoredPaper
from gleaner.notify import IssueRef
from gleaner.pipelines.daily import DailyResult, digest_path
from gleaner.store import DataBranch
from tests.conftest import (
    DEEP_REPORT,
    DIGEST_DAY,
    FIXTURES,
    PIPELINE_VOCABULARY,
    FakeOpenAI,
    KeywordEmbedder,
    git,
    interact_digest,
    interact_llm,
    pipeline_llm,
)

runner = CliRunner()
RSS_URL = "https://rss.arxiv.org/rss/cs.CL+cs.LG"
API = "https://api.github.com/repos/o/r"
SCORES = {"2610.00001": 9, "2610.00002": 7, "2610.00003": 4}
ENV_VARS = [
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_PROVIDER",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GITHUB_TOKEN",
    "GITHUB_REPOSITORY",
    "GITHUB_REPOSITORY_OWNER",
    "GITHUB_API_URL",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_EVENT_PATH",
    "S2_API_KEY",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Path:
    """一个干净的工作目录，只含指定分类的 config.yaml。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("sources:\n  arxiv:\n    categories: [cs.CL, cs.LG]\n")
    return tmp_path


@pytest.fixture
def repo(clone: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Path:
    """以一个 git 仓库为工作目录，配置齐全，LLM 和嵌入模型换成假的。"""
    path = clone("repo")
    monkeypatch.chdir(path)
    (path / "config.yaml").write_text("sources:\n  arxiv:\n    categories: [cs.CL, cs.LG]\n")
    (path / "profile").mkdir()
    (path / "profile" / "interests.md").write_text("我研究长上下文推理。", encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setattr(
        cli, "LLMClient", lambda config: LLMClient(config, client=FakeOpenAI(pipeline_llm(SCORES)))
    )
    monkeypatch.setattr(cli, "build_embedder", lambda config: KeywordEmbedder(PIPELINE_VOCABULARY))
    monkeypatch.setattr("gleaner.notify.github_issue.time.sleep", lambda seconds: None)
    return path


def github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")


def data_branch_exists(remote: Path) -> bool:
    return bool(git("ls-remote", "--heads", str(remote), "gleaner-data", cwd=remote))


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


@respx.mock
def test_daily_dry_run(repo: Path, rss_bytes: bytes, git_remote: Path) -> None:
    respx.get(RSS_URL).mock(return_value=httpx.Response(200, content=rss_bytes))

    result = runner.invoke(app, ["daily", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "今天以长上下文推理为主" in result.output
    assert "[1] 9/10  2610.00001  LongKV" in result.output
    assert "🎯 2610.00001 与长上下文推理相关" in result.output
    assert "[2] 7/10  2610.00002" in result.output
    assert "2610.00003" not in result.output  # 低于 min_score
    assert "抓取 3 篇" in result.output
    assert not data_branch_exists(git_remote)  # dry-run 不提交数据分支


@respx.mock
def test_daily_publishes_issue_and_data(
    repo: Path,
    rss_bytes: bytes,
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_env(monkeypatch)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    respx.get(RSS_URL).mock(return_value=httpx.Response(200, content=rss_bytes))
    respx.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/labels").mock(return_value=httpx.Response(201, json={}))
    respx.post(f"{API}/issues").mock(
        return_value=httpx.Response(
            201, json={"number": 5, "html_url": "https://github.com/o/r/issues/5"}
        )
    )
    comments = respx.post(f"{API}/issues/5/comments").mock(
        return_value=httpx.Response(201, json={"id": 100})
    )

    result = runner.invoke(app, ["daily"])

    assert result.exit_code == 0, result.output
    assert "✓ 已发布：https://github.com/o/r/issues/5" in result.output
    assert comments.call_count == 2
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    saved = git("show", f"gleaner-data:history/digests/{today}.json", cwd=git_remote)
    assert '"issue_number": 5' in saved
    assert f"## Paper Gleaner · {today}" in summary.read_text(encoding="utf-8")


@respx.mock
def test_daily_skips_when_already_published(
    repo: Path, git_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_env(monkeypatch)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    existing = {
        "number": 4,
        "title": f"📚 Paper Gleaner · {today} · 10 篇",
        "html_url": "https://github.com/o/r/issues/4",
        "created_at": "2026-10-26T01:30:00Z",
    }
    respx.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[existing]))
    rss = respx.get(RSS_URL)

    result = runner.invoke(app, ["daily"])

    assert result.exit_code == 0, result.output
    assert "今天的日报已经发布：https://github.com/o/r/issues/4" in result.output
    assert rss.call_count == 0
    assert not data_branch_exists(git_remote)


def seed_data(clone: Callable[[str], Path], *digests: Digest) -> None:
    """在远端的 gleaner-data 分支上放几份日报。"""
    seed_repo = clone("data-seed")
    seed = DataBranch(seed_repo, seed_repo / ".gleaner-data", backoff_seconds=0)
    store = seed.checkout()
    for digest in digests:
        store.write_model(digest_path(digest.day), digest)
    seed.commit_and_push("data: seed digests")


def todays_digest(**fields) -> Digest:
    """今天（按配置的时区）已发布的日报，评论都在。"""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    comment_ids = {"2610.00001": 801, "2610.00002": 802}
    return interact_digest(**({"day": today, "comment_ids": comment_ids} | fields))


def reactions_json(*items: tuple[str, str]) -> list[dict]:
    return [
        {"id": i, "user": {"login": login, "type": "User"}, "content": content}
        for i, (login, content) in enumerate(items, start=1)
    ]


@respx.mock
def test_daily_dry_run_syncs_reactions_without_committing(
    repo: Path,
    clone: Callable[[str], Path],
    rss_bytes: bytes,
    git_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_data(clone, todays_digest())
    github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "Owner")
    before = git("rev-parse", "gleaner-data", cwd=git_remote)
    respx.get(RSS_URL).mock(return_value=httpx.Response(200, content=rss_bytes))
    first = respx.get(f"{API}/issues/comments/801/reactions").mock(
        return_value=httpx.Response(200, json=reactions_json(("owner", "+1"), ("bob", "-1")))
    )
    respx.get(f"{API}/issues/comments/802/reactions").mock(
        return_value=httpx.Response(200, json=reactions_json(("bob", "+1")))
    )

    result = runner.invoke(app, ["daily", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert first.called
    assert "· 同步了 1 条表情反馈" in result.output
    assert "[1] 9/10  2610.00001" in result.output
    assert git("rev-parse", "gleaner-data", cwd=git_remote) == before  # dry-run 不写数据


@respx.mock
def test_daily_dry_run_warns_without_github_env(repo: Path, rss_bytes: bytes) -> None:
    respx.get(RSS_URL).mock(return_value=httpx.Response(200, content=rss_bytes))

    result = runner.invoke(app, ["daily", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("! 没有 GITHUB_TOKEN 和 GITHUB_REPOSITORY，跳过表情同步")


@respx.mock
def test_daily_commits_synced_reactions_when_already_published(
    repo: Path,
    clone: Callable[[str], Path],
    git_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = todays_digest(issue_number=4)
    seed_data(clone, digest)
    github_env(monkeypatch)  # 没有 GITHUB_REPOSITORY_OWNER 时从 GITHUB_REPOSITORY 取出 owner
    existing = {
        "number": 4,
        "title": f"📚 Paper Gleaner · {digest.day.isoformat()} · 2 篇",
        "html_url": "https://github.com/o/r/issues/4",
        "created_at": f"{digest.day.isoformat()}T01:30:00Z",
    }
    respx.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[existing]))
    respx.get(f"{API}/issues/comments/801/reactions").mock(
        return_value=httpx.Response(200, json=reactions_json(("O", "-1")))
    )
    respx.get(f"{API}/issues/comments/802/reactions").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = runner.invoke(app, ["daily"])

    assert result.exit_code == 0, result.output
    assert "今天的日报已经发布" in result.output
    message = git("log", "-1", "--format=%s", "gleaner-data", cwd=git_remote)
    assert message == f"data: sync reactions {digest.day.isoformat()}"
    month = digest.day.strftime("%Y-%m")
    saved = git("show", f"gleaner-data:feedback/{month}.jsonl", cwd=git_remote)
    assert '"signal": "down"' in saved


def test_daily_requires_github_env(repo: Path) -> None:
    result = runner.invoke(app, ["daily"])
    assert result.exit_code == 1
    assert "GITHUB_TOKEN" in result.output
    assert "--dry-run" in result.output


def test_daily_requires_llm_key(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.setattr(cli, "LLMClient", LLMClient)
    result = runner.invoke(app, ["daily", "--dry-run"])
    assert result.exit_code == 1
    assert "LLM_API_KEY" in result.output


def test_daily_requires_interests(repo: Path) -> None:
    (repo / "profile" / "interests.md").unlink()
    result = runner.invoke(app, ["daily", "--dry-run"])
    assert result.exit_code == 1
    assert "interests.example.md" in result.output


@respx.mock
def test_daily_without_announcements(
    repo: Path, empty_rss_bytes: bytes, git_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_env(monkeypatch)
    respx.get(f"{API}/issues").mock(return_value=httpx.Response(200, json=[]))
    respx.get(RSS_URL).mock(return_value=httpx.Response(200, content=empty_rss_bytes))

    result = runner.invoke(app, ["daily"])

    assert result.exit_code == 0, result.output
    assert "今天没有新公告" in result.output
    assert not data_branch_exists(git_remote)


@respx.mock
def test_dry_run_without_announcements_uses_recent_papers(
    repo: Path, empty_rss_bytes: bytes, api_bytes: bytes
) -> None:
    respx.get(RSS_URL).mock(return_value=httpx.Response(200, content=empty_rss_bytes))
    api = respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, content=api_bytes)
    )

    result = runner.invoke(app, ["daily", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "改用最近提交的 2 篇论文试运行" in result.output
    assert "[1] 9/10  2610.00001  LongKV" in result.output
    assert api.calls.last.request.url.params["max_results"] == "20"


@respx.mock
def test_daily_network_error(repo: Path) -> None:
    respx.get(RSS_URL).mock(return_value=httpx.Response(403))

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
    (workdir / "profile" / "interests.md").write_text("长上下文推理", encoding="utf-8")
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


def test_report_warns_about_missing_comments() -> None:
    digest = Digest(
        day=date(2026, 10, 26),
        generated_at=datetime.now(ZoneInfo("UTC")),
        papers=[ScoredPaper(id="2610.00001", title="P", abstract="A", llm_score=8)],
        issue_number=5,
        issue_url="https://github.com/o/r/issues/5",
    )
    published = _report(DailyResult("published", digest), missing=1)
    assert "✓ 已发布：https://github.com/o/r/issues/5" in published
    assert "还有 1 篇论文的评论没有发出去" in published

    existing = IssueRef(5, "https://github.com/o/r/issues/5")
    repaired = _report(DailyResult("repaired", digest, existing))
    assert repaired == "✓ 已补发缺失的论文评论：https://github.com/o/r/issues/5"


# --- 指令交互 -------------------------------------------------------------------

HTML_URL = "https://arxiv.org/html/2610.00001"


@pytest.fixture
def interact_repo(
    repo: Path, clone: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """repo 之外再准备好 gleaner-data 分支上的日报，LLM 换成深读、对比用的假模型。"""
    seed_repo = clone("data-seed")  # "seed" 已被 git_remote 占用
    seed = DataBranch(seed_repo, seed_repo / ".gleaner-data", backoff_seconds=0)
    seed.checkout().write_model(digest_path(DIGEST_DAY), interact_digest())
    seed.commit_and_push("data: seed digest")
    monkeypatch.setattr(
        cli, "LLMClient", lambda config: LLMClient(config, client=FakeOpenAI(interact_llm))
    )
    return repo


def event_file(tmp_path: Path, body: str) -> Path:
    data = json.loads((FIXTURES / "issue_comment_event.json").read_text(encoding="utf-8"))
    data["comment"]["body"] = body
    path = tmp_path / "event.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def remote_files(remote: Path) -> list[str]:
    return git("ls-tree", "-r", "--name-only", "gleaner-data", cwd=remote).split()


def test_handle_comment_dry_run(interact_repo: Path, tmp_path: Path, git_remote: Path) -> None:
    before = git("rev-parse", "gleaner-data", cwd=git_remote)

    result = runner.invoke(
        app, ["handle-comment", "--event", str(event_file(tmp_path, "/more 2")), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "· 给评论 9001 加 eyes" in result.output
    assert "----- 回复 Issue 1（第 1 条）-----" in result.output
    assert "### [3] Candidate 0" in result.output
    assert "### [4] Candidate 1" in result.output
    assert "· 给评论 9001 加 rocket" in result.output
    assert "✓ 完成 `/more 2`" in result.output
    assert git("rev-parse", "gleaner-data", cwd=git_remote) == before


@respx.mock
def test_handle_comment_posts_reply_and_caches_report(
    interact_repo: Path,
    tmp_path: Path,
    git_remote: Path,
    html_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_env(monkeypatch)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file(tmp_path, "/deep 1")))
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=html_text))
    reactions = respx.post(f"{API}/issues/comments/9001/reactions").mock(
        return_value=httpx.Response(201, json={})
    )
    comments = respx.post(f"{API}/issues/1/comments").mock(
        return_value=httpx.Response(201, json={"id": 555})
    )

    result = runner.invoke(app, ["handle-comment"])

    assert result.exit_code == 0, result.output
    assert [json.loads(c.request.content)["content"] for c in reactions.calls] == [
        "eyes",
        "rocket",
    ]
    assert DEEP_REPORT in json.loads(comments.calls[0].request.content)["body"]
    files = remote_files(git_remote)
    assert "reports/2610.00001.md" in files
    assert any(f.startswith("feedback/") for f in files)  # 按当前月份分片
    assert "## Paper Gleaner · Issue 1" in summary.read_text(encoding="utf-8")


@respx.mock
def test_handle_comment_reports_config_errors(
    interact_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_env(monkeypatch)
    (interact_repo / "config.yaml").write_text("ranking:\n  top_n: 5\n")
    respx.post(f"{API}/issues/comments/9001/reactions").mock(
        return_value=httpx.Response(201, json={})
    )
    comments = respx.post(f"{API}/issues/1/comments").mock(
        return_value=httpx.Response(201, json={"id": 555})
    )

    result = runner.invoke(app, ["handle-comment", "--event", str(event_file(tmp_path, "/help"))])

    assert result.exit_code == 1
    body = json.loads(comments.calls[0].request.content)["body"]
    assert body.startswith("⚠️ 配置有误，请检查 config.yaml：")
    assert "ranking.top_n" in body


def test_handle_comment_requires_event(repo: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["handle-comment", "--dry-run"])
    assert result.exit_code == 1
    assert "GITHUB_EVENT_PATH" in result.output

    missing = runner.invoke(app, ["handle-comment", "--event", str(tmp_path / "x.json")])
    assert missing.exit_code == 1
    assert "找不到事件文件" in missing.output


def test_handle_comment_requires_github_token(repo: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["handle-comment", "--event", str(event_file(tmp_path, "/help"))])
    assert result.exit_code == 1
    assert "GITHUB_TOKEN" in result.output


@respx.mock
def test_local_deep(interact_repo: Path, git_remote: Path, html_text: str) -> None:
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=html_text))

    result = runner.invoke(app, ["deep", "1"])  # 按最近一份日报解析序号

    assert result.exit_code == 0, result.output
    assert "## 🔍 深读 · [LongKV" in result.output
    assert DEEP_REPORT in result.output
    assert "reports/2610.00001.md" not in remote_files(git_remote)


@respx.mock
def test_local_compare(interact_repo: Path, html_text: str) -> None:
    respx.get(HTML_URL).mock(return_value=httpx.Response(200, text=html_text))
    respx.get("https://arxiv.org/html/2610.00002").mock(
        return_value=httpx.Response(200, text=html_text)
    )

    result = runner.invoke(app, ["compare", "1", "2610.00002", "--day", "2026-10-25"])

    assert result.exit_code == 0, result.output
    assert "## ⚖️ 对比 · 2 篇论文" in result.output


def test_local_deep_reports_bad_refs(interact_repo: Path) -> None:
    result = runner.invoke(app, ["deep", "9"])
    assert result.exit_code == 1
    assert "序号 9 超出范围" in result.output
