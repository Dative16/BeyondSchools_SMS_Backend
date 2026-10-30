"""Guardian request and response models."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.admissions import normalise_phone
from app.models.guardian import Relationship


class GuardianResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    phone: str
    alt_phone: str | None
    email: str | None
    national_id: str | None
    occupation: str | None
    address: str | None
    locale: str
    sms_opted_out: bool


class GuardianDetails(BaseModel):
    """The person. Matched on phone, which is what makes a parent with three
    children one guardian and three links."""

    full_name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=9, max_length=20)
    alt_phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=160)
    national_id: str | None = Field(default=None, max_length=32)
    occupation: str | None = Field(default=None, max_length=80)
    address: str | None = None
    locale: str = Field(default="en", max_length=8)

    @field_validator("phone", "alt_phone")
    @classmethod
    def normalise(cls, value: str | None) -> str | None:
        """Stored as 0754118902 however it was typed.

        Parents write +255754118902, 255754118902 and 0754118902 for the same
        handset. Matching on the raw string would give one family three
        guardian records, three fee reminders and no combined balance.
        """
        if value is None:
            return None
        phone = normalise_phone(value)
        if len(phone) != 10 or not phone.startswith("0"):
            raise ValueError("that does not look like a Tanzanian mobile number")
        return phone


class GuardianLinkDetails(BaseModel):
    """The four decisions that hang off the link, not off the person.

    A father can be the primary contact for one child and not for another, and
    whichever guardian receives the invoices is a per-child choice.
    """

    relationship: Relationship
    is_primary: bool = False
    receives_invoices: bool = True
    receives_sms: bool = True
    may_collect: bool = True


class GuardianLinkRequest(BaseModel):
    guardian: GuardianDetails
    link: GuardianLinkDetails


class GuardianLinkedResponse(BaseModel):
    guardian_id: UUID
    full_name: str


class GuardianChildResponse(BaseModel):
    """One child, as their guardian's own screen shows them."""

    student_id: UUID
    admission_no: str
    full_name: str
    class_name: str | None
    relationship: str
    is_primary: bool
    receives_invoices: bool
    billed: Decimal
    settled: Decimal
    balance: Decimal


class GuardianAtThisNumber(BaseModel):
    """Who already uses a phone number, for the counter admission form.

    Their children come with them, named and with their classes, because
    "this number is already on a pupil's record" is not useful on its own —
    the registrar needs to see *whose*, to ask the person in front of them
    whether it is the same family.
    """

    guardian: GuardianResponse
    children: list[GuardianChildResponse]
