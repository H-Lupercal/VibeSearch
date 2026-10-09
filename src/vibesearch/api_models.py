"""Typed representations of the list and detail API responses."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class GalleryListItem(APIModel):
    id: int = Field(gt=0)
    media_id: str
    english_title: str
    japanese_title: str | None = None
    tag_ids: list[int] = Field(default_factory=list)
    num_pages: int = Field(ge=0)


class GalleryListResponse(APIModel):
    result: list[GalleryListItem]
    num_pages: int = Field(ge=0)
    per_page: int = Field(default=25, ge=1, le=100)
    total: int | None = None


class GalleryTitle(APIModel):
    english: str
    japanese: str | None = None
    pretty: str


class GalleryTag(APIModel):
    id: int = Field(gt=0)
    type: str
    name: str


class GalleryDetail(APIModel):
    id: int = Field(gt=0)
    media_id: str
    title: GalleryTitle
    tags: list[GalleryTag]
    num_pages: int = Field(ge=0)
    upload_date: int = Field(ge=0)


# The upstream response is the detail object itself, not a list item.
GalleryDetailResponse = GalleryDetail
