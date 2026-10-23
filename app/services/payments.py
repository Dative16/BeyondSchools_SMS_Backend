"""Payment service: the worked example of the layering.

Read this one before writing another service. It imports no FastAPI, raises no
HTTPException, and writes no SQL. It takes facts, asks the domain what should
happen, and tells the repository to do it.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.payments import Decision, Disposition, InvoiceFacts, decide
from app.integrations.payments.base import CallbackEvent
from app.repositories.base import UnitOfWork
from app.repositories.payments import PaymentRepository


class PaymentRejected(Exception):
    """The service's own error. The API layer maps it to a status code;
    the service does not know what a status code is."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CallbackRecorded:
    payment_id: UUID | None
    disposition: Disposition
    reason: str


class PaymentService:
    def __init__(self, uow: UnitOfWork, payments: PaymentRepository) -> None:
        self._uow = uow
        self._payments = payments

    async def record_callback(
        self, school_id: UUID, event: CallbackEvent
    ) -> CallbackRecorded:
        """Apply one already-verified callback to the ledger.

        The caller verifies the signature before this is reached: an unverified
        payload must never get this far, and this service has no way to tell the
        difference, which is deliberate — one responsibility each.
        """
        async with self._uow:
            duplicate = await self._payments.external_ref_exists(
                school_id, event.external_ref
            )
            invoice = None
            if not duplicate:
                invoice = await self._locate_invoice(school_id, event)

            decision = decide(
                amount=event.amount,
                currency=event.currency,
                already_recorded=duplicate,
                invoice=invoice,
            )

            result = await self._apply(school_id, event, invoice, decision)
            await self._uow.commit()
            return result

    async def _locate_invoice(
        self, school_id: UUID, event: CallbackEvent
    ) -> InvoiceFacts | None:
        """Three ways a payment finds its invoice, best first."""
        if event.reference:
            found = await self._payments.find_invoice_by_reference(
                school_id, event.reference
            )
            if found is not None:
                return found
        return await self._payments.find_open_invoice_by_msisdn(
            school_id, event.msisdn, event.amount
        )

    async def _apply(
        self,
        school_id: UUID,
        event: CallbackEvent,
        invoice: InvoiceFacts | None,
        decision: Decision,
    ) -> CallbackRecorded:
        if decision.disposition is Disposition.IGNORE_DUPLICATE:
            return CallbackRecorded(None, decision.disposition, decision.reason)

        if decision.disposition is Disposition.REJECT:
            raise PaymentRejected(decision.reason)

        if decision.disposition is Disposition.SUSPENSE:
            payment_id = await self._payments.record_suspense(
                school_id=school_id,
                external_ref=event.external_ref,
                amount=event.amount,
                channel=str(event.channel),
                msisdn=event.msisdn,
                raw=event.raw,
                reason=decision.reason,
            )
            return CallbackRecorded(payment_id, decision.disposition, decision.reason)

        payment_id = await self._payments.record_payment(
            school_id=school_id,
            invoice_id=invoice.invoice_id if invoice else None,
            external_ref=event.external_ref,
            amount=event.amount,
            applied=decision.applied,
            channel=str(event.channel),
            msisdn=event.msisdn,
            raw=event.raw,
        )
        return CallbackRecorded(payment_id, decision.disposition, decision.reason)
