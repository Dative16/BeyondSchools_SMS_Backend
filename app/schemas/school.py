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

from app.domain.registration import FieldKind, FieldScope


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


# --------------------------------------------------------------------------
# The fields a school adds for itself
# --------------------------------------------------------------------------
class SchoolFieldResponse(BaseModel):
    """One field this school has decided it needs."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    label: str
    help_text: str | None
    kind: FieldKind
    scope: FieldScope
    options: list[str] | None
    required_at_registration: bool
    is_sensitive: bool
    is_active: bool
    sort_order: int


class SchoolFieldCreateRequest(BaseModel):
    """Add a field.

    Every field added here appears, empty, on every pupil record the school
    has, and somebody then has to fill them all in. That cost is the reason
    `required_at_registration` defaults to false: a required field on the
    public form costs completed applications.
    """

    key: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=120)
    kind: FieldKind
    scope: FieldScope = FieldScope.STUDENT
    help_text: str | None = Field(default=None, max_length=400)
    #: Only for a choice field, and then at least two.
    options: list[str] = Field(default_factory=list, max_length=40)
    required_at_registration: bool = False
    #: Health, religion, anything a school should not be dropping into a
    #: spreadsheet. Kept out of ordinary exports.
    is_sensitive: bool = False


class SchoolFieldUpdateRequest(BaseModel):
    """Relabel a field, or retire it.

    No key and no kind. Values already recorded are stored under that key and
    typed by that kind, so changing either would reinterpret answers people
    have already given.
    """

    label: str | None = Field(default=None, min_length=1, max_length=120)
    help_text: str | None = Field(default=None, max_length=400)
    options: list[str] | None = Field(default=None, max_length=40)
    required_at_registration: bool | None = None
    is_sensitive: bool | None = None
    #: False retires it: every value kept, never asked for again.
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=500)
