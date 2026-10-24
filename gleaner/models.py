"""核心数据模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Paper(BaseModel):
    id: str  # 不带版本号的 arXiv ID，如 "2410.12345"
    title: str
    abstract: str
    authors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    announce_type: str | None = None  # new / cross / replace / replace-cross
    announced: datetime | None = None

    @property
    def url(self) -> str:
        return f"https://arxiv.org/abs/{self.id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.id}"


class ScoredPaper(Paper):
    """经过粗筛、精排后的论文。"""

    coarse_score: float | None = None
    coarse_match: str | None = None  # 向量最接近的兴趣子方向
    forced: bool = False  # 命中 include_keywords，跳过了粗筛
    llm_score: int | None = None  # 0–10；None 表示精排失败或未精排
    matched_interests: list[str] = Field(default_factory=list)
    reason: str = ""
    tldr: str = ""
    tags: list[str] = Field(default_factory=list)
