"""Admission request and response models.

Split by who is reading. `Apply*` is the public form, filled in by somebody
with no account and a verified phone number. `Queue*` and `Application*` are
the registrar's screens. They are not the same shapes: the parent never needs
to see a duplicate score, and the registrar never needs the field help text.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.admissions import ApplicationState
from app.domain.registration import FieldKind, Residency
from app.models.guardian import Relationship


# --------------------------------------------------------------------------
# Starting the form
# --------------------------------------------------------------------------
class ApplyStartRequest(BaseModel):
    """The phone number a code goes to.

    Verified before anything else is collected, which kills most junk
    applications and is the only credential this parent will have.
    """

    phone: str = Field(min_length=9, max_length=20)

    @field_validator("phone")
    @classmethod
    def normalise(cls, value: str) -> str:
        from app.domain.admissions import normalise_phone

        phone = normalise_phone(value)
        if len(phone) != 10 or not phone.startswith("0"):
            raise ValueError("that does not look like a Tanzanian mobile number")
        return phone


class ApplyVerifyRequest(ApplyStartRequest):
    code: str = Field(min_length=4, max_length=8)


class ApplyFieldOption(BaseModel):
    """One of a school's own questions, for the form to render."""

    key: str
    label: str
    help_text: str | None
    kind: FieldKind
    options: list[str]
    required: bool


class ApplyDocumentOption(BaseModel):
    kind: str
    label: str
    required: bool


class ApplyLevelOption(BaseModel):
    id: UUID
    code: str
    name: str


class ApplyOptionsResponse(BaseModel):
    """Everything the public form needs to draw itself.

    Served unauthenticated, from the subdomain, and holds nothing about any
    child: the levels on offer, the papers to bring, and the school's own
    questions.
    """

    school_name: str
    academic_year: str | None
    levels: list[ApplyLevelOption]
    documents: list[ApplyDocumentOption]
    fields: list[ApplyFieldOption]


# --------------------------------------------------------------------------
# Filling it in
# --------------------------------------------------------------------------
class ApplySaveRequest(BaseModel):
    """Save progress. Every field is optional, on purpose.

    A half-finished application is a legal row rather than a validation error,
    which is what lets a parent stop at question nine on a weak connection and
    come back by code.
    """

    child_first_name: str | None = Field(default=None, max_length=64)
    child_middle_name: str | None = Field(default=None, max_length=64)
    child_surname: str | None = Field(default=None, max_length=64)
    date_of_birth: date | None = None
    sex: Literal["f", "m"] | None = None
    birth_certificate_no: str | None = Field(default=None, max_length=64)
    previous_school: str | None = Field(default=None, max_length=160)
    special_needs: str | None = None
    level_applied_id: UUID | None = None

    guardian_name: str | None = Field(default=None, max_length=160)
    guardian_phone: str | None = Field(default=None, max_length=20)
    guardian_email: str | None = Field(default=None, max_length=160)
    guardian_relationship: Relationship | None = None
    guardian_national_id: str | None = Field(default=None, max_length=32)
    guardian_address: str | None = None


class ApplyChecklistItem(BaseModel):
    key: str
    label: str
    required: bool
    done: bool


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reference: str
    state: ApplicationState
    phone: str
    phone_verified_at: datetime | None

    child_first_name: str | None
    child_middle_name: str | None
    child_surname: str | None
    date_of_birth: date | None
    sex: str | None
    previous_school: str | None
    special_needs: str | None
    level_applied_id: UUID | None

    guardian_name: str | None
    guardian_phone: str | None
    guardian_email: str | None
    guardian_relationship: Relationship | None
    #: Write-only until now: `ApplySaveRequest` took all three and nothing
    #: ever sent them back, so a parent who filled them in and returned the
    #: next day found the boxes empty and typed them again — or decided the
    #: form had not saved.
    guardian_national_id: str | None = None
    guardian_address: str | None = None
    birth_certificate_no: str | None = None

    admission_no: str | None
    offer_expires_on: date | None
    deposit_due: Decimal | None
    submitted_at: datetime | None
    offered_at: datetime | None
    accepted_at: datetime | None
    enrolled_at: datetime | None
    decline_reason: str | None


class ApplyDocument(BaseModel):
    """One file a parent has attached.

    `file_url` is the storage key and is deliberately absent: it is not a URL
    anybody can open, and sending it would put an object key in a page a
    stranger reaches.
    """

    id: UUID
    kind: str
    file_name: str | None
    content_type: str | None
    size_bytes: int | None
    verified_at: datetime | None
    created_at: datetime


class ApplyStateResponse(BaseModel):
    """What the form shows a parent: where they are, and what is left."""

    application: ApplicationResponse
    level_name: str | None
    documents: list[ApplyDocument]
    field_values: dict[str, Any]
    #: What the school would like to have: documents, extra fields.
    checklist: list[ApplyChecklistItem]
    complete: bool
    missing: list[str]
    #: What the server will refuse a submission without — a different and much
    #: shorter list. A form that disables its send button on `missing` puts a
    #: birth certificate between a parent and a submission the server would
    #: have accepted.
    can_submit: bool = False
    missing_to_submit: list[str] = []
    deposit_billed: Decimal | None
    deposit_settled: Decimal | None


class ApplySentResponse(BaseModel):
    sent: Literal[True] = True
    expires_at: datetime
    resend_after_seconds: int


# --------------------------------------------------------------------------
# The registrar's screens
# --------------------------------------------------------------------------
class QueueRowResponse(BaseModel):
    """One line of the review queue."""

    id: UUID
    reference: str
    state: ApplicationState
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
    #: How long this family has been waiting, which is the number a registrar
    #: actually sorts by.
    waiting_days: int
    duplicate_score: int | None
    duplicate_reason: str | None
    screening_note: str | None
    admission_no: str | None
    offer_expires_on: date | None
    offer_lapsed: bool
    deposit_due: Decimal | None
    deposit_settled: Decimal | None
    documents_held: int


class DuplicateResponse(BaseModel):
    student_id: UUID
    score: int
    reason: str


class ApplicationDetailResponse(ApplyStateResponse):
    """The registrar's view: the parent's view plus what screening found."""

    duplicates: list[DuplicateResponse]


class ScreenRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class DeclineRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class EnrolRequest(BaseModel):
    """The last step: which class, and whether they board."""

    class_group_id: UUID
    residency: Residency = Residency.DAY


class EnrolledResponse(BaseModel):
    student_id: UUID
    enrolment_id: UUID
    admission_no: str
    guardian_id: UUID


class AdmitChild(BaseModel):
    """The child, as the counter form asks for them.

    The same fields as `StudentCreateRequest` and the same birth-date rule,
    deliberately not inherited: that model also accepts `admission_year`, and
    an admission number's year comes from the class being admitted into, not
    from anything a form may send.
    """

    first_name: str = Field(min_length=1, max_length=64)
    middle_name: str | None = Field(default=None, max_length=64)
    surname: str = Field(min_length=1, max_length=64)
    date_of_birth: date
    sex: Literal["f", "m"]
    nationality: str = Field(default="Tanzanian", max_length=48)
    birth_certificate_no: str | None = Field(default=None, max_length=64)
    previous_school: str | None = Field(default=None, max_length=160)

    @field_validator("date_of_birth")
    @classmethod
    def _plausible_birth(cls, value: date) -> date:
        from app.domain.enrolment import date_of_birth_problem

        problem = date_of_birth_problem(value, today=date.today())
        if problem:
            raise ValueError(problem)
        return value


class AdmitGuardian(BaseModel):
    """One guardian is enough to start.

    This person receives the invoices and the SMS, and the phone number is how
    they sign in — so it is checked here the same way the public form checks
    the number it sends a code to. Others are linked later from the pupil
    record.
    """

    full_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=9, max_length=20)
    relationship: Relationship = Relationship.OTHER
    national_id: str | None = Field(default=None, max_length=32)

    @field_validator("phone")
    @classmethod
    def normalise(cls, value: str) -> str:
        from app.domain.admissions import normalise_phone

        phone = normalise_phone(value)
        if len(phone) != 10 or not phone.startswith("0"):
            raise ValueError("that does not look like a Tanzanian mobile number")
        return phone


class AdmitByHandRequest(BaseModel):
    """A family at the counter who never used the public form.

    The academic year is not asked for. It is the year the chosen class
    belongs to, which is the only answer that can be right — a class and a
    year that disagreed would put a child on a roll nobody is looking at.
    """

    child: AdmitChild
    class_group_id: UUID
    residency: Residency = Residency.DAY
    guardian: AdmitGuardian
    #: Defaults to today. Set it for a child who started last Monday and is
    #: being entered now, which is the ordinary case at a busy intake.
    enrolled_on: date | None = None


class AdmittedResponse(BaseModel):
    """Everything the admission created, named.

    The registrar reads the admission number out to the family and opens the
    invoice if they are paying today, so both come back rather than leaving
    the screen to go looking.
    """

    student_id: UUID
    enrolment_id: UUID
    admission_no: str
    class_name: str
    guardian_id: UUID
    #: True when the phone number was already a guardian here. The screen says
    #: so, because it means the name typed in was not used.
    guardian_existed: bool
    #: What happened to the family's login: created, promoted from an
    #: applicant, or left alone because that number belongs to a staff
    #: account.
    login: str
    invoice_id: UUID
    invoice_reference: str
    billed: Decimal


class FunnelResponse(BaseModel):
    """What the dashboard draws.

    A deposit payment is what converts an application into an enrolment, which
    is why the step between offered and enrolled is the one worth watching.
    """

    started: int = 0
    submitted: int = 0
    screened: int = 0
    offered: int = 0
    accepted: int = 0
    enrolled: int = 0
    declined: int = 0
    withdrawn: int = 0


class ManualPaymentRequest(BaseModel):
    """Money a bursar has in hand.

    Phase 1 has no aggregator, so this is how a deposit becomes a pupil. The
    receipt number is required and unique per school: entering the same
    receipt twice is caught rather than doubling a credit.
    """

    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    receipt: str = Field(min_length=1, max_length=64)
    channel: Literal["mpesa", "airtel_money", "mixx", "halopesa", "bank", "card", "counter"] = (
        "counter"
    )
    received_on: date | None = None
    msisdn: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)


class ManualPaymentResponse(BaseModel):
    payment_id: UUID
    invoice_reference: str
    billed: Decimal
    settled: Decimal
    balance: Decimal
    #: Whether this payment moved the application on. Acceptance is the
    #: deposit arriving, so the bursar's screen can say so immediately.
    application_state: ApplicationState | None = None
