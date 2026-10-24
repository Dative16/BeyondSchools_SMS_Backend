"""Guardians persistence. SQLAlchemy lives here and nowhere above it.

A parent with three children at the school is one guardian and three links.
Everything downstream depends on that: who receives one SMS rather than three,
who sees a combined balance across the family, and which phone number a
payment can be traced back to when it arrives with no reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.guardian import Guardian, GuardianStudent


@dataclass(frozen=True, slots=True)
class GuardianChild:
    """One child, as their guardian's screen needs them."""

    student_id: UUID
    admission_no: str
    full_name: str
    class_name: str | None
    relationship: str
    is_primary: bool
    receives_invoices: bool
    billed: Decimal
    settled: Decimal


class GuardianRepository(Protocol):
    async def search(
        self, q: str | None, *, limit: int, offset: int
    ) -> tuple[list[Guardian], int]: ...
    async def get(self, guardian_id: UUID) -> Guardian | None: ...
    async def by_phone(self, phone: str) -> Guardian | None: ...
    async def add(self, **values: Any) -> Guardian: ...
    async def children(self, guardian_id: UUID) -> list[GuardianChild]: ...
    async def guardians_of(self, student_id: UUID) -> list[dict[str, Any]]: ...
    async def link(self, **values: Any) -> GuardianStudent: ...
    async def get_link(self, *, guardian_id: UUID, student_id: UUID) -> GuardianStudent | None: ...
    async def unlink(self, link_id: UUID) -> bool: ...
    async def demote_other_primaries(self, student_id: UUID, *, keep: UUID | None) -> None: ...
    async def student_ids_for(self, guardian_id: UUID) -> frozenset[UUID]: ...


class SqlGuardianRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(
        self, q: str | None, *, limit: int, offset: int
    ) -> tuple[list[Guardian], int]:
        stmt = select(Guardian)
        count_stmt = select(text("count(*)")).select_from(Guardian)
        if q:
            # Name or phone: a bursar matching a payment has one or the other.
            like = f"%{q.strip()}%"
            condition = Guardian.full_name.ilike(like) | Guardian.phone.ilike(like)
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = await self._session.scalar(count_stmt)
        rows = (
            await self._session.execute(
                stmt.order_by(Guardian.full_name).limit(limit).offset(offset)
            )
        ).scalars()
        return list(rows.all()), int(total or 0)

    async def get(self, guardian_id: UUID) -> Guardian | None:
        return await self._session.get(Guardian, guardian_id)

    async def by_phone(self, phone: str) -> Guardian | None:
        return (
            await self._session.execute(select(Guardian).where(Guardian.phone == phone))
        ).scalar_one_or_none()

    async def add(self, **values: Any) -> Guardian:
        guardian = Guardian(**values)
        self._session.add(guardian)
        await self._session.flush()
        return guardian

    async def children(self, guardian_id: UUID) -> list[GuardianChild]:
        """Every child this guardian is linked to, with what each one owes.

        One query, because this is the parent portal's landing screen: four
        numbers per child, and a round trip per child on a 3G connection is
        the difference between a usable page and a blank one.
        """
        rows = await self._session.execute(
            text(
                "SELECT s.id AS student_id, s.admission_no, "
                "       s.first_name || ' ' || s.surname AS full_name, "
                "       cg.name AS class_name, gs.relationship::text AS relationship, "
                "       gs.is_primary, gs.receives_invoices, "
                "       COALESCE((SELECT sum(i.billed) FROM invoice i "
                "                  WHERE i.student_id = s.id AND NOT i.is_void), 0) AS billed, "
                "       COALESCE((SELECT sum(i.settled) FROM invoice i "
                "                  WHERE i.student_id = s.id AND NOT i.is_void), 0) AS settled "
                "  FROM guardian_student gs "
                "  JOIN student s ON s.id = gs.student_id "
                "  LEFT JOIN enrolment e ON e.student_id = s.id AND e.outcome = 'active' "
                "  LEFT JOIN class_group cg ON cg.id = e.class_group_id "
                " WHERE gs.guardian_id = :guardian_id "
                " ORDER BY s.first_name"
            ),
            {"guardian_id": guardian_id},
        )
        return [GuardianChild(**row._mapping) for row in rows]

    async def guardians_of(self, student_id: UUID) -> list[dict[str, Any]]:
        """Who to contact about this child, the primary one first."""
        rows = await self._session.execute(
            text(
                "SELECT gs.id AS link_id, g.id AS guardian_id, g.full_name, g.phone, "
                "       g.alt_phone, g.email, g.national_id, g.occupation, "
                "       gs.relationship::text AS relationship, gs.is_primary, "
                "       gs.receives_invoices, gs.receives_sms, gs.may_collect, "
                "       g.sms_opted_out "
                "  FROM guardian_student gs "
                "  JOIN guardian g ON g.id = gs.guardian_id "
                " WHERE gs.student_id = :student_id "
                " ORDER BY gs.is_primary DESC, g.full_name"
            ),
            {"student_id": student_id},
        )
        return [dict(row._mapping) for row in rows]

    async def link(self, **values: Any) -> GuardianStudent:
        link = GuardianStudent(**values)
        self._session.add(link)
        await self._session.flush()
        return link

    async def get_link(
        self, *, guardian_id: UUID, student_id: UUID
    ) -> GuardianStudent | None:
        return (
            await self._session.execute(
                select(GuardianStudent).where(
                    GuardianStudent.guardian_id == guardian_id,
                    GuardianStudent.student_id == student_id,
                )
            )
        ).scalar_one_or_none()

    async def unlink(self, link_id: UUID) -> bool:
        link = await self._session.get(GuardianStudent, link_id)
        if link is None:
            return False
        await self._session.delete(link)
        await self._session.flush()
        return True

    async def demote_other_primaries(self, student_id: UUID, *, keep: UUID | None) -> None:
        """Make room for a new primary guardian.

        A partial unique index allows one per child, so the old one has to be
        demoted before the new one is promoted — in that order, or the write
        fails on the constraint.
        """
        await self._session.execute(
            text(
                "UPDATE guardian_student SET is_primary = false "
                " WHERE student_id = :student_id AND is_primary "
                # The cast is load-bearing: a bare parameter compared against
                # NULL leaves PostgreSQL unable to infer its type.
                "   AND (CAST(:keep AS uuid) IS NULL OR id <> CAST(:keep AS uuid))"
            ),
            {"student_id": student_id, "keep": keep},
        )

    async def student_ids_for(self, guardian_id: UUID) -> frozenset[UUID]:
        """The children a guardian may see. Rows, not claims — which is why
        this question cannot be answered from a token."""
        rows = await self._session.execute(
            text("SELECT student_id FROM guardian_student WHERE guardian_id = :id"),
            {"id": guardian_id},
        )
        return frozenset(row.student_id for row in rows)
