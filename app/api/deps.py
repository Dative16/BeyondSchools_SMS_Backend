"""Request dependencies: the current user, the current school, a bound session.

Routes depend on `tenant_session`, never on `get_session` directly. That is what
keeps the row-level security guarantee true for every endpoint by default: the
tenant is resolved once, here, and pushed into the PostgreSQL session before a
single query runs.

Three ways a request gets a school, in order of trust:

1. the `school_id` claim on a validated access token, for anything behind auth;
2. the subdomain of the Host header, for sign-in and the public application
   form, which have no token yet;
3. an `X-School-Subdomain` header, which is the same thing for local
   development, where the web app runs on `localhost:5173` and there is no
   subdomain to read.

The third is only honoured where no token is present, so it can never be used
to point an authenticated request at somebody else's school.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import TokenError, decode_access_token
from app.core.tenancy import bind_tenant, clear_tenant, unscoped
from app.db.session import get_session
from app.domain.roles import Permission, Principal, can
from app.integrations.sms.base import SmsSender
from app.integrations.sms.provider import get_sms_sender
from app.integrations.storage import ObjectStore, get_object_store
from app.repositories.academic import SqlAcademicRepository
from app.repositories.admissions import SqlAdmissionsRepository
from app.repositories.audit import SqlAuditRepository
from app.repositories.base import SqlAlchemyUnitOfWork
from app.repositories.fees import SqlFeesRepository
from app.repositories.guardians import SqlGuardianRepository
from app.repositories.payments import SqlPaymentRepository
from app.repositories.schools import (
    PublicSchool,
    SqlRegistrationSettingsRepository,
    SqlSchoolRepository,
)
from app.repositories.students import SqlStudentRepository
from app.repositories.users import SqlUserRepository
from app.services.academic import AcademicService
from app.services.admissions import AdmissionsService
from app.services.auth import AuthService, RequestContext
from app.services.invoicing import InvoicingService
from app.services.notifications import NotificationsService
from app.services.students import StudentService

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def request_context(request: Request) -> RequestContext:
    """Where the call came from, for the audit trail.

    `X-Forwarded-For`'s first entry is the client as the edge saw it. It is
    only trustworthy because nothing but our own proxy can reach the app; it
    goes in the audit log, never into an authorisation decision.
    """
    forwarded = request.headers.get("x-forwarded-for")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else None
    )
    return RequestContext(ip=ip, user_agent=request.headers.get("user-agent"))


# --------------------------------------------------------------------------
# Who is asking
# --------------------------------------------------------------------------
async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Validate the bearer token and return who it speaks for.

    No database query: everything needed is in the token, and a lookup per
    request would be a cost paid on every call to protect against something
    the 15-minute lifetime already bounds. Revocation goes through the refresh
    token, so a dismissal takes effect within the quarter hour.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return decode_access_token(authorization.split(" ", 1)[1].strip())
    except TokenError as exc:
        # The reason goes to the log, not the response: telling a caller why
        # their token failed helps them forge the next one.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


PrincipalDep = Annotated[Principal, Depends(current_principal)]


async def tenant_session(
    principal: PrincipalDep,
    session: SessionDep,
) -> AsyncIterator[AsyncSession]:
    """A session pinned to the caller's school for the rest of the transaction."""
    if principal.school_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This endpoint is scoped to one school"
        )
    await bind_tenant(session, principal.school_id)
    yield session


TenantSessionDep = Annotated[AsyncSession, Depends(tenant_session)]


def requires(permission: Permission) -> Callable[[Principal], Awaitable[Principal]]:
    """Guard a route with one named capability.

    The service layer checks permissions too, and that is the check that
    counts — the same rule has to hold when a call arrives from a Celery task
    or the AI platform's API. This one exists so a route cannot be *reached*
    without the capability, which keeps the 403 out of the service and makes
    the requirement readable in the route signature.
    """

    async def guard(principal: PrincipalDep) -> Principal:
        if not can(principal, permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This account may not {permission.value.replace(':', ' ')}",
            )
        return principal

    return guard


# --------------------------------------------------------------------------
# Which school, before there is a token
# --------------------------------------------------------------------------
def _subdomain_from_host(host: str | None) -> str | None:
    if not host:
        return None
    hostname = host.split(":")[0].strip().lower()
    parts = hostname.split(".")
    if len(parts) < 3 or parts[0] in ("www", "api", "localhost"):
        return None
    return parts[0]


def _requested_subdomain(
    request: Request, x_school_subdomain: str | None
) -> str | None:
    subdomain = _subdomain_from_host(request.headers.get("host"))
    if subdomain is not None:
        return subdomain
    if not x_school_subdomain:
        return None
    if settings.ENVIRONMENT == "production":
        # In production the subdomain is the tenant, and a header that can
        # override it is a way to aim a sign-in at another school.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Reach this school on its own subdomain"
        )
    return x_school_subdomain.strip().lower()


async def optional_school(
    request: Request,
    session: SessionDep,
    x_school_subdomain: Annotated[str | None, Header()] = None,
) -> PublicSchool | None:
    """The school this unauthenticated request is for, if it names one.

    None means the platform itself, which is only the platform admin signing
    in. The lookup runs unscoped and goes through
    `resolve_school_by_subdomain`, so it returns branding and nothing else.
    """
    subdomain = _requested_subdomain(request, x_school_subdomain)
    if not subdomain:
        return None

    async with unscoped(session):
        school = await SqlSchoolRepository(session).resolve_by_subdomain(subdomain)

    if school is None:
        # The same answer for "no such school" and "suspended": which schools
        # exist on the platform is not something a stranger needs to map.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such school")
    return school


async def resolve_school(
    school: Annotated[PublicSchool | None, Depends(optional_school)],
) -> PublicSchool:
    """Same, for the routes that cannot work without a school: the branding
    endpoint and the public application form."""
    if school is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Could not tell which school this request is for",
        )
    return school


SchoolDep = Annotated[PublicSchool, Depends(resolve_school)]
OptionalSchoolDep = Annotated[PublicSchool | None, Depends(optional_school)]


async def auth_session(
    session: SessionDep,
    school: OptionalSchoolDep,
) -> AsyncIterator[AsyncSession]:
    """A session for the sign-in routes, bound before any lookup runs.

    This matters more than it looks. Sign-in finds a user by email or phone
    *without* a `school_id` filter, exactly like every other query in this
    codebase — so the only thing stopping a staff member at one school signing
    in through another school's subdomain is that the session is already pinned
    here. Bind after resolving, never after looking up.
    """
    if school is not None:
        await bind_tenant(session, school.id)
    else:
        await clear_tenant(session)
    yield session


AuthSessionDep = Annotated[AsyncSession, Depends(auth_session)]


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------
def sms_sender() -> SmsSender:
    return get_sms_sender()


async def auth_service(
    session: AuthSessionDep,
    sms: Annotated[SmsSender, Depends(sms_sender)],
) -> AuthService:
    """The auth service, on a session already bound to the school whose
    subdomain the request arrived on."""
    return AuthService(
        uow=SqlAlchemyUnitOfWork(session),
        users=SqlUserRepository(session),
        audit=SqlAuditRepository(session),
        sms=sms,
        admissions=SqlAdmissionsRepository(session),
    )


AuthServiceDep = Annotated[AuthService, Depends(auth_service)]


async def tenant_auth_service(
    session: TenantSessionDep,
    sms: Annotated[SmsSender, Depends(sms_sender)],
) -> AuthService:
    """The same service, on a session bound from the *token* instead.

    The distinction matters. Sign-in has no token, so it is scoped by the
    subdomain the request arrived on. Everything after sign-in is scoped by the
    school in the token, which is both more trustworthy and the only thing
    available to a client that is not on a school subdomain — a native app, or
    the staff app in local development.
    """
    return AuthService(
        uow=SqlAlchemyUnitOfWork(session),
        users=SqlUserRepository(session),
        audit=SqlAuditRepository(session),
        sms=sms,
        admissions=SqlAdmissionsRepository(session),
    )


TenantAuthServiceDep = Annotated[AuthService, Depends(tenant_auth_service)]


async def academic_service(session: TenantSessionDep) -> AcademicService:
    """Academic structure, on a session bound from the token's school."""
    return AcademicService(
        uow=SqlAlchemyUnitOfWork(session),
        academic=SqlAcademicRepository(session),
        schools=SqlSchoolRepository(session),
        settings_repo=SqlRegistrationSettingsRepository(session),
        audit=SqlAuditRepository(session),
    )


AcademicServiceDep = Annotated[AcademicService, Depends(academic_service)]


def object_store() -> ObjectStore:
    return get_object_store()


async def student_service(
    session: TenantSessionDep,
    storage: Annotated[ObjectStore, Depends(object_store)],
) -> StudentService:
    """Students, enrolments, guardians and the detail around them."""
    return StudentService(
        uow=SqlAlchemyUnitOfWork(session),
        students=SqlStudentRepository(session),
        guardians=SqlGuardianRepository(session),
        schools=SqlSchoolRepository(session),
        settings_repo=SqlRegistrationSettingsRepository(session),
        audit=SqlAuditRepository(session),
        storage=storage,
    )


StudentServiceDep = Annotated[StudentService, Depends(student_service)]


def _invoicing(session: AsyncSession) -> InvoicingService:
    return InvoicingService(
        uow=SqlAlchemyUnitOfWork(session),
        fees=SqlFeesRepository(session),
        payments=SqlPaymentRepository(session),
        audit=SqlAuditRepository(session),
    )


async def invoicing_service(session: TenantSessionDep) -> InvoicingService:
    """Raising bills and recording money, scoped by the token."""
    return _invoicing(session)


InvoicingServiceDep = Annotated[InvoicingService, Depends(invoicing_service)]


def _notifications(session: AsyncSession) -> NotificationsService:
    return NotificationsService(
        uow=SqlAlchemyUnitOfWork(session),
        sms=get_sms_sender(),
        schools=SqlSchoolRepository(session),
        audit=SqlAuditRepository(session),
    )


async def notifications_service(session: TenantSessionDep) -> NotificationsService:
    """What the school says to a family, scoped by the token."""
    return _notifications(session)


NotificationsServiceDep = Annotated[NotificationsService, Depends(notifications_service)]


def _admissions(session: AsyncSession) -> AdmissionsService:
    return AdmissionsService(
        uow=SqlAlchemyUnitOfWork(session),
        admissions=SqlAdmissionsRepository(session),
        students=SqlStudentRepository(session),
        guardians=SqlGuardianRepository(session),
        schools=SqlSchoolRepository(session),
        settings_repo=SqlRegistrationSettingsRepository(session),
        invoicing=_invoicing(session),
        users=SqlUserRepository(session),
        audit=SqlAuditRepository(session),
        storage=get_object_store(),
        notifications=_notifications(session),
    )


async def admissions_service(session: TenantSessionDep) -> AdmissionsService:
    """The funnel, scoped by the token: the registrar's screens."""
    return _admissions(session)


AdmissionsServiceDep = Annotated[AdmissionsService, Depends(admissions_service)]


async def public_admissions_service(session: AuthSessionDep) -> AdmissionsService:
    """The same funnel, scoped by the subdomain.

    For the two steps that happen before a token exists: what the form should
    ask for, and starting or resuming an application once a phone number has
    been verified.
    """
    return _admissions(session)


PublicAdmissionsServiceDep = Annotated[
    AdmissionsService, Depends(public_admissions_service)
]
