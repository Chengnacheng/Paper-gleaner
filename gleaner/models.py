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
