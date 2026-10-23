"""Domain tests. No database, no fixtures, no event loop — just the rules."""

from decimal import Decimal
from uuid import uuid4

from app.domain.payments import Disposition, InvoiceFacts, decide

SCHOOL = uuid4()


def invoice(billed: str, settled: str = "0", void: bool = False) -> InvoiceFacts:
    return InvoiceFacts(uuid4(), SCHOOL, Decimal(billed), Decimal(settled), void)


def test_replayed_callback_is_a_no_op() -> None:
    d = decide(amount=Decimal("150000"), currency="TZS",
               already_recorded=True, invoice=invoice("340000"))
    assert d.disposition is Disposition.IGNORE_DUPLICATE
    assert d.applied == 0


def test_duplicate_wins_even_when_the_invoice_is_void() -> None:
    """Ordering matters: a replay must not depend on anything else holding still."""
    d = decide(amount=Decimal("150000"), currency="TZS",
               already_recorded=True, invoice=invoice("340000", void=True))
    assert d.disposition is Disposition.IGNORE_DUPLICATE


def test_payment_with_no_invoice_goes_to_suspense() -> None:
    d = decide(amount=Decimal("150000"), currency="TZS",
               already_recorded=False, invoice=None)
    assert d.disposition is Disposition.SUSPENSE
    assert d.applied == 0


def test_exact_balance_settles_the_invoice() -> None:
    d = decide(amount=Decimal("150000"), currency="TZS",
               already_recorded=False, invoice=invoice("340000", "190000"))
    assert d.disposition is Disposition.CREDIT_INVOICE
    assert d.applied == Decimal("150000")
    assert d.overpaid == 0


def test_overpayment_settles_and_holds_the_remainder() -> None:
    d = decide(amount=Decimal("200000"), currency="TZS",
               already_recorded=False, invoice=invoice("340000", "190000"))
    assert d.disposition is Disposition.CREDIT_AND_OVERPAY
    assert d.applied == Decimal("150000")
    assert d.overpaid == Decimal("50000")


def test_foreign_currency_is_rejected_not_suspended() -> None:
    d = decide(amount=Decimal("100"), currency="USD",
               already_recorded=False, invoice=invoice("340000"))
    assert d.disposition is Disposition.REJECT
