"""Students persistence. SQLAlchemy lives here and nowhere above it.

The roll query is the one worth reading. It joins a student to their *active*
enrolment rather than storing a class on the student, which is the decision the
whole schema rests on — and it is also why the list screen can filter by class
without the data ever going stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.registration import Residency
from app.models.student import (
    EmergencyContact,
    Enrolment,
    Student,
    StudentDocument,
    StudentHealth,
)


@dataclass(frozen=True, slots=True)
class RollRow:
    """One line of the students screen.

    Assembled by the repository because it is one query rather than four: a
    roll of 500 children that fetched guardians and fees per row would be the
    screen a teacher gives up waiting for.
    """

    student_id: UUID
    admission_no: str
    first_name: str
    middle_name: str | None
    surname: str
    date_of_birth: date
    sex: str
    status: str
    photo_url: str | None
    enrolment_id: UUID | None
    class_group_id: UUID | None
    class_name: str | None
    level_id: UUID | None
    residency: str | None
    uses_transport: bool | None
    transport_route: str | None
    primary_guardian: str | None
    primary_guardian_phone: str | None
    billed: Decimal
    settled: Decimal
    ai_platform_user_ref: str | None


@dataclass(frozen=True, slots=True)
class RollFilters:
    q: str | None = None
    class_group_id: UUID | None = None
    level_id: UUID | None = None
    academic_year_id: UUID | None = None
    status: str | None = None
    residency: Residency | None = None
    uses_transport: bool | None = None
    #: settled | part_paid | in_arrears. Replaces an earlier `in_arrears`
    #: boolean, which claimed to mean arrears and actually meant "owes
    #: anything" — a child billed yesterday and not yet due was counted as
    #: being in arrears, which is the difference between a reminder and an
    #: accusation.
    fee_status: str | None = None


class StudentRepository(Protocol):
    async def roll(
        self, filters: RollFilters, *, limit: int, offset: int
    ) -> tuple[list[RollRow], int]: ...
    async def get(self, student_id: UUID) -> Student | None: ...
    async def by_admission_no(self, admission_no: str) -> Student | None: ...
    async def add(self, **values: Any) -> Student: ...
    async def enrolments(self, student_id: UUID) -> list[dict[str, Any]]: ...
    async def active_enrolment(self, student_id: UUID) -> Enrolment | None: ...
    async def enrolment(self, enrolment_id: UUID) -> Enrolment | None: ...
    async def add_enrolment(self, **values: Any) -> Enrolment: ...
    async def close_enrolment(
        self, enrolment_id: UUID, *, outcome: str, exited_on: date, reason: str | None
    ) -> None: ...
    async def health(self, student_id: UUID) -> StudentHealth | None: ...
    async def upsert_health(
        self, student_id: UUID, *, school_id: UUID, **values: Any
    ) -> StudentHealth: ...
    async def emergency_contacts(self, student_id: UUID) -> list[EmergencyContact]: ...
    async def replace_emergency_contacts(
        self, student_id: UUID, *, school_id: UUID, contacts: list[dict[str, Any]]
    ) -> list[EmergencyContact]: ...
    async def documents(self, student_id: UUID) -> list[StudentDocument]: ...
    async def add_document(self, **values: Any) -> StudentDocument: ...
    async def field_values(
        self, *, student_id: UUID | None, enrolment_id: UUID | None
    ) -> dict[str, Any]: ...
    async def set_field_value(
        self,
        *,
        school_id: UUID,
        field_id: UUID,
        student_id: UUID | None,
        enrolment_id: UUID | None,
        value: Any,
        set_by_id: UUID | None,
    ) -> None: ...
    async def teacher_class_group_ids(self, user_id: UUID) -> frozenset[UUID]: ...
    async def document_kinds_held(self, student_id: UUID) -> frozenset[str]: ...
    async def has_guardian(self, student_id: UUID) -> bool: ...
    async def has_emergency_contact(self, student_id: UUID) -> bool: ...
    async def fee_summary(self, student_id: UUID) -> dict[str, Decimal]: ...
    async def next_admission_sequence(self, *, school_id: UUID, scope: str) -> int: ...
    async def class_group_place(self, class_group_id: UUID) -> dict[str, Any] | None: ...


class SqlStudentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- the roll ---------------------------------------------------------
    _ROLL_FROM = """
        FROM student s
        LEFT JOIN enrolment e
               ON e.student_id = s.id AND e.outcome = 'active'
        LEFT JOIN class_group cg ON cg.id = e.class_group_id
        LEFT JOIN guardian_student gs ON gs.student_id = s.id AND gs.is_primary
        LEFT JOIN guardian g ON g.id = gs.guardian_id
    """

    def _roll_where(self, filters: RollFilters) -> tuple[str, dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}

        if filters.q:
            # Name or admission number, which is how a registrar searches:
            # they have a parent on the phone saying either one.
            clauses.append(
                "(s.first_name ILIKE :q OR s.surname ILIKE :q "
                " OR s.middle_name ILIKE :q OR s.admission_no ILIKE :q "
                " OR (s.first_name || ' ' || s.surname) ILIKE :q)"
            )
            params["q"] = f"%{filters.q.strip()}%"
        if filters.class_group_id:
            clauses.append("e.class_group_id = :class_group_id")
            params["class_group_id"] = filters.class_group_id
        if filters.level_id:
            clauses.append("cg.level_id = :level_id")
            params["level_id"] = filters.level_id
        if filters.academic_year_id:
            clauses.append("e.academic_year_id = :academic_year_id")
            params["academic_year_id"] = filters.academic_year_id
        if filters.status:
            clauses.append("s.status = :status")
            params["status"] = filters.status
        if filters.residency:
            clauses.append("e.residency = :residency")
            params["residency"] = filters.residency.value
        if filters.uses_transport is not None:
            clauses.append("e.uses_transport = :uses_transport")
            params["uses_transport"] = filters.uses_transport
        if filters.fee_status:
            # Arrears needs the due date; a balance alone is not arrears.
            owing = (
                "EXISTS (SELECT 1 FROM invoice i "
                "         WHERE i.student_id = s.id AND NOT i.is_void "
                "           AND i.billed > i.settled{extra})"
            )
            if filters.fee_status == "in_arrears":
                clauses.append(owing.format(extra=" AND i.due_date < current_date"))
            elif filters.fee_status == "part_paid":
                # Owes something, but nothing has fallen due yet.
                clauses.append(owing.format(extra=""))
                clauses.append(
                    "NOT " + owing.format(extra=" AND i.due_date < current_date")
                )
            elif filters.fee_status == "settled":
                # Billed at all, and owing nothing. A child with no invoice is
                # not settled, they are unbilled, and lumping the two together
                # would hide a whole class the term run missed.
                clauses.append(
                    "EXISTS (SELECT 1 FROM invoice i "
                    "         WHERE i.student_id = s.id AND NOT i.is_void)"
                )
                clauses.append("NOT " + owing.format(extra=""))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    async def roll(
        self, filters: RollFilters, *, limit: int, offset: int
    ) -> tuple[list[RollRow], int]:
        where, params = self._roll_where(filters)

        total = await self._session.scalar(
            text(f"SELECT count(DISTINCT s.id) {self._ROLL_FROM} {where}"), params
        )

        rows = await self._session.execute(
            # noqa justified: the only interpolated parts are this class's own
            # constant FROM clause and a WHERE assembled from fixed strings in
            # `_roll_where`. Every value the caller supplies is a bound
            # parameter — there is no path from a request to this SQL's text.
            text(
                f"""
                SELECT s.id AS student_id, s.admission_no, s.first_name, s.middle_name,
                       s.surname, s.date_of_birth, s.sex::text AS sex, s.status::text AS status,
                       s.photo_url, s.ai_platform_user_ref,
                       e.id AS enrolment_id, e.class_group_id, cg.name AS class_name,
                       cg.level_id, e.residency::text AS residency, e.uses_transport,
                       e.transport_route,
                       g.full_name AS primary_guardian, g.phone AS primary_guardian_phone,
                       COALESCE((SELECT sum(i.billed) FROM invoice i
                                  WHERE i.student_id = s.id AND NOT i.is_void), 0) AS billed,
                       COALESCE((SELECT sum(i.settled) FROM invoice i
                                  WHERE i.student_id = s.id AND NOT i.is_void), 0) AS settled
                {self._ROLL_FROM}
                {where}
                ORDER BY s.surname, s.first_name, s.admission_no
                LIMIT :limit OFFSET :offset
                """  # noqa: S608
            ),
            {**params, "limit": limit, "offset": offset},
        )

        return [RollRow(**row._mapping) for row in rows], int(total or 0)

    # --- one student ------------------------------------------------------
    async def get(self, student_id: UUID) -> Student | None:
        return await self._session.get(Student, student_id)

    async def by_admission_no(self, admission_no: str) -> Student | None:
        return (
            await self._session.execute(select(Student).where(Student.admission_no == admission_no))
        ).scalar_one_or_none()

    async def add(self, **values: Any) -> Student:
        student = Student(**values)
        self._session.add(student)
        await self._session.flush()
        return student

    async def enrolments(self, student_id: UUID) -> list[dict[str, Any]]:
        """The child's whole history, newest year first.

        The thing promotion is designed to preserve: one row per year, with the
        class and the outcome, so a report card from two years ago can still
        say which class it was earned in.
        """
        rows = await self._session.execute(
            text(
                "SELECT e.id, e.academic_year_id, y.label AS academic_year, "
                "       e.class_group_id, cg.name AS class_name, e.outcome::text AS outcome, "
                "       e.enrolled_on, e.exited_on, e.exit_reason, e.is_repeating, "
                "       e.residency::text AS residency, e.dormitory, e.uses_transport, "
                "       e.transport_route, e.transport_stop, e.promotion_run_id, "
                "       t.full_name AS class_teacher "
                "  FROM enrolment e "
                "  JOIN academic_year y ON y.id = e.academic_year_id "
                "  JOIN class_group cg ON cg.id = e.class_group_id "
                "  LEFT JOIN app_user t ON t.id = cg.class_teacher_id "
                " WHERE e.student_id = :student_id "
                " ORDER BY y.starts_on DESC"
            ),
            {"student_id": student_id},
        )
        return [dict(row._mapping) for row in rows]

    async def class_group_place(self, class_group_id: UUID) -> dict[str, Any] | None:
        """A class, what it prices against, and whether there is room in it.

        The level and the stream come back because they are what a fee
        structure is chosen by, and the registrar picked a class rather than a
        level. The headcount comes back because a client's list of classes
        with places left goes stale the moment a second registrar admits
        somebody — the count that decides is the one read in the same
        transaction as the enrolment.
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT cg.id, cg.school_id, cg.academic_year_id, cg.level_id, "
                    "       cg.stream_id, cg.name, cg.capacity, "
                    "       (SELECT count(*) FROM enrolment e "
                    "         WHERE e.class_group_id = cg.id "
                    "           AND e.outcome = 'active') AS pupils "
                    "  FROM class_group cg WHERE cg.id = :id"
                ),
                {"id": class_group_id},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def active_enrolment(self, student_id: UUID) -> Enrolment | None:
        return (
            await self._session.execute(
                select(Enrolment).where(
                    Enrolment.student_id == student_id, Enrolment.outcome == "active"
                )
            )
        ).scalar_one_or_none()

    async def enrolment(self, enrolment_id: UUID) -> Enrolment | None:
        return await self._session.get(Enrolment, enrolment_id)

    async def add_enrolment(self, **values: Any) -> Enrolment:
        enrolment = Enrolment(**values)
        self._session.add(enrolment)
        await self._session.flush()
        return enrolment

    async def close_enrolment(
        self, enrolment_id: UUID, *, outcome: str, exited_on: date, reason: str | None
    ) -> None:
        await self._session.execute(
            update(Enrolment)
            .where(Enrolment.id == enrolment_id)
            .values(outcome=outcome, exited_on=exited_on, exit_reason=reason)
        )

    # --- health and contacts ---------------------------------------------
    async def health(self, student_id: UUID) -> StudentHealth | None:
        return (
            await self._session.execute(
                select(StudentHealth).where(StudentHealth.student_id == student_id)
            )
        ).scalar_one_or_none()

    async def upsert_health(
        self, student_id: UUID, *, school_id: UUID, **values: Any
    ) -> StudentHealth:
        """One row per child, created on first write.

        Most children never get one, which is the point: a column of nulls on
        every student is a column somebody exports.
        """
        existing = await self.health(student_id)
        if existing is None:
            existing = StudentHealth(school_id=school_id, student_id=student_id, **values)
            self._session.add(existing)
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        await self._session.flush()
        return existing

    async def emergency_contacts(self, student_id: UUID) -> list[EmergencyContact]:
        return list(
            (
                await self._session.execute(
                    select(EmergencyContact)
                    .where(EmergencyContact.student_id == student_id)
                    .order_by(EmergencyContact.priority)
                )
            )
            .scalars()
            .all()
        )

    async def replace_emergency_contacts(
        self, student_id: UUID, *, school_id: UUID, contacts: list[dict[str, Any]]
    ) -> list[EmergencyContact]:
        """Replace the whole list.

        A set rather than one at a time, because the ordering is the point: a
        partial update is how a child ends up with two people at priority 1,
        which the index refuses anyway.
        """
        await self._session.execute(
            text("DELETE FROM emergency_contact WHERE student_id = :student_id"),
            {"student_id": student_id},
        )
        written: list[EmergencyContact] = []
        for contact in contacts:
            row = EmergencyContact(school_id=school_id, student_id=student_id, **contact)
            self._session.add(row)
            written.append(row)
        await self._session.flush()
        return written

    # --- documents --------------------------------------------------------
    async def documents(self, student_id: UUID) -> list[StudentDocument]:
        return list(
            (
                await self._session.execute(
                    select(StudentDocument)
                    .where(StudentDocument.student_id == student_id)
                    .order_by(StudentDocument.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    async def add_document(self, **values: Any) -> StudentDocument:
        document = StudentDocument(**values)
        self._session.add(document)
        await self._session.flush()
        return document

    async def document_kinds_held(self, student_id: UUID) -> frozenset[str]:
        """Which kinds this child already has, for the checklist."""
        rows = await self._session.execute(
            text(
                "SELECT DISTINCT kind::text AS kind FROM student_document "
                " WHERE student_id = :student_id"
            ),
            {"student_id": student_id},
        )
        return frozenset(row.kind for row in rows)

    # --- custom fields ----------------------------------------------------
    async def field_values(
        self, *, student_id: UUID | None, enrolment_id: UUID | None
    ) -> dict[str, Any]:
        """A school's own fields for this child, keyed by field key."""
        # The casts are load-bearing: with a NULL on one side PostgreSQL
        # cannot infer a bare parameter's type and refuses the statement.
        rows = await self._session.execute(
            text(
                "SELECT f.key, v.value "
                "  FROM school_field_value v "
                "  JOIN school_field f ON f.id = v.field_id "
                " WHERE v.student_id = CAST(:student_id AS uuid) "
                "    OR v.enrolment_id = CAST(:enrolment_id AS uuid)"
            ),
            {"student_id": student_id, "enrolment_id": enrolment_id},
        )
        return {row.key: row.value for row in rows}

    async def set_field_value(
        self,
        *,
        school_id: UUID,
        field_id: UUID,
        student_id: UUID | None,
        enrolment_id: UUID | None,
        value: Any,
        set_by_id: UUID | None,
    ) -> None:
        import json

        await self._session.execute(
            text(
                "INSERT INTO school_field_value (id, school_id, field_id, student_id, "
                " enrolment_id, value, set_by_id, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :field_id, :student_id, "
                " :enrolment_id, CAST(:value AS jsonb), :set_by_id, now(), now()) "
                "ON CONFLICT (field_id, student_id) WHERE student_id IS NOT NULL "
                "DO UPDATE SET value = CAST(:value AS jsonb), set_by_id = :set_by_id, "
                "              updated_at = now()"
            ),
            {
                "school_id": school_id,
                "field_id": field_id,
                "student_id": student_id,
                "enrolment_id": enrolment_id,
                "value": json.dumps(value),
                "set_by_id": set_by_id,
            },
        )

    # --- counts for the dashboard ----------------------------------------
    async def counts(self, academic_year_id: UUID | None) -> dict[str, int]:
        params: dict[str, Any] = {"year": academic_year_id}
        row = (
            await self._session.execute(
                text(
                    "SELECT "
                    " (SELECT count(*) FROM enrolment e "
                    "   WHERE e.outcome = 'active' "
                    "     AND (:year IS NULL OR e.academic_year_id = :year)) AS enrolled, "
                    " (SELECT count(*) FROM enrolment e "
                    "   WHERE e.outcome = 'active' AND e.residency = 'boarder' "
                    "     AND (:year IS NULL OR e.academic_year_id = :year)) AS boarders, "
                    " (SELECT count(*) FROM enrolment e "
                    "   WHERE e.outcome = 'active' AND e.uses_transport "
                    "     AND (:year IS NULL OR e.academic_year_id = :year)) AS on_transport"
                ),
                params,
            )
        ).one()
        return {
            "enrolled": row.enrolled,
            "boarders": row.boarders,
            "on_transport": row.on_transport,
        }

    async def next_admission_sequence(self, *, school_id: UUID, scope: str) -> int:
        """Issue the next admission number for this school and year.

        Locks and increments in one statement, so two registrars admitting in
        the same second get 0142 and 0143 rather than both getting 0142.
        """
        row = (
            await self._session.execute(
                text(
                    "INSERT INTO school_sequence (id, school_id, name, scope, value) "
                    "VALUES (gen_random_uuid(), :school_id, 'admission_no', :scope, 1) "
                    "ON CONFLICT (school_id, name, scope) "
                    "DO UPDATE SET value = school_sequence.value + 1 "
                    "RETURNING value"
                ),
                {"school_id": school_id, "scope": scope},
            )
        ).one()
        return int(row.value)

    async def teacher_class_group_ids(self, user_id: UUID) -> frozenset[UUID]:
        """The classes a teacher may see children in.

        Both ways a teacher is attached to a class: as its class teacher, and
        through a subject they are assigned to teach it.

        Resolved per request rather than carried in the token. Putting a
        subject teacher's dozen class ids in every token would bloat it, and a
        timetable change would then take fifteen minutes to take effect
        instead of one request.
        """
        rows = await self._session.execute(
            text(
                "SELECT id AS class_group_id FROM class_group WHERE class_teacher_id = :uid "
                "UNION "
                "SELECT class_group_id FROM teaching_assignment WHERE teacher_id = :uid"
            ),
            {"uid": user_id},
        )
        return frozenset(row.class_group_id for row in rows)

    async def has_guardian(self, student_id: UUID) -> bool:
        found = await self._session.scalar(
            text("SELECT 1 FROM guardian_student WHERE student_id = :id LIMIT 1"),
            {"id": student_id},
        )
        return found is not None

    async def has_emergency_contact(self, student_id: UUID) -> bool:
        found = await self._session.scalar(
            text("SELECT 1 FROM emergency_contact WHERE student_id = :id LIMIT 1"),
            {"id": student_id},
        )
        return found is not None

    async def fee_summary(self, student_id: UUID) -> dict[str, Decimal]:
        row = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(text("billed")), 0).label("billed"),
                    func.coalesce(func.sum(text("settled")), 0).label("settled"),
                )
                .select_from(text("invoice"))
                .where(text("student_id = :student_id AND NOT is_void"))
                .params(student_id=student_id)
            )
        ).one()
        billed = Decimal(row.billed or 0)
        settled = Decimal(row.settled or 0)
        return {"billed": billed, "settled": settled, "balance": max(billed - settled, Decimal(0))}
