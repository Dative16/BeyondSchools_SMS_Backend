"""Academic structure endpoints: years, terms, the ladder, classes, subjects.

Every route is the same four lines of work — parse, call one service method,
map the answer or the exception — and holds no rule of its own. The service
checks the permission too, and that is the check that counts; the dependency
here exists so the requirement is readable in the signature and the 403 never
reaches the business layer.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import AcademicServiceDep, PrincipalDep, request_context
from app.domain.academic_year import TermDraft
from app.schemas.academic import (
    AcademicYearCreateRequest,
    AcademicYearResponse,
    ClassGroupCreateRequest,
    ClassGroupResponse,
    ClassGroupUpdateRequest,
    ClassTeacherRequest,
    ExitLevelsReplaceRequest,
    LadderResponse,
    LadderStepResponse,
    LevelCreateRequest,
    LevelResponse,
    LevelUpdateRequest,
    StreamCreateRequest,
    StreamResponse,
    SubjectCreateRequest,
    SubjectLevelOnSubject,
    SubjectResponse,
    SubjectRowResponse,
    SubjectUpdateRequest,
    TeachingAssignmentCreateRequest,
    TeachingAssignmentResponse,
    TeachingStaffResponse,
    TermResponse,
    TermsReplaceRequest,
)
from app.services.academic import Invalid, LadderView, NotAllowed, NotFound
from app.services.auth import RequestContext

router = APIRouter()

ContextDep = Annotated[RequestContext, Depends(request_context)]


def _handle(exc: Exception) -> HTTPException:
    """One mapping from the service's vocabulary to status codes.

    Written once rather than repeated in fifteen routes, because the mapping
    is a property of the service's exceptions, not of any one endpoint.
    """
    if isinstance(exc, NotAllowed):
        return HTTPException(status.HTTP_403_FORBIDDEN, exc.reason)
    if isinstance(exc, NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, exc.reason)
    if isinstance(exc, Invalid):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason)
    raise exc


def _as_ladder(view: LadderView, *, academic_year_id: UUID | None) -> LadderResponse:
    return LadderResponse(
        academic_year_id=academic_year_id,
        levels=[LevelResponse.model_validate(level) for level in view.levels],
        exit_level_ids=sorted(view.exit_level_ids, key=str),
        steps=[
            LadderStepResponse(
                level_id=level_id,
                outcome=step.outcome,
                next_level_id=step.next_level_id,
                reason=step.reason,
            )
            for level_id, step in view.steps.items()
        ],
        problems=list(view.problems),
        summary=view.summary,
    )


# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------
@router.get("/years", response_model=list[AcademicYearResponse], summary="Academic years")
async def list_years(
    principal: PrincipalDep, service: AcademicServiceDep
) -> list[AcademicYearResponse]:
    try:
        years = await service.years(principal)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [AcademicYearResponse.model_validate(year) for year in years]


@router.post(
    "/years",
    response_model=AcademicYearResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open an academic year",
)
async def create_year(
    body: AcademicYearCreateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> AcademicYearResponse:
    try:
        year = await service.create_year(
            principal,
            label=body.label,
            starts_on=body.starts_on,
            ends_on=body.ends_on,
            make_current=body.make_current,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return AcademicYearResponse.model_validate(year)


@router.post(
    "/years/{year_id}/current",
    response_model=AcademicYearResponse,
    summary="Move the school into this year",
)
async def make_year_current(
    year_id: UUID,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> AcademicYearResponse:
    """Invoicing and promotion both pivot on this flag, so the change is
    audited with the year's label in the note."""
    try:
        year = await service.make_year_current(principal, year_id=year_id, context=context)
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return AcademicYearResponse.model_validate(year)


@router.get(
    "/years/{year_id}/terms", response_model=list[TermResponse], summary="A year's terms"
)
async def list_terms(
    year_id: UUID, principal: PrincipalDep, service: AcademicServiceDep
) -> list[TermResponse]:
    try:
        terms = await service.terms(principal, year_id=year_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [TermResponse.model_validate(term) for term in terms]


@router.put(
    "/years/{year_id}/terms",
    response_model=list[TermResponse],
    summary="Set a year's terms",
)
async def replace_terms(
    year_id: UUID,
    body: TermsReplaceRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> list[TermResponse]:
    """The whole set at once, because no-overlap and in-date-order are
    properties of the set rather than of any one term."""
    try:
        terms = await service.replace_terms(
            principal,
            year_id=year_id,
            drafts=[
                TermDraft(
                    index=term.index,
                    starts_on=term.starts_on,
                    ends_on=term.ends_on,
                    label=term.label,
                )
                for term in body.terms
            ],
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return [TermResponse.model_validate(term) for term in terms]


@router.post(
    "/terms/{term_id}/current", response_model=TermResponse, summary="Mark the current term"
)
async def make_term_current(
    term_id: UUID,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> TermResponse:
    try:
        term = await service.make_term_current(principal, term_id=term_id, context=context)
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return TermResponse.model_validate(term)


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------
@router.get(
    "/ladder",
    response_model=LadderResponse,
    summary="The level ladder, the exit cohorts, and what is wrong with them",
)
async def ladder(
    principal: PrincipalDep,
    service: AcademicServiceDep,
    year_id: Annotated[UUID | None, Query()] = None,
) -> LadderResponse:
    """What the promotion engine will ask, available to a screen first.

    The problems come back with the data on purpose. In 2027 a primary school
    holds two exit cohorts at once, and the difference between "correctly
    configured for the transition" and "somebody forgot a cohort" is exactly
    what a head of academics needs to see in September rather than December.
    """
    try:
        view = await service.ladder(principal, year_id=year_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return _as_ladder(view, academic_year_id=year_id)


@router.get("/levels", response_model=list[LevelResponse], summary="Levels")
async def list_levels(
    principal: PrincipalDep, service: AcademicServiceDep
) -> list[LevelResponse]:
    try:
        levels = await service.levels(principal)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [LevelResponse.model_validate(level) for level in levels]


@router.post(
    "/levels",
    response_model=LadderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a level",
)
async def create_level(
    body: LevelCreateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> LadderResponse:
    """Answers with the whole ladder, not just the new rung.

    A level is only meaningful in relation to the others — what sits above it,
    whether its order collides — so the response is the thing the screen has to
    redraw anyway, with any new problems attached.
    """
    try:
        view = await service.create_level(
            principal,
            code=body.code,
            name=body.name,
            stage=body.stage,
            order_index=body.order_index,
            entry_age=body.entry_age,
            next_level_id=body.next_level_id,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _as_ladder(view, academic_year_id=None)


@router.patch("/levels/{level_id}", response_model=LadderResponse, summary="Edit a level")
async def update_level(
    level_id: UUID,
    body: LevelUpdateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> LadderResponse:
    try:
        view = await service.update_level(
            principal,
            level_id=level_id,
            changes=body.model_dump(exclude_unset=True),
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _as_ladder(view, academic_year_id=None)


@router.put(
    "/years/{year_id}/exit-levels",
    response_model=LadderResponse,
    summary="Record which cohorts leave at the end of a year",
)
async def set_exit_levels(
    year_id: UUID,
    body: ExitLevelsReplaceRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> LadderResponse:
    """The most consequential setting in this module.

    A list rather than a single value, because 2027 has two: Standard 7
    finishing the old seven-year cycle and Standard 6 finishing the new
    six-year one, both sitting the PSLE in the same year.
    """
    try:
        view = await service.set_exit_levels(
            principal,
            year_id=year_id,
            levels=[(level.level_id, level.note) for level in body.levels],
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _as_ladder(view, academic_year_id=year_id)


# --------------------------------------------------------------------------
# Classes
# --------------------------------------------------------------------------
@router.get("/streams", response_model=list[StreamResponse], summary="Streams")
async def list_streams(
    principal: PrincipalDep,
    service: AcademicServiceDep,
    level_id: Annotated[UUID | None, Query()] = None,
) -> list[StreamResponse]:
    try:
        streams = await service.streams(principal, level_id=level_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [StreamResponse.model_validate(stream) for stream in streams]


@router.post(
    "/streams",
    response_model=StreamResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a stream to a level",
)
async def create_stream(
    body: StreamCreateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> StreamResponse:
    try:
        stream = await service.create_stream(
            principal,
            level_id=body.level_id,
            name=body.name,
            capacity=body.capacity,
            is_boarding=body.is_boarding,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return StreamResponse.model_validate(stream)


@router.get(
    "/class-groups",
    response_model=list[ClassGroupResponse],
    summary="Classes, with how many children are in each",
)
async def list_class_groups(
    principal: PrincipalDep,
    service: AcademicServiceDep,
    year_id: Annotated[UUID | None, Query()] = None,
) -> list[ClassGroupResponse]:
    """Defaults to the current year: that is what a school means by "the
    classes", and asking every caller to pass a year invites the bug where one
    screen shows last year's."""
    try:
        views = await service.class_groups(principal, year_id=year_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [
        ClassGroupResponse(
            id=view.group.id,
            academic_year_id=view.group.academic_year_id,
            level_id=view.group.level_id,
            stream_id=view.group.stream_id,
            name=view.group.name,
            class_teacher_id=view.group.class_teacher_id,
            room=view.group.room,
            capacity=view.group.capacity,
            pupils=view.pupils,
        )
        for view in views
    ]


@router.post(
    "/class-groups",
    response_model=ClassGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a class",
)
async def create_class_group(
    body: ClassGroupCreateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> ClassGroupResponse:
    try:
        group = await service.create_class_group(
            principal,
            academic_year_id=body.academic_year_id,
            level_id=body.level_id,
            stream_id=body.stream_id,
            class_teacher_id=body.class_teacher_id,
            room=body.room,
            capacity=body.capacity,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return ClassGroupResponse(
        id=group.id,
        academic_year_id=group.academic_year_id,
        level_id=group.level_id,
        stream_id=group.stream_id,
        name=group.name,
        class_teacher_id=group.class_teacher_id,
        room=group.room,
        capacity=group.capacity,
        pupils=0,
    )


@router.patch(
    "/class-groups/{group_id}",
    response_model=ClassGroupResponse,
    summary="Change a class's room or capacity",
)
async def update_class_group(
    group_id: UUID,
    body: ClassGroupUpdateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> ClassGroupResponse:
    """Capacity is the one that matters.

    The counter admission form refuses a full class and tells the registrar to
    come here and open it up, which until now was advice with nowhere to act
    on it.
    """
    try:
        view = await service.update_class_group(
            principal,
            group_id=group_id,
            changes=body.model_dump(exclude_unset=True),
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return ClassGroupResponse(
        id=view.group.id,
        academic_year_id=view.group.academic_year_id,
        level_id=view.group.level_id,
        stream_id=view.group.stream_id,
        name=view.group.name,
        class_teacher_id=view.group.class_teacher_id,
        room=view.group.room,
        capacity=view.group.capacity,
        pupils=view.pupils,
    )


@router.get(
    "/teaching-staff",
    response_model=list[TeachingStaffResponse],
    summary="Who can be put in front of a class",
)
async def teaching_staff(
    principal: PrincipalDep,
    service: AcademicServiceDep,
    year_id: Annotated[UUID | None, Query()] = None,
) -> list[TeachingStaffResponse]:
    """The same roles the save would accept, so the picker cannot offer
    somebody it would then refuse."""
    try:
        rows = await service.teaching_staff(principal, academic_year_id=year_id)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [TeachingStaffResponse(**row) for row in rows]


@router.put(
    "/class-groups/{group_id}/class-teacher",
    response_model=ClassGroupResponse,
    summary="Put a teacher in front of a class",
)
async def set_class_teacher(
    group_id: UUID,
    body: ClassTeacherRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> ClassGroupResponse:
    """An authorisation change as much as an administrative one: a class
    teacher may read every child in their class."""
    try:
        group = await service.assign_class_teacher(
            principal, group_id=group_id, teacher_id=body.teacher_id, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return ClassGroupResponse(
        id=group.id,
        academic_year_id=group.academic_year_id,
        level_id=group.level_id,
        stream_id=group.stream_id,
        name=group.name,
        class_teacher_id=group.class_teacher_id,
        room=group.room,
        capacity=group.capacity,
        pupils=0,
    )


# --------------------------------------------------------------------------
# Subjects
# --------------------------------------------------------------------------
@router.get("/subjects", response_model=list[SubjectRowResponse], summary="Subjects")
async def list_subjects(
    principal: PrincipalDep, service: AcademicServiceDep
) -> list[SubjectRowResponse]:
    """Each subject with the levels it is taught at.

    One query for the levels rather than one per subject: a dozen subjects
    asking one at a time is a dozen round trips on the connection this app is
    built for.
    """
    try:
        rows = await service.subjects_with_levels(principal)
        names = await service.level_names(principal)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [
        SubjectRowResponse(
            id=subject.id,
            code=subject.code,
            name=subject.name,
            is_core=subject.is_core,
            is_examinable=subject.is_examinable,
            is_active=subject.is_active,
            levels=[
                SubjectLevelOnSubject(
                    level_id=link.level_id,
                    level_name=names.get(link.level_id, "—"),
                    periods_per_week=link.periods_per_week,
                )
                for link in levels
            ],
        )
        for subject, levels in rows
    ]


@router.patch(
    "/subjects/{subject_id}",
    response_model=SubjectResponse,
    summary="Rename, move or retire a subject",
)
async def update_subject(
    subject_id: UUID,
    body: SubjectUpdateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> SubjectResponse:
    """There is no delete.

    A report card from a closed year names its subjects, so retiring one —
    `is_active: false` — keeps every old record and stops it appearing on new
    mark sheets. Renaming is safe: the code is what records point at.
    """
    changes = body.model_dump(exclude_unset=True, exclude={"levels"})
    try:
        subject = await service.update_subject(
            principal,
            subject_id=subject_id,
            changes=changes,
            levels=(
                [(level.level_id, level.periods_per_week) for level in body.levels]
                if body.levels is not None
                else None
            ),
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return SubjectResponse.model_validate(subject)


@router.post(
    "/subjects",
    response_model=SubjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a subject",
)
async def create_subject(
    body: SubjectCreateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> SubjectResponse:
    try:
        subject = await service.create_subject(
            principal,
            code=body.code,
            name=body.name,
            is_core=body.is_core,
            is_examinable=body.is_examinable,
            levels=[(level.level_id, level.periods_per_week) for level in body.levels],
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return SubjectResponse.model_validate(subject)


@router.get(
    "/teaching-assignments",
    response_model=list[TeachingAssignmentResponse],
    summary="Who teaches what",
)
async def list_teaching_assignments(
    principal: PrincipalDep,
    service: AcademicServiceDep,
    class_group_id: Annotated[UUID | None, Query()] = None,
    teacher_id: Annotated[UUID | None, Query()] = None,
) -> list[TeachingAssignmentResponse]:
    try:
        assignments = await service.teaching_assignments(
            principal, class_group_id=class_group_id, teacher_id=teacher_id
        )
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return [TeachingAssignmentResponse.model_validate(a) for a in assignments]


@router.post(
    "/teaching-assignments",
    response_model=TeachingAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Give a teacher a subject in a class",
)
async def create_teaching_assignment(
    body: TeachingAssignmentCreateRequest,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> TeachingAssignmentResponse:
    """One teacher per subject per class, enforced by a unique index: two
    people able to enter the same mark means nobody can say whose it was."""
    try:
        assignment = await service.assign_teaching(
            principal,
            teacher_id=body.teacher_id,
            subject_id=body.subject_id,
            class_group_id=body.class_group_id,
            context=context,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return TeachingAssignmentResponse.model_validate(assignment)


@router.delete(
    "/teaching-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take a subject off a teacher",
)
async def remove_teaching_assignment(
    assignment_id: UUID,
    principal: PrincipalDep,
    service: AcademicServiceDep,
    context: ContextDep,
) -> None:
    try:
        await service.remove_teaching(
            principal, assignment_id=assignment_id, context=context
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
