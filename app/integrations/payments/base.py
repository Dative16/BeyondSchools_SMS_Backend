"""The payment abstraction.

Every aggregator is an adapter behind this protocol. Switching from Selcom to
Azampay should mean writing one new adapter, not touching the fee ledger.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

# The vocabulary belongs to the ledger, not to any one aggregator, so it lives
# in the domain and is re-exported here for the adapters that import it.
from app.domain.payments import Channel, PaymentState

__all__ = [
    "CallbackEvent",
    "Channel",
    "ChargeRequest",
    "ChargeResult",
    "PaymentProvider",
    "PaymentState",
    "SettlementLine",
]


@dataclass(frozen=True, slots=True)
class ChargeRequest:
    invoice_id: UUID
    school_id: UUID
    amount: Decimal
    currency: str
    msisdn: str
    channel: Channel
    reference: str


@dataclass(frozen=True, slots=True)
class ChargeResult:
    external_ref: str
    state: PaymentState
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CallbackEvent:
    """A verified callback. Never construct one without checking the signature."""

    external_ref: str
    amount: Decimal
    currency: str
    channel: Channel
    state: PaymentState
    msisdn: str
    reference: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SettlementLine:
    external_ref: str
    amount: Decimal
    settled_at: str
    raw: dict[str, Any]


class PaymentProvider(Protocol):
    """Four methods. Anything an aggregator needs fits in them."""

    name: str

    async def initiate(self, request: ChargeRequest) -> ChargeResult:
        """Push a collection request to the customer's handset."""
        ...

    def verify_callback(self, headers: dict[str, str], body: bytes) -> CallbackEvent:
        """Check the signature and parse. Raises on a forged or replayed payload."""
        ...

    async def query_status(self, external_ref: str) -> ChargeResult:
        """Ask the aggregator what happened, for when a callback never arrived."""
        ...

    async def reconcile(self, day: str) -> list[SettlementLine]:
        """Pull one day's settlement report for the nightly reconciliation job."""
        ...
