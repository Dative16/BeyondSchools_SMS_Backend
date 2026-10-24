"""SMS gateway protocol.

The same shape as the payment abstraction, for the same reason: every Tanzanian
gateway has its own idea of what a send looks like, and the only way to keep
that out of the fee reminders and the admissions OTP is to put one protocol in
front of all of them.

SMS is not a nice-to-have in this product. Many guardians will never open the
portal, so fee reminders, absence alerts and results notices go out by message
with a short link, and the public application form cannot verify a phone
number without one. That makes `SmsSender` a Phase 1 dependency even though the
Communications module is Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class SmsKind(StrEnum):
    """Why a message is being sent, which decides whether an opt-out applies.

    A fee reminder and an OTP are transactional: a guardian who has opted out
    of messages has not opted out of being told their child's fees are due, and
    a parent who asked for a code has asked to be sent one. A newsletter is
    not, and `sms_opted_out` on the guardian silences it.
    """

    TRANSACTIONAL = "transactional"
    MARKETING = "marketing"


class SmsState(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SmsMessage:
    #: Normalised to 0754118902 by app.domain.admissions.normalise_phone.
    to: str
    body: str
    kind: SmsKind = SmsKind.TRANSACTIONAL
    #: Set for anything a delivery log should be able to tie back to a record.
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class SmsResult:
    external_ref: str | None
    state: SmsState
    raw: dict[str, Any]
    error: str | None = None


class SmsSender(Protocol):
    """Two methods. A gateway that needs more than this is doing too much."""

    name: str

    async def send(self, message: SmsMessage) -> SmsResult:
        """Hand one message to the gateway. Never raises for a rejected
        message: a failed send is a result, because a fee-reminder run of 400
        messages must not stop at the first bad number."""
        ...

    async def status(self, external_ref: str) -> SmsResult:
        """Ask the gateway what happened, for the delivery log."""
        ...


def to_e164(phone: str) -> str:
    """0754118902 to +255754118902.

    Stored and compared in the local form, because that is how parents write
    it and how duplicate screening matches it; sent in E.164, because that is
    what every gateway wants.
    """
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0"):
        digits = "255" + digits[1:]
    if not digits.startswith("255"):
        digits = "255" + digits
    return "+" + digits
