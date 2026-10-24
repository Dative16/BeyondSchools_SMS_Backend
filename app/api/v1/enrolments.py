"""Enrolment endpoints.

An enrolment is a child in one class for one academic year. Promotion writes a
new one rather than editing the old one, so these routes only ever touch the
year a school is actually in.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import PrincipalDep, StudentServiceDep, request_context
from app.schemas.student import (
    EnrolmentCreateRequest,
    EnrolmentResponse,
    EnrolmentUpdateRequest,
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


@router.post(
    "",
    response_model=EnrolmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Put a child in a class for a year",
)
async def create_enrolment(
    body: EnrolmentCreateRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> EnrolmentResponse:
    """Refused if the child already has an open enrolment.

    The database allows one active enrolment per child, so rather than quietly
    rewriting last year's row this says so and leaves the decision — close it,
    or let a promotion run move them — with the person making it.
    """
    try:
        enrolment = await service.enrol(
            principal,
            student_id=body.student_id,
            academic_year_id=body.academic_year_id,
            class_group_id=body.class_group_id,
            residency=body.residency,
            enrolled_on=body.enrolled_on,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return EnrolmentResponse.model_validate(enrolment)


@router.patch(
    "/{enrolment_id}",
    response_model=EnrolmentResponse,
    summary="Change this year's class, boarding or bus",
)
async def update_enrolment(
    enrolment_id: UUID,
    body: EnrolmentUpdateRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> EnrolmentResponse:
    """All of these are per-year facts.

    A child who moves from Blue to Green in March, starts boarding in May and
    comes off the bus in September has one row that tells the truth about this
    year — and last year's row is untouched, which is what a closed year
    means.
    """
    try:
        enrolment = await service.update_enrolment(
            principal,
            enrolment_id=enrolment_id,
            changes=body.model_dump(exclude_unset=True),
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return EnrolmentResponse.model_validate(enrolment)
