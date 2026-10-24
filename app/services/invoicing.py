"""Invoicing: raising a bill, and recording money against it by hand.

Phase 1 runs on manual entry — no aggregator is wired up — so the two ways
money moves are the webhook that does not exist yet and the audited manual
entry in this file. The arithmetic is all in `app/domain/invoicing.py`; this
service owns the sequence and the trail.

Three rules this file exists to keep:

**Nothing is invoiced from a draft fee structure.** A school that has not
published Standard 6's fees for the year cannot raise Standard 6 invoices,
which is why the dashboard warns about it in week four rather than letting 38
invoices go out at last year's prices.

**A balance is never assigned.** Lines and payments are written; the database
derives `billed` and `settled` from them. A correction is a credit note or a
payment, never an edit — and the guard trigger means that is a fact rather
than a convention.

**A manual payment names its author.** The database refuses one that does not.
The first fee dispute a school cannot settle is the one where nobody knows who
typed the number in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.domain.invoicing import (
    DraftLine,
    InvoiceLineKind,
    billed_total,
    deposit_due,
    lines_for_joining,
    lines_for_term,
    shillings,
)
from app.domain.payments import Channel, PaymentState
from app.domain.roles import Permission, Principal, can
from app.models.fees import Invoice, InvoiceKind
from app.repositories.audit import AuditRepository
from app.repositories.base import UnitOfWork
from app.repositories.fees import FeesRepository, InvoiceTotals
from app.repositories.payments import PaymentRepository
from app.services.auth import RequestContext


class InvoicingError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotAllowed(InvoicingError):
    """The principal lacks the capability."""


class NotFound(InvoicingError):
    """No such record in this school."""


class Invalid(InvoicingError):
    """The bill cannot be raised, or the money cannot be recorded."""


@dataclass(frozen=True, slots=True)
class RaisedInvoice:
    invoice: Invoice
    billed: Decimal
    lines: tuple[DraftLine, ...]


class InvoicingService:
    def __init__(
        self,
        uow: UnitOfWork,
        fees: FeesRepository,
        payments: PaymentRepository,
        audit: AuditRepository,
    ) -> None:
        self._uow = uow
        self._fees = fees
        self._payments = payments
        self._audit = audit

    def _require(self, principal: Principal, permission: Permission) -> None:
        if not can(principal, permission):
            raise NotAllowed(f"this account may not {permission.value.replace(':', ' ')}")

    # ----------------------------------------------------------------------
    # The deposit an offer asks for
    # ----------------------------------------------------------------------
    async def raise_deposit_invoice(
        self,
        *,
        school_id: UUID,
        application_id: UUID,
        academic_year_id: UUID,
        level_id: UUID,
        term_id: UUID | None,
        term_index: int,
        year_label: str,
        deposit_percent: int,
        grace_days: int,
        issued_on: date,
        actor: Principal,
        context: RequestContext,
    ) -> RaisedInvoice:
        """The bill that converts an offer into a pupil.

        Two kinds of charge go on it: the one-off joining items in full — the
        admission fee, the uniform, the first set of exercise books — and a
        percentage of the first term's fees. A school that asks for the whole
        term up front loses places it would have filled; one that asks for
        nothing fills places that never pay.

        Attached to the application rather than to an enrolment, because there
        is no enrolment yet. That is the whole reason `invoice` allows either.
        """
        items = await self._fees.published_items(
            academic_year_id=academic_year_id, level_id=level_id, stream_id=None
        )
        if not items:
            raise Invalid(
                "this level has no published fee structure for the year, so no "
                "deposit can be worked out"
            )

        term_lines = lines_for_term(
            items, term_index=term_index, is_first_term_of_year=term_index == 1
        )
        joining_lines = lines_for_joining(items)
        due = deposit_due(term_lines, joining_lines, percent=deposit_percent)
        if due <= Decimal(0):
            raise Invalid("the deposit for this level works out as nothing")

        # The deposit invoice carries the joining charges as they are, plus one
        # line for the part of the term being asked for. Showing the term as a
        # single "deposit" line rather than a fraction of each charge is what
        # a parent can actually check against what they were told.
        term_portion = shillings(due - sum((line.amount for line in joining_lines), Decimal(0)))
        lines = [*joining_lines]
        if term_portion > Decimal(0):
            lines.append(
                DraftLine(
                    item=f"Deposit, {deposit_percent}% of Term {term_index} fees",
                    amount=term_portion,
                    kind=InvoiceLineKind.CHARGE,
                )
            )

        sequence = await self._fees.next_invoice_sequence(
            school_id=school_id, scope=f"{year_label}-deposit"
        )
        invoice = await self._fees.create_invoice(
            school_id=school_id,
            reference=f"DEP/{year_label}/{sequence:04d}",
            kind=InvoiceKind.DEPOSIT,
            application_id=application_id,
            term_id=term_id,
            issued_on=issued_on,
            due_date=date.fromordinal(issued_on.toordinal() + grace_days),
            created_by_id=actor.user_id,
        )
        await self._fees.add_lines(
            invoice_id=invoice.id, school_id=school_id, lines=lines
        )

        await self._audit.record(
            school_id=school_id,
            action="invoice.raised",
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            entity_type="invoice",
            entity_id=invoice.id,
            changed={"kind": "deposit", "reference": invoice.reference},
            note=f"{len(lines)} line(s), {billed_total(lines)} due",
            ip=context.ip,
            user_agent=context.user_agent,
        )

        return RaisedInvoice(
            invoice=invoice, billed=billed_total(lines), lines=tuple(lines)
        )

    async def totals(self, invoice_id: UUID) -> InvoiceTotals | None:
        return await self._fees.totals(invoice_id)

    # ----------------------------------------------------------------------
    # Money, by hand
    # ----------------------------------------------------------------------
    async def record_manual_payment(
        self,
        principal: Principal,
        *,
        invoice_id: UUID,
        amount: Decimal,
        channel: Channel,
        reference: str,
        received_on: date | None,
        msisdn: str | None,
        note: str | None,
        context: RequestContext,
    ) -> UUID:
        """Record money a bursar has in hand.

        Phase 1 has no aggregator, so this is how a deposit becomes a pupil and
        how term fees get settled. Three things make it safe enough to be the
        only path:

        The entry names its author, and the database refuses one that does not.
        The reference is unique per school, so entering the same receipt twice
        is caught rather than doubling a credit. And the amount moves
        `invoice.settled` by trigger, so there is no field anybody can type a
        balance into.
        """
        self._require(principal, Permission.PAYMENT_RECORD_MANUAL)

        if principal.school_id is None:
            raise NotFound("this account belongs to no school")
        school_id = principal.school_id

        if amount <= Decimal(0):
            raise Invalid("a payment has to be more than nothing")
        if not reference.strip():
            raise Invalid("a manual payment needs a receipt number")

        async with self._uow:
            totals = await self._fees.totals(invoice_id)
            if totals is None:
                raise NotFound("no such invoice")
            if totals.is_void:
                raise Invalid("that invoice has been voided; it cannot take money")

            # Scoped per school by the unique index, and prefixed so a
            # hand-written receipt number can never collide with an
            # aggregator's transaction id once one is wired up.
            external_ref = f"MANUAL-{reference.strip().upper()}"
            if await self._payments.external_ref_exists(school_id, external_ref):
                raise Invalid(
                    f"receipt {reference} has already been recorded against this school"
                )

            applied = min(amount, totals.balance)
            payment_id = await self._payments.record_payment(
                school_id=school_id,
                invoice_id=invoice_id,
                external_ref=external_ref,
                amount=amount,
                applied=applied,
                channel=str(channel),
                msisdn=msisdn or "",
                raw={
                    "source": "manual",
                    "receipt": reference.strip(),
                    "recorded_by": str(principal.user_id),
                },
                is_manual=True,
                recorded_by_id=principal.user_id,
                received_on=received_on,
                note=note,
                state=PaymentState.SETTLED,
            )

            await self._audit.record(
                school_id=school_id,
                action="payment.recorded_manually",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="invoice",
                entity_id=invoice_id,
                changed={
                    "amount": str(amount),
                    "applied": str(applied),
                    "channel": str(channel),
                    "receipt": reference.strip(),
                },
                note=note,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        # An overpayment is not an error: the remainder is the family's
        # credit, visible on the payment rather than quietly absorbed.
        return payment_id
