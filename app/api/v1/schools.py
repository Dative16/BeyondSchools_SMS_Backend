"""School profile endpoints.

Two routes, both about the school a caller already belongs to. There is no
route here that takes a school id: the tenant comes from the token, and an
endpoint that accepted one as a parameter would be an endpoint somebody could
try another school's id against.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AcademicServiceDep, PrincipalDep, request_context
from app.schemas.school import (
    SchoolFieldCreateRequest,
    SchoolFieldResponse,
    SchoolFieldUpdateRequest,
    SchoolResponse,
    SchoolUpdateRequest,
)
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


# --------------------------------------------------------------------------
# The fields a school adds for itself
# --------------------------------------------------------------------------
@router.get(
    "/me/fields",
    response_model=list[SchoolFieldResponse],
    summary="The fields this school adds",
)
async def my_school_fields(
    principal: PrincipalDep, service: AcademicServiceDep
) -> list[SchoolFieldResponse]:
    """Readable by any staff account, because the pupil profile has to draw
    them for anybody who can read a pupil."""
    try:
        fields = await service.school_fields(principal)
    except NotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.reason) from exc
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.reason) from exc
    return [SchoolFieldResponse.model_validate(field) for field in fields]


@router.post(
    "/me/fields",
    response_model=SchoolFieldResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a field",
)
async def add_my_school_field(
    body: SchoolFieldCreateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> SchoolFieldResponse:
    """Every field added here appears, empty, on every pupil record the school
    has. Somebody then has to fill them all in."""
    try:
        field = await service.add_school_field(
            principal,
            key=body.key,
            label=body.label,
            kind=body.kind,
            scope=body.scope,
            options=body.options,
            help_text=body.help_text,
            required_at_registration=body.required_at_registration,
            is_sensitive=body.is_sensitive,
            context=context,
        )
    except NotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.reason) from exc
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.reason) from exc
    except Invalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason) from exc
    return SchoolFieldResponse.model_validate(field)


@router.patch(
    "/me/fields/{field_id}",
    response_model=SchoolFieldResponse,
    summary="Relabel or retire a field",
)
async def update_my_school_field(
    field_id: UUID,
    body: SchoolFieldUpdateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> SchoolFieldResponse:
    """Neither the key nor the kind can change.

    Values already recorded are stored under that key and typed by that kind,
    so changing either would reinterpret answers people have already given.
    Retiring the field keeps every value and stops it being asked for again.
    """
    try:
        field = await service.update_school_field(
            principal,
            field_id=field_id,
            changes=body.model_dump(exclude_unset=True),
            context=context,
        )
    except NotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.reason) from exc
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.reason) from exc
    except Invalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason) from exc
    return SchoolFieldResponse.model_validate(field)
