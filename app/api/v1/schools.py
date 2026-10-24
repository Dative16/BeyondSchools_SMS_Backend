"""School profile endpoints.

Two routes, both about the school a caller already belongs to. There is no
route here that takes a school id: the tenant comes from the token, and an
endpoint that accepted one as a parameter would be an endpoint somebody could
try another school's id against.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AcademicServiceDep, PrincipalDep, request_context
from app.schemas.school import SchoolResponse, SchoolUpdateRequest
from app.services.academic import Invalid, NotAllowed, NotFound
from app.services.auth import RequestContext

router = APIRouter()

ContextDep = Annotated[RequestContext, Depends(request_context)]


@router.get("/me", response_model=SchoolResponse, summary="This school's profile")
async def my_school(
    principal: PrincipalDep, service: AcademicServiceDep
) -> SchoolResponse:
    try:
        school = await service.profile(principal)
    except NotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.reason) from exc
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.reason) from exc
    return SchoolResponse.model_validate(school)


@router.patch("/me", response_model=SchoolResponse, summary="Edit this school")
async def update_my_school(
    body: SchoolUpdateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> SchoolResponse:
    """A partial update: only the fields present in the body are touched.

    `exclude_unset` matters here. Without it, a form that posts five fields
    would clear every other setting to its default, including the two arrears
    switches — which is how a school ends up withholding report cards nobody
    asked it to withhold.
    """
    try:
        school = await service.update_profile(
            principal, changes=body.model_dump(exclude_unset=True), context=context
        )
    except NotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.reason) from exc
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.reason) from exc
    except Invalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason) from exc
    return SchoolResponse.model_validate(school)
