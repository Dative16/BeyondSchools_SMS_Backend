"""The public application form.

Everything here is reached by somebody with no account. A verified phone number
is the whole credential, and the session it buys is scoped to one application —
so none of these routes takes an application id, and there is nothing for a
caller to change.

The form is four steps and about four minutes. That is a constraint, not a
style: a form that takes longer than that on a phone does not get finished, and
an application that is not finished is a pupil the school never hears about.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import (
    AdmissionsServiceDep,
    AuthServiceDep,
    PrincipalDep,
    PublicAdmissionsServiceDep,
    SchoolDep,
    request_context,
)
from app.core.config import settings
from app.domain.identity import OtpPurpose
from app.domain.registration import FieldScope
from app.schemas.admission import (
    ApplicationResponse,
    ApplyChecklistItem,
    ApplyDocumentOption,
    ApplyFieldOption,
    ApplyLevelOption,
    ApplyOptionsResponse,
    ApplySaveRequest,
    ApplySentResponse,
    ApplyStartRequest,
    ApplyStateResponse,
    ApplyVerifyRequest,
)
from app.schemas.auth import TokenResponse
from app.schemas.student import DocumentCreateRequest, UploadTicketRequest, UploadTicketResponse
from app.services.admissions import ApplicationView, Invalid, NotAllowed, NotFound
from app.services.auth import OtpRejected, RequestContext

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


def _as_state(view: ApplicationView) -> ApplyStateResponse:
    return ApplyStateResponse(
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
        can_submit=not view.missing_to_submit,
        missing_to_submit=list(view.missing_to_submit),
        deposit_billed=view.deposit_billed,
        deposit_settled=view.deposit_settled,
    )


@router.get(
    "/options",
    response_model=ApplyOptionsResponse,
    summary="What this school is asking for",
)
async def options(
    school: SchoolDep, service: PublicAdmissionsServiceDep
) -> ApplyOptionsResponse:
    """Served unauthenticated, from the subdomain, and holds nothing about any
    child: the levels on offer, the papers to bring, and the school's own
    questions.

    Only levels with a published fee structure appear — a level nobody can be
    quoted a deposit for is a level nobody should be able to apply to.
    """
    levels, documents, fields, year = await service.form_options()

    return ApplyOptionsResponse(
        school_name=school.name,
        academic_year=year,
        levels=[ApplyLevelOption(**level) for level in levels],
        documents=[
            ApplyDocumentOption(
                kind=requirement.kind,
                label=requirement.label,
                required=requirement.is_required,
            )
            for requirement in documents
        ],
        fields=[
            ApplyFieldOption(
                key=field.key,
                label=field.label,
                help_text=None,
                kind=field.kind,
                options=list(field.options),
                required=field.required_at_registration,
            )
            for field in fields
            if field.scope is FieldScope.APPLICATION
        ],
    )


@router.post(
    "/start", response_model=ApplySentResponse, summary="Send a code to this number"
)
async def start(
    body: ApplyStartRequest,
    school: SchoolDep,
    auth: AuthServiceDep,
    context: ContextDep,
) -> ApplySentResponse:
    """Step one. The number is verified before anything else is collected.

    The answer is the same whether or not an application already exists for
    that number: a different response would turn this into a way to ask which
    families have applied.
    """
    try:
        expires_at = await auth.start_otp(
            school_id=school.id,
            phone=body.phone,
            purpose=OtpPurpose.APPLY,
            context=context,
        )
    except OtpRejected as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.reason) from exc

    return ApplySentResponse(expires_at=expires_at, resend_after_seconds=60)


@router.post(
    "/verify",
    response_model=TokenResponse,
    summary="Verify the code and start or resume the form",
)
async def verify(
    body: ApplyVerifyRequest,
    response: Response,
    school: SchoolDep,
    auth: AuthServiceDep,
    service: PublicAdmissionsServiceDep,
    context: ContextDep,
) -> TokenResponse:
    """Step two, and the only place a session is handed out.

    Resumes an open application for that number rather than starting a new
    one — a parent who lost the page at question nine comes back to question
    nine.
    """
    try:
        await auth.verify_otp_code(
            school_id=school.id,
            phone=body.phone,
            purpose=OtpPurpose.APPLY,
            code=body.code,
            context=context,
        )
    except OtpRejected as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.reason) from exc

    application = await service.start_or_resume(
        school_id=school.id,
        phone=body.phone,
        academic_year_id=None,
        context=context,
    )
    session = await auth.open_applicant_session(
        school_id=school.id,
        application_id=application.id,
        phone=body.phone,
        context=context,
    )

    response.set_cookie(
        "bs_refresh",
        session.refresh_token,
        max_age=settings.REFRESH_TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.ENVIRONMENT != "local",
        path="/api/v1/auth",
    )
    from app.api.v1.auth import _as_token_response

    return _as_token_response(session)


@router.get("/me", response_model=ApplyStateResponse, summary="My application")
async def my_application(
    principal: PrincipalDep, service: AdmissionsServiceDep
) -> ApplyStateResponse:
    try:
        view = await service.my_application(principal)
    except (NotAllowed, NotFound) as exc:
        raise _handle(exc) from exc
    return _as_state(view)


@router.patch("/me", response_model=ApplyStateResponse, summary="Save progress")
async def save(
    body: ApplySaveRequest,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    context: ContextDep,
) -> ApplyStateResponse:
    """Called as the parent types. Nothing here is required.

    Saved against the verified phone number, so the form survives a dropped
    connection, a closed browser and a flat battery.
    """
    try:
        await service.save_progress(
            principal, changes=body.model_dump(exclude_unset=True), context=context
        )
        view = await service.my_application(principal)
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _as_state(view)


@router.post(
    "/me/submit", response_model=ApplyStateResponse, summary="Hand it to the school"
)
async def submit(
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    context: ContextDep,
) -> ApplyStateResponse:
    """Checks the shortest honest list: a name, a date of birth, a sex, a level
    and a guardian who can be reached.

    Screening for a child the school already has runs on the way in, so the
    registrar opens a queue that already says which applications look like
    duplicates.
    """
    try:
        await service.submit(principal, context=context)
        view = await service.my_application(principal)
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _as_state(view)


@router.post(
    "/me/documents/upload-url",
    response_model=UploadTicketResponse,
    summary="Get a URL to upload a document to",
)
async def document_upload_url(
    body: UploadTicketRequest,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
) -> UploadTicketResponse:
    """Step one of two. The parent's phone PUTs the photograph straight to
    storage; the bytes never come through this API.

    The best moment to get a birth certificate is while somebody is sitting
    with the form open and the certificate in their hand. Every week after
    that is a phone call.
    """
    try:
        ticket = await service.request_upload(
            principal,
            kind=body.kind,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
        )
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc

    return UploadTicketResponse(
        url=ticket.url,
        object_key=ticket.object_key,
        expires_at=ticket.expires_at,
        headers=ticket.headers,
        max_bytes=ticket.max_bytes,
    )


@router.post(
    "/me/documents",
    response_model=ApplyStateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm an uploaded document",
)
async def attach_document(
    body: DocumentCreateRequest,
    principal: PrincipalDep,
    service: AdmissionsServiceDep,
    context: ContextDep,
) -> ApplyStateResponse:
    """Step two. Storage is asked what actually arrived rather than trusting
    what the client said it would send — a presigned PUT cannot enforce a
    length, and on a 3G connection a real fraction of uploads never finish.

    Answers with the whole form state, so the checklist on the parent's screen
    updates without a second request.
    """
    try:
        await service.attach_document(
            principal,
            object_key=body.object_key,
            kind=body.kind,
            file_name=body.file_name,
            context=context,
        )
        view = await service.my_application(principal)
    except (NotAllowed, NotFound, Invalid) as exc:
        raise _handle(exc) from exc
    return _as_state(view)
