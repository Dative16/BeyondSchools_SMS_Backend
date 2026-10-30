"""Guardian endpoints.

The interesting one is `/guardians/me/children`: it is scoped by the guardian
on the token and takes no id at all, so there is nothing for a caller to
change. Every other route here is staff-facing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import PrincipalDep, StudentServiceDep, request_context
from app.schemas.common import MAX_PAGE_SIZE, Page, PageParams
from app.schemas.guardian import (
    GuardianAtThisNumber,
    GuardianChildResponse,
    GuardianResponse,
)
from app.services.auth import RequestContext
from app.services.students import Invalid, NotAllowed, NotFound

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


@router.get(
    "/me/children",
    response_model=list[GuardianChildResponse],
    summary="My children",
)
async def my_children(
    principal: PrincipalDep, service: StudentServiceDep
) -> list[GuardianChildResponse]:
    """The parent portal's landing screen: every child, with what each owes.

    One query rather than one per child. This is the page a guardian opens in
    a queue at the bursar's office on a weak connection, and four numbers per
    child is the whole point of it.
    """
    try:
        children = await service.my_children(principal)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    return [
        GuardianChildResponse(
            student_id=child.student_id,
            admission_no=child.admission_no,
            full_name=child.full_name,
            class_name=child.class_name,
            relationship=child.relationship,
            is_primary=child.is_primary,
            receives_invoices=child.receives_invoices,
            billed=child.billed,
            settled=child.settled,
            balance=max(child.billed - child.settled, Decimal(0)),
        )
        for child in children
    ]


@router.get(
    "/at-number",
    response_model=GuardianAtThisNumber | None,
    summary="Who already uses this phone number",
)
async def guardian_at_number(
    principal: PrincipalDep,
    service: StudentServiceDep,
    phone: Annotated[str, Query(min_length=9, max_length=20)],
) -> GuardianAtThisNumber | None:
    """Null when the number is new, which is the ordinary answer.

    Asked by the counter admission form before it submits. The admission
    matches a guardian on phone and the record already on file wins on every
    field, name included — so without this a registrar could type a different
    spelling of a name the school already holds and never be told their
    version was dropped.

    Declared before the list route for the same reason the by-hand admission
    is declared before the application one: a fixed path must not be read as
    a parameter.
    """
    from app.domain.admissions import normalise_phone

    try:
        found = await service.guardian_by_phone(principal, phone=normalise_phone(phone))
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    if found is None:
        return None

    guardian, children = found
    return GuardianAtThisNumber(
        guardian=GuardianResponse.model_validate(guardian),
        children=[
            GuardianChildResponse(
                student_id=child.student_id,
                admission_no=child.admission_no,
                full_name=child.full_name,
                class_name=child.class_name,
                relationship=child.relationship,
                is_primary=child.is_primary,
                receives_invoices=child.receives_invoices,
                billed=child.billed,
                settled=child.settled,
                balance=max(child.billed - child.settled, Decimal(0)),
            )
            for child in children
        ],
    )


@router.get("", response_model=Page[GuardianResponse], summary="Guardians")
async def list_guardians(
    principal: PrincipalDep,
    service: StudentServiceDep,
    q: Annotated[str | None, Query(max_length=80)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> Page[GuardianResponse]:
    """Searched by name or phone, which is what a bursar has in front of them
    when a payment arrives with no reference."""
    params = PageParams(page=page, size=size)
    try:
        guardians, total = await service.search_guardians(
            principal, q=q, limit=params.size, offset=params.offset
        )
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    return Page.of(
        [GuardianResponse.model_validate(guardian) for guardian in guardians],
        total=total,
        params=params,
    )
