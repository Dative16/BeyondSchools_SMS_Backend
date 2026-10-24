"""Academic structure persistence: years, terms, levels, streams, classes,
subjects and teaching assignments.

Every query in this file runs on a session already bound to one school, so not
one of them filters by `school_id` — that is the row-level security guarantee
doing its job. The `school_id` values written on insert are the one exception,
and they are there because the column is NOT NULL, not because the application
is choosing a tenant.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.academic_year import Level as LevelFacts
from app.domain.enrolment import LevelStage
from app.models.academic import (
    AcademicYear,
    AcademicYearExitLevel,
    ClassGroup,
    Level,
    Stream,
    Subject,
    SubjectLevel,
    TeachingAssignment,
    Term,
)


class AcademicRepository(Protocol):
    """What the academic service needs from storage."""

    # years and terms
    async def years(self) -> list[AcademicYear]: ...
    async def year(self, year_id: UUID) -> AcademicYear | None: ...
    async def current_year(self) -> AcademicYear | None: ...
    async def add_year(self, **values: Any) -> AcademicYear: ...
    async def make_year_current(self, year_id: UUID) -> None: ...
    async def terms(self, year_id: UUID) -> list[Term]: ...
    async def term(self, term_id: UUID) -> Term | None: ...
    async def add_term(self, **values: Any) -> Term: ...
    async def make_term_current(self, term_id: UUID) -> None: ...

    # the ladder
    async def levels(self) -> list[Level]: ...
    async def level(self, level_id: UUID) -> Level | None: ...
    async def add_level(self, **values: Any) -> Level: ...
    async def level_facts(self) -> list[LevelFacts]: ...
    async def exit_level_ids(self, year_id: UUID) -> frozenset[UUID]: ...
    async def set_exit_levels(
        self, year_id: UUID, *, school_id: UUID, levels: list[tuple[UUID, str | None]]
    ) -> None: ...

    # classes
    async def streams(self, level_id: UUID | None = None) -> list[Stream]: ...
    async def stream(self, stream_id: UUID) -> Stream | None: ...
    async def add_stream(self, **values: Any) -> Stream: ...
    async def class_groups(self, year_id: UUID | None = None) -> list[ClassGroup]: ...
    async def class_group(self, group_id: UUID) -> ClassGroup | None: ...
    async def add_class_group(self, **values: Any) -> ClassGroup: ...
    async def enrolment_counts(self, year_id: UUID | None = None) -> dict[UUID, int]: ...

    # subjects and who teaches them
    async def subjects(self) -> list[Subject]: ...
    async def subject(self, subject_id: UUID) -> Subject | None: ...
    async def add_subject(self, **values: Any) -> Subject: ...
    async def subject_levels(self, subject_id: UUID) -> list[SubjectLevel]: ...
    async def set_subject_levels(
        self,
        subject_id: UUID,
        *,
        school_id: UUID,
        levels: list[tuple[UUID, int | None]],
    ) -> None: ...
    async def teaching_assignments(
        self, *, class_group_id: UUID | None = None, teacher_id: UUID | None = None
    ) -> list[TeachingAssignment]: ...
    async def add_teaching_assignment(self, **values: Any) -> TeachingAssignment: ...
    async def remove_teaching_assignment(self, assignment_id: UUID) -> bool: ...
    async def teacher_can_be_assigned(self, teacher_id: UUID) -> bool: ...


class SqlAcademicRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- years and terms --------------------------------------------------
    async def years(self) -> list[AcademicYear]:
        return list(
            (
                await self._session.execute(
                    select(AcademicYear).order_by(AcademicYear.starts_on.desc())
                )
            )
            .scalars()
            .all()
        )

    async def year(self, year_id: UUID) -> AcademicYear | None:
        return await self._session.get(AcademicYear, year_id)

    async def current_year(self) -> AcademicYear | None:
        return (
            await self._session.execute(
                select(AcademicYear).where(AcademicYear.is_current.is_(True))
            )
        ).scalar_one_or_none()

    async def add_year(self, **values: Any) -> AcademicYear:
        year = AcademicYear(**values)
        self._session.add(year)
        await self._session.flush()
        return year

    async def make_year_current(self, year_id: UUID) -> None:
        """Move the "current year" marker.

        Unset before set, in that order: a partial unique index allows exactly
        one current year per school, so doing it the other way round fails on
        the constraint rather than quietly leaving two.
        """
        await self._session.execute(
            update(AcademicYear)
            .where(AcademicYear.is_current.is_(True), AcademicYear.id != year_id)
            .values(is_current=False)
        )
        await self._session.execute(
            update(AcademicYear).where(AcademicYear.id == year_id).values(is_current=True)
        )

    async def terms(self, year_id: UUID) -> list[Term]:
        return list(
            (
                await self._session.execute(
                    select(Term).where(Term.academic_year_id == year_id).order_by(Term.index)
                )
            )
            .scalars()
            .all()
        )

    async def term(self, term_id: UUID) -> Term | None:
        return await self._session.get(Term, term_id)

    async def add_term(self, **values: Any) -> Term:
        term = Term(**values)
        self._session.add(term)
        await self._session.flush()
        return term

    async def make_term_current(self, term_id: UUID) -> None:
        """One current term across the school, not one per year.

        A school is in one term at a time. Scoping this to the year would let
        last year keep a current term, and every "this term" query would then
        have to say which year it meant.
        """
        await self._session.execute(
            update(Term).where(Term.is_current.is_(True), Term.id != term_id).values(
                is_current=False
            )
        )
        await self._session.execute(
            update(Term).where(Term.id == term_id).values(is_current=True)
        )

    # --- the ladder -------------------------------------------------------
    async def levels(self) -> list[Level]:
        return list(
            (await self._session.execute(select(Level).order_by(Level.order_index)))
            .scalars()
            .all()
        )

    async def level(self, level_id: UUID) -> Level | None:
        return await self._session.get(Level, level_id)

    async def add_level(self, **values: Any) -> Level:
        level = Level(**values)
        self._session.add(level)
        await self._session.flush()
        return level

    async def level_facts(self) -> list[LevelFacts]:
        """The ladder in the shape the rules want.

        A deliberate translation: `app/domain/academic_year.py` must not import
        a SQLAlchemy model, so the repository hands it plain facts.
        """
        return [
            LevelFacts(
                level_id=level.id,
                code=level.code,
                name=level.name,
                stage=LevelStage(level.stage),
                order_index=level.order_index,
                next_level_id=level.next_level_id,
                is_active=level.is_active,
            )
            for level in await self.levels()
        ]

    async def exit_level_ids(self, year_id: UUID) -> frozenset[UUID]:
        rows = await self._session.execute(
            select(AcademicYearExitLevel.level_id).where(
                AcademicYearExitLevel.academic_year_id == year_id
            )
        )
        return frozenset(rows.scalars().all())

    async def set_exit_levels(
        self, year_id: UUID, *, school_id: UUID, levels: list[tuple[UUID, str | None]]
    ) -> None:
        """Replace the exit cohorts for one year.

        Replace rather than merge, because the screen that calls this shows the
        whole set and a partial update would leave last year's cohort behind —
        which in 2028, the year after the double exit, would strand a cohort
        that should be promoting normally.
        """
        await self._session.execute(
            text("DELETE FROM academic_year_exit_level WHERE academic_year_id = :year"),
            {"year": year_id},
        )
        for level_id, note in levels:
            self._session.add(
                AcademicYearExitLevel(
                    school_id=school_id,
                    academic_year_id=year_id,
                    level_id=level_id,
                    note=note,
                )
            )
        await self._session.flush()

    # --- classes ----------------------------------------------------------
    async def streams(self, level_id: UUID | None = None) -> list[Stream]:
        stmt = select(Stream).order_by(Stream.name)
        if level_id is not None:
            stmt = stmt.where(Stream.level_id == level_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def stream(self, stream_id: UUID) -> Stream | None:
        return await self._session.get(Stream, stream_id)

    async def add_stream(self, **values: Any) -> Stream:
        stream = Stream(**values)
        self._session.add(stream)
        await self._session.flush()
        return stream

    async def class_groups(self, year_id: UUID | None = None) -> list[ClassGroup]:
        stmt = select(ClassGroup).order_by(ClassGroup.name)
        if year_id is not None:
            stmt = stmt.where(ClassGroup.academic_year_id == year_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def class_group(self, group_id: UUID) -> ClassGroup | None:
        return await self._session.get(ClassGroup, group_id)

    async def add_class_group(self, **values: Any) -> ClassGroup:
        group = ClassGroup(**values)
        self._session.add(group)
        await self._session.flush()
        return group

    async def enrolment_counts(self, year_id: UUID | None = None) -> dict[UUID, int]:
        """How many children are actually in each class.

        Counted from active enrolments rather than stored on the class group:
        a stored count is a number that goes stale the first time somebody
        transfers out, and a roll that disagrees with the register is the sort
        of thing that loses a school's trust.
        """
        sql = (
            "SELECT class_group_id, count(*) AS pupils "
            "  FROM enrolment "
            " WHERE outcome = 'active' "
        )
        params: dict[str, Any] = {}
        if year_id is not None:
            sql += " AND academic_year_id = :year "
            params["year"] = year_id
        sql += " GROUP BY class_group_id"

        rows = await self._session.execute(text(sql), params)
        return {row.class_group_id: row.pupils for row in rows}

    # --- subjects ---------------------------------------------------------
    async def subjects(self) -> list[Subject]:
        return list(
            (await self._session.execute(select(Subject).order_by(Subject.name)))
            .scalars()
            .all()
        )

    async def subject(self, subject_id: UUID) -> Subject | None:
        return await self._session.get(Subject, subject_id)

    async def add_subject(self, **values: Any) -> Subject:
        subject = Subject(**values)
        self._session.add(subject)
        await self._session.flush()
        return subject

    async def subject_levels(self, subject_id: UUID) -> list[SubjectLevel]:
        return list(
            (
                await self._session.execute(
                    select(SubjectLevel).where(SubjectLevel.subject_id == subject_id)
                )
            )
            .scalars()
            .all()
        )

    async def set_subject_levels(
        self,
        subject_id: UUID,
        *,
        school_id: UUID,
        levels: list[tuple[UUID, int | None]],
    ) -> None:
        await self._session.execute(
            text("DELETE FROM subject_level WHERE subject_id = :subject"),
            {"subject": subject_id},
        )
        for level_id, periods in levels:
            self._session.add(
                SubjectLevel(
                    school_id=school_id,
                    subject_id=subject_id,
                    level_id=level_id,
                    periods_per_week=periods,
                )
            )
        await self._session.flush()

    # --- who teaches what -------------------------------------------------
    async def teaching_assignments(
        self, *, class_group_id: UUID | None = None, teacher_id: UUID | None = None
    ) -> list[TeachingAssignment]:
        stmt = select(TeachingAssignment)
        if class_group_id is not None:
            stmt = stmt.where(TeachingAssignment.class_group_id == class_group_id)
        if teacher_id is not None:
            stmt = stmt.where(TeachingAssignment.teacher_id == teacher_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def add_teaching_assignment(self, **values: Any) -> TeachingAssignment:
        assignment = TeachingAssignment(**values)
        self._session.add(assignment)
        await self._session.flush()
        return assignment

    async def remove_teaching_assignment(self, assignment_id: UUID) -> bool:
        assignment = await self._session.get(TeachingAssignment, assignment_id)
        if assignment is None:
            return False
        await self._session.delete(assignment)
        await self._session.flush()
        return True

    async def teacher_can_be_assigned(self, teacher_id: UUID) -> bool:
        """Whether this user is somebody who can be given a class or a subject.

        Checked here rather than trusted from the request: an id from a form is
        an id somebody could have typed, and assigning a guardian to teach
        Standard 4 would give them every teacher's view of that class.
        """
        row = await self._session.scalar(
            text(
                "SELECT 1 FROM app_user "
                " WHERE id = :id AND is_active "
                "   AND role IN ('class_teacher', 'subject_teacher', "
                "                'head_of_academics', 'school_admin')"
            ),
            {"id": teacher_id},
        )
        return row is not None

    async def names_for_levels(self, level_ids: list[UUID]) -> dict[UUID, str]:
        """Level names, for building a class group's display name."""
        if not level_ids:
            return {}
        rows = await self._session.execute(
            select(Level.id, Level.name).where(Level.id.in_(level_ids))
        )
        return {row.id: row.name for row in rows}

    async def terms_in_range(self, starts_on: date, ends_on: date) -> list[Term]:
        """Terms touching a date range, for the invoicing run to ask about."""
        return list(
            (
                await self._session.execute(
                    select(Term)
                    .where(Term.ends_on >= starts_on, Term.starts_on <= ends_on)
                    .order_by(Term.starts_on)
                )
            )
            .scalars()
            .all()
        )
