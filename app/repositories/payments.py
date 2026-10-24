"""Payment and invoice persistence.

The only file in the payment path that writes SQL. Two things it deliberately
does not do: it does not decide whether a payment is accepted — that is
`app/domain/payments.py` — and it does not touch `invoice.settled`, which the
database derives from the rows written here.

`record_payment` relies on the unique index on `(school_id, external_ref)`
rather than checking first and then inserting. A replayed webhook that arrives
twice in the same second would pass both checks and insert twice; the index is
the only thing that cannot be raced.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.payments import InvoiceFacts, PaymentState
from app.models.payment import Payment


class PaymentRepository(Protocol):
    """What the payment and invoicing services need from storage."""

    async def external_ref_exists(self, school_id: UUID, external_ref: str) -> bool: ...

    async def unmatched(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

    async def invoice_facts(self, invoice_id: UUID) -> InvoiceFacts | None: ...

    async def find_invoice_by_reference(
        self, school_id: UUID, reference: str
    ) -> InvoiceFacts | None: ...

    async def find_open_invoice_by_msisdn(
        self, school_id: UUID, msisdn: str, amount: Decimal
    ) -> InvoiceFacts | None: ...

    async def record_payment(
        self,
        *,
        school_id: UUID,
        invoice_id: UUID | None,
        external_ref: str,
        amount: Decimal,
        applied: Decimal,
        channel: str,
        msisdn: str,
        raw: dict[str, Any],
        is_manual: bool = False,
        recorded_by_id: UUID | None = None,
        received_on: date | None = None,
        note: str | None = None,
        state: PaymentState = PaymentState.SETTLED,
    ) -> UUID: ...

    async def record_suspense(
        self,
        *,
        school_id: UUID,
        external_ref: str,
        amount: Decimal,
        channel: str,
        msisdn: str,
        raw: dict[str, Any],
        reason: str,
    ) -> UUID: ...


class SqlPaymentRepository:
    """SQLAlchemy implementation. The only file in the payment path that
    writes SQL — if you find yourself reaching for `select()` in a service or a
    route, the query belongs here instead."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def external_ref_exists(self, school_id: UUID, external_ref: str) -> bool:
        """Whether this school has already recorded this reference.

        Checked so a user reads a sentence rather than a constraint violation.
        The unique index is what actually guarantees it.
        """
        found = await self._session.scalar(
            select(Payment.id).where(Payment.external_ref == external_ref).limit(1)
        )
        return found is not None

    async def invoice_facts(self, invoice_id: UUID) -> InvoiceFacts | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id, school_id, billed, settled, is_void "
                    "  FROM invoice WHERE id = :id"
                ),
                {"id": invoice_id},
            )
        ).one_or_none()
        return _facts(row) if row else None

    async def find_invoice_by_reference(
        self, school_id: UUID, reference: str
    ) -> InvoiceFacts | None:
        """The first and best of the three ways a payment finds its invoice.

        A parent who typed the reference from their bill is telling us exactly
        which invoice to credit.
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT id, school_id, billed, settled, is_void "
                    "  FROM invoice "
                    " WHERE upper(reference) = upper(:reference) AND NOT is_void "
                    " LIMIT 1"
                ),
                {"reference": reference.strip()},
            )
        ).one_or_none()
        return _facts(row) if row else None

    async def find_open_invoice_by_msisdn(
        self, school_id: UUID, msisdn: str, amount: Decimal
    ) -> InvoiceFacts | None:
        """The fallback: money from a number we know, with no reference.

        Ordered so the closest match to the amount wins, then the oldest
        unpaid bill. An exact match on the outstanding balance is almost
        always the invoice the parent meant; where it is not, the payment goes
        to the oldest debt, which is what a bursar would do by hand.

        Returns nothing where the number is linked to more than one child with
        an open balance: guessing between two siblings is how a payment lands
        on the wrong one and somebody spends an afternoon unpicking it.
        """
        rows = await self._session.execute(
            text(
                "SELECT i.id, i.school_id, i.billed, i.settled, i.is_void, "
                "       i.student_id "
                "  FROM invoice i "
                "  JOIN guardian_student gs ON gs.student_id = i.student_id "
                "  JOIN guardian g ON g.id = gs.guardian_id "
                " WHERE g.phone = :msisdn AND NOT i.is_void AND i.billed > i.settled "
                " ORDER BY abs((i.billed - i.settled) - CAST(:amount AS numeric)), "
                "          i.due_date "
            ),
            {"msisdn": msisdn.strip(), "amount": amount},
        )
        found = rows.all()
        if not found:
            return None

        children = {row.student_id for row in found}
        if len(children) > 1 and abs(
            (found[0].billed - found[0].settled) - amount
        ) != Decimal(0):
            # Two children, and the amount does not settle either exactly.
            # Suspense is the honest answer.
            return None
        return _facts(found[0])

    async def record_payment(
        self,
        *,
        school_id: UUID,
        invoice_id: UUID | None,
        external_ref: str,
        amount: Decimal,
        applied: Decimal,
        channel: str,
        msisdn: str,
        raw: dict[str, Any],
        is_manual: bool = False,
        recorded_by_id: UUID | None = None,
        received_on: date | None = None,
        note: str | None = None,
        state: PaymentState = PaymentState.SETTLED,
    ) -> UUID:
        payment = Payment(
            school_id=school_id,
            invoice_id=invoice_id,
            external_ref=external_ref,
            amount=amount,
            applied=applied,
            channel=channel,
            state=state,
            msisdn=msisdn or None,
            raw=raw,
            is_manual=is_manual,
            recorded_by_id=recorded_by_id,
            settled_on=received_on,
            reason=note,
            provider="manual" if is_manual else None,
        )
        self._session.add(payment)
        await self._session.flush()
        return payment.id

    async def record_suspense(
        self,
        *,
        school_id: UUID,
        external_ref: str,
        amount: Decimal,
        channel: str,
        msisdn: str,
        raw: dict[str, Any],
        reason: str,
    ) -> UUID:
        """Money that arrived with nothing to match it.

        Recorded, visible, and waiting for a bursar. Dropping it on the floor
        or guessing an invoice for it is how a month-end stops balancing.
        """
        payment = Payment(
            school_id=school_id,
            invoice_id=None,
            external_ref=external_ref,
            amount=amount,
            applied=Decimal(0),
            channel=channel,
            state=PaymentState.SETTLED,
            msisdn=msisdn or None,
            raw=raw,
            reason=reason,
        )
        self._session.add(payment)
        await self._session.flush()
        return payment.id

    async def unmatched(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The bursar's suspense list."""
        rows = await self._session.execute(
            text(
                "SELECT id, external_ref, amount, channel::text AS channel, msisdn, "
                "       payer_reference, received_at, reason "
                "  FROM payment "
                " WHERE invoice_id IS NULL AND state = 'settled' "
                " ORDER BY received_at DESC "
                " LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [dict(row._mapping) for row in rows]


def _facts(row: Any) -> InvoiceFacts:
    return InvoiceFacts(
        invoice_id=row.id,
        school_id=row.school_id,
        billed=Decimal(row.billed),
        settled=Decimal(row.settled),
        is_void=row.is_void,
    )
