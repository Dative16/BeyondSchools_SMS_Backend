"""Admissions persistence. SQLAlchemy lives here and nowhere above it.

The query worth reading is `duplicate_candidates`. It is the one place this
system looks for a child who might already be enrolled, and it is deliberately
wide: a surname, a date of birth or a guardian's phone is enough to be
considered, and the scoring in `app/domain/admissions.py` decides what that
means. Narrowing it here would make the scoring moot — a candidate the query
never returns is a duplicate nobody can flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.admissions import ApplicationState, CandidateStudent
from app.models.admission import Application


@dataclass(frozen=True, slots=True)
class QueueRow:
    """One line of the review queue."""

    id: UUID
    reference: str
    state: str
    child_name: str
    date_of_birth: date | None
    sex: str | None
    level_name: str | None
    previous_school: str | None
    guardian_name: str | None
    guardian_phone: str | None
    phone: str
    submitted_at: datetime | None
    created_at: datetime
    duplicate_score: int | None
    duplicate_reason: str | None
    screening_note: str | None
    admission_no: str | None
    offer_expires_on: date | None
    deposit_due: Any
    deposit_settled: Any
    documents_held: int


class AdmissionsRepository(Protocol):
    async def queue(
        self, *, states: list[str] | None, q: str | None, limit: int, offset: int
    ) -> tuple[list[QueueRow], int]: ...
    async def get(self, application_id: UUID) -> Application | None: ...
    async def by_phone(self, phone: str, *, open_only: bool = True) -> Application | None: ...
    async def add(self, **values: Any) -> Application: ...
    async def duplicate_candidates(
        self, *, surname: str | None, date_of_birth: date | None, guardian_phone: str | None
    ) -> list[CandidateStudent]: ...
    async def documents(self, application_id: UUID) -> list[dict[str, Any]]: ...
    async def document_kinds_held(self, application_id: UUID) -> frozenset[str]: ...
    async def counts_by_state(self) -> dict[str, int]: ...
    async def add_document(self, **values: Any) -> dict[str, Any]: ...
    async def field_values(self, application_id: UUID) -> dict[str, Any]: ...
    async def set_field_value(
        self, *, school_id: UUID, field_id: UUID, application_id: UUID, value: Any
    ) -> None: ...
    async def move_documents_to_student(
        self, *, application_id: UUID, student_id: UUID
    ) -> int: ...
    async def move_field_values_to_student(
        self, *, application_id: UUID, student_id: UUID
    ) -> int: ...
    async def current_year(self) -> dict[str, Any] | None: ...
    async def academic_year(self, academic_year_id: UUID) -> dict[str, Any] | None: ...
    async def first_term(self, academic_year_id: UUID) -> dict[str, Any] | None: ...
    async def level_name(self, level_id: UUID) -> str | None: ...
    async def levels_open_for_application(self) -> list[dict[str, Any]]: ...
    async def attach_invoice_to_student(
        self, *, invoice_id: UUID, student_id: UUID, enrolment_id: UUID
    ) -> None: ...


class SqlAdmissionsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- the queue --------------------------------------------------------
    async def queue(
        self, *, states: list[str] | None, q: str | None, limit: int, offset: int
    ) -> tuple[list[QueueRow], int]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if states:
            clauses.append("a.state = ANY(:states)")
            params["states"] = states
        if q:
            clauses.append(
                "(a.child_first_name ILIKE :q OR a.child_surname ILIKE :q "
                " OR a.reference ILIKE :q OR a.guardian_name ILIKE :q "
                " OR a.phone ILIKE :q OR a.guardian_phone ILIKE :q)"
            )
            params["q"] = f"%{q.strip()}%"

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        total = await self._session.scalar(
            text(f"SELECT count(*) FROM application a {where}"),  # noqa: S608
            params,
        )

        rows = await self._session.execute(
            text(  # noqa: S608
                f"""
                SELECT a.id, a.reference, a.state::text AS state,
                       trim(coalesce(a.child_first_name, '') || ' ' ||
                            coalesce(a.child_surname, '')) AS child_name,
                       a.date_of_birth, a.sex::text AS sex,
                       l.name AS level_name, a.previous_school,
                       a.guardian_name, a.guardian_phone, a.phone,
                       a.submitted_at, a.created_at,
                       a.duplicate_score, a.duplicate_reason, a.screening_note,
                       a.admission_no, a.offer_expires_on, a.deposit_due,
                       COALESCE(i.settled, 0) AS deposit_settled,
                       (SELECT count(*) FROM student_document d
                         WHERE d.application_id = a.id) AS documents_held
                  FROM application a
                  LEFT JOIN level l ON l.id = a.level_applied_id
                  LEFT JOIN invoice i ON i.id = a.deposit_invoice_id
                {where}
                 ORDER BY a.created_at
                 LIMIT :limit OFFSET :offset
                """  # noqa: S608 - the only interpolation is a WHERE built
                # from fixed strings above; every value is a bound parameter
            ),
            params,
        )
        return [QueueRow(**row._mapping) for row in rows], int(total or 0)

    async def counts_by_state(self) -> dict[str, int]:
        """What the dashboard's funnel draws.

        Counted rather than stored: an application moves state several times
        and a stored tally is a tally that drifts.
        """
        rows = await self._session.execute(
            text("SELECT state::text AS state, count(*) AS n FROM application GROUP BY state")
        )
        return {row.state: row.n for row in rows}

    # --- one application -------------------------------------------------
    async def get(self, application_id: UUID) -> Application | None:
        return await self._session.get(Application, application_id)

    async def by_phone(self, phone: str, *, open_only: bool = True) -> Application | None:
        """The application a returning parent comes back to.

        Newest first, and only an open one: a family whose first child was
        enrolled last year and is now applying for a second should get a new
        form, not last year's.
        """
        stmt = select(Application).where(Application.phone == phone)
        if open_only:
            stmt = stmt.where(
                Application.state.in_(
                    [
                        ApplicationState.STARTED,
                        ApplicationState.SUBMITTED,
                        ApplicationState.SCREENED,
                        ApplicationState.OFFERED,
                        ApplicationState.ACCEPTED,
                    ]
                )
            )
        stmt = stmt.order_by(Application.created_at.desc()).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, **values: Any) -> Application:
        application = Application(**values)
        self._session.add(application)
        await self._session.flush()
        return application

    # --- screening --------------------------------------------------------
    async def duplicate_candidates(
        self, *, surname: str | None, date_of_birth: date | None, guardian_phone: str | None
    ) -> list[CandidateStudent]:
        """Children who might be the one applying.

        Wide on purpose. Any one of a shared surname, a matching date of birth
        or a guardian's phone number puts a child in front of the scoring,
        which then decides whether it looks like a duplicate or a sibling. A
        candidate this query never returns is a duplicate nobody can flag.
        """
        if not any((surname, date_of_birth, guardian_phone)):
            return []

        rows = await self._session.execute(
            text(
                "SELECT s.id, s.first_name, s.surname, s.date_of_birth, "
                "       array_remove(array_agg(g.phone), NULL) AS guardian_phones "
                "  FROM student s "
                "  LEFT JOIN guardian_student gs ON gs.student_id = s.id "
                "  LEFT JOIN guardian g ON g.id = gs.guardian_id "
                " WHERE s.surname ILIKE COALESCE(:surname, '~never~') "
                "    OR s.date_of_birth = CAST(:dob AS date) "
                "    OR g.phone = CAST(:phone AS varchar) "
                " GROUP BY s.id, s.first_name, s.surname, s.date_of_birth "
                " LIMIT 50"
            ),
            {
                "surname": surname,
                "dob": date_of_birth,
                "phone": guardian_phone,
            },
        )
        return [
            CandidateStudent(
                student_id=row.id,
                first_name=row.first_name,
                surname=row.surname,
                date_of_birth=row.date_of_birth,
                guardian_phones=frozenset(row.guardian_phones or ()),
            )
            for row in rows
        ]

    # --- documents --------------------------------------------------------
    async def documents(self, application_id: UUID) -> list[dict[str, Any]]:
        rows = await self._session.execute(
            text(
                "SELECT id, kind::text AS kind, file_url, file_name, content_type, "
                "       size_bytes, note, verified_at, created_at "
                "  FROM student_document "
                " WHERE application_id = :id "
                " ORDER BY created_at"
            ),
            {"id": application_id},
        )
        return [dict(row._mapping) for row in rows]

    async def document_kinds_held(self, application_id: UUID) -> frozenset[str]:
        rows = await self._session.execute(
            text(
                "SELECT DISTINCT kind::text AS kind FROM student_document "
                " WHERE application_id = :id"
            ),
            {"id": application_id},
        )
        return frozenset(row.kind for row in rows)

    async def add_document(self, **values: Any) -> dict[str, Any]:
        from app.models.student import StudentDocument

        document = StudentDocument(**values)
        self._session.add(document)
        await self._session.flush()
        return {
            "id": document.id,
            "kind": str(document.kind),
            "file_url": document.file_url,
            "file_name": document.file_name,
            "content_type": document.content_type,
            "size_bytes": document.size_bytes,
            "note": document.note,
            "verified_at": document.verified_at,
            "created_at": document.created_at,
        }

    async def move_documents_to_student(
        self, *, application_id: UUID, student_id: UUID
    ) -> int:
        """Point an application's documents at the child they are now about.

        `application_id` is kept rather than cleared, so the trail from a
        birth certificate back to the form it arrived on survives. A school
        asked three years later where a document came from can answer.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE student_document SET student_id = :student_id "
                    " WHERE application_id = :application_id AND student_id IS NULL"
                ),
                {"student_id": student_id, "application_id": application_id},
            ),
        )
        return result.rowcount or 0

    # --- custom field values ---------------------------------------------
    async def field_values(self, application_id: UUID) -> dict[str, Any]:
        rows = await self._session.execute(
            text(
                "SELECT f.key, v.value "
                "  FROM school_field_value v "
                "  JOIN school_field f ON f.id = v.field_id "
                " WHERE v.application_id = :id"
            ),
            {"id": application_id},
        )
        return {row.key: row.value for row in rows}

    async def set_field_value(
        self, *, school_id: UUID, field_id: UUID, application_id: UUID, value: Any
    ) -> None:
        import json

        await self._session.execute(
            text(
                "INSERT INTO school_field_value (id, school_id, field_id, application_id, "
                " value, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :field_id, :application_id, "
                " CAST(:value AS jsonb), now(), now())"
            ),
            {
                "school_id": school_id,
                "field_id": field_id,
                "application_id": application_id,
                "value": json.dumps(value),
            },
        )

    async def move_field_values_to_student(
        self, *, application_id: UUID, student_id: UUID
    ) -> int:
        """Carry the school's own answers across to the child.

        Only the fields the school scoped to a student: an answer collected
        about the application stays with the application.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE school_field_value v SET student_id = :student_id, "
                    "       application_id = NULL "
                    "  FROM school_field f "
                    " WHERE f.id = v.field_id AND v.application_id = :application_id "
                    "   AND f.scope = 'student'"
                ),
                {"student_id": student_id, "application_id": application_id},
            ),
        )
        return result.rowcount or 0

    # --- the calendar an admission lands in -------------------------------
    async def current_year(self) -> dict[str, Any] | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, label, starts_on, ends_on FROM academic_year "
                    " WHERE is_current LIMIT 1"
                )
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def academic_year(self, academic_year_id: UUID) -> dict[str, Any] | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, label, starts_on, ends_on FROM academic_year WHERE id = :id"
                ),
                {"id": academic_year_id},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def first_term(self, academic_year_id: UUID) -> dict[str, Any] | None:
        """The term a deposit is reckoned against.

        The current term where the year has one, otherwise the earliest: a
        January intake is quoted Term 1's fees, and a child joining in May is
        quoted the term they are actually joining.
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT id, index, label, starts_on, ends_on FROM term "
                    " WHERE academic_year_id = :year "
                    " ORDER BY is_current DESC, index "
                    " LIMIT 1"
                ),
                {"year": academic_year_id},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def level_name(self, level_id: UUID) -> str | None:
        name = await self._session.scalar(
            text("SELECT name FROM level WHERE id = :id"), {"id": level_id}
        )
        return str(name) if name is not None else None

    async def levels_open_for_application(self) -> list[dict[str, Any]]:
        """What the public form offers.

        Only active levels, and only those with a published fee structure for
        the current year — a level nobody can be quoted a deposit for is a
        level nobody should be able to apply to.
        """
        rows = await self._session.execute(
            text(
                "SELECT DISTINCT l.id, l.code, l.name, l.order_index "
                "  FROM level l "
                "  JOIN fee_structure s ON s.level_id = l.id AND s.status = 'published' "
                "  JOIN academic_year y ON y.id = s.academic_year_id AND y.is_current "
                " WHERE l.is_active "
                " ORDER BY l.order_index"
            )
        )
        return [dict(row._mapping) for row in rows]

    async def attach_invoice_to_student(
        self, *, invoice_id: UUID, student_id: UUID, enrolment_id: UUID
    ) -> None:
        """Point the deposit invoice at the child it turned out to be for.

        It was raised against the application, before the child existed. The
        application link is kept as well, so the trail from the bill back to
        the form survives — and the money shows up on the child's statement
        either way.
        """
        await self._session.execute(
            text(
                "UPDATE invoice SET student_id = :student_id, enrolment_id = :enrolment_id "
                " WHERE id = :invoice_id"
            ),
            {
                "student_id": student_id,
                "enrolment_id": enrolment_id,
                "invoice_id": invoice_id,
            },
        )
