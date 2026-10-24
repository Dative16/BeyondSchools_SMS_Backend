"""Student request and response models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enrolment import EnrolmentOutcome, StudentStatus
from app.domain.registration import Residency


class RollRowResponse(BaseModel):
    """One line of the students screen.

    Carries the class and the fee totals because the roll shows both, and
    because a list that made the client fetch them per row would be a list
    nobody waits for on a 3G connection.
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
    residency: str | None
    uses_transport: bool | None
    transport_route: str | None
    primary_guardian: str | None
    primary_guardian_phone: str | None
    billed: Decimal
    settled: Decimal
    balance: Decimal
    #: Whether this child has crossed to aibeyondschools.co.tz yet. The roll
    #: shows it because a class with nobody signed in is the thing a head of
    #: ICT needs to see.
    on_ai_platform: bool


class StudentCreateRequest(BaseModel):
    """Admit a child by hand.

    No admission number: it is issued from a locked per-school sequence, not
    accepted from a form, so two registrars admitting at once cannot mint the
    same one.
    """

    first_name: str = Field(min_length=1, max_length=64)
    middle_name: str | None = Field(default=None, max_length=64)
    surname: str = Field(min_length=1, max_length=64)
    date_of_birth: date
    sex: Literal["f", "m"]
    nationality: str = Field(default="Tanzanian", max_length=48)
    birth_certificate_no: str | None = Field(default=None, max_length=64)
    previous_school: str | None = Field(default=None, max_length=160)
    #: Free text, and it has to be: a class teacher needs to know a child
    #: cannot hear from the back of the room. Medical detail goes to the health
    #: record instead, behind its own permission.
    special_needs: str | None = None
    photo_url: str | None = None
    #: Which year's sequence the admission number comes from.
    admission_year: int | None = Field(default=None, ge=2000, le=2100)


class StudentUpdateRequest(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=64)
    middle_name: str | None = Field(default=None, max_length=64)
    surname: str | None = Field(default=None, min_length=1, max_length=64)
    date_of_birth: date | None = None
    sex: Literal["f", "m"] | None = None
    nationality: str | None = Field(default=None, max_length=48)
    birth_certificate_no: str | None = Field(default=None, max_length=64)
    previous_school: str | None = Field(default=None, max_length=160)
    special_needs: str | None = None
    photo_url: str | None = None
    status: StudentStatus | None = None


class StudentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    admission_no: str
    first_name: str
    middle_name: str | None
    surname: str
    date_of_birth: date
    sex: str
    status: StudentStatus
    nationality: str
    birth_certificate_no: str | None
    special_needs: str | None
    previous_school: str | None
    photo_url: str | None
    arrears_brought_forward: Decimal
    ai_platform_user_ref: str | None


class EnrolmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    student_id: UUID
    academic_year_id: UUID
    class_group_id: UUID
    outcome: EnrolmentOutcome
    enrolled_on: date
    exited_on: date | None
    exit_reason: str | None
    is_repeating: bool
    residency: Residency
    dormitory: str | None
    uses_transport: bool
    transport_route: str | None
    transport_stop: str | None


class EnrolmentHistoryRow(BaseModel):
    """One year of a child's history, as the profile timeline draws it."""

    id: UUID
    academic_year: str
    class_name: str
    outcome: EnrolmentOutcome
    enrolled_on: date
    exited_on: date | None
    is_repeating: bool
    residency: str
    dormitory: str | None
    transport_route: str | None
    class_teacher: str | None


class EnrolmentCreateRequest(BaseModel):
    student_id: UUID
    academic_year_id: UUID
    class_group_id: UUID
    residency: Residency = Residency.DAY
    enrolled_on: date | None = None


class EnrolmentUpdateRequest(BaseModel):
    """This year's facts. All of them are per-year by design."""

    class_group_id: UUID | None = None
    residency: Residency | None = None
    dormitory: str | None = Field(default=None, max_length=64)
    uses_transport: bool | None = None
    transport_route: str | None = Field(default=None, max_length=80)
    transport_stop: str | None = Field(default=None, max_length=80)
    is_repeating: bool | None = None


class TransferOutRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    on: date | None = None


class GuardianOnStudent(BaseModel):
    """A guardian as the student profile shows them."""

    link_id: UUID
    guardian_id: UUID
    full_name: str
    phone: str
    alt_phone: str | None
    email: str | None
    relationship: str
    is_primary: bool
    receives_invoices: bool
    receives_sms: bool
    may_collect: bool
    sms_opted_out: bool


class HealthResponse(BaseModel):
    """Medical detail. Only ever served to a caller holding `health:read`."""

    model_config = ConfigDict(from_attributes=True)

    student_id: UUID
    blood_group: str | None
    allergies: str | None
    chronic_conditions: str | None
    medication: str | None
    emergency_plan: str | None
    doctor_name: str | None
    doctor_phone: str | None
    insurance_scheme: str | None
    insurance_number: str | None
    note: str | None
    updated_at: datetime


class HealthUpdateRequest(BaseModel):
    blood_group: str | None = Field(default=None, max_length=8)
    allergies: str | None = None
    chronic_conditions: str | None = None
    medication: str | None = None
    #: What somebody should actually do. An asthma plan is useless as a
    #: diagnosis and valuable as an instruction.
    emergency_plan: str | None = None
    doctor_name: str | None = Field(default=None, max_length=120)
    doctor_phone: str | None = Field(default=None, max_length=20)
    insurance_scheme: str | None = Field(default=None, max_length=80)
    insurance_number: str | None = Field(default=None, max_length=64)
    note: str | None = None


class EmergencyContactModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    full_name: str = Field(min_length=2, max_length=160)
    relationship: str = Field(min_length=2, max_length=48)
    phone: str = Field(min_length=9, max_length=20)
    alt_phone: str | None = Field(default=None, max_length=20)
    #: 1 is "ring this person first". One per child.
    priority: int = Field(default=1, ge=1, le=5)
    note: str | None = None
    may_collect: bool = False


class EmergencyContactsReplaceRequest(BaseModel):
    contacts: list[EmergencyContactModel] = Field(min_length=1, max_length=5)


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    file_url: str
    file_name: str | None
    content_type: str | None
    size_bytes: int | None
    note: str | None
    verified_at: datetime | None
    created_at: datetime


class DocumentCreateRequest(BaseModel):
    """Attach a document that has already been stored somewhere.

    The bytes do not pass through this endpoint: object storage is not wired up
    yet, so the file is uploaded to it directly and the reference recorded
    here. When storage lands this gains a presigned-upload step; the shape of
    the record does not change.
    """

    kind: str = Field(min_length=2, max_length=40)
    file_url: str = Field(min_length=4)
    file_name: str | None = Field(default=None, max_length=255)
    content_type: str | None = Field(default=None, max_length=80)
    size_bytes: int | None = Field(default=None, ge=0)
    note: str | None = None


class ChecklistItemResponse(BaseModel):
    key: str
    label: str
    required: bool
    done: bool


class ChecklistResponse(BaseModel):
    """What is still missing from a child's record.

    `complete` is computed by the same function the admissions queue uses, so
    the two screens cannot disagree about what complete means.
    """

    items: list[ChecklistItemResponse]
    complete: bool
    missing: list[str]


class FieldValuesRequest(BaseModel):
    """A school's own fields, keyed by the field's key.

    Each value is checked against its field's declared kind before it is
    stored — a number field that accepted "about 40" would be a column nobody
    can add up.
    """

    values: dict[str, Any]


class StudentProfileResponse(BaseModel):
    """Everything the profile screen draws, in one answer."""

    student: StudentResponse
    enrolment: EnrolmentResponse | None
    class_name: str | None
    history: list[EnrolmentHistoryRow]
    guardians: list[GuardianOnStudent]
    billed: Decimal
    settled: Decimal
    balance: Decimal
    field_values: dict[str, Any]
    #: Present only for a caller holding health:read, and null rather than
    #: absent so the client can tell "not allowed" from "nothing recorded".
    health: HealthResponse | None
    emergency_contacts: list[EmergencyContactModel]
