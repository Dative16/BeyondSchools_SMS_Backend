"""The registrar's admissions screens.

Six states, and the shape of this file follows the one thing that is not a
button: acceptance. There is no `POST /accept`. A deposit is recorded against
the invoice, and the application moves itself — so an unpaid place cannot look
accepted, and nobody can mark one accepted by mistake.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import AdmissionsServiceDep, InvoicingServiceDep, PrincipalDep, request_context
from app.domain.admissions import offer_has_lapsed
from app.domain.payments import Channel
from app.schemas.admission import (
    AdmitByHandRequest,
    AdmittedResponse,
    ApplicationDetailResponse,
    ApplicationResponse,
    ApplyChecklistItem,
    DeclineRequest,
    DuplicateResponse,
    EnrolledResponse,
    EnrolRequest,
    FunnelResponse,
    ManualPaymentRequest,
    ManualPaymentResponse,
    QueueRowResponse,
    ScreenRequest,
)
from app.schemas.common import MAX_PAGE_SIZE, Page, PageParams
from app.services.admissions import ApplicationView, Invalid, NotAllowed, NotFound
from app.services.auth import RequestContext
from app.services.invoicing import Invalid as InvoicingInvalid
from app.services.invoicing import NotAllowed as InvoicingNotAllowed
from app.services.invoicing import NotFound as InvoicingNotFound

router = APIRouter()

ContextDep = Annotated[RequestContext, Depends(request_context)]


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, (NotAllowed, InvoicingNotAllowed)):
        return HTTPException(status.HTTP_403_FORBIDDEN, exc.reason)
    if isinstance(exc, (NotFound, InvoicingNotFound)):
        return HTTPException(status.HTTP_404_NOT_FOUND, exc.reason)
    if isinstance(exc, (Invalid, InvoicingInvalid)):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason)
    raise exc


def _as_detail(view: ApplicationView) -> ApplicationDetailResponse:
    return ApplicationDetailResponse(
        application=ApplicationResponse.model_validate(view.application),
        level_name=view.level_name,
        documents=view.documents,
        field_values=view.field_values,
        checklist=[
            ApplyChecklistItem(
                key=item.key, label=item.label, required=item.required, done=item.done
            )
            for item in view.checklist.items
        ],
        complete=view.checklist.complete,
        missing=[item.label for item in view.checklist.missing],
        deposit_billed=view.deposit_billed,
        deposit_settled=view.deposit_settled,
        duplicates=[
            DuplicateResponse(
                student_id=match.student_id, score=match.score, reason=match.reason
            )
            for match in view.duplicates
        ],
    )


@router.get("", response_model=Page[QueueRowResponse], summary="The review queue")
async def queue(
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    state: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=80)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> Page[QueueRowResponse]:
    """Oldest first, because the number a registrar sorts by is how long a
    family has been waiting.

    Defaults to the two states somebody is expected to act on. A queue that
    showed every application ever would be a queue nobody opens.
    """
    params = PageParams(page=page, size=size)
    states = state or ["submitted", "screened"]

    try:
        rows, total = await service.queue(
            principal, states=states, q=q, limit=params.size, offset=params.offset
        )
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    today = date.today()
    items = [
        QueueRowResponse(
            id=row.id,
            reference=row.reference,
            state=row.state,
            child_name=row.child_name or "(no name yet)",
            date_of_birth=row.date_of_birth,
            sex=row.sex,
            level_name=row.level_name,
            previous_school=row.previous_school,
            guardian_name=row.guardian_name,
            guardian_phone=row.guardian_phone,
            phone=row.phone,
            submitted_at=row.submitted_at,
            created_at=row.created_at,
            waiting_days=(today - (row.submitted_at or row.created_at).date()).days,
            duplicate_score=row.duplicate_score,
            duplicate_reason=row.duplicate_reason,
            screening_note=row.screening_note,
            admission_no=row.admission_no,
            offer_expires_on=row.offer_expires_on,
            offer_lapsed=offer_has_lapsed(expires_on=row.offer_expires_on, as_of=today),
            deposit_due=row.deposit_due,
            deposit_settled=row.deposit_settled,
            documents_held=row.documents_held,
        )
        for row in rows
    ]
    return Page.of(items, total=total, params=params)


@router.get("/funnel", response_model=FunnelResponse, summary="The intake funnel")
async def funnel(
    principal: PrincipalDep, service: AdmissionsServiceDep
) -> FunnelResponse:
    """What the dashboard draws. The step worth watching is offered to
    enrolled: a deposit payment is what converts an application into a
    pupil."""
    try:
        counts = await service.funnel(principal)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return FunnelResponse(**counts)


@router.get(
    "/{application_id}",
    response_model=ApplicationDetailResponse,
    summary="One application",
)
async def detail(
    application_id: UUID, principal: PrincipalDep, service: AdmissionsServiceDep
) -> ApplicationDetailResponse:
    try:
        view = await service.view(principal, application_id=application_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return _as_detail(view)


@router.post(
    "/{application_id}/screen",
    response_model=ApplicationDetailResponse,
    summary="Mark it screened",
)
async def screen(
    application_id: UUID,
    body: ScreenRequest,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    context: ContextDep,
) -> ApplicationDetailResponse:
    """An explicit step, because the next one burns an admission number and
    the school should be able to say who decided this child is new."""
    try:
        await service.screened(
            principal, application_id=application_id, note=body.note, context=context
        )
        view = await service.view(principal, application_id=application_id)
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _as_detail(view)


@router.post(
    "/{application_id}/offer",
    response_model=ApplicationDetailResponse,
    summary="Offer a place",
)
async def offer(
    application_id: UUID,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    context: ContextDep,
) -> ApplicationDetailResponse:
    """Issues the admission number and raises the deposit invoice.

    The number comes from a locked per-school sequence and is burnt here
    rather than at enrolment, because the offer letter carries it — a family
    written to with a number keeps it.
    """
    try:
        view = await service.offer(
            principal, application_id=application_id, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _as_detail(view)


@router.post(
    "/{application_id}/decline",
    response_model=ApplicationResponse,
    summary="Decline an application",
)
async def decline(
    application_id: UUID,
    body: DeclineRequest,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    context: ContextDep,
) -> ApplicationResponse:
    """A reason is required: a family that rings to ask why deserves an
    answer, and the registrar who declined it may not be the one who takes
    the call."""
    try:
        application = await service.decline(
            principal, application_id=application_id, reason=body.reason, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return ApplicationResponse.model_validate(application)


@router.post(
    "/{application_id}/deposit",
    response_model=ManualPaymentResponse,
    summary="Record the deposit, which accepts the place",
)
async def record_deposit(
    application_id: UUID,
    body: ManualPaymentRequest,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    invoicing: InvoicingServiceDep,
    context: ContextDep,
) -> ManualPaymentResponse:
    """The only way an application becomes accepted.

    Phase 1 has no aggregator, so a bursar records the money and the
    application moves itself. There is no accept button to press by mistake,
    and a place nobody has paid for cannot look accepted in the funnel.
    """
    try:
        view = await service.view(principal, application_id=application_id)
        application = view.application
        if application.deposit_invoice_id is None:
            raise Invalid("this application has no deposit invoice; offer a place first")

        payment_id = await invoicing.record_manual_payment(
            principal,
            invoice_id=application.deposit_invoice_id,
            amount=body.amount,
            channel=Channel(body.channel),
            reference=body.receipt,
            received_on=body.received_on,
            msisdn=body.msisdn,
            note=body.note,
            context=context,
        )
        moved = await service.settle_if_paid(
            principal, application_id=application_id, context=context
        )
        totals = await invoicing.totals(application.deposit_invoice_id)
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    except (InvoicingNotAllowed, InvoicingNotFound, InvoicingInvalid) as exc:
        raise _handle(exc) from exc

    return ManualPaymentResponse(
        payment_id=payment_id,
        invoice_reference=totals.reference if totals else "",
        billed=totals.billed if totals else Decimal(0),
        settled=totals.settled if totals else Decimal(0),
        balance=totals.balance if totals else Decimal(0),
        application_state=moved.state,
    )


@router.post(
    "/by-hand",
    response_model=AdmittedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Admit a child at the counter",
)
async def admit_by_hand(
    body: AdmitByHandRequest,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    context: ContextDep,
) -> AdmittedResponse:
    """For a family at the office who did not use the public form.

    One transaction: the pupil record, the enrolment, the guardian, the
    family's login and this term's invoice. Half of it is a child the roll
    cannot find or a bill nobody can sign in to pay, and there is a parent
    standing at the desk.

    Declared before `/{application_id}/enrol` so that "by-hand" is read as
    itself rather than as an application id.
    """
    try:
        outcome = await service.admit_by_hand(
            principal,
            child=body.child.model_dump(),
            class_group_id=body.class_group_id,
            residency=body.residency,
            guardian=body.guardian.model_dump(),
            enrolled_on=body.enrolled_on,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc

    return AdmittedResponse(
        student_id=outcome.student_id,
        enrolment_id=outcome.enrolment_id,
        admission_no=outcome.admission_no,
        class_name=outcome.class_name,
        guardian_id=outcome.guardian_id,
        guardian_existed=outcome.guardian_existed,
        login=outcome.login,
        invoice_id=outcome.invoice_id,
        invoice_reference=outcome.invoice_reference,
        billed=outcome.billed,
    )


@router.post(
    "/{application_id}/enrol",
    response_model=EnrolledResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Turn an accepted application into a pupil",
)
async def enrol(
    application_id: UUID,
    body: EnrolRequest,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    context: ContextDep,
) -> EnrolledResponse:
    """One transaction: a child, a guardian, an enrolment and a login.

    All of it or none of it. Half of this is a pupil the roll cannot find or a
    parent who cannot sign in to pay — and the deposit has already been taken
    by this point, so there is no graceful way to leave it half done.
    """
    try:
        outcome = await service.enrol(
            principal,
            application_id=application_id,
            class_group_id=body.class_group_id,
            residency=body.residency,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc

    return EnrolledResponse(
        student_id=outcome.student_id,
        enrolment_id=outcome.enrolment_id,
        admission_no=outcome.admission_no,
        guardian_id=outcome.guardian_id,
    )
