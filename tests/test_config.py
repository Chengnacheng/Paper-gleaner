from pathlib import Path

import pytest

from gleaner.config import (
    Config,
    ConfigError,
    load_config,
    resolve_config_path,
)

REPO_ROOT = Path(__file__).parent.parent


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_matches_defaults() -> None:
    # 示例配置应与代码中的默认值保持一致，改了一边要记得改另一边
    assert load_config(REPO_ROOT / "config.example.yaml", env={}) == Config()


def test_empty_file_uses_defaults(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, ""), env={}) == Config()


def test_partial_config_merges_with_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "sources:\n  arxiv:\n    categories: [cs.CV]\n"), env={})
    assert config.sources.arxiv.categories == ["cs.CV"]
    assert config.sources.arxiv.include_cross_list is True
    assert config.ranking.final_top_n == 10


def test_unknown_key_reports_its_location(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"ranking\.top_n"):
        load_config(write(tmp_path, "ranking:\n  top_n: 5\n"), env={})


def test_top_n_cannot_exceed_top_k(tmp_path: Path) -> None:
    text = "ranking:\n  coarse_top_k: 5\n  final_top_n: 10\n"
    with pytest.raises(ConfigError, match="final_top_n"):
        load_config(write(tmp_path, text), env={})


def test_invalid_timezone(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Mars/Base"):
        load_config(write(tmp_path, "timezone: Mars/Base\n"), env={})


def test_rejects_api_key_in_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        load_config(write(tmp_path, "llm:\n  api_key: sk-xxx\n"), env={})


def test_rejects_non_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="顶层"):
        load_config(write(tmp_path, "- a\n- b\n"), env={})


def test_rejects_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="YAML"):
        load_config(write(tmp_path, "llm: [unclosed\n"), env={})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "nope.yaml", env={})


def test_env_provides_secrets(tmp_path: Path) -> None:
    env = {"LLM_API_KEY": "sk-test", "LLM_BASE_URL": "https://dashscope.example/v1"}
    config = load_config(write(tmp_path, ""), env=env)
    assert config.llm.api_key is not None
    assert config.llm.api_key.get_secret_value() == "sk-test"
    assert config.llm.base_url == "https://dashscope.example/v1"


def test_empty_env_values_are_ignored(tmp_path: Path) -> None:
    # GitHub Actions 中未配置的 Secret 是空字符串
    config = load_config(write(tmp_path, ""), env={"LLM_API_KEY": "", "LLM_BASE_URL": " "})
    assert config.llm.api_key is None
    assert config.llm.base_url == "https://api.deepseek.com"


def test_api_key_is_not_serialized(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, ""), env={"LLM_API_KEY": "sk-test"})
    assert "sk-test" not in config.model_dump_json()
    assert "api_key" not in config.model_dump()["llm"]


def test_resolve_prefers_explicit_path(tmp_path: Path) -> None:
    assert resolve_config_path(tmp_path / "custom.yaml") == tmp_path / "custom.yaml"


def test_resolve_falls_back_to_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text("")
    assert resolve_config_path() == Path("config.example.yaml")

    (tmp_path / "config.yaml").write_text("")
    assert resolve_config_path() == Path("config.yaml")


def test_resolve_without_any_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="config.example.yaml"):
        resolve_config_path()
