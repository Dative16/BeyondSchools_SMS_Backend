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
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.registration import (
    DocumentRequirement as DocumentRequirementFacts,
)
from app.domain.registration import (
    FieldDefinition,
    FieldKind,
    FieldScope,
)
from app.models.custom_field import SchoolField
from app.models.school import DocumentRequirement, School


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


class SqlRegistrationSettingsRepository:
    """A school's own checklist and its own fields.

    Separate from `SqlSchoolRepository` because these are read by three
    services — students, admissions and the settings screen — and none of them
    wants the rest of the school record to come along.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- the document checklist ------------------------------------------
    async def document_requirements(
        self, *, active_only: bool = True
    ) -> list[DocumentRequirement]:
        stmt = select(DocumentRequirement).order_by(
            DocumentRequirement.sort_order, DocumentRequirement.label
        )
        if active_only:
            stmt = stmt.where(DocumentRequirement.is_active.is_(True))
        return list((await self._session.execute(stmt)).scalars().all())

    async def replace_document_requirements(
        self, *, school_id: UUID, rows: list[dict[str, Any]]
    ) -> list[DocumentRequirement]:
        """Replace the whole checklist.

        A set rather than one row at a time: the screen shows all of it, and a
        partial update is how a school ends up still demanding a document it
        removed last term.
        """
        await self._session.execute(text("DELETE FROM document_requirement"))
        written: list[DocumentRequirement] = []
        for row in rows:
            requirement = DocumentRequirement(school_id=school_id, **row)
            self._session.add(requirement)
            written.append(requirement)
        await self._session.flush()
        return written

    # --- custom fields ----------------------------------------------------
    async def fields(self, *, scope: FieldScope | None = None) -> list[SchoolField]:
        stmt = select(SchoolField).order_by(SchoolField.sort_order, SchoolField.label)
        if scope is not None:
            stmt = stmt.where(SchoolField.scope == scope)
        return list((await self._session.execute(stmt)).scalars().all())

    async def field(self, field_id: UUID) -> SchoolField | None:
        return await self._session.get(SchoolField, field_id)

    async def field_by_key(self, *, scope: FieldScope, key: str) -> SchoolField | None:
        return (
            await self._session.execute(
                select(SchoolField).where(
                    SchoolField.scope == scope, SchoolField.key == key
                )
            )
        ).scalar_one_or_none()

    async def add_field(self, **values: Any) -> SchoolField:
        field = SchoolField(**values)
        self._session.add(field)
        await self._session.flush()
        return field

    async def field_definitions(
        self, *, scope: FieldScope | None = None, include_sensitive: bool = True
    ) -> list[FieldDefinition]:
        """The fields in the shape the rules want.

        A translation on purpose: `app/domain/registration.py` must not import
        a SQLAlchemy model, so the repository hands it plain facts.
        """
        return [
            FieldDefinition(
                field_id=field.id,
                key=field.key,
                label=field.label,
                kind=FieldKind(field.kind),
                scope=FieldScope(field.scope),
                options=tuple(field.options or ()),
                required_at_registration=field.required_at_registration,
                is_sensitive=field.is_sensitive,
                is_active=field.is_active,
            )
            for field in await self.fields(scope=scope)
            if include_sensitive or not field.is_sensitive
        ]

    async def requirement_facts(self) -> list[DocumentRequirementFacts]:
        return [
            DocumentRequirementFacts(
                requirement_id=row.id,
                kind=row.kind,
                label=row.label,
                is_required=row.is_required,
                boarders_only=row.boarders_only,
                level_id=row.level_id,
            )
            for row in await self.document_requirements()
        ]
