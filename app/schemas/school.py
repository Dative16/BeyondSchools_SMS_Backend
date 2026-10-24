"""School request and response models.

The profile a school admin edits, which is not the same thing as the branding
an anonymous login page may read — that one is `SchoolBrandResponse` in
`app/schemas/auth.py` and holds seven fields. This one holds the settings, so
it sits behind `school:manage`.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SchoolResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    short_name: str | None
    subdomain: str
    registration_number: str | None
    status: str

    motto: str | None
    logo_url: str | None
    brand_color: str

    phone: str | None
    email: str | None
    address: str | None
    region: str | None
    district: str | None

    currency: str
    timezone: str
    locale: str

    admission_no_format: str
    deposit_percent: int
    invoice_grace_days: int

    arrears_blocks_results: bool
    arrears_blocks_promotion: bool
    arrears_threshold: Decimal | None


class SchoolUpdateRequest(BaseModel):
    """Everything a school admin may change about their own school.

    Absent from this list, on purpose: `subdomain`, because it is the tenant
    key and changing it would break every bookmark and every parent's saved
    link; `status`, because suspending a school is a platform decision; and
    `currency`, because invoices already exist in it.
    """

    name: str | None = Field(default=None, min_length=2, max_length=160)
    short_name: str | None = Field(default=None, max_length=32)
    registration_number: str | None = Field(default=None, max_length=64)
    motto: str | None = Field(default=None, max_length=200)
    logo_url: str | None = None
    brand_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=160)
    address: str | None = None
    region: str | None = Field(default=None, max_length=80)
    district: str | None = Field(default=None, max_length=80)

    timezone: str | None = Field(default=None, max_length=64)
    locale: str | None = Field(default=None, max_length=8)

    #: Only {year} and {sequence} are supported, and the service renders it
    #: through app.domain.enrolment.admission_number, which refuses anything
    #: else rather than producing a malformed admission number.
    admission_no_format: str | None = Field(default=None, max_length=64)
    deposit_percent: int | None = Field(default=None, ge=0, le=100)
    invoice_grace_days: int | None = Field(default=None, ge=0, le=90)

    # Whether arrears withhold a report card or a promotion is a head
    # teacher's decision. Both default to off in the schema, and every change
    # is audited, because a system that silently withholds a child's results
    # has to be able to say who asked it to.
    arrears_blocks_results: bool | None = None
    arrears_blocks_promotion: bool | None = None
    arrears_threshold: Decimal | None = Field(default=None, ge=0)
