"""Students, enrolments, guardians, and the detail a school collects.

The service that decides who may see a child. Three rules are worth reading
before changing anything here:

**A teacher sees their own classes.** Not the whole school. A Standard 2
teacher has no business reading a Form 2 pupil's file, and "every member of
staff can see everything" is the shape of leak that ends up in a newspaper.

**A guardian sees their own children, and that is a question about rows.** It
cannot be answered from a token, so it is answered with a query, every time.

**Health is separate.** `health:read` is its own capability because children's
health data is sensitive personal data under the Personal Data Protection Act,
2022. Reading it is audited; the rest of a child's record is not, because an
audit log that records every page view is one nobody can search.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.enrolment import EnrolmentOutcome, StudentStatus, admission_number
from app.domain.registration import (
    Checklist,
    ChecklistSubject,
    FieldScope,
    FieldValueRejected,
    Residency,
    build_checklist,
    coerce_field_value,
)
from app.domain.roles import Permission, Principal, Role, can, may_see_student
from app.models.audit import AuditOutcome
from app.models.student import Enrolment, Student, StudentDocument, StudentHealth
from app.repositories.audit import AuditRepository
from app.repositories.base import UnitOfWork
from app.repositories.guardians import GuardianRepository
from app.repositories.schools import SchoolRepository, SqlRegistrationSettingsRepository
from app.repositories.students import RollFilters, RollRow, StudentRepository
from app.services.auth import RequestContext


class StudentError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotAllowed(StudentError):
    """The principal lacks the capability, or the child is not theirs to see."""


class NotFound(StudentError):
    """No such record in this school. A 404 rather than a 403 where the record
    belongs to somebody else's family: whether that child exists is not
    something the asker needs to learn."""


class Invalid(StudentError):
    """The change does not make sense. The reason is for the user to read."""


@dataclass(frozen=True, slots=True)
class StudentProfile:
    """Everything the profile screen draws, in one answer.

    Assembled server-side because the alternative is six round trips from a
    phone on 3G, and because the fee totals have to agree with the invoice
    screen — which they only do if one place computes them.
    """

    student: Student
    enrolment: Enrolment | None
    class_name: str | None
    history: list[dict[str, Any]]
    guardians: list[dict[str, Any]]
    fees: dict[str, Decimal]
    field_values: dict[str, Any]
    #: Only populated for a caller who holds health:read.
    health: StudentHealth | None
    emergency_contacts: list[Any]


class StudentService:
    def __init__(
        self,
        uow: UnitOfWork,
        students: StudentRepository,
        guardians: GuardianRepository,
        schools: SchoolRepository,
        settings_repo: SqlRegistrationSettingsRepository,
        audit: AuditRepository,
    ) -> None:
        self._uow = uow
        self._students = students
        self._guardians = guardians
        self._schools = schools
        self._settings = settings_repo
        self._audit = audit

    # ----------------------------------------------------------------------
    # Guards
    # ----------------------------------------------------------------------
    def _require(self, principal: Principal, permission: Permission) -> None:
        if not can(principal, permission):
            raise NotAllowed(f"this account may not {permission.value.replace(':', ' ')}")

    async def _with_class_scope(self, principal: Principal) -> Principal:
        """Fill in a teacher's classes.

        They are not in the token on purpose — see
        `teacher_class_group_ids` — so the scope is resolved here, once per
        request, and only for the two roles that have one.
        """
        if principal.role not in (Role.CLASS_TEACHER, Role.SUBJECT_TEACHER):
            return principal
        if principal.class_group_ids:
            return principal
        return replace(
            principal,
            class_group_ids=await self._students.teacher_class_group_ids(principal.user_id),
        )

    async def _require_can_see(self, principal: Principal, student_id: UUID) -> Student:
        """The narrowest and most-asked question in the system.

        Order matters. The student is fetched first so that a child in another
        school is a 404 before any permission is considered — row-level
        security has already made them invisible, and saying "forbidden" would
        confirm they exist.
        """
        student = await self._students.get(student_id)
        if student is None:
            raise NotFound("no such student")

        principal = await self._with_class_scope(principal)

        if principal.role is Role.GUARDIAN:
            if principal.guardian_id is None:
                raise NotAllowed("this account is not linked to a guardian record")
            children = await self._guardians.student_ids_for(principal.guardian_id)
            if student_id not in children:
                # Deliberately a 404: a guardian probing ids should not be able
                # to tell a child who exists from one who does not.
                raise NotFound("no such student")
            return student

        self._require(principal, Permission.STUDENT_READ)

        enrolment = await self._students.active_enrolment(student_id)
        class_group_id = enrolment.class_group_id if enrolment else None
        if class_group_id is not None and not may_see_student(
            principal, student_id=student_id, class_group_id=class_group_id
        ):
            raise NotAllowed("this child is not in one of your classes")
        return student

    # ----------------------------------------------------------------------
    # The roll
    # ----------------------------------------------------------------------
    async def roll(
        self,
        principal: Principal,
        *,
        limit: int,
        offset: int,
        q: str | None = None,
        class_group_id: UUID | None = None,
        level_id: UUID | None = None,
        academic_year_id: UUID | None = None,
        status: str | None = None,
        residency: Residency | None = None,
        uses_transport: bool | None = None,
        in_arrears: bool | None = None,
    ) -> tuple[list[RollRow], int]:
        """The students screen.

        A teacher's view is narrowed to their own classes here rather than
        being left to the client: a list endpoint that returns everything and
        trusts the caller to filter is a list endpoint that leaks.
        """
        self._require(principal, Permission.STUDENT_READ)
        principal = await self._with_class_scope(principal)

        filters = RollFilters(
            q=q,
            class_group_id=class_group_id,
            level_id=level_id,
            academic_year_id=academic_year_id,
            status=status,
            residency=residency,
            uses_transport=uses_transport,
            in_arrears=in_arrears,
        )

        if principal.role in (Role.CLASS_TEACHER, Role.SUBJECT_TEACHER):
            if not principal.class_group_ids:
                return [], 0
            if filters.class_group_id is None:
                # No class asked for: show the first of theirs rather than the
                # school. Choosing silently is better than returning nothing,
                # and the screen names the class it is showing.
                filters = replace(filters, class_group_id=next(iter(principal.class_group_ids)))
            elif filters.class_group_id not in principal.class_group_ids:
                raise NotAllowed("that class is not one of yours")

        return await self._students.roll(filters, limit=limit, offset=offset)

    async def profile(self, principal: Principal, *, student_id: UUID) -> StudentProfile:
        student = await self._require_can_see(principal, student_id)

        enrolment = await self._students.active_enrolment(student_id)
        history = await self._students.enrolments(student_id)
        guardians = await self._guardians.guardians_of(student_id)
        fees = await self._students.fee_summary(student_id)
        values = await self._students.field_values(student_id=student_id, enrolment_id=None)

        health = None
        contacts: list[Any] = []
        if can(principal, Permission.HEALTH_READ):
            health = await self._students.health(student_id)
            contacts = await self._students.emergency_contacts(student_id)

        class_name = next(
            (
                row["class_name"]
                for row in history
                if row["id"] == (enrolment.id if enrolment else None)
            ),
            None,
        )

        return StudentProfile(
            student=student,
            enrolment=enrolment,
            class_name=class_name,
            history=history,
            guardians=guardians,
            fees=fees,
            field_values=values,
            health=health,
            emergency_contacts=contacts,
        )

    # ----------------------------------------------------------------------
    # Writing a child's record
    # ----------------------------------------------------------------------
    async def create_student(
        self,
        principal: Principal,
        *,
        details: dict[str, Any],
        context: RequestContext,
    ) -> Student:
        """Admit a child by hand.

        The admissions funnel is the ordinary path; this is the registrar
        typing in a pupil who arrived with a transfer letter. The admission
        number is issued from a locked sequence, not counted from the roll, so
        two registrars admitting at once cannot mint the same one.
        """
        self._require(principal, Permission.STUDENT_MANAGE)

        async with self._uow:
            if principal.school_id is None:
                raise NotFound("this account belongs to no school")
            school = await self._schools.get(principal.school_id)
            if school is None:
                raise NotFound("no such school")

            year = details.pop("admission_year", None) or date.today().year
            sequence = await self._students.next_admission_sequence(
                school_id=school.id, scope=str(year)
            )
            try:
                admission_no = admission_number(
                    school.admission_no_format, year=year, sequence=sequence
                )
            except ValueError as exc:
                raise Invalid(str(exc)) from exc

            student = await self._students.add(
                school_id=school.id,
                admission_no=admission_no,
                status=StudentStatus.PROSPECTIVE,
                **details,
            )
            await self._audit.record(
                school_id=school.id,
                action="student.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student.id,
                changed={"admission_no": admission_no},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return student

    async def update_student(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        changes: dict[str, Any],
        context: RequestContext,
    ) -> Student:
        self._require(principal, Permission.STUDENT_MANAGE)

        async with self._uow:
            student = await self._students.get(student_id)
            if student is None:
                raise NotFound("no such student")

            applied: dict[str, Any] = {}
            for field, value in changes.items():
                if not hasattr(student, field):
                    continue
                if getattr(student, field) != value:
                    applied[field] = value
                    setattr(student, field, value)

            if applied:
                await self._audit.record(
                    school_id=student.school_id,
                    action="student.updated",
                    actor_user_id=principal.user_id,
                    actor_role=principal.role.value,
                    entity_type="student",
                    entity_id=student.id,
                    changed={k: str(v) for k, v in applied.items()},
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
            await self._uow.commit()
            return student

    async def transfer_out(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        reason: str,
        on: date | None,
        context: RequestContext,
    ) -> Student:
        """Close a child's enrolment and mark them gone.

        The enrolment is closed rather than deleted, and the student row stays:
        a school asked for a transfer letter two years later has to be able to
        answer, and arrears do not disappear because a child left.
        """
        self._require(principal, Permission.STUDENT_MANAGE)

        async with self._uow:
            student = await self._students.get(student_id)
            if student is None:
                raise NotFound("no such student")

            enrolment = await self._students.active_enrolment(student_id)
            if enrolment is not None:
                await self._students.close_enrolment(
                    enrolment.id,
                    outcome=EnrolmentOutcome.TRANSFERRED_OUT.value,
                    exited_on=on or date.today(),
                    reason=reason,
                )
            student.status = StudentStatus.TRANSFERRED_OUT

            await self._audit.record(
                school_id=student.school_id,
                action="student.transferred_out",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student.id,
                note=reason,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return student

    # ----------------------------------------------------------------------
    # Enrolments
    # ----------------------------------------------------------------------
    async def enrol(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        academic_year_id: UUID,
        class_group_id: UUID,
        residency: Residency,
        enrolled_on: date | None,
        context: RequestContext,
    ) -> Enrolment:
        """Put a child in a class for a year.

        The database allows one active enrolment per child, so this closes
        nothing and assumes nothing: if the child is already enrolled, the
        caller is told rather than having last year's row quietly rewritten.
        """
        self._require(principal, Permission.STUDENT_MANAGE)

        async with self._uow:
            student = await self._students.get(student_id)
            if student is None:
                raise NotFound("no such student")

            existing = await self._students.active_enrolment(student_id)
            if existing is not None:
                raise Invalid(
                    "this child already has an open enrolment; close it first, "
                    "or let a promotion run move them"
                )

            enrolment = await self._students.add_enrolment(
                school_id=student.school_id,
                student_id=student_id,
                academic_year_id=academic_year_id,
                class_group_id=class_group_id,
                outcome=EnrolmentOutcome.ACTIVE,
                residency=residency,
                enrolled_on=enrolled_on or date.today(),
            )
            student.status = StudentStatus.ACTIVE

            await self._audit.record(
                school_id=student.school_id,
                action="enrolment.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="enrolment",
                entity_id=enrolment.id,
                changed={"student": str(student_id), "class_group": str(class_group_id)},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return enrolment

    async def update_enrolment(
        self,
        principal: Principal,
        *,
        enrolment_id: UUID,
        changes: dict[str, Any],
        context: RequestContext,
    ) -> Enrolment:
        """Change this year's facts: class, boarding, bus.

        All three are per-year by design. A child who moves from Blue to Green
        in March, starts boarding in May and leaves the bus in September has
        one enrolment row that tells the truth about this year, and last
        year's row is untouched.
        """
        self._require(principal, Permission.STUDENT_MANAGE)

        async with self._uow:
            enrolment = await self._students.enrolment(enrolment_id)
            if enrolment is None:
                raise NotFound("no such enrolment")
            if enrolment.outcome is not EnrolmentOutcome.ACTIVE:
                raise Invalid("that enrolment is closed; a closed year is not edited")

            applied: dict[str, Any] = {}
            for field, value in changes.items():
                if not hasattr(enrolment, field):
                    continue
                if getattr(enrolment, field) != value:
                    applied[field] = value
                    setattr(enrolment, field, value)

            if applied:
                await self._audit.record(
                    school_id=enrolment.school_id,
                    action="enrolment.updated",
                    actor_user_id=principal.user_id,
                    actor_role=principal.role.value,
                    entity_type="enrolment",
                    entity_id=enrolment.id,
                    changed={k: str(v) for k, v in applied.items()},
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
            await self._uow.commit()
            return enrolment

    # ----------------------------------------------------------------------
    # Guardians
    # ----------------------------------------------------------------------
    async def guardians_of(self, principal: Principal, *, student_id: UUID) -> list[dict[str, Any]]:
        await self._require_can_see(principal, student_id)
        return await self._guardians.guardians_of(student_id)

    async def link_guardian(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        guardian: dict[str, Any],
        link: dict[str, Any],
        context: RequestContext,
    ) -> dict[str, Any]:
        """Attach a guardian to a child, reusing the person if they are known.

        Matched on phone, which is what makes a parent with three children one
        guardian and three links. Without this, a family gets three guardian
        rows, three fee reminders for one household, and no combined balance.
        """
        self._require(principal, Permission.GUARDIAN_MANAGE)

        async with self._uow:
            student = await self._students.get(student_id)
            if student is None:
                raise NotFound("no such student")

            phone = guardian.get("phone")
            if not phone:
                raise Invalid("a guardian needs a phone number")

            existing = await self._guardians.by_phone(phone)
            if existing is None:
                existing = await self._guardians.add(school_id=student.school_id, **guardian)
            else:
                # Fill in anything the earlier record was missing rather than
                # overwriting: the first child's registration may have had
                # less detail than this one.
                for key, value in guardian.items():
                    if value and not getattr(existing, key, None):
                        setattr(existing, key, value)

            already = await self._guardians.get_link(guardian_id=existing.id, student_id=student_id)
            if already is not None:
                raise Invalid(f"{existing.full_name} is already linked to this child")

            if link.get("is_primary"):
                await self._guardians.demote_other_primaries(student_id, keep=None)

            await self._guardians.link(
                school_id=student.school_id,
                guardian_id=existing.id,
                student_id=student_id,
                **link,
            )
            await self._audit.record(
                school_id=student.school_id,
                action="guardian.linked",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student_id,
                changed={"guardian": existing.full_name, "phone": existing.phone},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        return {"guardian_id": existing.id, "full_name": existing.full_name}

    async def unlink_guardian(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        guardian_id: UUID,
        context: RequestContext,
    ) -> None:
        """Detach a guardian, unless they are the only one.

        A child with nobody to contact is the one state this must not be able
        to produce.
        """
        self._require(principal, Permission.GUARDIAN_MANAGE)

        async with self._uow:
            links = await self._guardians.guardians_of(student_id)
            if not any(row["guardian_id"] == guardian_id for row in links):
                raise NotFound("that guardian is not linked to this child")
            if len(links) == 1:
                raise Invalid("that is the only guardian on this child; link another one first")

            link = await self._guardians.get_link(guardian_id=guardian_id, student_id=student_id)
            if link is not None:
                await self._guardians.unlink(link.id)

            await self._audit.record(
                school_id=principal.school_id,
                action="guardian.unlinked",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student_id,
                changed={"guardian": str(guardian_id)},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

    async def search_guardians(
        self, principal: Principal, *, q: str | None, limit: int, offset: int
    ) -> tuple[list[Any], int]:
        """Find a guardian by name or phone.

        What a bursar has in front of them when a payment arrives with no
        reference: a number, and a family to match it to.
        """
        self._require(principal, Permission.STUDENT_READ)
        return await self._guardians.search(q, limit=limit, offset=offset)

    async def my_children(self, principal: Principal) -> list[Any]:
        """The parent portal's landing screen.

        Scoped by the guardian on the token and nothing else, so there is no
        id for a caller to change.
        """
        if principal.role is not Role.GUARDIAN or principal.guardian_id is None:
            raise NotAllowed("this is for a guardian's own account")
        return await self._guardians.children(principal.guardian_id)

    # ----------------------------------------------------------------------
    # Health
    # ----------------------------------------------------------------------
    async def health(
        self, principal: Principal, *, student_id: UUID, context: RequestContext
    ) -> StudentHealth | None:
        """Read a child's medical detail, and record that it was read.

        The one read in this service that is audited. Under the Act this is
        sensitive personal data, and "who looked at this child's health record"
        is a question a school may have to answer.
        """
        self._require(principal, Permission.HEALTH_READ)
        await self._require_can_see(principal, student_id)

        record = await self._students.health(student_id)

        async with self._uow:
            await self._audit.record(
                school_id=principal.school_id,
                action="health.read",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student_id,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        return record

    async def set_health(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        values: dict[str, Any],
        context: RequestContext,
    ) -> StudentHealth:
        self._require(principal, Permission.HEALTH_MANAGE)

        async with self._uow:
            student = await self._students.get(student_id)
            if student is None:
                raise NotFound("no such student")

            record = await self._students.upsert_health(
                student_id,
                school_id=student.school_id,
                updated_by_id=principal.user_id,
                **values,
            )
            await self._audit.record(
                school_id=student.school_id,
                action="health.updated",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student_id,
                # The fields that changed, never the values: an audit row
                # holding a child's diagnosis duplicates the sensitive data it
                # exists to protect.
                changed={"fields": sorted(values)},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return record

    # ----------------------------------------------------------------------
    # Emergency contacts
    # ----------------------------------------------------------------------
    async def emergency_contacts(self, principal: Principal, *, student_id: UUID) -> list[Any]:
        self._require(principal, Permission.STUDENT_READ)
        await self._require_can_see(principal, student_id)
        return await self._students.emergency_contacts(student_id)

    async def set_emergency_contacts(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        contacts: list[dict[str, Any]],
        context: RequestContext,
    ) -> list[Any]:
        self._require(principal, Permission.STUDENT_MANAGE)

        if not contacts:
            raise Invalid("a child needs at least one emergency contact")
        priorities = [contact["priority"] for contact in contacts]
        if len(set(priorities)) != len(priorities):
            raise Invalid("two contacts share the same priority")
        if 1 not in priorities:
            raise Invalid("one contact has to be first to call")

        async with self._uow:
            student = await self._students.get(student_id)
            if student is None:
                raise NotFound("no such student")

            written = await self._students.replace_emergency_contacts(
                student_id, school_id=student.school_id, contacts=contacts
            )
            await self._audit.record(
                school_id=student.school_id,
                action="emergency_contact.replaced",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student_id,
                note=f"{len(written)} contact(s)",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return written

    # ----------------------------------------------------------------------
    # Documents
    # ----------------------------------------------------------------------
    async def documents(self, principal: Principal, *, student_id: UUID) -> list[StudentDocument]:
        self._require(principal, Permission.STUDENT_READ)
        await self._require_can_see(principal, student_id)
        return await self._students.documents(student_id)

    async def add_document(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        document: dict[str, Any],
        context: RequestContext,
    ) -> StudentDocument:
        self._require(principal, Permission.STUDENT_MANAGE)

        async with self._uow:
            student = await self._students.get(student_id)
            if student is None:
                raise NotFound("no such student")

            row = await self._students.add_document(
                school_id=student.school_id,
                student_id=student_id,
                uploaded_by_id=principal.user_id,
                **document,
            )
            await self._audit.record(
                school_id=student.school_id,
                action="document.added",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student_id,
                changed={"kind": str(row.kind)},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return row

    # ----------------------------------------------------------------------
    # The checklist and the school's own fields
    # ----------------------------------------------------------------------
    async def checklist(self, principal: Principal, *, student_id: UUID) -> Checklist:
        """What is still missing from this child's record.

        The same function the admissions queue uses, so "complete" means one
        thing across the product rather than three.
        """
        student = await self._require_can_see(principal, student_id)
        enrolment = await self._students.active_enrolment(student_id)

        documents = await self._students.document_kinds_held(student_id)
        values = await self._students.field_values(student_id=student_id, enrolment_id=None)

        level_id = None
        if enrolment is not None:
            history = await self._students.enrolments(student_id)
            level_id = next(
                (row.get("level_id") for row in history if row["id"] == enrolment.id), None
            )

        subject = ChecklistSubject(
            core={
                "first_name": student.first_name,
                "surname": student.surname,
                "date_of_birth": student.date_of_birth,
                "sex": student.sex,
            },
            documents=documents,
            field_values=frozenset(k for k, v in values.items() if v is not None),
            residency=Residency(enrolment.residency) if enrolment else Residency.DAY,
            level_id=level_id,
            has_guardian=await self._students.has_guardian(student_id),
            has_emergency_contact=await self._students.has_emergency_contact(student_id),
        )

        return build_checklist(
            subject=subject,
            requirements=await self._settings.requirement_facts(),
            fields=await self._settings.field_definitions(scope=FieldScope.STUDENT),
        )

    async def set_field_values(
        self,
        principal: Principal,
        *,
        student_id: UUID,
        values: dict[str, Any],
        context: RequestContext,
    ) -> dict[str, Any]:
        """Write a school's own fields for one child.

        Each value is checked against its field's declared kind before it is
        stored, which is the difference between a bounded custom field and a
        free-text dump nobody can report on.
        """
        self._require(principal, Permission.STUDENT_MANAGE)

        async with self._uow:
            student = await self._students.get(student_id)
            if student is None:
                raise NotFound("no such student")

            definitions = {
                definition.key: definition
                for definition in await self._settings.field_definitions(scope=FieldScope.STUDENT)
            }

            unknown = sorted(set(values) - set(definitions))
            if unknown:
                raise Invalid(f"this school has no field called {', '.join(unknown)}")

            for key, raw in values.items():
                definition = definitions[key]
                if not definition.is_active:
                    raise Invalid(f"{definition.label} is no longer in use")
                try:
                    coerced = coerce_field_value(definition, raw)
                except FieldValueRejected as exc:
                    raise Invalid(str(exc)) from exc

                await self._students.set_field_value(
                    school_id=student.school_id,
                    field_id=definition.field_id,
                    student_id=student_id,
                    enrolment_id=None,
                    value=coerced,
                    set_by_id=principal.user_id,
                )

            await self._audit.record(
                school_id=student.school_id,
                action="student.fields_updated",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student_id,
                changed={"fields": sorted(values)},
                outcome=AuditOutcome.SUCCESS,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        return await self._students.field_values(student_id=student_id, enrolment_id=None)


def _now() -> datetime:
    return datetime.now(UTC)
