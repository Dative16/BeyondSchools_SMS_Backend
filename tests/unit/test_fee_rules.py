"""Discounts and the term invoice run, tested without a database.

The run decisions carry the weight here. A term run walks several hundred
children, and the two properties that make that survivable — it can be
previewed, and it can be repeated — are both decided by one pure function.
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.domain.invoicing import (
    Discount,
    DraftLine,
    FeeCategory,
    InvoiceLineKind,
    RunOutcome,
    RunSummary,
    apply_discounts,
    arrears_line,
    billed_total,
    decide_run,
)

TERM_START = date(2027, 1, 8)
TUITION = uuid4()
TRANSPORT = uuid4()

CATEGORIES = {TUITION: FeeCategory.TUITION, TRANSPORT: FeeCategory.TRANSPORT}


def lines() -> list[DraftLine]:
    return [
        DraftLine("Tuition, Term 1", Decimal("300000"), InvoiceLineKind.CHARGE, TUITION),
        DraftLine("Transport", Decimal("60000"), InvoiceLineKind.CHARGE, TRANSPORT),
    ]


# --------------------------------------------------------------------------
# Discounts
# --------------------------------------------------------------------------
def test_a_discount_is_its_own_line_not_a_reduced_charge() -> None:
    """A parent can see what they were charged and what they were let off,
    which is the difference between a bill they trust and one they ring up
    about."""
    out = apply_discounts(
        lines(), [Discount(label="Staff child", percent=50)], as_of=TERM_START
    )

    charges = [line for line in out if line.kind is InvoiceLineKind.CHARGE]
    discounts = [line for line in out if line.kind is InvoiceLineKind.DISCOUNT]

    assert [line.amount for line in charges] == [Decimal("300000"), Decimal("60000")]
    assert discounts[0].amount == Decimal("180000.00")
    assert billed_total(out) == Decimal("180000.00")


def test_a_scoped_percentage_does_not_touch_the_rest() -> None:
    """Half off tuition must not quietly discount the bus."""
    out = apply_discounts(
        lines(),
        [Discount(label="Half tuition", percent=50, category=FeeCategory.TUITION)],
        as_of=TERM_START,
        categories=CATEGORIES,
    )

    discount = next(line for line in out if line.kind is InvoiceLineKind.DISCOUNT)
    assert discount.amount == Decimal("150000.00")


def test_a_fixed_discount_is_capped_at_what_is_charged() -> None:
    """A 500,000 bursary against a 360,000 bill is a 360,000 discount, not a
    credit the school owes."""
    out = apply_discounts(
        lines(), [Discount(label="Full bursary", amount=Decimal("500000"))], as_of=TERM_START
    )

    discount = next(line for line in out if line.kind is InvoiceLineKind.DISCOUNT)
    assert discount.amount == Decimal("360000.00")
    assert billed_total(out) == Decimal("0.00")


def test_a_discount_outside_its_dates_is_not_applied() -> None:
    """A bursary ended on a date, rather than unpicked from six bills."""
    ended = Discount(
        label="Last year's bursary",
        percent=50,
        starts_on=date(2026, 1, 1),
        ends_on=date(2026, 12, 31),
    )
    future = Discount(label="Next year's", percent=50, starts_on=date(2028, 1, 1))

    out = apply_discounts(lines(), [ended, future], as_of=TERM_START)

    assert not [line for line in out if line.kind is InvoiceLineKind.DISCOUNT]


def test_two_discounts_both_apply() -> None:
    out = apply_discounts(
        lines(),
        [
            Discount(label="Sibling", percent=10),
            Discount(label="Staff child", amount=Decimal("50000")),
        ],
        as_of=TERM_START,
    )

    discounts = [line for line in out if line.kind is InvoiceLineKind.DISCOUNT]
    assert len(discounts) == 2
    assert billed_total(out) == Decimal("274000.00")


def test_a_discount_against_a_category_nobody_is_charged_for_is_skipped() -> None:
    out = apply_discounts(
        lines(),
        [Discount(label="Boarding help", percent=50, category=FeeCategory.BOARDING)],
        as_of=TERM_START,
        categories=CATEGORIES,
    )

    assert not [line for line in out if line.kind is InvoiceLineKind.DISCOUNT]


def test_arrears_are_carried_as_their_own_line() -> None:
    """A parent has to see that they are being asked for two terms, or the new
    invoice reads as a price rise."""
    line = arrears_line(Decimal("45000"))

    assert line.kind is InvoiceLineKind.ARREARS_BROUGHT_FORWARD
    assert line.item == "Arrears brought forward"
    assert billed_total([*lines(), line]) == Decimal("405000.00")


# --------------------------------------------------------------------------
# The term run
# --------------------------------------------------------------------------
def decision(**overrides):
    defaults = {
        "enrolment_id": uuid4(),
        "student_id": uuid4(),
        "already_invoiced": False,
        "has_published_structure": True,
        "billed": Decimal("320000"),
        "dry_run": False,
    }
    return decide_run(**{**defaults, **overrides})


def test_an_ordinary_child_is_billed() -> None:
    assert decision().outcome is RunOutcome.RAISED


def test_a_preview_decides_everything_and_writes_nothing() -> None:
    """How a bursar finds out that Standard 6's structure is still a draft
    before 38 invoices go out rather than after."""
    assert decision(dry_run=True).outcome is RunOutcome.WOULD_RAISE


def test_a_child_who_already_has_an_invoice_is_skipped() -> None:
    """The property that makes the run repeatable. A run over 500 children
    will be interrupted eventually, and running it again has to be the safe
    recovery rather than the thing that double-bills a school."""
    outcome = decision(already_invoiced=True)

    assert outcome.outcome is RunOutcome.ALREADY_INVOICED
    assert outcome.billed == Decimal(0)


def test_a_level_with_no_published_structure_is_a_problem_not_a_zero_bill() -> None:
    """Billing nothing would look like success. This is the case the preview
    exists to surface."""
    outcome = decision(has_published_structure=False)

    assert outcome.outcome is RunOutcome.NO_STRUCTURE
    assert outcome.is_problem


def test_nothing_to_bill_is_not_an_error() -> None:
    """A level whose items are all annual, in a term that is not the first."""
    outcome = decision(billed=Decimal(0))

    assert outcome.outcome is RunOutcome.NOTHING_DUE
    assert not outcome.is_problem


def test_the_summary_adds_up_what_the_run_did() -> None:
    summary = RunSummary(
        decisions=(
            decision(),
            decision(),
            decision(already_invoiced=True),
            decision(has_published_structure=False),
        )
    )

    assert summary.raised == 2
    assert summary.skipped == 1
    assert summary.total_billed == Decimal("640000.00")
    assert len(summary.problems) == 1
