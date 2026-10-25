"""Issue 评论指令的解析与分发（docs/architecture.md 3.2）。

指令写在评论第一行，例如 "/deep 3 --force"、"/compare 3 5"、"/more 10"。
<ref> 支持三种写法：当前 Issue 中的序号、arXiv ID（可带版本号）、arXiv 链接。
序号按这个 Issue 对应的那一天解析，所以在旧日报下评论同样有效；
日报推荐的论文编号 1…N，/more 列出的候选接着编号 N+1…。

这里只负责「做什么」：返回回复的 Markdown，并把报告缓存、反馈写进 DataStore；
发评论、加表情、提交数据分支由 pipelines/interact.py 完成。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cached_property

from pydantic import ValidationError

from gleaner.config import Config
from gleaner.llm import LLMClient, LLMError
from gleaner.models import Digest, Paper
from gleaner.pipelines.daily import DIGEST_DIR, digest_path
from gleaner.profile import ProfileError, command_feedback, load_interests, record_feedback
from gleaner.profile.memory import CommandSignal
from gleaner.reader import (
    MAX_COMPARE_PAPERS,
    FullText,
    FulltextFetcher,
    compare_papers,
    compare_related,
    deep_read,
    load_report,
    save_report,
)
from gleaner.render.replies import (
    code,
    compare_fallback_reply,
    compare_reply,
    deep_fallback_reply,
    deep_reply,
    help_reply,
    more_reply,
    related_fallback_reply,
    related_reply,
)
from gleaner.sources import ArxivSource, SemanticScholar, SourceError
from gleaner.store import DataStore

COMMANDS = ("deep", "compare", "more", "help")
DEFAULT_MORE = 5
MAX_MORE = 20

_FORCE_FLAGS = {"--force", "-f"}
# 参数之间可以用空格、逗号或顿号分隔："/compare 3 5"、"/compare 3,5"、"/compare 3、5"
_ARG_SEPARATOR = re.compile(r"[\s,，、]+")
_ARXIV_ID_RE = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?$", re.IGNORECASE)
_ARXIV_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/"
    r"(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)

Ref = int | str  # 序号，或不带版本号的 arXiv ID


class CommandError(Exception):
    """指令写错了或无法执行（参数不对、序号越界、找不到论文等）；原因会回复给用户。"""


class UnknownCommandError(CommandError):
    def __init__(self, name: str) -> None:
        super().__init__(f"不认识的指令 /{name}")
        self.name = name


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()
    force: bool = False

    @property
    def text(self) -> str:
        parts = [f"/{self.name}", *self.args, *(["--force"] if self.force else [])]
        return " ".join(parts)


@dataclass
class Services:
    """指令用到的外部服务；测试和本地调试时可以替换。"""

    arxiv: ArxivSource
    fetcher: FulltextFetcher
    s2: SemanticScholar
    # 用到时才创建：没有配置 LLM_API_KEY 时，/more、/help 照常可用
    llm_factory: Callable[[], LLMClient]


@dataclass
class Context:
    config: Config
    services: Services
    store: DataStore
    day: date | None = None  # 当前 Issue 对应的日报日期；None 表示不是日报 Issue
    issue_number: int | None = None
    persist: bool = True  # 写入报告缓存和隐式反馈；本地调试时为 False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @cached_property
    def llm(self) -> LLMClient:
        return self.services.llm_factory()

    @cached_property
    def interests(self) -> str | None:
        try:
            return load_interests(self.config.profile.interests_file)
        except ProfileError as e:
            logger.warning("读取兴趣描述失败，报告中不结合研究兴趣：%s", e)
            return None

    @cached_property
    def digest(self) -> Digest | None:
        if self.day is None:
            return None
        try:
            return self.store.read_model(digest_path(self.day), Digest)
        except ValidationError as e:
            logger.warning("%s 格式不正确：%s", digest_path(self.day), e)
            return None


def parse_command(body: str) -> Command:
    """解析评论第一行的指令。"""
    lines = body.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if not first.startswith("/"):
        raise CommandError("指令需要以 / 开头")
    tokens = [token for token in _ARG_SEPARATOR.split(first[1:]) if token]
    name = tokens[0].lower() if tokens else ""
    if name not in COMMANDS:
        raise UnknownCommandError(name)
    rest = tokens[1:]
    force = any(token.lower() in _FORCE_FLAGS for token in rest)
    args = tuple(token for token in rest if token.lower() not in _FORCE_FLAGS)
    return Command(name, args, force)


def parse_ref(token: str) -> Ref:
    """把 <ref> 解析成序号（int）或不带版本号的 arXiv ID（str）。"""
    token = token.strip().strip("<>()[]")  # 链接可能被 Markdown 包了一层
    if token.isdigit():
        index = int(token)
        if index < 1:
            raise CommandError("序号从 1 开始")
        return index
    if match := _ARXIV_ID_RE.match(token) or _ARXIV_URL_RE.match(token):
        return match.group(1)
    raise CommandError(f"无法识别 {code(token)}：请写序号、arXiv ID 或 arXiv 链接")


def latest_digest_day(store: DataStore) -> date | None:
    """data 分支上最近一份日报的日期，本地调试按序号引用论文时使用。"""
    directory = store.path(DIGEST_DIR)
    stems = sorted(path.stem for path in directory.glob("*.json")) if directory.is_dir() else []
    for stem in reversed(stems):
        try:
            return date.fromisoformat(stem)
        except ValueError:
            continue
    return None


def execute(command: Command, ctx: Context) -> str:
    """执行指令，返回回复的 Markdown。用户能修正的问题抛出 CommandError。"""
    handlers = {"deep": _deep, "compare": _compare, "more": _more, "help": _help}
    return handlers[command.name](command, ctx)


def _help(command: Command, ctx: Context) -> str:
    return help_reply()


def _deep(command: Command, ctx: Context) -> str:
    if len(command.args) != 1:
        raise CommandError("用法：`/deep <ref> [--force]`，一次深读一篇；多篇请用 `/compare`")
    [paper], notes = resolve_papers([parse_ref(command.args[0])], ctx)

    cached = None if command.force else load_report(ctx.store, paper.id)
    if cached is not None:
        if paper.title == paper.id:  # arXiv API 不可用时只有 ID，用报告里记下的标题
            paper = paper.model_copy(update={"title": cached.title})
        _record_feedback(ctx, [paper], "deep")
        return deep_reply(paper, cached, cached=True)

    paper, fulltext = _fetch(paper, ctx)
    try:
        llm = ctx.llm
        report = deep_read(
            paper,
            fulltext,
            llm=llm,
            interests=ctx.interests,
            config=ctx.config.deep_read,
            language=ctx.config.language,
            now=ctx.now,
        )
    except LLMError as e:
        logger.warning("深读 %s 失败：%s", paper.id, e)
        return deep_fallback_reply(paper, fulltext, error=e)

    if ctx.persist:
        save_report(ctx.store, report)
    _record_feedback(ctx, [paper], "deep")
    tokens = llm.tracker.total.total_tokens
    return deep_reply(
        paper, report, cached=False, notes=notes, fulltext_notes=fulltext.notes, tokens=tokens
    )


def _compare(command: Command, ctx: Context) -> str:
    if not command.args:
        raise CommandError(
            "用法：`/compare <ref> <ref> ...` 横向对比多篇，或 `/compare <ref>` 与相关工作对比"
        )
    if len(command.args) > MAX_COMPARE_PAPERS:
        raise CommandError(f"一次最多对比 {MAX_COMPARE_PAPERS} 篇论文")
    papers, notes = resolve_papers([parse_ref(arg) for arg in command.args], ctx)
    if len(papers) == 1:
        return _compare_related(papers[0], notes, ctx)

    fetched = [_fetch(paper, ctx) for paper in papers]
    papers = [paper for paper, _ in fetched]
    fulltexts = [fulltext for _, fulltext in fetched]
    try:
        llm = ctx.llm
        body = compare_papers(
            papers,
            fulltexts,
            llm=llm,
            interests=ctx.interests,
            config=ctx.config.deep_read,
            language=ctx.config.language,
        )
    except LLMError as e:
        logger.warning("对比失败：%s", e)
        return compare_fallback_reply(papers, fulltexts, error=e)

    _record_feedback(ctx, papers, "compare")
    tokens = llm.tracker.total.total_tokens
    return compare_reply(
        papers, fulltexts, body, model=llm.model_for("deep"), notes=notes, tokens=tokens
    )


def _compare_related(paper: Paper, notes: list[str], ctx: Context) -> str:
    paper, fulltext = _fetch(paper, ctx)
    related = ctx.services.s2.related_works(paper.id)
    try:
        llm = ctx.llm
        body = compare_related(
            paper,
            fulltext,
            related.papers,
            llm=llm,
            interests=ctx.interests,
            config=ctx.config.deep_read,
            language=ctx.config.language,
        )
    except LLMError as e:
        logger.warning("%s 与相关工作对比失败：%s", paper.id, e)
        return related_fallback_reply(paper, fulltext, related, error=e)

    _record_feedback(ctx, [paper], "compare")
    tokens = llm.tracker.total.total_tokens
    return related_reply(
        paper, fulltext, related, body, model=llm.model_for("deep"), notes=notes, tokens=tokens
    )


def _more(command: Command, ctx: Context) -> str:
    count = DEFAULT_MORE
    if command.args:
        if len(command.args) > 1 or not command.args[0].isdigit():
            raise CommandError("用法：`/more [n]`，n 是要展开的篇数，默认 5")
        count = min(max(int(command.args[0]), 1), MAX_MORE)
    digest = _require_digest(ctx)
    start = len(digest.papers) + 1
    items = list(enumerate(digest.others, start=start))[:count]
    return more_reply(digest, items)


def resolve_papers(refs: list[Ref], ctx: Context) -> tuple[list[Paper], list[str]]:
    """把 <ref> 解析成论文（去掉重复的），同时返回降级说明。

    序号和当天日报中的 arXiv ID 直接用 digest 中的元数据；其余的通过 arXiv API 查询，
    API 不可用时只保留 ID，标题和摘要稍后从全文中补全。
    """
    needs_digest = any(isinstance(ref, int) for ref in refs)
    digest = _require_digest(ctx) if needs_digest else ctx.digest
    candidates: list[Paper] = [*digest.papers, *digest.others] if digest else []
    by_id = {paper.id: paper for paper in candidates}

    resolved: list[Paper | str] = []
    for ref in refs:
        if isinstance(ref, str):
            resolved.append(by_id.get(ref, ref))
        elif ref <= len(candidates):
            resolved.append(candidates[ref - 1])
        else:
            assert digest is not None
            raise CommandError(_out_of_range(ref, digest))

    missing = list(dict.fromkeys(item for item in resolved if isinstance(item, str)))
    found: dict[str, Paper] = {}
    notes: list[str] = []
    if missing:
        try:
            found = {paper.id: paper for paper in ctx.services.arxiv.fetch_by_ids(missing)}
        except SourceError as e:
            logger.warning("查询 arXiv 元数据失败：%s", e)
            notes.append("arXiv API 暂时不可用，论文信息从全文中获取")
            found = {
                paper_id: Paper(id=paper_id, title=paper_id, abstract="") for paper_id in missing
            }
        unknown = [paper_id for paper_id in missing if paper_id not in found]
        if unknown:
            raise CommandError(
                f"arXiv 上找不到 {'、'.join(code(paper_id) for paper_id in unknown)}"
            )

    papers = [found[item] if isinstance(item, str) else item for item in resolved]
    unique = list({paper.id: paper for paper in papers}.values())
    return unique, notes


def _require_digest(ctx: Context) -> Digest:
    if ctx.day is None:
        raise CommandError("这个 Issue 不是日报，没法按序号找论文，请改用 arXiv ID 或链接")
    digest = ctx.digest
    if digest is None:
        raise CommandError(
            f"找不到 {ctx.day.isoformat()} 的日报数据（gleaner-data 分支上的 "
            f"{digest_path(ctx.day)}），请改用 arXiv ID 或链接"
        )
    if (
        ctx.issue_number is not None
        and digest.issue_number is not None
        and digest.issue_number != ctx.issue_number
    ):
        # 同一天的日报用 --force 重新发布过：序号以新的 Issue 为准
        raise CommandError(
            f"{ctx.day.isoformat()} 的日报已重新发布在 #{digest.issue_number}，"
            "这里的序号可能对不上：请到新的日报下评论，或改用 arXiv ID"
        )
    return digest


def _out_of_range(index: int, digest: Digest) -> str:
    recommended = len(digest.papers)
    message = f"序号 {index} 超出范围：{digest.day.isoformat()} 的日报推荐了 {recommended} 篇"
    if digest.others:
        last = recommended + len(digest.others)
        message += f"，另有 {len(digest.others)} 篇候选（序号 {recommended + 1}–{last}，"
        message += "评论 `/more` 查看）"
    return message


def _fetch(paper: Paper, ctx: Context) -> tuple[Paper, FullText]:
    """获取全文；论文信息不全时（arXiv API 不可用）用全文中的标题和摘要补上。"""
    fulltext = ctx.services.fetcher.fetch(paper)
    updates = {}
    if paper.title == paper.id and fulltext.title:
        updates["title"] = fulltext.title
    if not paper.abstract and fulltext.abstract:
        updates["abstract"] = fulltext.abstract
    if updates:
        paper = paper.model_copy(update=updates)
    if not fulltext.sections:
        reasons = "；".join(fulltext.notes) or "没有摘要"
        raise CommandError(f"没能获取 {paper.id} 的全文和摘要（{reasons}），请稍后重试")
    return paper, fulltext


def _record_feedback(
    ctx: Context, papers: Iterable[Paper], signal: CommandSignal, note: str | None = None
) -> None:
    """写入指令产生的反馈（/deep、/compare 为隐式正反馈），本地调试时不写。"""
    if not ctx.persist:
        return
    records = [command_feedback(paper, signal, now=ctx.now, note=note) for paper in papers]
    record_feedback(ctx.store, records, timezone=ctx.config.timezone)
