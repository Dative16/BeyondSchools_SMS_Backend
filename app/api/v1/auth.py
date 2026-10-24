"""Auth endpoints.

Every route here is the same four lines of work: parse, call one service
method, turn the result or the exception into a response, and — where a session
is involved — set or clear the refresh cookie. There is no rule in this file,
which is the point: the rules are in `app/domain/identity.py` where they can be
tested without a request.

The cookie is the one piece of real policy in the file, and it is here rather
than in the service because a cookie is an HTTP thing. httpOnly so no script
can read it, SameSite=Lax so a cross-site form post cannot spend it, Secure
outside local development, and scoped to the refresh path so it is not sent
with every ordinary API call.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status

from app.api.deps import (
    AuthServiceDep,
    OptionalSchoolDep,
    PrincipalDep,
    SchoolDep,
    TenantAuthServiceDep,
    request_context,
)
from app.core.config import settings
from app.core.security import HANDOFF_TTL_SECONDS
from app.domain.identity import OtpPurpose
from app.domain.roles import Role, permissions_of
from app.schemas.auth import (
    ChangePasswordRequest,
    HandoffRequest,
    HandoffResponse,
    LoginRequest,
    OtpLoginRequest,
    OtpSentResponse,
    OtpStartRequest,
    PrincipalResponse,
    SchoolBrandResponse,
    TokenResponse,
)
from app.services.auth import (
    AccountLocked,
    AuthError,
    InvalidCredentials,
    OtpRejected,
    RequestContext,
    SessionExpired,
)
from app.services.auth import Session as AuthSession

router = APIRouter()

REFRESH_COOKIE = "bs_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"

ContextDep = Annotated[RequestContext, Depends(request_context)]
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE)]


def _set_refresh_cookie(response: Response, session: AuthSession) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        session.refresh_token,
        max_age=settings.REFRESH_TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.ENVIRONMENT != "local",
        path=REFRESH_COOKIE_PATH,
    )


def _as_token_response(session: AuthSession) -> TokenResponse:
    return TokenResponse(
        access_token=session.access_token,
        expires_in=settings.ACCESS_TOKEN_TTL_SECONDS,
        expires_at=session.access_expires_at,
        principal=PrincipalResponse(
            user_id=session.principal.user_id,
            school_id=session.principal.school_id,
            full_name=session.full_name,
            role=session.principal.role,
            student_id=session.principal.student_id,
            guardian_id=session.principal.guardian_id,
            permissions=sorted(permissions_of(session.principal.role)),
            must_change_password=session.must_change_password,
        ),
    )


@router.get(
    "/school",
    response_model=SchoolBrandResponse,
    summary="Branding for the login page and the application form",
)
async def school_brand(school: SchoolDep) -> SchoolBrandResponse:
    """Unauthenticated, and holds nothing but branding.

    The login page needs a name and a logo before anybody has signed in. What
    it must not be able to read is anything about the school's money or its
    children, which is why this is served by a SECURITY DEFINER function with
    a fixed column list rather than by a query against `school`.
    """
    return SchoolBrandResponse(
        id=school.id,
        name=school.name,
        subdomain=school.subdomain,
        brand_color=school.brand_color,
        logo_url=school.logo_url,
        locale=school.locale,
        timezone=school.timezone,
    )


@router.post("/login", response_model=TokenResponse, summary="Sign in with a password")
async def login(
    body: LoginRequest,
    response: Response,
    service: AuthServiceDep,
    school: OptionalSchoolDep,
    context: ContextDep,
) -> TokenResponse:
    try:
        session = await service.login(
            school_id=school.id if school else None,
            identifier=body.identifier,
            password=body.password,
            context=context,
        )
    except AccountLocked as exc:
        # 423 rather than 401: the client has to stop retrying and say why.
        raise HTTPException(status.HTTP_423_LOCKED, exc.reason) from exc
    except InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.reason) from exc

    _set_refresh_cookie(response, session)
    return _as_token_response(session)


@router.post(
    "/otp/start",
    response_model=OtpSentResponse,
    summary="Send a one-time code by SMS",
)
async def start_otp(
    body: OtpStartRequest,
    service: AuthServiceDep,
    school: SchoolDep,
    context: ContextDep,
) -> OtpSentResponse:
    """How everybody without a password gets in.

    The response is identical whether or not the number belongs to anybody at
    the school. A 404 here would turn this route into a way to ask which
    parents a school has.
    """
    try:
        expires_at = await service.start_otp(
            school_id=school.id,
            phone=body.phone,
            purpose=OtpPurpose(body.purpose),
            context=context,
        )
    except OtpRejected as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.reason) from exc

    return OtpSentResponse(expires_at=expires_at, resend_after_seconds=60)


@router.post(
    "/otp/login",
    response_model=TokenResponse,
    summary="Sign in with a one-time code",
)
async def otp_login(
    body: OtpLoginRequest,
    response: Response,
    service: AuthServiceDep,
    school: SchoolDep,
    context: ContextDep,
) -> TokenResponse:
    try:
        session = await service.login_with_otp(
            school_id=school.id, phone=body.phone, code=body.code, context=context
        )
    except OtpRejected as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.reason) from exc
    except InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.reason) from exc

    _set_refresh_cookie(response, session)
    return _as_token_response(session)


@router.post("/refresh", response_model=TokenResponse, summary="Rotate the session")
async def refresh(
    response: Response,
    service: AuthServiceDep,
    context: ContextDep,
    bs_refresh: RefreshCookie = None,
) -> TokenResponse:
    """Exchange the refresh cookie for a new pair.

    The old token is spent in the process. A token presented twice means two
    parties hold it, and the service cuts every session for that user — so a
    401 here can mean "your cookie is stale" or "somebody copied it", and the
    client's answer to both is the same: sign in again.
    """
    if not bs_refresh:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No session")

    try:
        session = await service.refresh(refresh_token=bs_refresh, context=context)
    except SessionExpired as exc:
        response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.reason) from exc

    _set_refresh_cookie(response, session)
    return _as_token_response(session)


@router.post(
    "/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session"
)
async def logout(
    response: Response,
    service: AuthServiceDep,
    context: ContextDep,
    bs_refresh: RefreshCookie = None,
) -> Response:
    """Always 204. Signing out of a session that has already expired is not a
    problem worth showing somebody who is leaving."""
    await service.logout(refresh_token=bs_refresh, context=context)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=PrincipalResponse, summary="Who am I")
async def me(principal: PrincipalDep, service: TenantAuthServiceDep) -> PrincipalResponse:
    """The shell's first call after sign-in.

    The name comes from the database rather than the token: a token lives
    fifteen minutes, and somebody who corrects the spelling of their own name
    should not have to wait that long to see it.
    """
    try:
        described = await service.describe(principal)
    except InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.reason) from exc

    return PrincipalResponse(
        user_id=principal.user_id,
        school_id=principal.school_id,
        full_name=described.full_name,
        role=principal.role,
        student_id=principal.student_id,
        guardian_id=principal.guardian_id,
        permissions=sorted(permissions_of(principal.role)),
        must_change_password=described.must_change_password,
    )


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change my own password",
)
async def change_password(
    body: ChangePasswordRequest,
    response: Response,
    principal: PrincipalDep,
    service: TenantAuthServiceDep,
    context: ContextDep,
) -> Response:
    """Ends every session, including this one, on purpose: somebody changing
    their password after a scare must not leave a thief's session alive."""
    try:
        await service.change_password(
            principal=principal,
            current_password=body.current_password,
            new_password=body.new_password,
            context=context,
        )
    except InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.reason) from exc
    except AuthError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason) from exc

    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/ai-handoff",
    response_model=HandoffResponse,
    summary="Cross to aibeyondschools.co.tz already signed in",
)
async def ai_handoff(
    body: HandoffRequest,
    principal: PrincipalDep,
    service: TenantAuthServiceDep,
    context: ContextDep,
) -> HandoffResponse:
    """The route the whole project is for.

    A student taps a homework link, this mints a two-minute token for the AI
    platform, and they arrive already signed in. A second login screen at that
    moment would lose most of a cohort.

    The redirect URL is built here rather than accepted from the caller: a
    signed token plus a client-chosen destination is an open redirect that
    leaks the token to whoever asked for it.
    """
    try:
        token = await service.handoff(
            principal=principal, student_id=body.student_id, context=context
        )
    except AuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.reason) from exc

    base = (settings.AI_PLATFORM_BASE_URL or "").rstrip("/")
    return HandoffResponse(
        token=token.token,
        expires_at=token.expires_at,
        expires_in=HANDOFF_TTL_SECONDS,
        redirect_url=f"{base}/sso/callback?token={token.token}",
    )


@router.get(
    "/roles",
    summary="The authorisation model, as the server sees it",
)
async def roles(principal: PrincipalDep) -> dict[str, list[str]]:
    """What each role may do.

    Served so the staff app can build its navigation from the server's answer
    instead of keeping a second copy of the permission matrix that drifts from
    this one. Readable by any signed-in user: it describes the model, not
    anybody's data.
    """
    return {
        role.value: sorted(p.value for p in permissions_of(role))
        for role in Role
        if role is not Role.PLATFORM_ADMIN
    }
