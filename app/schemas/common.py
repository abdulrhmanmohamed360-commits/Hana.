from __future__ import annotations

from typing import Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel

from app.core.config import Settings, get_settings

T = TypeVar("T")

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class PageParams(BaseModel):
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    settings: Settings = Depends(get_settings),
) -> PageParams:
    size = min(page_size or settings.default_page_size, settings.max_page_size)
    return PageParams(page=page, page_size=size)


def make_page(schema: type, items: list, total: int, params: PageParams) -> dict:
    """Validate ORM rows through `schema` (from_attributes) and build the Page payload."""
    return {
        "items": [schema.model_validate(i, from_attributes=True) for i in items],
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
    }
