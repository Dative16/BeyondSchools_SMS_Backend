"""Fee endpoints: structures, the term run, invoices, statements, arrears.

No arithmetic in this file. The amounts come from `app/domain/invoicing.py`
and the totals from database triggers; these routes decide status and ageing
against a day they are given, which is the only computation a presentation
layer should be doing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import InvoicingServiceDep, PrincipalDep, request_context
from app.domain.invoicing import InvoiceFacts, RunDecision, days_overdue, status_of
from app.domain.payments import arrears_age_bucket
from app.schemas.common import MAX_PAGE_SIZE, Page, PageParams
from app.schemas.fees import (
    ArrearsRow,
    CollectionResponse,
    CreditNoteRequest,
    FeeItemsReplaceRequest,
    FeeStructureCreateRequest,
    FeeStructureDetailResponse,
    FeeStructureResponse,
    FeeStructureRow,
    InvoiceDetailResponse,
    InvoiceLineResponse,
    InvoiceRow,
    InvoiceTotalsResponse,
    PaymentOnInvoice,
    RunDecisionResponse,
    StatementResponse,
    StatementRow,
    TermRunRequest,
    TermRunResponse,
    VoidInvoiceRequest,
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


def _detail(detail: dict[str, Any]) -> FeeStructureDetailResponse:
    """One shape for all four structure endpoints.

    They each answer the same question — what is in this structure now — and a
    screen that got a different shape back from publishing than from editing
    would have to hold two readings of one thing.
    """
    return FeeStructureDetailResponse(
        structure=FeeStructureResponse.model_validate(detail["structure"]),
        items=detail["items"],
        enrolled_count=detail["enrolled_count"],
    )


def _facts(row: dict[str, Any], *, is_void: bool = False) -> InvoiceFacts:
    return InvoiceFacts(
        billed=Decimal(row["billed"]),
        settled=Decimal(row["settled"]),
        due_date=row["due_date"],
        is_void=is_void or bool(row.get("is_void")),
    )


# --------------------------------------------------------------------------
# Fee structures
# --------------------------------------------------------------------------
@router.get(
    "/structures", response_model=list[FeeStructureRow], summary="Fee structures"
)
async def structures(
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    academic_year_id: Annotated[UUID | None, Query()] = None,
) -> list[FeeStructureRow]:
    try:
        rows = await service.structures(principal, academic_year_id=academic_year_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [FeeStructureRow(**row) for row in rows]


@router.post(
    "/structures",
    response_model=FeeStructureDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a fee structure",
)
async def create_structure(
    body: FeeStructureCreateRequest,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> FeeStructureDetailResponse:
    """It begins as a draft, and nothing can be invoiced from a draft."""
    try:
        detail = await service.create_structure(
            principal,
            academic_year_id=body.academic_year_id,
            level_id=body.level_id,
            stream_id=body.stream_id,
            label=body.label,
            items=[item.model_dump() for item in body.items],
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _detail(detail)


@router.get(
    "/structures/{structure_id}",
    response_model=FeeStructureDetailResponse,
    summary="One fee structure and its items",
)
async def structure(
    structure_id: UUID,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
) -> FeeStructureDetailResponse:
    """The only way to reopen a draft.

    The list says a structure has five items; this says which five. Without it
    a half-finished draft could be read back only by whoever still had the
    create response in front of them, so closing the tab lost the work.
    """
    try:
        detail = await service.structure_detail(principal, structure_id=structure_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return _detail(detail)


@router.put(
    "/structures/{structure_id}/items",
    response_model=FeeStructureDetailResponse,
    summary="Set a draft's fee items",
)
async def set_items(
    structure_id: UUID,
    body: FeeItemsReplaceRequest,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> FeeStructureDetailResponse:
    """Refused once published: invoices already raised would otherwise change
    retrospectively, and a parent who photographed a bill must find the same
    figure on it three weeks later."""
    try:
        detail = await service.set_structure_items(
            principal,
            structure_id=structure_id,
            items=[item.model_dump() for item in body.items],
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _detail(detail)


@router.post(
    "/structures/{structure_id}/publish",
    response_model=FeeStructureDetailResponse,
    summary="Make a structure live",
)
async def publish_structure(
    structure_id: UUID,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> FeeStructureDetailResponse:
    """An empty structure cannot be published: a term run would bill every
    child nothing and look as though it worked."""
    try:
        detail = await service.publish_structure(
            principal, structure_id=structure_id, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _detail(detail)


@router.post(
    "/structures/{structure_id}/archive",
    response_model=FeeStructureDetailResponse,
    summary="Discard a draft structure",
)
async def archive_structure(
    structure_id: UUID,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> FeeStructureDetailResponse:
    """Refused once published. A draft, though, has billed nobody."""
    try:
        detail = await service.archive_structure(
            principal, structure_id=structure_id, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _detail(detail)


# --------------------------------------------------------------------------
# The term run
# --------------------------------------------------------------------------
@router.post(
    "/terms/{term_id}/run",
    response_model=TermRunResponse,
    summary="Raise this term's invoices",
)
async def run_term(
    term_id: UUID,
    body: TermRunRequest,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> TermRunResponse:
    """Previews by default.

    `dry_run` is true unless asked otherwise: a run over 500 children is not
    something to start by accident, and the preview is how a bursar finds out
    that Standard 6's structure is still a draft before the invoices go out.

    Repeatable. A child who already has an invoice for the term is skipped, so
    running it again after an interruption is the safe recovery rather than
    the thing that double-bills a school.
    """
    try:
        summary = await service.run_term_invoices(
            principal,
            term_id=term_id,
            issued_on=body.issued_on,
            dry_run=body.dry_run,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc

    def as_decision(decision: RunDecision) -> RunDecisionResponse:
        return RunDecisionResponse(
            enrolment_id=decision.enrolment_id,
            student_id=decision.student_id,
            outcome=decision.outcome,
            billed=decision.billed,
            reason=decision.reason,
        )

    return TermRunResponse(
        dry_run=body.dry_run,
        raised=summary.raised,
        skipped=summary.skipped,
        total_billed=summary.total_billed,
        problems=[as_decision(d) for d in summary.problems],
        decisions=[as_decision(d) for d in summary.decisions],
    )


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
@router.get("/invoices", response_model=Page[InvoiceRow], summary="Invoices")
async def invoices(
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    term_id: Annotated[UUID | None, Query()] = None,
    class_group_id: Annotated[UUID | None, Query()] = None,
    student_id: Annotated[UUID | None, Query()] = None,
    unpaid_only: Annotated[bool, Query()] = False,
    q: Annotated[str | None, Query(max_length=80)] = None,
    as_of: Annotated[date | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> Page[InvoiceRow]:
    """Status and ageing are computed against `as_of`, which defaults to today.

    Passed in rather than assumed so a statement produced for last month is
    read against last month.
    """
    params = PageParams(page=page, size=size)
    day = as_of or date.today()

    try:
        rows, total = await service.invoices(
            principal,
            term_id=term_id,
            class_group_id=class_group_id,
            student_id=student_id,
            unpaid_only=unpaid_only,
            q=q,
            limit=params.size,
            offset=params.offset,
        )
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    items = []
    for row in rows:
        facts = _facts(row)
        items.append(
            InvoiceRow(
                id=row["id"],
                reference=row["reference"],
                kind=row["kind"],
                student_id=row["student_id"],
                admission_no=row["admission_no"],
                student_name=row["student_name"],
                class_name=row["class_name"],
                term_label=row["term_label"],
                issued_on=row["issued_on"],
                due_date=row["due_date"],
                billed=facts.billed,
                settled=facts.settled,
                balance=max(facts.billed - facts.settled, Decimal(0)),
                status=status_of(facts, day).value,
                days_overdue=days_overdue(facts, day),
            )
        )
    return Page.of(items, total=total, params=params)


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceDetailResponse,
    summary="One invoice, its lines and its payments",
)
async def invoice_detail(
    invoice_id: UUID,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    as_of: Annotated[date | None, Query()] = None,
) -> InvoiceDetailResponse:
    try:
        detail = await service.invoice_detail(principal, invoice_id=invoice_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    invoice = detail["invoice"]
    subject = detail["subject"]
    day = as_of or date.today()
    facts = InvoiceFacts(
        billed=Decimal(invoice.billed),
        settled=Decimal(invoice.settled),
        due_date=invoice.due_date,
        is_void=invoice.is_void,
    )

    return InvoiceDetailResponse(
        id=invoice.id,
        reference=invoice.reference,
        kind=str(invoice.kind),
        student_id=invoice.student_id,
        admission_no=subject.get("admission_no"),
        student_name=subject.get("student_name"),
        class_name=subject.get("class_name"),
        term_id=invoice.term_id,
        term_label=subject.get("term_label"),
        academic_year=subject.get("academic_year"),
        issued_on=invoice.issued_on,
        due_date=invoice.due_date,
        currency=invoice.currency,
        billed=facts.billed,
        settled=facts.settled,
        balance=max(facts.billed - facts.settled, Decimal(0)),
        status=status_of(facts, day).value,
        days_overdue=days_overdue(facts, day),
        is_void=invoice.is_void,
        void_reason=invoice.void_reason,
        note=invoice.note,
        lines=[InvoiceLineResponse(**line) for line in detail["lines"]],
        payments=[PaymentOnInvoice(**payment) for payment in detail["payments"]],
    )


@router.get(
    "/students/{student_id}/statement",
    response_model=StatementResponse,
    summary="A child's statement",
)
async def statement(
    student_id: UUID, principal: PrincipalDep, service: InvoicingServiceDep
) -> StatementResponse:
    """Across years, because arrears follow the child."""
    try:
        rows = await service.statement(principal, student_id=student_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    invoices_out = [
        StatementRow(
            id=row["id"],
            reference=row["reference"],
            kind=row["kind"],
            academic_year=row["academic_year"],
            term_label=row["term_label"],
            issued_on=row["issued_on"],
            due_date=row["due_date"],
            billed=Decimal(row["billed"]),
            settled=Decimal(row["settled"]),
            balance=max(Decimal(row["billed"]) - Decimal(row["settled"]), Decimal(0)),
            is_void=row["is_void"],
        )
        for row in rows
    ]
    live = [row for row in invoices_out if not row.is_void]

    return StatementResponse(
        student_id=student_id,
        invoices=invoices_out,
        billed=sum((row.billed for row in live), Decimal(0)),
        settled=sum((row.settled for row in live), Decimal(0)),
        balance=sum((row.balance for row in live), Decimal(0)),
    )


# --------------------------------------------------------------------------
# Corrections
# --------------------------------------------------------------------------
@router.post(
    "/invoices/{invoice_id}/void",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Void an invoice that should not have been raised",
)
async def void_invoice(
    invoice_id: UUID,
    body: VoidInvoiceRequest,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> None:
    """Refused once money is against it: a payment attached to a bill that
    officially does not exist is worse than the mistake. Write a credit note
    instead."""
    try:
        await service.void_invoice(
            principal, invoice_id=invoice_id, reason=body.reason, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc


@router.post(
    "/invoices/{invoice_id}/credit-note",
    response_model=InvoiceTotalsResponse,
    summary="Take something off a bill",
)
async def credit_note(
    invoice_id: UUID,
    body: CreditNoteRequest,
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    context: ContextDep,
) -> InvoiceTotalsResponse:
    """Another line, never an edit. The original charge stays, so the bill a
    parent photographed and the bill the school holds remain one document."""
    try:
        totals = await service.credit_note(
            principal,
            invoice_id=invoice_id,
            amount=body.amount,
            note=body.note,
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


# --------------------------------------------------------------------------
# Arrears and collection
# --------------------------------------------------------------------------
@router.get("/arrears", response_model=Page[ArrearsRow], summary="Who owes what")
async def arrears(
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    as_of: Annotated[date | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> Page[ArrearsRow]:
    """One row per child, worst first.

    A bursar chasing arrears rings a family rather than a document, so three
    overdue bills for one child is one phone call and one row.
    """
    params = PageParams(page=page, size=size)
    try:
        rows, total = await service.arrears(
            principal, as_of=as_of, limit=params.size, offset=params.offset
        )
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    items = [
        ArrearsRow(
            student_id=row["student_id"],
            admission_no=row["admission_no"],
            student_name=row["student_name"],
            class_name=row["class_name"],
            guardian_name=row["guardian_name"],
            guardian_phone=row["guardian_phone"],
            balance=Decimal(row["balance"]),
            days_overdue=row["days_overdue"],
            overdue_invoices=row["overdue_invoices"],
            bucket=arrears_age_bucket(row["days_overdue"]),
        )
        for row in rows
    ]
    return Page.of(items, total=total, params=params)


@router.get(
    "/collection", response_model=CollectionResponse, summary="Fees collected"
)
async def collection(
    principal: PrincipalDep,
    service: InvoicingServiceDep,
    academic_year_id: Annotated[UUID | None, Query()] = None,
    as_of: Annotated[date | None, Query()] = None,
) -> CollectionResponse:
    """The dashboard's money tiles, read against `as_of` — today by default,
    and the same day the arrears list beneath them is read against."""
    try:
        totals = await service.collection(
            principal, academic_year_id=academic_year_id, as_of=as_of
        )
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    billed = Decimal(totals["billed"])
    settled = Decimal(totals["settled"])
    percent = int((settled / billed * 100).to_integral_value()) if billed > 0 else 0

    return CollectionResponse(
        billed=billed,
        settled=settled,
        outstanding=max(billed - settled, Decimal(0)),
        overdue=Decimal(totals["overdue"]),
        students_in_arrears=totals["students_in_arrears"],
        students_over_60_days=totals["students_over_60_days"],
        collected_percent=percent,
    )
