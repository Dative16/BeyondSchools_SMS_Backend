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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.invoicing import (
    DraftLine,
    FeeStructureStatus,
    InvoiceLineKind,
    RunDecision,
    RunOutcome,
    RunSummary,
    apply_discounts,
    arrears_line,
    billed_total,
    decide_run,
    deposit_due,
    due_date_for,
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

    # ----------------------------------------------------------------------
    # Fee structures
    # ----------------------------------------------------------------------
    async def structures(
        self, principal: Principal, *, academic_year_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        self._require(principal, Permission.INVOICE_READ)
        return await self._fees.structures(academic_year_id=academic_year_id)

    async def structure_detail(
        self, principal: Principal, *, structure_id: UUID
    ) -> dict[str, Any]:
        self._require(principal, Permission.INVOICE_READ)
        structure = await self._fees.structure(structure_id)
        if structure is None:
            raise NotFound("no such fee structure")
        return {"structure": structure, "items": await self._fees.items_of(structure_id)}

    async def create_structure(
        self,
        principal: Principal,
        *,
        academic_year_id: UUID,
        level_id: UUID,
        stream_id: UUID | None,
        label: str,
        items: list[dict[str, Any]],
        context: RequestContext,
    ) -> dict[str, Any]:
        """Start a fee structure. It begins as a draft, and nothing can be
        invoiced from a draft."""
        self._require(principal, Permission.FEE_STRUCTURE_MANAGE)
        if principal.school_id is None:
            raise NotFound("this account belongs to no school")

        async with self._uow:
            structure = await self._fees.add_structure(
                school_id=principal.school_id,
                academic_year_id=academic_year_id,
                level_id=level_id,
                stream_id=stream_id,
                label=label.strip(),
                status=FeeStructureStatus.DRAFT,
            )
            if items:
                await self._fees.replace_items(
                    structure_id=structure.id,
                    school_id=principal.school_id,
                    items=items,
                )
            await self._audit.record(
                school_id=principal.school_id,
                action="fee_structure.created",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="fee_structure",
                entity_id=structure.id,
                changed={"label": structure.label, "items": len(items)},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        return await self.structure_detail(principal, structure_id=structure.id)

    async def set_structure_items(
        self,
        principal: Principal,
        *,
        structure_id: UUID,
        items: list[dict[str, Any]],
        context: RequestContext,
    ) -> dict[str, Any]:
        """Replace a draft's items.

        Refused once published. Invoices already raised against a structure
        would otherwise change retrospectively, and a parent who photographed
        a bill and came back three weeks later must find the same figure on it.
        """
        self._require(principal, Permission.FEE_STRUCTURE_MANAGE)
        if principal.school_id is None:
            raise NotFound("this account belongs to no school")

        async with self._uow:
            structure = await self._fees.structure(structure_id)
            if structure is None:
                raise NotFound("no such fee structure")
            if structure.status is FeeStructureStatus.PUBLISHED:
                raise Invalid(
                    "this structure is published and cannot be edited; invoices have "
                    "been raised from it. Create next year's structure instead"
                )

            await self._fees.replace_items(
                structure_id=structure_id,
                school_id=principal.school_id,
                items=items,
            )
            await self._audit.record(
                school_id=principal.school_id,
                action="fee_structure.items_set",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="fee_structure",
                entity_id=structure_id,
                changed={"items": len(items)},
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        return await self.structure_detail(principal, structure_id=structure_id)

    async def publish_structure(
        self, principal: Principal, *, structure_id: UUID, context: RequestContext
    ) -> dict[str, Any]:
        """Make a structure live, and freeze it.

        The gate that matters: an empty structure cannot be published. A
        published structure with no items would let a term run complete
        "successfully" having billed every child nothing, which is worse than
        failing — it looks like it worked.
        """
        self._require(principal, Permission.FEE_STRUCTURE_MANAGE)

        async with self._uow:
            structure = await self._fees.structure(structure_id)
            if structure is None:
                raise NotFound("no such fee structure")
            if structure.status is FeeStructureStatus.PUBLISHED:
                raise Invalid("that structure is already published")

            items = await self._fees.items_of(structure_id)
            if not items:
                raise Invalid(
                    "a structure with no fee items cannot be published: a term run "
                    "would bill every child nothing and look as though it worked"
                )

            structure.status = FeeStructureStatus.PUBLISHED
            structure.published_at = datetime.now(UTC)
            structure.published_by_id = principal.user_id

            await self._audit.record(
                school_id=structure.school_id,
                action="fee_structure.published",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="fee_structure",
                entity_id=structure_id,
                changed={
                    "label": structure.label,
                    "items": len(items),
                    "total": str(sum(Decimal(item["amount"]) for item in items)),
                },
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        return await self.structure_detail(principal, structure_id=structure_id)

    # ----------------------------------------------------------------------
    # A term's invoice run
    # ----------------------------------------------------------------------
    async def run_term_invoices(
        self,
        principal: Principal,
        *,
        term_id: UUID,
        issued_on: date | None,
        dry_run: bool,
        context: RequestContext,
    ) -> RunSummary:
        """Raise this term's invoices for every child on the roll.

        Two properties make this safe to point at 500 children.

        **It can be previewed.** `dry_run` walks the whole roll and decides
        everything without writing a row, so a bursar finds out that Standard
        6's structure is still a draft before the invoices go out rather than
        after. The decisions come back per child, named.

        **It can be repeated.** A child who already has an invoice for the
        term is skipped. A run this size will be interrupted sooner or later —
        a timeout, a restart, a closed laptop — and running it again has to be
        the safe recovery rather than the thing that double-bills a school.
        """
        self._require(principal, Permission.INVOICE_MANAGE)
        if principal.school_id is None:
            raise NotFound("this account belongs to no school")
        school_id = principal.school_id

        term = await self._fees.term(term_id)
        if term is None:
            raise NotFound("no such term")
        day = issued_on or date.today()

        roll = await self._fees.enrolments_to_invoice(term["academic_year_id"])
        already = await self._fees.enrolments_already_invoiced(term_id)
        structures = await self._fees.published_items_by_level(term["academic_year_id"])
        discounts = await self._fees.discounts_for(as_of=day)

        categories = {
            item.fee_item_id: item.category
            for items in structures.values()
            for item in items
        }

        decisions: list[RunDecision] = []
        for child in roll:
            # A structure scoped to the child's stream wins over the
            # level-wide one: that is how a school says "boarders pay more"
            # without writing two whole structures.
            items = structures.get(
                (child["level_id"], child["stream_id"])
            ) or structures.get((child["level_id"], None))

            lines: list[DraftLine] = []
            if items:
                lines = lines_for_term(
                    items,
                    term_index=term["index"],
                    is_first_term_of_year=term["index"] == 1,
                )
                lines = apply_discounts(
                    lines,
                    discounts.get(child["student_id"], []),
                    as_of=day,
                    categories=categories,
                )
                # Arrears follow the child, not the closed enrolment, so they
                # land on this term's bill as their own line — or the new
                # invoice reads as a price rise.
                carried = Decimal(child["arrears_brought_forward"] or 0)
                if carried > Decimal(0) and term["index"] == 1:
                    lines.append(arrears_line(carried))

            decision = decide_run(
                enrolment_id=child["enrolment_id"],
                student_id=child["student_id"],
                already_invoiced=child["enrolment_id"] in already,
                has_published_structure=bool(items),
                billed=billed_total(lines) if lines else Decimal(0),
                dry_run=dry_run,
            )
            decisions.append(decision)

            if dry_run or decision.outcome is not RunOutcome.RAISED:
                continue

            async with self._uow:
                sequence = await self._fees.next_invoice_sequence(
                    school_id=school_id, scope=f"{term['academic_year_id']}-{term['index']}"
                )
                invoice = await self._fees.create_invoice(
                    school_id=school_id,
                    reference=f"INV/{term['label'].replace(' ', '')}/{sequence:04d}",
                    kind=InvoiceKind.TERM,
                    student_id=child["student_id"],
                    enrolment_id=child["enrolment_id"],
                    term_id=term_id,
                    issued_on=day,
                    due_date=due_date_for(
                        day, term_starts_on=term["starts_on"], grace_days=14
                    ),
                    created_by_id=principal.user_id,
                )
                await self._fees.add_lines(
                    invoice_id=invoice.id, school_id=school_id, lines=lines
                )
                await self._uow.commit()

        summary = RunSummary(decisions=tuple(decisions))

        if not dry_run:
            async with self._uow:
                await self._audit.record(
                    school_id=school_id,
                    action="invoice.term_run",
                    actor_user_id=principal.user_id,
                    actor_role=principal.role.value,
                    entity_type="term",
                    entity_id=term_id,
                    changed={
                        "raised": summary.raised,
                        "skipped": summary.skipped,
                        "billed": str(summary.total_billed),
                        "problems": len(summary.problems),
                    },
                    note=f"{term['label']}",
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
                await self._uow.commit()

        return summary

    # ----------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------
    async def invoices(
        self,
        principal: Principal,
        *,
        term_id: UUID | None = None,
        class_group_id: UUID | None = None,
        student_id: UUID | None = None,
        unpaid_only: bool = False,
        q: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        self._require(principal, Permission.INVOICE_READ)
        return await self._fees.invoices(
            term_id=term_id,
            class_group_id=class_group_id,
            student_id=student_id,
            unpaid_only=unpaid_only,
            q=q,
            limit=limit,
            offset=offset,
        )

    async def invoice_detail(
        self, principal: Principal, *, invoice_id: UUID
    ) -> dict[str, Any]:
        """One bill, its lines and the money against it.

        Assembled server-side because the three have to agree: a parent
        looking at a total that does not match the lines above it stops
        trusting every other number on the page.
        """
        self._require(principal, Permission.INVOICE_READ)

        invoice = await self._fees.invoice(invoice_id)
        if invoice is None:
            raise NotFound("no such invoice")

        return {
            "invoice": invoice,
            "subject": await self._fees.invoice_subject(invoice_id) or {},
            "lines": await self._fees.lines(invoice_id),
            "payments": await self._fees.payments_for(invoice_id),
        }

    async def statement(
        self, principal: Principal, *, student_id: UUID
    ) -> list[dict[str, Any]]:
        self._require(principal, Permission.INVOICE_READ)
        return await self._fees.statement(student_id)

    async def arrears(
        self,
        principal: Principal,
        *,
        as_of: date | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Who owes what, and for how long.

        The day is an argument. A month-end report run in May for April must
        not quietly use today, or the ageing buckets are wrong and the one
        number a bursar is judged on stops reconciling.
        """
        self._require(principal, Permission.REPORT_READ)
        return await self._fees.arrears(
            as_of=as_of or date.today(), limit=limit, offset=offset
        )

    async def collection(
        self,
        principal: Principal,
        *,
        academic_year_id: UUID | None = None,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """The dashboard's money tiles, read against a given day.

        The day is an argument here for the same reason it is one on `arrears`:
        the two have to agree, and a tile computed against "now" while the list
        beneath it is computed against a date is how a bursar ends up with two
        different arrears figures on one screen.
        """
        self._require(principal, Permission.INVOICE_READ)
        return await self._fees.collection_totals(
            academic_year_id, as_of=as_of or date.today()
        )

    async def recent_payments(
        self, principal: Principal, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """The money that came in, newest first.

        Behind `payment:read` rather than `invoice:read`: this is a list of who
        paid what, which is a different question from what a child owes, and a
        teacher who can see a balance has no business reading the school's
        takings.
        """
        self._require(principal, Permission.PAYMENT_READ)
        return await self._fees.recent_payments(limit=limit)

    # ----------------------------------------------------------------------
    # Corrections
    # ----------------------------------------------------------------------
    async def void_invoice(
        self,
        principal: Principal,
        *,
        invoice_id: UUID,
        reason: str,
        context: RequestContext,
    ) -> None:
        """Void a bill that should not have been raised.

        Refused once money is against it: voiding a part-paid invoice would
        leave a payment attached to a bill that officially does not exist, and
        the right correction then is a credit note.
        """
        self._require(principal, Permission.INVOICE_MANAGE)

        async with self._uow:
            totals = await self._fees.totals(invoice_id)
            if totals is None:
                raise NotFound("no such invoice")
            if totals.settled > Decimal(0):
                raise Invalid(
                    "money has been paid against this invoice; write a credit note "
                    "instead of voiding it"
                )

            await self._fees.void(invoice_id, reason=reason, by_id=principal.user_id)
            await self._audit.record(
                school_id=principal.school_id,
                action="invoice.voided",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="invoice",
                entity_id=invoice_id,
                changed={"reference": totals.reference, "billed": str(totals.billed)},
                note=reason,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

    async def credit_note(
        self,
        principal: Principal,
        *,
        invoice_id: UUID,
        amount: Decimal,
        note: str,
        context: RequestContext,
    ) -> InvoiceTotals:
        """Take something off a bill without rewriting it.

        How a mistake is corrected: another line, never an edit. The original
        charge stays, so the bill a parent photographed and the bill the school
        holds remain the same document.
        """
        self._require(principal, Permission.INVOICE_MANAGE)
        if principal.school_id is None:
            raise NotFound("this account belongs to no school")

        if amount <= Decimal(0):
            raise Invalid("a credit note has to be more than nothing")

        async with self._uow:
            totals = await self._fees.totals(invoice_id)
            if totals is None:
                raise NotFound("no such invoice")
            if totals.is_void:
                raise Invalid("that invoice is void; there is nothing to credit")
            if amount > totals.billed:
                raise Invalid(
                    f"a credit note cannot exceed what was billed ({totals.billed})"
                )

            await self._fees.add_credit_note(
                invoice_id=invoice_id,
                school_id=principal.school_id,
                amount=amount,
                note=note,
                by_id=principal.user_id,
            )
            await self._audit.record(
                school_id=principal.school_id,
                action="invoice.credit_note",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="invoice",
                entity_id=invoice_id,
                changed={"amount": str(amount), "reference": totals.reference},
                note=note,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        after = await self._fees.totals(invoice_id)
        if after is None:  # pragma: no cover - it was there a moment ago
            raise NotFound("no such invoice")
        return after

    # ----------------------------------------------------------------------
    # Money nobody could place
    # ----------------------------------------------------------------------
    async def suspense(self, principal: Principal) -> list[dict[str, Any]]:
        """Payments that arrived with nothing to match them.

        The dashboard's first "needs your attention" row. Money on the floor
        is how a month-end stops balancing.
        """
        self._require(principal, Permission.PAYMENT_READ)
        return await self._payments.unmatched()

    async def match_suspense(
        self,
        principal: Principal,
        *,
        payment_id: UUID,
        invoice_id: UUID,
        context: RequestContext,
    ) -> InvoiceTotals:
        """Attach money to the invoice a bursar says it belongs to.

        A decision, not a guess: the system never picks between two siblings
        with open balances, because the wrong choice takes an afternoon to
        unpick and a parent's phone call to discover.
        """
        self._require(principal, Permission.PAYMENT_MATCH_SUSPENSE)

        async with self._uow:
            payment = await self._fees.payment(payment_id)
            if payment is None:
                raise NotFound("no such payment")
            if payment["invoice_id"] is not None:
                raise Invalid("that payment is already against an invoice")
            if payment["reversed_at"] is not None:
                raise Invalid("that payment was reversed")

            totals = await self._fees.totals(invoice_id)
            if totals is None:
                raise NotFound("no such invoice")
            if not totals.is_void and totals.balance <= Decimal(0):
                raise Invalid("that invoice is already settled")

            applied = min(Decimal(payment["amount"]), totals.balance)
            await self._fees.match_payment(
                payment_id=payment_id,
                invoice_id=invoice_id,
                applied=applied,
                by_id=principal.user_id,
            )
            await self._audit.record(
                school_id=principal.school_id,
                action="payment.matched",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="payment",
                entity_id=payment_id,
                changed={
                    "invoice": totals.reference,
                    "applied": str(applied),
                    "amount": str(payment["amount"]),
                },
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

        after = await self._fees.totals(invoice_id)
        if after is None:  # pragma: no cover
            raise NotFound("no such invoice")
        return after
