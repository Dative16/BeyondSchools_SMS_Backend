"""Fee structures and invoice arithmetic. Pure.

Money rules are the ones a school notices first and forgives last, so the
arithmetic lives here where it can be tested exhaustively without a database,
and `app/domain/payments.py` decides what a verified payment does to it.

Everything is Decimal. A float cannot hold 0.1 exactly, and a bursar's term
close has to come out to the shilling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID

ZERO = Decimal("0.00")


class FeeCategory(StrEnum):
    TUITION = "tuition"
    ADMISSION = "admission"
    EXAMINATION = "examination"
    BOARDING = "boarding"
    TRANSPORT = "transport"
    UNIFORM = "uniform"
    MEALS = "meals"
    DEVELOPMENT = "development"
    OTHER = "other"


class FeeFrequency(StrEnum):
    """How often an item is billed.

    PER_TERM is billed on every term invoice, ANNUAL once a year on the first
    term's invoice, and ONE_OFF only when a child joins — which is what makes
    the admission fee and the uniform charge land on the deposit invoice and
    never again.
    """

    PER_TERM = "per_term"
    ANNUAL = "annual"
    ONE_OFF = "one_off"


class FeeStructureStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class InvoiceLineKind(StrEnum):
    """A line is a charge, a discount or a credit note, and the sign follows
    from the kind rather than from whoever typed the amount."""

    CHARGE = "charge"
    DISCOUNT = "discount"
    CREDIT_NOTE = "credit_note"
    ARREARS_BROUGHT_FORWARD = "arrears_brought_forward"


class InvoiceStatus(StrEnum):
    """Derived, never stored as the source of truth: it is a reading of the
    balance and the due date, and storing it is how an invoice ends up marked
    paid with money missing."""

    DRAFT = "draft"
    OPEN = "open"
    PART_PAID = "part_paid"
    SETTLED = "settled"
    IN_ARREARS = "in_arrears"
    VOID = "void"


def shillings(value: Decimal | int | str) -> Decimal:
    """Round to the shilling, half up. TZS has no subunit in practice."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class FeeItem:
    """One line of a published fee structure."""

    fee_item_id: UUID
    name: str
    amount: Decimal
    category: FeeCategory
    frequency: FeeFrequency
    is_mandatory: bool = True
    #: Set only for an item billed in one particular term.
    term_index: int | None = None


@dataclass(frozen=True, slots=True)
class DraftLine:
    """A line an invoice is about to be built with."""

    item: str
    amount: Decimal
    kind: InvoiceLineKind
    fee_item_id: UUID | None = None


def signed(line: DraftLine) -> Decimal:
    """What a line contributes to the total."""
    if line.kind is InvoiceLineKind.CHARGE:
        return shillings(line.amount)
    if line.kind is InvoiceLineKind.ARREARS_BROUGHT_FORWARD:
        return shillings(line.amount)
    return -shillings(line.amount)


def billed_total(lines: list[DraftLine]) -> Decimal:
    """The invoice total, which the database also maintains as a trigger.

    Two implementations of one number is usually a smell; here it is the point.
    The trigger keeps `invoice.billed` true no matter which code path writes a
    line, and this function lets an invoice be costed and shown to a bursar
    before anything is written at all.
    """
    total = sum((signed(line) for line in lines), ZERO)
    return shillings(max(total, ZERO))


def lines_for_term(
    items: list[FeeItem],
    *,
    term_index: int,
    is_first_term_of_year: bool,
) -> list[DraftLine]:
    """Which fee items land on one term's invoice.

    An annual item on the first term's invoice and nowhere else; a one-off item
    on neither, because it belongs to joining the school rather than to a term.
    """
    chosen: list[DraftLine] = []
    for item in items:
        if item.frequency is FeeFrequency.ONE_OFF:
            continue
        if item.frequency is FeeFrequency.ANNUAL and not is_first_term_of_year:
            continue
        if item.term_index is not None and item.term_index != term_index:
            continue
        chosen.append(
            DraftLine(
                item=item.name,
                amount=shillings(item.amount),
                kind=InvoiceLineKind.CHARGE,
                fee_item_id=item.fee_item_id,
            )
        )
    return chosen


def lines_for_joining(items: list[FeeItem]) -> list[DraftLine]:
    """The one-off items a child pays once, on the admission deposit invoice."""
    return [
        DraftLine(
            item=item.name,
            amount=shillings(item.amount),
            kind=InvoiceLineKind.CHARGE,
            fee_item_id=item.fee_item_id,
        )
        for item in items
        if item.frequency is FeeFrequency.ONE_OFF
    ]


def deposit_due(
    term_lines: list[DraftLine], joining_lines: list[DraftLine], *, percent: int
) -> Decimal:
    """What a parent must pay for an offer to convert into an enrolment.

    The one-off joining charges in full, plus a percentage of the first term's
    fees. A school that asks for the whole term up front loses places it would
    have filled; one that asks for nothing fills places that never pay.
    """
    if not 0 <= percent <= 100:
        raise ValueError("deposit percent must be between 0 and 100")
    joining = sum((signed(line) for line in joining_lines), ZERO)
    term = sum((signed(line) for line in term_lines), ZERO)
    return shillings(joining + term * Decimal(percent) / Decimal(100))


def due_date_for(issued_on: date, *, term_starts_on: date, grace_days: int = 14) -> date:
    """When a term invoice falls due.

    Never before the term starts: an invoice raised in the holidays that is
    already overdue on the first day of term tells a parent the system is
    wrong, and they are right.
    """
    return max(issued_on, term_starts_on) + timedelta(days=grace_days)


@dataclass(frozen=True, slots=True)
class InvoiceFacts:
    """The minimum needed to read an invoice's state. Mirrors the shape the
    web app's `src/domain/fees.ts` works with, deliberately."""

    billed: Decimal
    settled: Decimal
    due_date: date
    is_void: bool


def balance_of(invoice: InvoiceFacts) -> Decimal:
    return shillings(max(invoice.billed - invoice.settled, ZERO))


def credit_of(invoice: InvoiceFacts) -> Decimal:
    """Money received beyond what was billed. It is the school's to carry
    forward, not to keep quiet about."""
    return shillings(max(invoice.settled - invoice.billed, ZERO))


def status_of(invoice: InvoiceFacts, as_of: date) -> InvoiceStatus:
    """The day is an argument. A statement produced for last month must not be
    read against today."""
    if invoice.is_void:
        return InvoiceStatus.VOID
    balance = balance_of(invoice)
    if balance == ZERO:
        return InvoiceStatus.SETTLED
    if as_of > invoice.due_date:
        return InvoiceStatus.IN_ARREARS
    return InvoiceStatus.PART_PAID if invoice.settled > ZERO else InvoiceStatus.OPEN


def days_overdue(invoice: InvoiceFacts, as_of: date) -> int:
    if balance_of(invoice) == ZERO or invoice.is_void:
        return 0
    return max((as_of - invoice.due_date).days, 0)


def can_accept_payment(invoice: InvoiceFacts) -> bool:
    """A void or settled invoice must not take money: the callback would land
    in suspense and somebody would spend an afternoon untangling it."""
    return not invoice.is_void and balance_of(invoice) > ZERO


def total_outstanding(invoices: list[InvoiceFacts]) -> Decimal:
    """What a guardian owes across every child they are linked to."""
    return shillings(
        sum((balance_of(i) for i in invoices if not i.is_void), ZERO)
    )
