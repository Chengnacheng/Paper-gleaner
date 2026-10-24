import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from gleaner.config import LLMConfig
from gleaner.llm import LLMClient
from gleaner.ranking.embedder import normalize

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rss_bytes() -> bytes:
    return (FIXTURES / "arxiv_rss.xml").read_bytes()


@pytest.fixture
def empty_rss_bytes() -> bytes:
    return (FIXTURES / "arxiv_rss_empty.xml").read_bytes()


@pytest.fixture
def api_bytes() -> bytes:
    return (FIXTURES / "arxiv_api.xml").read_bytes()


def git(*args: str, cwd: Path) -> str:
    # git 输出 UTF-8；不指定编码时 Windows 会按系统默认编码（如 GBK）解码而出错
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip()


@pytest.fixture
def git_remote(tmp_path: Path) -> Path:
    """一个裸仓库作为远端，main 分支上有一个提交。"""
    remote = tmp_path / "remote.git"
    git("init", "--bare", "--quiet", "--initial-branch=main", str(remote), cwd=tmp_path)
    seed = _clone(remote, tmp_path / "seed")
    (seed / "README.md").write_text("main\n")
    git("add", ".", cwd=seed)
    git("commit", "--quiet", "-m", "init", cwd=seed)
    git("push", "--quiet", "origin", "HEAD:main", cwd=seed)
    return remote


@pytest.fixture
def clone(tmp_path: Path, git_remote: Path) -> Callable[[str], Path]:
    """克隆远端到 tmp_path/<name>，并配置好提交身份。"""
    return lambda name: _clone(git_remote, tmp_path / name)


def _clone(remote: Path, path: Path) -> Path:
    git("clone", "--quiet", str(remote), str(path), cwd=path.parent)
    git("config", "user.name", "Test", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "commit.gpgsign", "false", cwd=path)
    return path


class FakeOpenAI:
    """模拟 openai.OpenAI：handler 根据请求参数返回回复文本，也可以抛出异常。"""

    def __init__(self, handler: Callable[[dict[str, Any]], str]) -> None:
        self.handler = handler
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        reply = self.handler(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


def fake_llm(
    handler: Callable[[dict[str, Any]], str], **config: Any
) -> tuple[LLMClient, FakeOpenAI]:
    fake = FakeOpenAI(handler)
    return LLMClient(LLMConfig(**config), client=fake), fake


def user_prompt(request: dict[str, Any]) -> str:
    """取出请求中最后一条用户消息的内容。"""
    return next(m["content"] for m in reversed(request["messages"]) if m["role"] == "user")


class KeywordEmbedder:
    """按关键词出现次数生成向量的假嵌入模型，便于构造可预期的相似度。"""

    def __init__(self, vocabulary: list[str]) -> None:
        self.vocabulary = vocabulary
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> np.ndarray:
        self.calls.append(texts)
        # 最后一维是常数，避免出现零向量
        rows = [[text.lower().count(word) for word in self.vocabulary] + [0.1] for text in texts]
        return normalize(np.asarray(rows, dtype=np.float32))


# 与 tests/fixtures/arxiv_rss.xml 中的论文配套的假 LLM 和假嵌入
PIPELINE_ANCHORS = {
    "anchors": [
        {"name": "长上下文推理", "query": "KV cache compression for long-context inference"},
        {"name": "检索增强生成", "query": "Retrieval augmented generation"},
        {"name": "医学影像", "query": "Medical imaging", "negative": True},
    ]
}
PIPELINE_VOCABULARY = ["kv cache", "retrieval", "attention", "medical"]


def pipeline_llm(scores: dict[str, int]) -> Callable[[dict[str, Any]], str]:
    """按 prompt 类型分别模拟兴趣拆解、精排和概览三种调用。"""

    def handler(request: dict[str, Any]) -> str:
        system = request["messages"][0]["content"]
        prompt = user_prompt(request)
        if "拆解成若干个具体的子方向" in system:
            return json.dumps(PIPELINE_ANCHORS, ensure_ascii=False)
        if "<paper id=" in prompt:
            ids = re.findall(r'<paper id="([^"]+)">', prompt)
            results = [
                {
                    "id": paper_id,
                    "score": scores[paper_id],
                    "matched_interests": ["长上下文推理"],
                    "reason": f"{paper_id} 与长上下文推理相关",
                    "tldr": f"{paper_id} 的一句话摘要",
                    "tags": ["llm"],
                }
                for paper_id in ids
                if paper_id in scores
            ]
            return json.dumps({"results": results}, ensure_ascii=False)
        if "概括今天的整体情况" in prompt:
            return "今天以长上下文推理为主，建议先读 [1]。"
        raise AssertionError(f"意外的 prompt：{prompt[:80]}")

    return handler
