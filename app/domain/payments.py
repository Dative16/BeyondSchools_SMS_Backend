"""Payment rules. Pure.

Nothing here opens a connection, reads a clock or calls an aggregator. Give it
the facts and it tells you what should happen; the service layer is what makes
it happen. That is why these rules are testable without a database, and why the
nastiest cases — a replayed callback, an overpayment, money for a child who has
left — are cheap to pin down in unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class Channel(StrEnum):
    """How the money arrived.

    Defined here rather than in the aggregator adapter because the ledger, the
    bursar's statement and the models all name a channel, and none of them
    should have to import an integration to do it. An adapter's job is to map
    whatever the provider calls a rail onto one of these.
    """

    MPESA = "mpesa"
    AIRTEL_MONEY = "airtel_money"
    MIXX = "mixx"
    HALOPESA = "halopesa"
    BANK = "bank"
    CARD = "card"
    COUNTER = "counter"


class PaymentState(StrEnum):
    PENDING = "pending"
    SETTLED = "settled"
    FAILED = "failed"
    REVERSED = "reversed"


class Disposition(StrEnum):
    """What a verified callback should do to the ledger."""

    CREDIT_INVOICE = "credit_invoice"
    CREDIT_AND_OVERPAY = "credit_and_overpay"
    SUSPENSE = "suspense"
    IGNORE_DUPLICATE = "ignore_duplicate"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class InvoiceFacts:
    """The minimum the rules need to know about an invoice."""

    invoice_id: UUID
    school_id: UUID
    billed: Decimal
    settled: Decimal
    is_void: bool

    @property
    def balance(self) -> Decimal:
        return self.billed - self.settled


@dataclass(frozen=True, slots=True)
class Decision:
    disposition: Disposition
    applied: Decimal
    overpaid: Decimal
    reason: str


def decide(
    *,
    amount: Decimal,
    currency: str,
    already_recorded: bool,
    invoice: InvoiceFacts | None,
    school_currency: str = "TZS",
) -> Decision:
    """Decide what a verified callback does to the ledger.

    The ordering matters. Duplicates are checked first because a replayed
    callback must be a no-op even when the invoice has since been voided or
    settled by other means — the aggregator will replay, and the only safe
    answer is the one that does not depend on anything else having stayed still.
    """
    if already_recorded:
        return Decision(Disposition.IGNORE_DUPLICATE, Decimal(0), Decimal(0),
                        "external_ref already recorded for this school")

    if amount <= 0:
        return Decision(Disposition.REJECT, Decimal(0), Decimal(0),
                        "non-positive amount")

    if currency != school_currency:
        return Decision(Disposition.REJECT, Decimal(0), Decimal(0),
                        f"currency {currency} is not {school_currency}")

    if invoice is None or invoice.is_void:
        return Decision(Disposition.SUSPENSE, Decimal(0), Decimal(0),
                        "no open invoice matched this payment")

    balance = invoice.balance
    if balance <= 0:
        return Decision(Disposition.SUSPENSE, Decimal(0), Decimal(0),
                        "invoice already settled")

    if amount <= balance:
        return Decision(Disposition.CREDIT_INVOICE, amount, Decimal(0),
                        "credited in full")

    return Decision(Disposition.CREDIT_AND_OVERPAY, balance, amount - balance,
                    "invoice settled, remainder held as credit")


def arrears_age_bucket(days_overdue: int) -> str:
    """How the bursar's arrears report groups a balance."""
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    return "over-60"
