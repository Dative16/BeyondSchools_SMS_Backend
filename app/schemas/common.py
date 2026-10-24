"""Common request and response models.

One page shape for the whole API, so every list screen in the web app can
share a component and a hook.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: A roll of 40 fits on a screen; 500 students does not, and a list endpoint
#: with no ceiling is how a phone on 3G ends up downloading a whole school.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class Page[T](BaseModel):
    """One page of results, and enough to render "Showing 1–7 of 486".

    `total` is counted rather than estimated. On a school's worth of rows the
    count is cheap, and a parent or a bursar reading "about 500" in a fee
    report would rightly stop trusting the number next to it.
    """

    items: list[T]
    total: int
    page: int
    size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @classmethod
    def of(cls, items: list[T], *, total: int, params: PageParams) -> Page[T]:
        return cls(items=items, total=total, page=params.page, size=params.size)
