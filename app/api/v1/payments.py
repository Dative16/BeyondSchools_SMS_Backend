"""Payment endpoints: recording money, and placing money nobody could place.

Phase 1 has no aggregator, so the only way money enters the ledger is the
audited manual entry here. Two things make that safe enough to be the only
path: the entry names its author, and the receipt is unique per school, so
entering the same one twice is caught rather than doubling a credit.

What is absent is a route that sets a balance. There is none, anywhere: the
database derives `settled` from the payments, and the guard trigger discards
any attempt to assign it. A correction is a credit note or a payment.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import InvoicingServiceDep, PrincipalDep, request_context
from app.domain.payments import Channel
from app.schemas.fees import (
    InvoiceTotalsResponse,
    MatchSuspenseRequest,
    RecordPaymentRequest,
    SuspensePayment,
)
from app.services.auth import RequestContext
from app.services.invoicing import Invalid, NotAllowed, NotFound

router = APIRouter()

ContextDep = Annotated[RequestContext, Depends(request_context)]


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, NotAllowed):
        return HTTPException(status.HTTP_403_FORBIDDEN, exc.reason)
    if isinstance(exc, NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, exc.reason)
    if isinstance(exc, Invalid):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason)
    raise exc


@router.post(
    "/invoices/{invoice_id}/payments",
    response_model=InvoiceTotalsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record money against an invoice",
)
async def record_payment(
    invoice_id: UUID,
    body: RecordPaymentRequest,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> InvoiceTotalsResponse:
    """A named, audited entry — the whole payment system in Phase 1.

    An overpayment is not refused: the remainder is the family's credit and
    stays visible on the payment rather than being quietly absorbed.
    """
    try:
        await service.record_manual_payment(
            principal,
            invoice_id=invoice_id,
            amount=body.amount,
            channel=Channel(body.channel),
            reference=body.receipt,
            received_on=body.received_on,
            msisdn=body.msisdn,
            note=body.note,
            context=context,
        )
        totals = await service.totals(invoice_id)
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc

    if totals is None:  # pragma: no cover - it was there a moment ago
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such invoice")

    return InvoiceTotalsResponse(
        invoice_id=totals.invoice_id,
        reference=totals.reference,
        billed=totals.billed,
        settled=totals.settled,
        balance=totals.balance,
    )


@router.get(
    "/suspense",
    response_model=list[SuspensePayment],
    summary="Money nobody could place",
)
async def suspense(
    principal: PrincipalDep, service: InvoicingServiceDep
) -> list[SuspensePayment]:
    """The dashboard's first "needs your attention" row.

    A payment with no reference from a number the school does not hold is
    recorded, visible, and waiting for a bursar. Dropping it on the floor or
    guessing an invoice for it is how a month-end stops balancing.
    """
    try:
        rows = await service.suspense(principal)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [SuspensePayment(**row) for row in rows]


@router.post(
    "/{payment_id}/match",
    response_model=InvoiceTotalsResponse,
    summary="Attach a payment to the invoice it belongs to",
)
async def match(
    payment_id: UUID,
    body: MatchSuspenseRequest,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> InvoiceTotalsResponse:
    """A bursar's decision, not the system's guess.

    Nothing picks between two siblings with open balances: the wrong choice
    takes an afternoon to unpick and a parent's phone call to discover. Who
    decided is recorded.
    """
    try:
        totals = await service.match_suspense(
            principal,
            payment_id=payment_id,
            invoice_id=body.invoice_id,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc

    return InvoiceTotalsResponse(
        invoice_id=totals.invoice_id,
        reference=totals.reference,
        billed=totals.billed,
        settled=totals.settled,
        balance=totals.balance,
    )
