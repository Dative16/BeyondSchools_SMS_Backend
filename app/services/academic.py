"""Academic structure: the school profile, the calendar and the ladder.

Permissions are checked here rather than in the routes, so the same rule holds
whether a call arrives from the staff app, a Celery task or the AI platform's
API. The routes also guard themselves, which keeps the requirement readable in
a signature — but this is the check that counts.

The one piece of real judgement in this file is how hard to push back. Changing
`order_index` on a level, or marking a level as an exit cohort, decides what
happens to several hundred children in December. So writes that touch the
ladder return the problems the change introduces alongside the result, and the
screen shows them; they do not silently succeed, and they do not refuse a
school that knows what it is doing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from app.domain.academic_year import (
    LadderStep,
    TermDraft,
    YearDraft,
    describe_exit_cohorts,
    ladder_problems,
    next_level,
    validate_terms,
    validate_year,
)
from app.domain.enrolment import LevelStage, class_group_name
from app.domain.roles import Permission, Principal, can
from app.models.academic import (
    AcademicYear,
    ClassGroup,
    Level,
    Stream,
    Subject,
    TeachingAssignment,
    Term,
)
from app.models.audit import AuditOutcome
from app.models.school import School
from app.repositories.academic import AcademicRepository
from app.repositories.audit import AuditRepository
from app.repositories.base import UnitOfWork
from app.repositories.schools import SchoolRepository
from app.services.auth import RequestContext


class AcademicError(Exception):
    """Something the service will not do. The API maps it to a status code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotAllowed(AcademicError):
    """The principal lacks the capability."""


class NotFound(AcademicError):
    """No such record in this school. Indistinguishable, deliberately, from a
    record in another school: a 404 either way tells a caller nothing about
    what exists elsewhere on the platform."""


class Invalid(AcademicError):
    """The change does not make sense. The reason is for the user to read."""


@dataclass(frozen=True, slots=True)
class LadderView:
    """The ladder, plus what is wrong with it.

    One response rather than two calls, because the academic settings screen
    shows both together and a school that only sees the happy version is a
    school that finds out in December.
    """

    levels: list[Level]
    exit_level_ids: frozenset[UUID]
    problems: tuple[str, ...]
    summary: str
    steps: dict[UUID, LadderStep]


@dataclass(frozen=True, slots=True)
class ClassGroupView:
    group: ClassGroup
    pupils: int


class AcademicService:
    def __init__(
        self,
        uow: UnitOfWork,
        academic: AcademicRepository,
        schools: SchoolRepository,
        audit: AuditRepository,
    ) -> None:
        self._uow = uow
        self._academic = academic
        self._schools = schools
        self._audit = audit

    # ----------------------------------------------------------------------
    # Guards
    # ----------------------------------------------------------------------
    def _require(self, principal: Principal, permission: Permission) -> None:
        if not can(principal, permission):
            raise NotAllowed(f"this account may not {permission.value.replace(':', ' ')}")

    @staticmethod
    def _require_staff(principal: Principal) -> None:
        """Reading the structure is open to staff and to nobody else.

        A level's name is not sensitive, but a guardian has no use for the
        teaching assignments or the class rolls that hang off the same
        endpoints, and "harmless to read" is a judgement that ages badly.
        """
        if not principal.is_staff:
            raise NotAllowed("this account may not read the academic structure")

    async def _school(self, principal: Principal) -> School:
        if principal.school_id is None:
            raise NotFound("this account belongs to no school")
        school = await self._schools.get(principal.school_id)
        if school is None:
            raise NotFound("no such school")
        return school

    # ----------------------------------------------------------------------
    # The school profile
    # ----------------------------------------------------------------------
    async def profile(self, principal: Principal) -> School:
        self._require_staff(principal)
        return await self._school(principal)

    async def update_profile(
        self, principal: Principal, *, changes: dict[str, Any], context: RequestContext
    ) -> School:
        """Edit the school's own record.

        The arrears switches are in here, which is why every change is
        audited: `arrears_blocks_results` decides whether a child's report card
        is withheld, and somebody has to be able to say who turned it on.
        """
        self._require(principal, Permission.SCHOOL_MANAGE)

        async with self._uow:
            school = await self._school(principal)

            applied: dict[str, Any] = {}
            for field, value in changes.items():
                if value is None or not hasattr(school, field):
                    continue
                if getattr(school, field) != value:
                    applied[field] = value
                    setattr(school, field, value)

            if "deposit_percent" in applied and not 0 <= school.deposit_percent <= 100:
                raise Invalid("a deposit has to be between 0 and 100 per cent")

            if applied:
                await self._audit.record(
                    school_id=school.id,
                    action="school.updated",
                    actor_user_id=principal.user_id,
                    actor_role=principal.role.value,
                    entity_type="school",
                    entity_id=school.id,
                    changed=_stringify(applied),
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
            await self._uow.commit()
            return school

    # ----------------------------------------------------------------------
    # The calendar
    # ----------------------------------------------------------------------
    async def years(self, principal: Principal) -> list[AcademicYear]:
        self._require_staff(principal)
        return await self._academic.years()

    async def current_year(self, principal: Principal) -> AcademicYear | None:
        self._require_staff(principal)
        return await self._academic.current_year()

    async def create_year(
        self,
        principal: Principal,
        *,
        label: str,
        starts_on: date,
        ends_on: date,
        make_current: bool,
        context: RequestContext,
    ) -> AcademicYear:
        self._require(principal, Permission.ACADEMIC_MANAGE)
        school = await self._school(principal)

        problems = validate_year(YearDraft(label=label, starts_on=starts_on, ends_on=ends_on))
        if problems:
            raise Invalid("; ".join(problems))

        async with self._uow:
            year = await self._academic.add_year(
                school_id=school.id, label=label.strip(), starts_on=starts_on, ends_on=ends_on
            )
            if make_current:
                await self._academic.make_year_current(year.id)
            await self._audit.record(
                school_id=school.id,
                action="academic_year.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="academic_year",
                entity_id=year.id,
                changed={"label": label, "current": make_current},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return year

    async def make_year_current(
        self, principal: Principal, *, year_id: UUID, context: RequestContext
    ) -> AcademicYear:
        """Move the school into a different academic year.

        Audited loudly. Invoicing and promotion both pivot on this one flag, so
        a run started against the wrong year is the kind of mistake somebody
        needs to be able to trace.
        """
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            year = await self._academic.year(year_id)
            if year is None:
                raise NotFound("no such academic year")
            if year.is_closed:
                raise Invalid("that year has been closed; reopen it first")

            await self._academic.make_year_current(year_id)
            await self._audit.record(
                school_id=year.school_id,
                action="academic_year.made_current",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="academic_year",
                entity_id=year.id,
                note=f"the school is now working in {year.label}",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return year

    async def terms(self, principal: Principal, *, year_id: UUID) -> list[Term]:
        self._require_staff(principal)
        if await self._academic.year(year_id) is None:
            raise NotFound("no such academic year")
        return await self._academic.terms(year_id)

    async def replace_terms(
        self,
        principal: Principal,
        *,
        year_id: UUID,
        drafts: list[TermDraft],
        context: RequestContext,
    ) -> list[Term]:
        """Set a year's terms in one go.

        A whole set rather than one at a time, because the rules that matter —
        no overlaps, numbered in date order, inside the year — are properties
        of the set. Validating one term against the others as they arrive
        one by one lets a school get halfway into an incoherent calendar.
        """
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            year = await self._academic.year(year_id)
            if year is None:
                raise NotFound("no such academic year")

            problems = validate_terms(
                drafts,
                year=YearDraft(label=year.label, starts_on=year.starts_on, ends_on=year.ends_on),
            )
            if problems:
                raise Invalid("; ".join(problems))

            existing = {term.index: term for term in await self._academic.terms(year_id)}
            written: list[Term] = []
            for draft in sorted(drafts, key=lambda d: d.index):
                label = draft.label.strip() or f"Term {draft.index}"
                term = existing.get(draft.index)
                if term is None:
                    term = await self._academic.add_term(
                        school_id=year.school_id,
                        academic_year_id=year_id,
                        index=draft.index,
                        label=label,
                        starts_on=draft.starts_on,
                        ends_on=draft.ends_on,
                    )
                else:
                    term.label = label
                    term.starts_on = draft.starts_on
                    term.ends_on = draft.ends_on
                written.append(term)

            await self._audit.record(
                school_id=year.school_id,
                action="term.replaced",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="academic_year",
                entity_id=year_id,
                note=f"{len(written)} term(s) set for {year.label}",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return written

    async def make_term_current(
        self, principal: Principal, *, term_id: UUID, context: RequestContext
    ) -> Term:
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            term = await self._academic.term(term_id)
            if term is None:
                raise NotFound("no such term")
            await self._academic.make_term_current(term_id)
            await self._audit.record(
                school_id=term.school_id,
                action="term.made_current",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="term",
                entity_id=term.id,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return term

    # ----------------------------------------------------------------------
    # The ladder
    # ----------------------------------------------------------------------
    async def ladder(self, principal: Principal, *, year_id: UUID | None = None) -> LadderView:
        """The ladder, the exit cohorts, and everything wrong with both."""
        self._require_staff(principal)

        year = (
            await self._academic.year(year_id)
            if year_id is not None
            else await self._academic.current_year()
        )
        if year_id is not None and year is None:
            raise NotFound("no such academic year")

        levels = await self._academic.levels()
        facts = await self._academic.level_facts()
        exit_ids = await self._academic.exit_level_ids(year.id) if year else frozenset()

        steps = {
            level.level_id: next_level(level=level, levels=facts, exit_level_ids=exit_ids)
            for level in facts
        }

        return LadderView(
            levels=levels,
            exit_level_ids=exit_ids,
            problems=ladder_problems(facts, exit_ids),
            summary=describe_exit_cohorts(facts, exit_ids),
            steps=steps,
        )

    async def levels(self, principal: Principal) -> list[Level]:
        self._require_staff(principal)
        return await self._academic.levels()

    async def create_level(
        self,
        principal: Principal,
        *,
        code: str,
        name: str,
        stage: LevelStage,
        order_index: int,
        entry_age: int | None,
        next_level_id: UUID | None,
        context: RequestContext,
    ) -> LadderView:
        self._require(principal, Permission.ACADEMIC_MANAGE)
        school = await self._school(principal)

        async with self._uow:
            level = await self._academic.add_level(
                school_id=school.id,
                code=code.strip().upper(),
                name=name.strip(),
                stage=stage,
                order_index=order_index,
                entry_age=entry_age,
                next_level_id=next_level_id,
            )
            await self._audit.record(
                school_id=school.id,
                action="level.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="level",
                entity_id=level.id,
                changed={"code": level.code, "name": level.name, "order": order_index},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        # The whole ladder comes back, not just the new rung: a level is only
        # meaningful against the others, and any problem the change introduced
        # is reported rather than refused. A school halfway through setting up
        # its ladder legitimately has gaps, and a 422 would force it to add
        # levels in one order only.
        return await self.ladder(principal)

    async def update_level(
        self,
        principal: Principal,
        *,
        level_id: UUID,
        changes: dict[str, Any],
        context: RequestContext,
    ) -> LadderView:
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            level = await self._academic.level(level_id)
            if level is None:
                raise NotFound("no such level")

            applied: dict[str, Any] = {}
            for field, value in changes.items():
                if value is None or not hasattr(level, field):
                    continue
                if getattr(level, field) != value:
                    applied[field] = value
                    setattr(level, field, value)

            if applied:
                await self._audit.record(
                    school_id=level.school_id,
                    action="level.updated",
                    actor_user_id=principal.user_id,
                    actor_role=principal.role.value,
                    entity_type="level",
                    entity_id=level.id,
                    changed=_stringify(applied),
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
            await self._uow.commit()

        return await self.ladder(principal)

    async def set_exit_levels(
        self,
        principal: Principal,
        *,
        year_id: UUID,
        levels: list[tuple[UUID, str | None]],
        context: RequestContext,
    ) -> LadderView:
        """Record which cohorts leave at the end of a year.

        The single most consequential setting in this module. 2027 has two,
        which is why this takes a list and replaces the whole set — and why
        the audit note spells out what was chosen rather than recording that
        "exit levels changed".
        """
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            year = await self._academic.year(year_id)
            if year is None:
                raise NotFound("no such academic year")

            known = {level.id for level in await self._academic.levels()}
            unknown = [str(level_id) for level_id, _ in levels if level_id not in known]
            if unknown:
                raise Invalid(f"these are not levels at this school: {', '.join(unknown)}")

            await self._academic.set_exit_levels(
                year_id, school_id=year.school_id, levels=levels
            )
            await self._uow.commit()

        view = await self.ladder(principal, year_id=year_id)

        async with self._uow:
            await self._audit.record(
                school_id=year.school_id,
                action="academic_year.exit_levels_set",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="academic_year",
                entity_id=year_id,
                note=view.summary,
                outcome=AuditOutcome.SUCCESS,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        return view

    # ----------------------------------------------------------------------
    # Classes
    # ----------------------------------------------------------------------
    async def streams(self, principal: Principal, *, level_id: UUID | None) -> list[Stream]:
        self._require_staff(principal)
        return await self._academic.streams(level_id)

    async def create_stream(
        self,
        principal: Principal,
        *,
        level_id: UUID,
        name: str,
        capacity: int | None,
        is_boarding: bool,
        context: RequestContext,
    ) -> Stream:
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            level = await self._academic.level(level_id)
            if level is None:
                raise NotFound("no such level")
            stream = await self._academic.add_stream(
                school_id=level.school_id,
                level_id=level_id,
                name=name.strip(),
                capacity=capacity,
                is_boarding=is_boarding,
            )
            await self._audit.record(
                school_id=level.school_id,
                action="stream.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="stream",
                entity_id=stream.id,
                changed={"level": level.name, "name": stream.name},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return stream

    async def class_groups(
        self, principal: Principal, *, year_id: UUID | None
    ) -> list[ClassGroupView]:
        """The classes, each with how many children are actually in it."""
        self._require_staff(principal)

        if year_id is None:
            current = await self._academic.current_year()
            year_id = current.id if current else None

        groups = await self._academic.class_groups(year_id)
        counts = await self._academic.enrolment_counts(year_id)
        return [ClassGroupView(group=group, pupils=counts.get(group.id, 0)) for group in groups]

    async def create_class_group(
        self,
        principal: Principal,
        *,
        academic_year_id: UUID,
        level_id: UUID,
        stream_id: UUID | None,
        class_teacher_id: UUID | None,
        room: str | None,
        capacity: int | None,
        context: RequestContext,
    ) -> ClassGroup:
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            year = await self._academic.year(academic_year_id)
            if year is None:
                raise NotFound("no such academic year")
            if year.is_closed:
                raise Invalid("that year is closed; no new classes can be added to it")

            level = await self._academic.level(level_id)
            if level is None:
                raise NotFound("no such level")

            stream = None
            if stream_id is not None:
                stream = await self._academic.stream(stream_id)
                if stream is None:
                    raise NotFound("no such stream")
                if stream.level_id != level_id:
                    raise Invalid(f"{stream.name} does not belong to {level.name}")

            if class_teacher_id is not None and not await self._academic.teacher_can_be_assigned(
                class_teacher_id
            ):
                # An id from a form is an id somebody could have typed.
                raise Invalid("that person cannot be made a class teacher")

            group = await self._academic.add_class_group(
                school_id=year.school_id,
                academic_year_id=academic_year_id,
                level_id=level_id,
                stream_id=stream_id,
                # Written by the service from one function, so a roll, an
                # invoice and an SMS all say "Standard 4 Blue".
                name=class_group_name(level.name, stream.name if stream else None),
                class_teacher_id=class_teacher_id,
                room=room,
                capacity=capacity,
            )
            await self._audit.record(
                school_id=year.school_id,
                action="class_group.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="class_group",
                entity_id=group.id,
                changed={"name": group.name, "year": year.label},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return group

    async def teaching_staff(
        self, principal: Principal, *, academic_year_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        """Who can be put in front of a class, and what they already hold."""
        self._require(principal, Permission.ACADEMIC_MANAGE)
        return await self._academic.teaching_staff(academic_year_id)

    async def update_class_group(
        self,
        principal: Principal,
        *,
        group_id: UUID,
        changes: dict[str, Any],
        context: RequestContext,
    ) -> ClassGroupView:
        """Change a class's room or how many fit in it.

        Capacity is the one that matters, because the admission form refuses a
        full class and tells the registrar to come here and open it up — which
        was, until now, advice with nowhere to act on it.

        A capacity below the current headcount is allowed. A school that moves
        thirty-seven children into a room rated for thirty-five has done that,
        and a system that refused to record it would be insisting on a tidier
        world rather than describing this one. Nothing is thrown out: the class
        simply has no places left.

        The name is not editable here. It is written from the level and the
        stream by one function, so that a roll, an invoice and an SMS cannot
        disagree about what a class is called.
        """
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            group = await self._academic.class_group(group_id)
            if group is None:
                raise NotFound("no such class")

            year = await self._academic.year(group.academic_year_id)
            if year is not None and year.is_closed:
                raise Invalid("that year is closed; its classes are fixed")

            before = {"room": group.room, "capacity": group.capacity}
            if "room" in changes:
                group.room = changes["room"]
            if "capacity" in changes:
                group.capacity = changes["capacity"]

            await self._audit.record(
                school_id=group.school_id,
                action="class_group.updated",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="class_group",
                entity_id=group.id,
                changed={
                    "from": {k: v for k, v in before.items() if k in changes},
                    "to": {k: changes[k] for k in changes},
                },
                note=f"{group.name}",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        counts = await self._academic.enrolment_counts(group.academic_year_id)
        return ClassGroupView(group=group, pupils=counts.get(group.id, 0))

    async def assign_class_teacher(
        self,
        principal: Principal,
        *,
        group_id: UUID,
        teacher_id: UUID | None,
        context: RequestContext,
    ) -> ClassGroup:
        """Put a teacher in front of a class, or take them off it.

        Audited because it widens what somebody can see: a class teacher may
        read every child in their class, so this is an authorisation change as
        much as an administrative one.
        """
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            group = await self._academic.class_group(group_id)
            if group is None:
                raise NotFound("no such class")
            if teacher_id is not None and not await self._academic.teacher_can_be_assigned(
                teacher_id
            ):
                raise Invalid("that person cannot be made a class teacher")

            before = group.class_teacher_id
            group.class_teacher_id = teacher_id
            await self._audit.record(
                school_id=group.school_id,
                action="class_group.class_teacher_set",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="class_group",
                entity_id=group.id,
                changed={"from": str(before) if before else None,
                         "to": str(teacher_id) if teacher_id else None},
                note=f"{group.name}",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return group

    # ----------------------------------------------------------------------
    # Subjects and who teaches them
    # ----------------------------------------------------------------------
    async def subjects(self, principal: Principal) -> list[Subject]:
        self._require_staff(principal)
        return await self._academic.subjects()

    async def create_subject(
        self,
        principal: Principal,
        *,
        code: str,
        name: str,
        is_core: bool,
        is_examinable: bool,
        levels: list[tuple[UUID, int | None]],
        context: RequestContext,
    ) -> Subject:
        """One subject, and the levels it is taught at.

        Kiswahili at Standard 2 and at Form 3 are the same subject against a
        different syllabus, so the periods per week belong to the pairing
        rather than to the subject.
        """
        self._require(principal, Permission.ACADEMIC_MANAGE)
        school = await self._school(principal)

        async with self._uow:
            subject = await self._academic.add_subject(
                school_id=school.id,
                code=code.strip().upper(),
                name=name.strip(),
                is_core=is_core,
                is_examinable=is_examinable,
            )
            if levels:
                known = {level.id for level in await self._academic.levels()}
                unknown = [str(lid) for lid, _ in levels if lid not in known]
                if unknown:
                    raise Invalid(f"these are not levels at this school: {', '.join(unknown)}")
                await self._academic.set_subject_levels(
                    subject.id, school_id=school.id, levels=levels
                )
            await self._audit.record(
                school_id=school.id,
                action="subject.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="subject",
                entity_id=subject.id,
                changed={"code": subject.code, "name": subject.name},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return subject

    async def teaching_assignments(
        self, principal: Principal, *, class_group_id: UUID | None, teacher_id: UUID | None
    ) -> list[TeachingAssignment]:
        self._require_staff(principal)
        return await self._academic.teaching_assignments(
            class_group_id=class_group_id, teacher_id=teacher_id
        )

    async def assign_teaching(
        self,
        principal: Principal,
        *,
        teacher_id: UUID,
        subject_id: UUID,
        class_group_id: UUID,
        context: RequestContext,
    ) -> TeachingAssignment:
        """Give a teacher a subject in a class.

        One teacher per subject per class, enforced by a unique index. Co-teaching
        exists, but modelling it here would mean two people able to enter the same
        mark and nobody able to say whose it was.
        """
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            group = await self._academic.class_group(class_group_id)
            if group is None:
                raise NotFound("no such class")
            subject = await self._academic.subject(subject_id)
            if subject is None:
                raise NotFound("no such subject")
            if not await self._academic.teacher_can_be_assigned(teacher_id):
                raise Invalid("that person cannot be assigned to teach")

            # Checked before the insert so the user reads a sentence rather
            # than a constraint violation. The unique index is still the thing
            # that guarantees it under a race.
            existing = await self._academic.teaching_assignments(
                class_group_id=class_group_id
            )
            if any(a.subject_id == subject_id for a in existing):
                raise Invalid(f"{subject.name} already has a teacher in {group.name}")

            assignment = await self._academic.add_teaching_assignment(
                school_id=group.school_id,
                teacher_id=teacher_id,
                subject_id=subject_id,
                class_group_id=class_group_id,
            )
            await self._audit.record(
                school_id=group.school_id,
                action="teaching_assignment.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="teaching_assignment",
                entity_id=assignment.id,
                note=f"{subject.name} in {group.name}",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return assignment

    async def remove_teaching(
        self, principal: Principal, *, assignment_id: UUID, context: RequestContext
    ) -> None:
        self._require(principal, Permission.ACADEMIC_MANAGE)

        async with self._uow:
            removed = await self._academic.remove_teaching_assignment(assignment_id)
            if not removed:
                raise NotFound("no such teaching assignment")
            await self._audit.record(
                school_id=None if principal.school_id is None else principal.school_id,
                action="teaching_assignment.removed",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="teaching_assignment",
                entity_id=assignment_id,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()


def _stringify(changed: dict[str, Any]) -> dict[str, Any]:
    """Make a change set safe to store as JSON.

    Dates and UUIDs are the two that turn up constantly and are not JSON on
    their own, and an audit write that fails on serialisation would take the
    whole operation down with it.
    """
    return {
        key: (value if isinstance(value, (str, int, float, bool, type(None))) else str(value))
        for key, value in changed.items()
    }
