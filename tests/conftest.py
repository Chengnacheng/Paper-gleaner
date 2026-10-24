import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rss_bytes() -> bytes:
    return (FIXTURES / "arxiv_rss.xml").read_bytes()


@pytest.fixture
def empty_rss_bytes() -> bytes:
    return (FIXTURES / "arxiv_rss_empty.xml").read_bytes()


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
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
