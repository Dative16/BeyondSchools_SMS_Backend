"""Schools persistence. SQLAlchemy lives here and nowhere above it.

Most of this file is ordinary, except the first method. Resolving a subdomain
to a school happens *before* any tenant is bound — it is how sign-in and the
public application form work out which school they are talking to — so it
cannot go through a row-level security policy without that policy being
permissive enough to leak every school's settings to anyone who can reach the
login route. It calls the SECURITY DEFINER function created in the initial
migration instead, which returns a login page's worth of columns and nothing
more.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.school import School


@dataclass(frozen=True, slots=True)
class PublicSchool:
    """The only facts about a school that an unauthenticated caller may see."""

    id: UUID
    name: str
    subdomain: str
    brand_color: str
    logo_url: str | None
    status: str
    timezone: str
    locale: str


class SchoolRepository(Protocol):
    async def resolve_by_subdomain(self, subdomain: str) -> PublicSchool | None: ...

    async def get(self, school_id: UUID) -> School | None: ...

    async def next_sequence(self, *, school_id: UUID, name: str, scope: str = "") -> int: ...


class SqlSchoolRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_by_subdomain(self, subdomain: str) -> PublicSchool | None:
        row = (
            await self._session.execute(
                text("SELECT * FROM resolve_school_by_subdomain(:subdomain)"),
                {"subdomain": subdomain.strip().lower()},
            )
        ).one_or_none()
        if row is None:
            return None
        return PublicSchool(
            id=row.id,
            name=row.name,
            subdomain=row.subdomain,
            brand_color=row.brand_color,
            logo_url=row.logo_url,
            status=str(row.status),
            timezone=row.timezone,
            locale=row.locale,
        )

    async def get(self, school_id: UUID) -> School | None:
        return (
            await self._session.execute(select(School).where(School.id == school_id))
        ).scalar_one_or_none()

    async def next_sequence(self, *, school_id: UUID, name: str, scope: str = "") -> int:
        """Issue the next admission number, invoice reference or application
        reference for this school.

        `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING` takes the row lock
        and increments in one statement, so two registrars offering places in
        the same second get 0142 and 0143 rather than both getting 0142 and one
        of them an integrity error. Counting existing rows instead — which is
        the obvious implementation — has exactly that race.
        """
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO school_sequence (id, school_id, name, scope, value) "
                    "VALUES (gen_random_uuid(), :school_id, :name, :scope, 1) "
                    "ON CONFLICT (school_id, name, scope) "
                    "DO UPDATE SET value = school_sequence.value + 1 "
                    "RETURNING value"
                ),
                {"school_id": school_id, "name": name, "scope": scope},
            )
        ).one()
        return int(row.value)
