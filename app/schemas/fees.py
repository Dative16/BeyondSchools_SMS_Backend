"""Fee request and response models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.invoicing import (
    FeeCategory,
    FeeFrequency,
    FeeStructureStatus,
    InvoiceLineKind,
    RunOutcome,
)


# --------------------------------------------------------------------------
# Fee structures
# --------------------------------------------------------------------------
class FeeItemModel(BaseModel):
    """One line of a structure: what it is, how much, and how often."""

    name: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    category: FeeCategory
    frequency: FeeFrequency
    #: Set only for an item billed in one particular term.
    term_index: int | None = Field(default=None, ge=1, le=4)
    is_mandatory: bool = True


class FeeStructureRow(BaseModel):
    """A structure as the settings screen lists it.

    The item count and total come along because a structure with no items is
    the commonest reason a term run finds nothing to bill.
    """

    id: UUID
    academic_year_id: UUID
    level_id: UUID
    level_name: str
    stream_id: UUID | None
    stream_name: str | None
    label: str
    status: FeeStructureStatus
    published_at: datetime | None
    note: str | None
    item_count: int
    total_amount: Decimal


class FeeStructureCreateRequest(BaseModel):
    academic_year_id: UUID
    level_id: UUID
    #: Set to charge one stream differently — boarders, usually. A structure
    #: scoped to a stream wins over the level-wide one.
    stream_id: UUID | None = None
    label: str = Field(min_length=1, max_length=80)
    items: list[FeeItemModel] = Field(default_factory=list, max_length=40)


class FeeItemsReplaceRequest(BaseModel):
    items: list[FeeItemModel] = Field(max_length=40)


class FeeStructureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    academic_year_id: UUID
    level_id: UUID
    stream_id: UUID | None
    label: str
    status: FeeStructureStatus
    published_at: datetime | None
    note: str | None


class FeeStructureDetailResponse(BaseModel):
    structure: FeeStructureResponse
    items: list[dict[str, Any]]


# --------------------------------------------------------------------------
# The term run
# --------------------------------------------------------------------------
class TermRunRequest(BaseModel):
    """Raise a term's invoices.

    `dry_run` defaults to true on purpose. A run over 500 children is not
    something to start by accident, and a bursar should see what it would do —
    including which levels have no published structure — before any invoice
    exists.
    """

    issued_on: date | None = None
    dry_run: bool = True


class RunDecisionResponse(BaseModel):
    enrolment_id: UUID
    student_id: UUID
    outcome: RunOutcome
    billed: Decimal
    reason: str


class TermRunResponse(BaseModel):
    dry_run: bool
    raised: int
    skipped: int
    total_billed: Decimal
    #: Children who could not be billed, named. Almost always a level whose
    #: fee structure is still a draft.
    problems: list[RunDecisionResponse]
    decisions: list[RunDecisionResponse]


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
class InvoiceRow(BaseModel):
    """One line of the fees screen."""

    id: UUID
    reference: str
    kind: str
    student_id: UUID | None
    admission_no: str | None
    student_name: str | None
    class_name: str | None
    term_label: str | None
    issued_on: date
    due_date: date
    billed: Decimal
    settled: Decimal
    balance: Decimal
    #: Derived from the balance and the due date against a given day, never
    #: stored: a stored status is how an invoice ends up marked paid with money
    #: missing.
    status: str
    days_overdue: int


class InvoiceLineResponse(BaseModel):
    id: UUID
    item: str
    amount: Decimal
    kind: InvoiceLineKind
    note: str | None


class PaymentOnInvoice(BaseModel):
    id: UUID
    external_ref: str
    amount: Decimal
    applied: Decimal
    channel: str
    state: str
    msisdn: str | None
    received_at: datetime
    settled_on: date | None
    is_manual: bool
    reversed_at: datetime | None
    recorded_by: str | None


class InvoiceDetailResponse(BaseModel):
    """One bill, its lines and the money against it.

    Assembled server-side because the three have to agree: a parent looking at
    a total that does not match the lines above it stops trusting every other
    number on the page.
    """

    id: UUID
    reference: str
    kind: str
    student_id: UUID | None
    term_id: UUID | None
    issued_on: date
    due_date: date
    currency: str
    billed: Decimal
    settled: Decimal
    balance: Decimal
    status: str
    days_overdue: int
    is_void: bool
    void_reason: str | None
    note: str | None
    lines: list[InvoiceLineResponse]
    payments: list[PaymentOnInvoice]


class StatementRow(BaseModel):
    id: UUID
    reference: str
    kind: str
    academic_year: str | None
    term_label: str | None
    issued_on: date
    due_date: date
    billed: Decimal
    settled: Decimal
    balance: Decimal
    is_void: bool


class StatementResponse(BaseModel):
    """Every bill a child has ever had, across years.

    Across years because arrears follow the child: a statement that began at
    the current year would not explain the balance on it.
    """

    student_id: UUID
    invoices: list[StatementRow]
    billed: Decimal
    settled: Decimal
    balance: Decimal


# --------------------------------------------------------------------------
# Corrections
# --------------------------------------------------------------------------
class VoidInvoiceRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class CreditNoteRequest(BaseModel):
    """Take something off a bill without rewriting it."""

    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    note: str = Field(min_length=3, max_length=120)


class InvoiceTotalsResponse(BaseModel):
    invoice_id: UUID
    reference: str
    billed: Decimal
    settled: Decimal
    balance: Decimal


# --------------------------------------------------------------------------
# Arrears and collection
# --------------------------------------------------------------------------
class ArrearsRow(BaseModel):
    """One child, not one invoice: a bursar chasing arrears rings a family,
    and three overdue bills for one child is one phone call."""

    student_id: UUID
    admission_no: str
    student_name: str
    class_name: str | None
    guardian_name: str | None
    guardian_phone: str | None
    balance: Decimal
    days_overdue: int
    overdue_invoices: int
    #: current, 1-30, 31-60, over-60
    bucket: str


class CollectionResponse(BaseModel):
    """The dashboard's money tiles."""

    billed: Decimal
    settled: Decimal
    outstanding: Decimal
    overdue: Decimal
    students_in_arrears: int
    collected_percent: int


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------
class SuspensePayment(BaseModel):
    """Money that arrived with nothing to match it."""

    id: UUID
    external_ref: str
    amount: Decimal
    channel: str
    msisdn: str | None
    payer_reference: str | None
    received_at: datetime
    reason: str | None


class MatchSuspenseRequest(BaseModel):
    invoice_id: UUID


class RecordPaymentRequest(BaseModel):
    """Money a bursar has in hand.

    Phase 1 has no aggregator, so this is how fees get settled. The receipt is
    required and unique per school: entering the same one twice is caught
    rather than doubling a credit.
    """

    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    receipt: str = Field(min_length=1, max_length=64)
    channel: Literal[
        "mpesa", "airtel_money", "mixx", "halopesa", "bank", "card", "counter"
    ] = "counter"
    received_on: date | None = None
    msisdn: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)
