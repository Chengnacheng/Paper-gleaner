"""配置加载与校验。

配置来自两处：
- ``config.yaml``：可以公开的设置，提交在用户自己的 fork 中；
- 环境变量：API Key 等密钥，在 GitHub Actions 中由 Secrets 注入。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

DEFAULT_CONFIG_PATH = Path("config.yaml")
EXAMPLE_CONFIG_PATH = Path("config.example.yaml")


class ConfigError(Exception):
    """配置缺失或不合法。"""


class _Section(BaseModel):
    # 拒绝未知字段，拼错的配置项会直接报错，而不是被悄悄忽略
    model_config = ConfigDict(extra="forbid")


class ArxivConfig(_Section):
    categories: list[str] = Field(default_factory=lambda: ["cs.CL", "cs.LG", "cs.AI"], min_length=1)
    include_cross_list: bool = True
    include_replace: bool = False


class SourcesConfig(_Section):
    arxiv: ArxivConfig = Field(default_factory=ArxivConfig)


class ZoteroConfig(_Section):
    enabled: bool = False


class ProfileConfig(_Section):
    interests_file: Path = Path("profile/interests.md")
    zotero: ZoteroConfig = Field(default_factory=ZoteroConfig)


class FiltersConfig(_Section):
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class RankingWeights(_Section):
    interest: float = 1.0
    positive: float = 0.6
    zotero: float = 0.5
    negative: float = 0.8


class EmbeddingConfig(_Section):
    provider: Literal["local", "api"] = "local"
    model: str = "BAAI/bge-small-en-v1.5"


class RankingConfig(_Section):
    coarse_top_k: int = Field(50, ge=1)
    final_top_n: int = Field(10, ge=1)
    min_score: int = Field(6, ge=0, le=10)
    weights: RankingWeights = Field(default_factory=RankingWeights)
    feedback_half_life_days: int = Field(90, ge=1)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)

    @model_validator(mode="after")
    def _top_n_within_top_k(self) -> RankingConfig:
        if self.final_top_n > self.coarse_top_k:
            raise ValueError("final_top_n 不能大于 coarse_top_k")
        return self


class DeepReadConfig(_Section):
    auto_top_m: int = Field(1, ge=0)
    max_input_tokens: int = Field(30_000, ge=1_000)


class LLMModels(_Section):
    fast: str = "deepseek-chat"
    deep: str = "deepseek-reasoner"


class LLMConfig(_Section):
    base_url: str = "https://api.deepseek.com"
    models: LLMModels = Field(default_factory=LLMModels)
    daily_token_budget: int | None = Field(500_000, ge=1)
    timeout_seconds: float = Field(60, gt=0)
    max_retries: int = Field(3, ge=0)
    # 只从环境变量 LLM_API_KEY 读取，不允许写在配置文件里
    api_key: SecretStr | None = Field(default=None, exclude=True)


class GithubIssueNotifyConfig(_Section):
    enabled: bool = True
    per_paper_comment: bool = True
    close_after_days: int | None = Field(7, ge=1)


class ChannelConfig(_Section):
    enabled: bool = False


class NotifyConfig(_Section):
    github_issue: GithubIssueNotifyConfig = Field(default_factory=GithubIssueNotifyConfig)
    email: ChannelConfig = Field(default_factory=ChannelConfig)
    feishu: ChannelConfig = Field(default_factory=ChannelConfig)
    wecom: ChannelConfig = Field(default_factory=ChannelConfig)


class Config(_Section):
    language: str = "zh"
    timezone: str = "Asia/Shanghai"
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)
    deep_read: DeepReadConfig = Field(default_factory=DeepReadConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"未知时区：{value}") from e
        return value


def resolve_config_path(path: Path | None = None) -> Path:
    """确定要使用的配置文件：显式指定 > config.yaml > config.example.yaml。"""
    if path is not None:
        return path
    for candidate in (DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH):
        if candidate.is_file():
            return candidate
    raise ConfigError(f"找不到 {DEFAULT_CONFIG_PATH}，请从 {EXAMPLE_CONFIG_PATH} 复制一份")


def load_config(path: Path, env: Mapping[str, str] | None = None) -> Config:
    """读取并校验配置文件，再用环境变量补充密钥。"""
    env = os.environ if env is None else env

    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"配置文件不存在：{path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} 不是合法的 YAML：{e}") from e

    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} 的顶层必须是键值对")
    if isinstance(raw.get("llm"), dict) and "api_key" in raw["llm"]:
        raise ConfigError(
            "不要把 API Key 写进配置文件，请改用环境变量 LLM_API_KEY（GitHub Secret）"
        )

    try:
        config = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e

    # GitHub Actions 中未配置的 Secret 会以空字符串传入，按未设置处理
    if api_key := env.get("LLM_API_KEY", "").strip():
        config.llm.api_key = SecretStr(api_key)
    if base_url := env.get("LLM_BASE_URL", "").strip():
        config.llm.base_url = base_url
    return config


def _format_validation_error(path: Path, error: ValidationError) -> str:
    lines = [f"{path} 校验失败："]
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "(顶层)"
        lines.append(f"  - {location}: {item['msg']}")
    return "\n".join(lines)
