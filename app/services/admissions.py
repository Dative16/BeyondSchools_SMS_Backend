"""The admissions funnel.

Six states, and the two that matter most are the ones nobody presses a button
for. **Acceptance is a deposit arriving**, not a registrar's decision, so there
is no accept button to press by mistake and no way for an unpaid place to look
accepted in the funnel. **Enrolment is one transaction** that creates a child,
a guardian, an enrolment and a login together — getting halfway through that is
the failure that leaves a school with a pupil nobody can find.

The public half of this service runs for somebody with no account at all. A
verified phone number is the whole credential, which is why every method that
a parent can reach takes the application from the token rather than from the
request: there is no id for them to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.admissions import (
    ApplicationDraft,
    ApplicationState,
    CandidateStudent,
    DuplicateMatch,
    TransitionRejected,
    can_accept,
    duplicate_score,
    likely_duplicates,
    missing_to_submit,
    normalise_phone,
    offer_expiry,
    readiness_to_enrol,
    transition,
)
from app.domain.admissions import (
    reference as build_reference,
)
from app.domain.enrolment import EnrolmentOutcome, StudentStatus, admission_number
from app.domain.registration import (
    Checklist,
    ChecklistSubject,
    FieldScope,
    Residency,
    build_checklist,
)
from app.domain.roles import Permission, Principal, Role, can
from app.models.audit import AuditOutcome
from app.models.guardian import Relationship
from app.repositories.admissions import AdmissionsRepository, QueueRow
from app.repositories.audit import AuditRepository
from app.repositories.base import UnitOfWork
from app.repositories.guardians import GuardianRepository
from app.repositories.schools import SchoolRepository, SqlRegistrationSettingsRepository
from app.repositories.students import StudentRepository
from app.repositories.users import UserRepository
from app.services.auth import RequestContext
from app.services.invoicing import InvoicingService


class AdmissionsError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotAllowed(AdmissionsError):
    """The principal lacks the capability, or the application is not theirs."""


class NotFound(AdmissionsError):
    """No such application in this school."""


class Invalid(AdmissionsError):
    """The move does not make sense, or something is missing."""


@dataclass(frozen=True, slots=True)
class ApplicationView:
    """One application, as both the parent's form and the registrar's screen
    need it."""

    application: Any
    level_name: str | None
    documents: list[dict[str, Any]]
    field_values: dict[str, Any]
    checklist: Checklist
    duplicates: list[DuplicateMatch]
    deposit_billed: Decimal | None
    deposit_settled: Decimal | None


@dataclass(frozen=True, slots=True)
class EnrolledOutcome:
    student_id: UUID
    enrolment_id: UUID
    admission_no: str
    guardian_id: UUID


class AdmissionsService:
    def __init__(
        self,
        uow: UnitOfWork,
        admissions: AdmissionsRepository,
        students: StudentRepository,
        guardians: GuardianRepository,
        schools: SchoolRepository,
        settings_repo: SqlRegistrationSettingsRepository,
        invoicing: InvoicingService,
        users: UserRepository,
        audit: AuditRepository,
    ) -> None:
        self._uow = uow
        self._admissions = admissions
        self._students = students
        self._guardians = guardians
        self._schools = schools
        self._settings = settings_repo
        self._invoicing = invoicing
        self._users = users
        self._audit = audit

    # ----------------------------------------------------------------------
    # Guards
    # ----------------------------------------------------------------------
    def _require(self, principal: Principal, permission: Permission) -> None:
        if not can(principal, permission):
            raise NotAllowed(f"this account may not {permission.value.replace(':', ' ')}")

    async def _own_application(self, principal: Principal) -> Any:
        """The application an applicant's session is for, and only that one.

        Taken from the token rather than the request, so there is no id for a
        parent to change and no need to check that they own it.
        """
        if principal.role is not Role.APPLICANT or principal.application_id is None:
            raise NotAllowed("this is for an applicant's own form")
        application = await self._admissions.get(principal.application_id)
        if application is None:
            raise NotFound("no such application")
        return application

    # ----------------------------------------------------------------------
    # Starting and resuming
    # ----------------------------------------------------------------------
    async def start_or_resume(
        self,
        *,
        school_id: UUID,
        phone: str,
        academic_year_id: UUID | None,
        context: RequestContext,
    ) -> Any:
        """Called once a phone number has been verified by one-time code.

        Resumes rather than starting afresh where an open application already
        exists for that number: a form that takes more than about four minutes
        on a phone does not get finished, so a parent who lost the page at
        question nine comes back to question nine.
        """
        phone = normalise_phone(phone)

        async with self._uow:
            existing = await self._admissions.by_phone(phone, open_only=True)
            if existing is not None:
                existing.phone_verified_at = datetime.now(UTC)
                await self._audit.record(
                    school_id=school_id,
                    action="application.resumed",
                    entity_type="application",
                    entity_id=existing.id,
                    note=f"{phone} came back to {existing.reference}",
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
                await self._uow.commit()
                return existing

            year = date.today().year
            sequence = await self._schools.next_sequence(
                school_id=school_id, name="application_ref", scope=str(year)
            )
            application = await self._admissions.add(
                school_id=school_id,
                reference=build_reference(year=year, sequence=sequence),
                state=ApplicationState.STARTED,
                phone=phone,
                phone_verified_at=datetime.now(UTC),
                academic_year_id=academic_year_id,
                source="public_form",
            )
            await self._audit.record(
                school_id=school_id,
                action="application.started",
                entity_type="application",
                entity_id=application.id,
                note=f"{phone} started {application.reference}",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return application

    async def save_progress(
        self, principal: Principal, *, changes: dict[str, Any], context: RequestContext
    ) -> Any:
        """Save a partly-filled form.

        Every field is optional and nothing is validated beyond its own type.
        A half-finished application is a legal row, not an error — that is what
        lets a parent stop at question nine and come back by code.
        """
        async with self._uow:
            application = await self._own_application(principal)
            if application.state is not ApplicationState.STARTED:
                raise Invalid(
                    "this application has been submitted; the school is reviewing it"
                )

            if "guardian_phone" in changes and changes["guardian_phone"]:
                changes["guardian_phone"] = normalise_phone(changes["guardian_phone"])

            for field, value in changes.items():
                if hasattr(application, field):
                    setattr(application, field, value)

            await self._uow.commit()
            return application

    async def submit(self, principal: Principal, *, context: RequestContext) -> Any:
        """Hand the form to the school.

        What is required is the shortest honest list: a name, a date of birth,
        a sex, a level and a guardian who can be reached. Everything else a
        school wants is collected at enrolment, by somebody at a desk.
        """
        async with self._uow:
            application = await self._own_application(principal)

            draft = ApplicationDraft(
                phone_verified=application.phone_verified_at is not None,
                child_first_name=application.child_first_name,
                child_surname=application.child_surname,
                date_of_birth=application.date_of_birth,
                sex=application.sex,
                level_applied_id=application.level_applied_id,
                guardian_name=application.guardian_name,
                guardian_phone=application.guardian_phone,
                guardian_relationship=application.guardian_relationship,
            )
            missing = missing_to_submit(draft)
            if missing:
                raise Invalid(f"still needed: {', '.join(missing)}")

            try:
                application.state = transition(
                    application.state, ApplicationState.SUBMITTED
                )
            except TransitionRejected as exc:
                raise Invalid(str(exc)) from exc
            application.submitted_at = datetime.now(UTC)

            # Screened on the way in, so the registrar opens a queue that
            # already says which ones look like children they have.
            await self._screen(application)

            await self._audit.record(
                school_id=application.school_id,
                action="application.submitted",
                entity_type="application",
                entity_id=application.id,
                note=application.reference,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return application

    # ----------------------------------------------------------------------
    # Screening
    # ----------------------------------------------------------------------
    async def _screen(self, application: Any) -> list[DuplicateMatch]:
        """Look for a child the school already has.

        Name plus date of birth plus guardian phone, scored rather than
        matched, and never acted on automatically. The commonest high score is
        a sibling — one phone, several children, all of them real — so the
        registrar accepts or merges and the system only ever flags.
        """
        candidates: list[CandidateStudent] = await self._admissions.duplicate_candidates(
            surname=application.child_surname,
            date_of_birth=application.date_of_birth,
            guardian_phone=application.guardian_phone,
        )
        if not candidates:
            return []

        matches = [
            match
            for candidate in candidates
            if (
                match := duplicate_score(
                    first_name=application.child_first_name or "",
                    surname=application.child_surname or "",
                    date_of_birth=application.date_of_birth,
                    guardian_phone=application.guardian_phone,
                    candidate=candidate,
                )
            )
            is not None
        ]
        ranked = sorted(matches, key=lambda m: m.score, reverse=True)
        flagged = likely_duplicates(matches)

        if ranked:
            best = ranked[0]
            application.duplicate_of_student_id = best.student_id
            application.duplicate_score = best.score
            application.duplicate_reason = best.reason
            if not flagged:
                # Worth recording and not worth blocking: a shared phone on
                # its own is a sibling, and saying so stops a registrar
                # wondering why the flag is there.
                application.screening_note = (
                    "Possible match found but below the duplicate threshold — "
                    "most likely a sibling."
                )
        return ranked

    async def rescreen(
        self, principal: Principal, *, application_id: UUID, context: RequestContext
    ) -> list[DuplicateMatch]:
        self._require(principal, Permission.ADMISSION_REVIEW)

        async with self._uow:
            application = await self._admissions.get(application_id)
            if application is None:
                raise NotFound("no such application")
            matches = await self._screen(application)
            await self._uow.commit()
            return matches

    async def screened(
        self,
        principal: Principal,
        *,
        application_id: UUID,
        note: str | None,
        context: RequestContext,
    ) -> Any:
        """A registrar has looked at it and it is not a duplicate.

        An explicit step rather than an implied one, because the next step
        burns an admission number and the school should be able to say who
        decided this child is new.
        """
        self._require(principal, Permission.ADMISSION_REVIEW)

        async with self._uow:
            application = await self._admissions.get(application_id)
            if application is None:
                raise NotFound("no such application")

            try:
                application.state = transition(
                    application.state, ApplicationState.SCREENED
                )
            except TransitionRejected as exc:
                raise Invalid(str(exc)) from exc

            application.screened_at = datetime.now(UTC)
            application.screened_by_id = principal.user_id
            if note:
                application.screening_note = note

            await self._audit.record(
                school_id=application.school_id,
                action="application.screened",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="application",
                entity_id=application.id,
                note=note or "no duplicate",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return application

    # ----------------------------------------------------------------------
    # The offer
    # ----------------------------------------------------------------------
    async def offer(
        self,
        principal: Principal,
        *,
        application_id: UUID,
        context: RequestContext,
    ) -> ApplicationView:
        """Issue an admission number and raise the deposit invoice.

        The admission number comes from a locked per-school sequence, so two
        registrars offering places in the same second get 0142 and 0143. It is
        burnt here rather than at enrolment because the offer letter carries
        it, and a family that has been written to with a number must keep it.
        """
        self._require(principal, Permission.ADMISSION_REVIEW)

        async with self._uow:
            application = await self._admissions.get(application_id)
            if application is None:
                raise NotFound("no such application")
            if application.level_applied_id is None:
                raise Invalid("no level was applied for")

            school = await self._schools.get(application.school_id)
            if school is None:
                raise NotFound("no such school")

            year = await self._current_year(application)
            term = await self._first_term(year["id"])

            try:
                application.state = transition(application.state, ApplicationState.OFFERED)
            except TransitionRejected as exc:
                raise Invalid(str(exc)) from exc

            sequence = await self._students.next_admission_sequence(
                school_id=school.id, scope=year["label"]
            )
            try:
                application.admission_no = admission_number(
                    school.admission_no_format,
                    year=int(year["label"]) if year["label"].isdigit() else date.today().year,
                    sequence=sequence,
                )
            except ValueError as exc:
                raise Invalid(str(exc)) from exc

            raised = await self._invoicing.raise_deposit_invoice(
                school_id=school.id,
                application_id=application.id,
                academic_year_id=year["id"],
                level_id=application.level_applied_id,
                term_id=term["id"] if term else None,
                term_index=term["index"] if term else 1,
                year_label=year["label"],
                deposit_percent=school.deposit_percent,
                grace_days=school.invoice_grace_days,
                issued_on=date.today(),
                actor=principal,
                context=context,
            )

            application.deposit_invoice_id = raised.invoice.id
            application.deposit_due = raised.billed
            application.offered_at = datetime.now(UTC)
            application.offered_by_id = principal.user_id
            application.offer_expires_on = offer_expiry(offered_on=date.today())
            application.academic_year_id = year["id"]

            await self._audit.record(
                school_id=school.id,
                action="application.offered",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="application",
                entity_id=application.id,
                changed={
                    "admission_no": application.admission_no,
                    "deposit_due": str(raised.billed),
                    "expires_on": str(application.offer_expires_on),
                },
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        return await self.view(principal, application_id=application_id)

    async def decline(
        self,
        principal: Principal,
        *,
        application_id: UUID,
        reason: str,
        context: RequestContext,
    ) -> Any:
        self._require(principal, Permission.ADMISSION_REVIEW)

        async with self._uow:
            application = await self._admissions.get(application_id)
            if application is None:
                raise NotFound("no such application")

            try:
                application.state = transition(application.state, ApplicationState.DECLINED)
            except TransitionRejected as exc:
                raise Invalid(str(exc)) from exc

            application.declined_at = datetime.now(UTC)
            application.decline_reason = reason

            await self._audit.record(
                school_id=application.school_id,
                action="application.declined",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="application",
                entity_id=application.id,
                note=reason,
                outcome=AuditOutcome.SUCCESS,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return application

    # ----------------------------------------------------------------------
    # Acceptance, which is a payment
    # ----------------------------------------------------------------------
    async def settle_if_paid(
        self, principal: Principal, *, application_id: UUID, context: RequestContext
    ) -> Any:
        """Move an offer to accepted if the deposit has arrived.

        There is no accept button. Payment is the acceptance signal, so this
        reads the invoice and nothing else: a registrar cannot mark a place
        accepted that nobody has paid for, and an unpaid place cannot look
        accepted in the funnel.

        Called after a payment is recorded, and safe to call at any other
        time — it does nothing unless the money is actually there.
        """
        self._require(principal, Permission.ADMISSION_REVIEW)

        async with self._uow:
            application = await self._admissions.get(application_id)
            if application is None:
                raise NotFound("no such application")
            if application.deposit_invoice_id is None:
                return application

            totals = await self._invoicing.totals(application.deposit_invoice_id)
            settled = totals is not None and totals.is_settled

            if not can_accept(state=application.state, deposit_settled=settled):
                return application

            application.state = transition(application.state, ApplicationState.ACCEPTED)
            application.accepted_at = datetime.now(UTC)

            await self._audit.record(
                school_id=application.school_id,
                action="application.accepted",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="application",
                entity_id=application.id,
                note=f"deposit settled on {totals.reference if totals else 'unknown'}",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return application

    # ----------------------------------------------------------------------
    # Becoming a pupil
    # ----------------------------------------------------------------------
    async def enrol(
        self,
        principal: Principal,
        *,
        application_id: UUID,
        class_group_id: UUID,
        residency: Residency,
        context: RequestContext,
    ) -> EnrolledOutcome:
        """One transaction: a child, a guardian, an enrolment and a login.

        All of it or none of it. Half of this is a pupil the roll cannot find,
        a guardian with no children, or a child whose parent cannot sign in to
        pay — and the deposit has already been taken by this point, so there
        is no graceful way to leave it half done.

        The guardian is matched on phone, so a family's second child joins the
        guardian record the first one created rather than making a second.
        """
        self._require(principal, Permission.STUDENT_MANAGE)

        async with self._uow:
            application = await self._admissions.get(application_id)
            if application is None:
                raise NotFound("no such application")

            readiness = readiness_to_enrol(
                state=application.state,
                has_level=application.level_applied_id is not None,
                has_class_group=class_group_id is not None,
                has_guardian_details=bool(
                    application.guardian_name and application.guardian_phone
                ),
                admission_no=application.admission_no,
            )
            if not readiness.ready:
                raise Invalid("; ".join(readiness.problems))

            # Narrowed rather than asserted: readiness has already checked
            # both, and a type checker cannot see that through the dataclass.
            guardian_name = application.guardian_name or ""
            guardian_phone = application.guardian_phone or ""
            if not (guardian_name and guardian_phone):
                raise Invalid("the guardian's name and phone are needed")

            student = await self._students.add(
                school_id=application.school_id,
                admission_no=application.admission_no,
                first_name=application.child_first_name,
                middle_name=application.child_middle_name,
                surname=application.child_surname,
                date_of_birth=application.date_of_birth,
                sex=application.sex,
                status=StudentStatus.ACTIVE,
                birth_certificate_no=application.birth_certificate_no,
                previous_school=application.previous_school,
                special_needs=application.special_needs,
            )

            guardian = await self._guardians.by_phone(guardian_phone)
            if guardian is None:
                guardian = await self._guardians.add(
                    school_id=application.school_id,
                    full_name=guardian_name,
                    phone=guardian_phone,
                    email=application.guardian_email,
                    national_id=application.guardian_national_id,
                    address=application.guardian_address,
                )
            await self._guardians.link(
                school_id=application.school_id,
                guardian_id=guardian.id,
                student_id=student.id,
                relationship=application.guardian_relationship or Relationship.OTHER,
                is_primary=True,
            )

            enrolment = await self._students.add_enrolment(
                school_id=application.school_id,
                student_id=student.id,
                academic_year_id=application.academic_year_id,
                class_group_id=class_group_id,
                outcome=EnrolmentOutcome.ACTIVE,
                residency=residency,
                enrolled_on=date.today(),
            )

            # The deposit invoice was raised against the application, before
            # this child existed. Pointing it at them now is what makes the
            # money show up on their statement.
            await self._attach_deposit_to_student(application, student.id, enrolment.id)

            # The account that filled in the form becomes the family's login:
            # the same phone, the same row, a wider role. A parent who applied
            # in November signs in to pay fees in January with what they
            # already have, and the school never has two accounts for one
            # handset.
            await self._users.promote_applicant_to_guardian(
                phone=guardian_phone,
                guardian_id=guardian.id,
                full_name=guardian_name,
            )

            moved_documents = await self._admissions.move_documents_to_student(
                application_id=application.id, student_id=student.id
            )
            await self._admissions.move_field_values_to_student(
                application_id=application.id, student_id=student.id
            )

            application.state = transition(application.state, ApplicationState.ENROLLED)
            application.enrolled_at = datetime.now(UTC)
            application.student_id = student.id
            application.enrolment_id = enrolment.id

            await self._audit.record(
                school_id=application.school_id,
                action="application.enrolled",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=student.id,
                changed={
                    "application": application.reference,
                    "admission_no": student.admission_no,
                    "documents_carried_over": moved_documents,
                },
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

            return EnrolledOutcome(
                student_id=student.id,
                enrolment_id=enrolment.id,
                admission_no=student.admission_no,
                guardian_id=guardian.id,
            )

    async def _attach_deposit_to_student(
        self, application: Any, student_id: UUID, enrolment_id: UUID
    ) -> None:
        invoice = (
            await self._invoicing.totals(application.deposit_invoice_id)
            if application.deposit_invoice_id
            else None
        )
        if invoice is None:
            return
        await self._admissions.attach_invoice_to_student(
            invoice_id=invoice.invoice_id, student_id=student_id, enrolment_id=enrolment_id
        )

    async def form_options(self) -> tuple[list[dict[str, Any]], list[Any], list[Any], str | None]:
        """What the public form needs to draw itself.

        No principal: this is served to a stranger from the subdomain, and it
        holds nothing about any child. Only levels with a published fee
        structure appear — a level nobody can be quoted a deposit for is a
        level nobody should be able to apply to, and finding that out after
        filling in a form is worse than not seeing it.
        """
        levels = await self._admissions.levels_open_for_application()
        documents = await self._settings.requirement_facts(at_application=True)
        fields = await self._settings.field_definitions()
        year = await self._admissions.current_year()
        return levels, documents, fields, (year["label"] if year else None)

    # ----------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------
    async def queue(
        self,
        principal: Principal,
        *,
        states: list[str] | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[QueueRow], int]:
        self._require(principal, Permission.ADMISSION_REVIEW)
        return await self._admissions.queue(
            states=states, q=q, limit=limit, offset=offset
        )

    async def funnel(self, principal: Principal) -> dict[str, int]:
        """What the dashboard draws: how many sit at each step."""
        self._require(principal, Permission.ADMISSION_REVIEW)
        return await self._admissions.counts_by_state()

    async def view(
        self, principal: Principal, *, application_id: UUID
    ) -> ApplicationView:
        """One application, for a registrar."""
        self._require(principal, Permission.ADMISSION_REVIEW)
        application = await self._admissions.get(application_id)
        if application is None:
            raise NotFound("no such application")
        return await self._assemble(application)

    async def my_application(self, principal: Principal) -> ApplicationView:
        """The same application, for the parent filling it in."""
        application = await self._own_application(principal)
        return await self._assemble(application)

    async def _assemble(self, application: Any) -> ApplicationView:
        documents = await self._admissions.documents(application.id)
        values = await self._admissions.field_values(application.id)
        held = await self._admissions.document_kinds_held(application.id)

        subject = ChecklistSubject(
            core={
                "first_name": application.child_first_name,
                "surname": application.child_surname,
                "date_of_birth": application.date_of_birth,
                "sex": application.sex,
            },
            documents=held,
            field_values=frozenset(k for k, v in values.items() if v is not None),
            residency=Residency.DAY,
            level_id=application.level_applied_id,
            # An application has a guardian's details on it rather than a
            # linked record, and no emergency contact is asked for until
            # enrolment: a four-minute form cannot carry both.
            has_guardian=bool(application.guardian_name and application.guardian_phone),
            has_emergency_contact=True,
        )
        checklist = build_checklist(
            subject=subject,
            # Only what the public form asks for. A four-minute form that
            # demanded every paper a school eventually wants is a form nobody
            # finishes; the rest is collected at a desk, at enrolment.
            requirements=await self._settings.requirement_facts(at_application=True),
            fields=await self._settings.field_definitions(scope=FieldScope.APPLICATION),
        )

        deposit_billed = deposit_settled = None
        if application.deposit_invoice_id:
            totals = await self._invoicing.totals(application.deposit_invoice_id)
            if totals is not None:
                deposit_billed = totals.billed
                deposit_settled = totals.settled

        level_name = None
        if application.level_applied_id is not None:
            level_name = await self._admissions.level_name(application.level_applied_id)

        return ApplicationView(
            application=application,
            level_name=level_name,
            documents=documents,
            field_values=values,
            checklist=checklist,
            duplicates=(
                [
                    DuplicateMatch(
                        student_id=application.duplicate_of_student_id,
                        score=application.duplicate_score or 0,
                        reason=application.duplicate_reason or '',
                    )
                ]
                if application.duplicate_of_student_id
                else []
            ),
            deposit_billed=deposit_billed,
            deposit_settled=deposit_settled,
        )

    async def _current_year(self, application: Any) -> dict[str, Any]:
        year = (
            await self._admissions.academic_year(application.academic_year_id)
            if application.academic_year_id
            else await self._admissions.current_year()
        )
        if year is None:
            raise Invalid("this school has no current academic year to admit into")
        return dict(year)

    async def _first_term(self, academic_year_id: UUID) -> dict[str, Any] | None:
        return await self._admissions.first_term(academic_year_id)
