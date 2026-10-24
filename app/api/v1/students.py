"""Student endpoints: the roll, one child's record, and the detail around it.

No rule lives in this file. The service decides who may see a child, because
the same question has to be answered the same way when the caller is a Celery
task or the AI platform rather than a browser.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import PrincipalDep, StudentServiceDep, request_context
from app.domain.registration import Residency
from app.schemas.common import MAX_PAGE_SIZE, Page, PageParams
from app.schemas.guardian import (
    GuardianLinkedResponse,
    GuardianLinkRequest,
)
from app.schemas.student import (
    ChecklistItemResponse,
    ChecklistResponse,
    DocumentCreateRequest,
    DocumentResponse,
    EmergencyContactModel,
    EmergencyContactsReplaceRequest,
    EnrolmentHistoryRow,
    EnrolmentResponse,
    FieldValuesRequest,
    GuardianOnStudent,
    HealthResponse,
    HealthUpdateRequest,
    RollRowResponse,
    StudentCreateRequest,
    StudentProfileResponse,
    StudentResponse,
    StudentUpdateRequest,
    TransferOutRequest,
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


@router.get("", response_model=Page[RollRowResponse], summary="The roll")
async def list_students(
    principal: PrincipalDep,
    service: StudentServiceDep,
    q: Annotated[str | None, Query(max_length=80)] = None,
    class_group_id: Annotated[UUID | None, Query()] = None,
    level_id: Annotated[UUID | None, Query()] = None,
    academic_year_id: Annotated[UUID | None, Query()] = None,
    student_status: Annotated[str | None, Query(alias="status")] = None,
    residency: Annotated[Residency | None, Query()] = None,
    uses_transport: Annotated[bool | None, Query()] = None,
    in_arrears: Annotated[bool | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> Page[RollRowResponse]:
    """Search by name or admission number, filter by class, boarding or bus.

    A teacher's view is narrowed to their own classes by the service, not by
    the client: a list endpoint that returns the school and trusts the caller
    to filter is a list endpoint that leaks.
    """
    params = PageParams(page=page, size=size)
    try:
        rows, total = await service.roll(
            principal,
            limit=params.size,
            offset=params.offset,
            q=q,
            class_group_id=class_group_id,
            level_id=level_id,
            academic_year_id=academic_year_id,
            status=student_status,
            residency=residency,
            uses_transport=uses_transport,
            in_arrears=in_arrears,
        )
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    items = [
        RollRowResponse(
            **{
                key: value
                for key, value in asdict(row).items()
                if key not in ("level_id", "ai_platform_user_ref")
            },
            balance=max(row.billed - row.settled, Decimal(0)),
            on_ai_platform=row.ai_platform_user_ref is not None,
        )
        for row in rows
    ]
    return Page.of(items, total=total, params=params)


@router.post(
    "",
    response_model=StudentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Admit a child by hand",
)
async def create_student(
    body: StudentCreateRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> StudentResponse:
    """The path for a pupil who arrived with a transfer letter. The ordinary
    path is the admissions funnel."""
    try:
        student = await service.create_student(
            principal, details=body.model_dump(exclude_unset=True), context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return StudentResponse.model_validate(student)


@router.get(
    "/{student_id}",
    response_model=StudentProfileResponse,
    summary="One child's record",
)
async def student_profile(
    student_id: UUID, principal: PrincipalDep, service: StudentServiceDep
) -> StudentProfileResponse:
    """Everything the profile screen draws, in one answer.

    Health is present only for a caller holding `health:read`, and null rather
    than absent so the client can tell "not allowed to see" from "nothing
    recorded".
    """
    try:
        view = await service.profile(principal, student_id=student_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    return StudentProfileResponse(
        student=StudentResponse.model_validate(view.student),
        enrolment=(
            EnrolmentResponse.model_validate(view.enrolment) if view.enrolment else None
        ),
        class_name=view.class_name,
        history=[EnrolmentHistoryRow(**row) for row in view.history],
        guardians=[GuardianOnStudent(**row) for row in view.guardians],
        billed=view.fees["billed"],
        settled=view.fees["settled"],
        balance=view.fees["balance"],
        field_values=view.field_values,
        health=HealthResponse.model_validate(view.health) if view.health else None,
        emergency_contacts=[
            EmergencyContactModel.model_validate(contact)
            for contact in view.emergency_contacts
        ],
    )


@router.patch(
    "/{student_id}", response_model=StudentResponse, summary="Edit a child's details"
)
async def update_student(
    student_id: UUID,
    body: StudentUpdateRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> StudentResponse:
    try:
        student = await service.update_student(
            principal,
            student_id=student_id,
            changes=body.model_dump(exclude_unset=True),
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return StudentResponse.model_validate(student)


@router.get(
    "/{student_id}/enrolments",
    response_model=list[EnrolmentHistoryRow],
    summary="A child's whole history",
)
async def enrolment_history(
    student_id: UUID, principal: PrincipalDep, service: StudentServiceDep
) -> list[EnrolmentHistoryRow]:
    """One row per academic year. Promotion writes a new row rather than
    changing the old one, which is why a report card from two years ago can
    still say which class it was earned in."""
    try:
        view = await service.profile(principal, student_id=student_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [EnrolmentHistoryRow(**row) for row in view.history]


@router.post(
    "/{student_id}/transfer-out",
    response_model=StudentResponse,
    summary="Close a child's place at the school",
)
async def transfer_out(
    student_id: UUID,
    body: TransferOutRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> StudentResponse:
    """The enrolment is closed, never deleted, and the student record stays:
    a school asked for a transfer letter two years later has to answer, and
    arrears do not vanish because a child left."""
    try:
        student = await service.transfer_out(
            principal,
            student_id=student_id,
            reason=body.reason,
            on=body.on,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return StudentResponse.model_validate(student)


# --------------------------------------------------------------------------
# Guardians
# --------------------------------------------------------------------------
@router.get(
    "/{student_id}/guardians",
    response_model=list[GuardianOnStudent],
    summary="Who to contact about this child",
)
async def student_guardians(
    student_id: UUID, principal: PrincipalDep, service: StudentServiceDep
) -> list[GuardianOnStudent]:
    try:
        rows = await service.guardians_of(principal, student_id=student_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [GuardianOnStudent(**row) for row in rows]


@router.post(
    "/{student_id}/guardians",
    response_model=GuardianLinkedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Link a guardian to this child",
)
async def link_guardian(
    student_id: UUID,
    body: GuardianLinkRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> GuardianLinkedResponse:
    """Reuses the person if the phone number is already known, which is what
    makes a parent with three children one guardian and three links."""
    try:
        linked = await service.link_guardian(
            principal,
            student_id=student_id,
            guardian=body.guardian.model_dump(),
            link=body.link.model_dump(),
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return GuardianLinkedResponse(**linked)


@router.delete(
    "/{student_id}/guardians/{guardian_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink a guardian",
)
async def unlink_guardian(
    student_id: UUID,
    guardian_id: UUID,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> None:
    """Refused for the last guardian on a child: nobody to contact is the one
    state this must not be able to produce."""
    try:
        await service.unlink_guardian(
            principal, student_id=student_id, guardian_id=guardian_id, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc


# --------------------------------------------------------------------------
# Health, behind its own capability
# --------------------------------------------------------------------------
@router.get(
    "/{student_id}/health",
    response_model=HealthResponse | None,
    summary="Medical detail",
)
async def student_health(
    student_id: UUID,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> HealthResponse | None:
    """Behind `health:read`, and the read itself is audited.

    Children's health data is sensitive personal data under the Personal Data
    Protection Act, 2022, so "who opened this child's health record" is a
    question a school may have to answer.
    """
    try:
        record = await service.health(principal, student_id=student_id, context=context)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return HealthResponse.model_validate(record) if record else None


@router.put(
    "/{student_id}/health", response_model=HealthResponse, summary="Record medical detail"
)
async def set_student_health(
    student_id: UUID,
    body: HealthUpdateRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> HealthResponse:
    try:
        record = await service.set_health(
            principal,
            student_id=student_id,
            values=body.model_dump(exclude_unset=True),
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return HealthResponse.model_validate(record)


# --------------------------------------------------------------------------
# Emergency contacts
# --------------------------------------------------------------------------
@router.get(
    "/{student_id}/emergency-contacts",
    response_model=list[EmergencyContactModel],
    summary="Who to call, in order",
)
async def emergency_contacts(
    student_id: UUID, principal: PrincipalDep, service: StudentServiceDep
) -> list[EmergencyContactModel]:
    try:
        contacts = await service.emergency_contacts(principal, student_id=student_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [EmergencyContactModel.model_validate(contact) for contact in contacts]


@router.put(
    "/{student_id}/emergency-contacts",
    response_model=list[EmergencyContactModel],
    summary="Set who to call",
)
async def set_emergency_contacts(
    student_id: UUID,
    body: EmergencyContactsReplaceRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> list[EmergencyContactModel]:
    """The whole list at once, because the ordering is the point: three
    numbers in no particular order is three numbers nobody knows which to ring
    first."""
    try:
        written = await service.set_emergency_contacts(
            principal,
            student_id=student_id,
            contacts=[contact.model_dump() for contact in body.contacts],
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return [EmergencyContactModel.model_validate(contact) for contact in written]


# --------------------------------------------------------------------------
# Documents, the checklist and the school's own fields
# --------------------------------------------------------------------------
@router.get(
    "/{student_id}/documents",
    response_model=list[DocumentResponse],
    summary="Documents on file",
)
async def student_documents(
    student_id: UUID, principal: PrincipalDep, service: StudentServiceDep
) -> list[DocumentResponse]:
    try:
        documents = await service.documents(principal, student_id=student_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [DocumentResponse.model_validate(document) for document in documents]


@router.post(
    "/{student_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a document",
)
async def add_student_document(
    student_id: UUID,
    body: DocumentCreateRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> DocumentResponse:
    """Records a reference, not the bytes.

    Object storage is not wired up yet, so the file goes to it directly and
    this endpoint records where. When storage lands this gains a presigned
    upload step and the shape of the record does not change.
    """
    try:
        document = await service.add_document(
            principal, student_id=student_id, document=body.model_dump(), context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return DocumentResponse.model_validate(document)


@router.get(
    "/{student_id}/checklist",
    response_model=ChecklistResponse,
    summary="What is still missing from this record",
)
async def student_checklist(
    student_id: UUID, principal: PrincipalDep, service: StudentServiceDep
) -> ChecklistResponse:
    """Built from three sources — the core fields, the school's own document
    checklist, and whichever custom fields it marked as required — by the same
    function the admissions queue uses."""
    try:
        checklist = await service.checklist(principal, student_id=student_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc

    return ChecklistResponse(
        items=[
            ChecklistItemResponse(
                key=item.key, label=item.label, required=item.required, done=item.done
            )
            for item in checklist.items
        ],
        complete=checklist.complete,
        missing=[item.label for item in checklist.missing],
    )


@router.put(
    "/{student_id}/fields",
    response_model=dict[str, Any],
    summary="Set this school's own fields for a child",
)
async def set_student_fields(
    student_id: UUID,
    body: FieldValuesRequest,
    principal: PrincipalDep,
    service: StudentServiceDep,
    context: ContextDep,
) -> dict[str, Any]:
    """Each value is checked against its field's declared kind before it is
    stored. A number field that accepted "about 40" would be a column nobody
    can add up."""
    try:
        return await service.set_field_values(
            principal, student_id=student_id, values=body.values, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
