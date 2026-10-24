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
    #: Vifaa vya kujifunzia and vifaa vya shule: exercise books, pens, the
    #: termly stationery pack. A charge, which the fee structure already
    #: handles — no new mechanism needed.
    LEARNING_MATERIALS = "learning_materials"
    STATIONERY = "stationery"
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


# --------------------------------------------------------------------------
# Standing discounts
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Discount:
    """A standing reduction for one child: staff child, sibling, bursary.

    Held against the student rather than written into each invoice, so next
    term's run applies it without anybody remembering to — and so a bursary
    can be ended on a date instead of unpicked from six bills.
    """

    label: str
    amount: Decimal | None = None
    percent: int | None = None
    #: Narrow it to one category, for "free tuition, pays transport".
    category: FeeCategory | None = None
    starts_on: date | None = None
    ends_on: date | None = None

    def applies_on(self, day: date) -> bool:
        if self.starts_on is not None and day < self.starts_on:
            return False
        return not (self.ends_on is not None and day > self.ends_on)


def apply_discounts(
    lines: list[DraftLine],
    discounts: list[Discount],
    *,
    as_of: date,
    categories: dict[UUID, FeeCategory] | None = None,
) -> list[DraftLine]:
    """Add a discount line for each reduction that applies.

    Discount *lines* rather than reduced charges, for two reasons. A parent
    looking at a bill can see what they were charged and what they were let
    off, which is the difference between a bill they trust and one they ring
    up about. And the full charge stays on the invoice, so a school can still
    answer what a place actually costs.

    A percentage is taken against the charges it is scoped to, not the whole
    invoice: "half off tuition" must not quietly discount the bus.
    """
    if not discounts:
        return lines

    categories = categories or {}
    charges = [line for line in lines if line.kind is InvoiceLineKind.CHARGE]
    out = list(lines)

    for discount in discounts:
        if not discount.applies_on(as_of):
            continue

        scoped = (
            charges
            if discount.category is None
            else [
                line
                for line in charges
                if line.fee_item_id is not None
                and categories.get(line.fee_item_id) is discount.category
            ]
        )
        base = sum((line.amount for line in scoped), ZERO)
        if base <= ZERO:
            continue

        if discount.amount is not None:
            # Never more than what is being charged: a 200,000 bursary against
            # a 150,000 bill is a 150,000 discount, not a credit.
            reduction = min(shillings(discount.amount), base)
        elif discount.percent is not None:
            reduction = shillings(base * Decimal(discount.percent) / Decimal(100))
        else:
            continue

        if reduction > ZERO:
            out.append(
                DraftLine(
                    item=discount.label,
                    amount=reduction,
                    kind=InvoiceLineKind.DISCOUNT,
                )
            )

    return out


def arrears_line(amount: Decimal) -> DraftLine:
    """Last term's unpaid balance, carried onto this term's bill.

    Its own line rather than folded into the total: a parent has to be able to
    see that they are being asked for two terms, or the new invoice looks like
    a price rise.
    """
    return DraftLine(
        item="Arrears brought forward",
        amount=shillings(amount),
        kind=InvoiceLineKind.ARREARS_BROUGHT_FORWARD,
    )


# --------------------------------------------------------------------------
# A term's invoice run
# --------------------------------------------------------------------------
class RunOutcome(StrEnum):
    WOULD_RAISE = "would_raise"
    RAISED = "raised"
    #: Already has an invoice for this term. The run is safe to repeat, which
    #: matters because sooner or later one will be interrupted halfway.
    ALREADY_INVOICED = "already_invoiced"
    #: No published fee structure for this child's level and year.
    NO_STRUCTURE = "no_structure"
    #: Nothing to bill: every item was zero or out of scope for this term.
    NOTHING_DUE = "nothing_due"


@dataclass(frozen=True, slots=True)
class RunDecision:
    enrolment_id: UUID
    student_id: UUID
    outcome: RunOutcome
    billed: Decimal
    reason: str

    @property
    def is_problem(self) -> bool:
        return self.outcome is RunOutcome.NO_STRUCTURE


def decide_run(
    *,
    enrolment_id: UUID,
    student_id: UUID,
    already_invoiced: bool,
    has_published_structure: bool,
    billed: Decimal,
    dry_run: bool,
) -> RunDecision:
    """What a term's invoice run should do about one child.

    Pure, so a whole run can be previewed before anything is written — which is
    how a bursar finds out that Standard 6's structure is still a draft
    *before* 38 invoices go out rather than after.

    Idempotent by design: a child who already has an invoice for the term is
    skipped rather than billed twice. A run over 500 children will be
    interrupted eventually, and the only safe recovery is to run it again.
    """
    if already_invoiced:
        return RunDecision(
            enrolment_id,
            student_id,
            RunOutcome.ALREADY_INVOICED,
            ZERO,
            "already has an invoice for this term",
        )
    if not has_published_structure:
        return RunDecision(
            enrolment_id,
            student_id,
            RunOutcome.NO_STRUCTURE,
            ZERO,
            "no published fee structure for this level and year",
        )
    if billed <= ZERO:
        return RunDecision(
            enrolment_id, student_id, RunOutcome.NOTHING_DUE, ZERO, "nothing to bill"
        )

    return RunDecision(
        enrolment_id,
        student_id,
        RunOutcome.WOULD_RAISE if dry_run else RunOutcome.RAISED,
        billed,
        "ok",
    )


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What a run did, or would do."""

    decisions: tuple[RunDecision, ...]

    @property
    def raised(self) -> int:
        return sum(
            1
            for d in self.decisions
            if d.outcome in (RunOutcome.RAISED, RunOutcome.WOULD_RAISE)
        )

    @property
    def skipped(self) -> int:
        return sum(1 for d in self.decisions if d.outcome is RunOutcome.ALREADY_INVOICED)

    @property
    def total_billed(self) -> Decimal:
        return shillings(sum((d.billed for d in self.decisions), ZERO))

    @property
    def problems(self) -> tuple[RunDecision, ...]:
        return tuple(d for d in self.decisions if d.is_problem)
