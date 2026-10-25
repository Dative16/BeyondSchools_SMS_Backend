"""Sign-in, sessions, one-time codes, and the handoff to the AI platform.

This service owns the sequence; `app/domain/identity.py` owns the judgements
and `app/core/security.py` owns the cryptography. It raises its own exceptions
and imports no FastAPI, which is what lets the same code back an HTTP route, a
Celery task and a CLI script unchanged.

The property the whole product leans on is in `handoff`: a student who taps a
homework link arrives at aibeyondschools.co.tz already signed in. A second
login screen at that moment would lose most of a cohort, and with it the
distribution advantage that justifies building this at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import settings
from app.core.security import (
    AUDIENCE_AI_PLATFORM,
    IssuedToken,
    generate_otp,
    generate_token,
    hash_otp,
    hash_password,
    hash_token,
    issue_access_token,
    issue_handoff_token,
    verify_otp,
    verify_password,
)
from app.domain.identity import (
    OTP_TTL,
    LoginOutcome,
    OtpOutcome,
    OtpPurpose,
    evaluate_login,
    evaluate_otp,
    evaluate_refresh,
    may_send_otp,
)
from app.domain.roles import Principal, Role
from app.integrations.sms.base import SmsMessage, SmsSender
from app.models.audit import AuditOutcome
from app.models.user import AppUser
from app.repositories.admissions import AdmissionsRepository
from app.repositories.audit import AuditRepository
from app.repositories.base import UnitOfWork
from app.repositories.users import UserRepository


class AuthError(Exception):
    """Base for everything this service refuses to do. The API layer maps
    these to status codes; this service does not know what one is."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidCredentials(AuthError):
    """Wrong password, unknown user, or no password set.

    One exception for all three on purpose. Which accounts exist at a school is
    not public information, and an error that distinguishes them is a way to
    find out.
    """


class AccountLocked(AuthError):
    """Too many failed attempts, or an account a school has disabled."""


class SessionExpired(AuthError):
    """The refresh token is gone, spent or stale. Sign in again."""


class OtpRejected(AuthError):
    """A code that was wrong, expired, used up or too soon to resend."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Where the call came from, for the audit trail.

    Threaded through rather than read from a global: a service that reaches
    for request state is a service that cannot run in a Celery worker.
    """

    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True, slots=True)
class Me:
    """The caller, as the staff app's shell needs them."""

    full_name: str
    must_change_password: bool


@dataclass(frozen=True, slots=True)
class Session:
    """What a successful sign-in hands back.

    The access token goes in the response body and lives in the browser's
    memory; the refresh token goes in an httpOnly cookie. The access token is
    also what carries a student across to the AI platform, which is why it is
    never written to localStorage.
    """

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    principal: Principal
    full_name: str
    must_change_password: bool


class AuthService:
    def __init__(
        self,
        uow: UnitOfWork,
        users: UserRepository,
        audit: AuditRepository,
        sms: SmsSender,
        admissions: AdmissionsRepository,
    ) -> None:
        self._uow = uow
        self._users = users
        self._audit = audit
        self._sms = sms
        self._admissions = admissions

    # ----------------------------------------------------------------------
    # Passwords
    # ----------------------------------------------------------------------
    async def login(
        self,
        *,
        school_id: UUID | None,
        identifier: str,
        password: str,
        context: RequestContext,
        as_of: datetime | None = None,
    ) -> Session:
        """Exchange a password for a session.

        The session is bound to the school the caller resolved from the
        subdomain, and a user belonging to a different school is treated as
        no user at all — the lookup runs on a session already pinned to one
        school, so it cannot find them in the first place.
        """
        now = as_of or datetime.now(UTC)

        async with self._uow:
            user = await self._users.find_by_identifier(identifier)
            facts = await self._users.facts_for(user) if user else None

            # The hash is computed either way. Returning early for an unknown
            # user would make "no such account" measurably faster than "wrong
            # password", which is a way to enumerate a school's staff.
            matches = verify_password(password, user.password_hash if user else None)

            decision = evaluate_login(user=facts, password_matches=matches, as_of=now)

            if user is not None:
                await self._users.record_login_result(
                    user.id,
                    failed_logins=decision.failed_logins,
                    locked_until=decision.locked_until,
                    succeeded=decision.allowed,
                )

            if not decision.allowed:
                await self._audit.record(
                    school_id=school_id,
                    action="auth.login",
                    actor_user_id=user.id if user else None,
                    actor_role=user.role.value if user else None,
                    outcome=AuditOutcome.DENIED,
                    note=f"{decision.outcome}: {decision.reason}",
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
                await self._uow.commit()

                if decision.outcome in (LoginOutcome.LOCKED, LoginOutcome.INACTIVE):
                    raise AccountLocked(decision.reason)
                raise InvalidCredentials("those details did not match an account")

            assert user is not None  # evaluate_login cannot allow a missing user
            session = await self._open_session(user=user, context=context, now=now)

            await self._audit.record(
                school_id=user.school_id,
                action="auth.login",
                actor_user_id=user.id,
                actor_role=user.role.value,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return session

    async def change_password(
        self,
        *,
        principal: Principal,
        current_password: str,
        new_password: str,
        context: RequestContext,
    ) -> None:
        """Change one's own password, and end every other session.

        Revoking the rest is the point: somebody changing their password after
        a scare must not leave the thief's session alive for another fortnight.
        """
        if len(new_password) < 10:
            raise AuthError("a password needs at least 10 characters")

        async with self._uow:
            user = await self._users.find_by_id(principal.user_id)
            if user is None or not verify_password(current_password, user.password_hash):
                raise InvalidCredentials("the current password did not match")

            user.password_hash = hash_password(new_password)
            user.must_change_password = False
            revoked = await self._users.revoke_all_for_user(
                principal.user_id, reason="password changed"
            )
            await self._audit.record(
                school_id=principal.school_id,
                action="auth.password_changed",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="app_user",
                entity_id=principal.user_id,
                note=f"{revoked} session(s) revoked",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

    # ----------------------------------------------------------------------
    # Sessions
    # ----------------------------------------------------------------------
    async def refresh(
        self,
        *,
        refresh_token: str,
        context: RequestContext,
        as_of: datetime | None = None,
    ) -> Session:
        """Rotate a refresh token for a new pair.

        Single-use, which turns a stolen cookie from a fortnight of access into
        a race. If a token is presented after it has already been exchanged,
        two parties hold it: every session for that user is cut and both have
        to sign in again.
        """
        now = as_of or datetime.now(UTC)

        async with self._uow:
            facts = await self._users.find_refresh_token(hash_token(refresh_token))
            decision = evaluate_refresh(token=facts, as_of=now)

            if not decision.allowed:
                if decision.revoke_family and facts is not None:
                    revoked = await self._users.revoke_all_for_user(
                        facts.user_id, reason="refresh token reuse detected"
                    )
                    await self._audit.record(
                        school_id=facts.school_id,
                        action="auth.refresh_reuse_detected",
                        actor_user_id=facts.user_id,
                        outcome=AuditOutcome.DENIED,
                        note=f"{decision.reason}; {revoked} session(s) revoked",
                        ip=context.ip,
                        user_agent=context.user_agent,
                    )
                await self._uow.commit()
                raise SessionExpired(decision.reason)

            assert facts is not None
            user = await self._users.find_by_id(facts.user_id)
            if user is None or not user.is_active:
                # The 15-minute access token is what makes this check cheap:
                # it only has to happen here, once a quarter of an hour.
                await self._users.revoke_refresh_token(
                    facts.token_id, reason="user inactive or gone"
                )
                await self._uow.commit()
                raise SessionExpired("this account can no longer sign in")

            refresh_expires_at = now + timedelta(seconds=settings.REFRESH_TOKEN_TTL_SECONDS)
            new_refresh = generate_token()
            await self._users.rotate_refresh_token(
                old_token_id=facts.token_id,
                new_token_hash=hash_token(new_refresh),
                expires_at=refresh_expires_at,
                user_agent=context.user_agent,
                ip=context.ip,
            )

            principal = await self._principal_for(user)
            access = issue_access_token(principal)
            await self._uow.commit()

            return Session(
                access_token=access.token,
                access_expires_at=access.expires_at,
                refresh_token=new_refresh,
                refresh_expires_at=refresh_expires_at,
                principal=principal,
                full_name=user.full_name,
                must_change_password=user.must_change_password,
            )

    async def logout(self, *, refresh_token: str | None, context: RequestContext) -> None:
        """End this session. Idempotent: a cookie that is already dead is not
        an error worth showing somebody who is leaving."""
        if not refresh_token:
            return
        async with self._uow:
            facts = await self._users.find_refresh_token(hash_token(refresh_token))
            if facts is not None:
                await self._users.revoke_refresh_token(facts.token_id, reason="signed out")
                await self._audit.record(
                    school_id=facts.school_id,
                    action="auth.logout",
                    actor_user_id=facts.user_id,
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
            await self._uow.commit()

    # ----------------------------------------------------------------------
    # One-time codes
    # ----------------------------------------------------------------------
    async def start_otp(
        self,
        *,
        school_id: UUID | None,
        phone: str,
        purpose: OtpPurpose,
        context: RequestContext,
        as_of: datetime | None = None,
    ) -> datetime:
        """Send a code, and say when it expires.

        The front door for everybody without a password: an applicant coming
        back to a half-finished form, a guardian checking a balance, a parent
        opening a results link in the holidays. It is also the cheapest defence
        against junk applications, which is why the public form verifies a
        number before collecting anything else.

        What this deliberately does not reveal is whether the number is known.
        A code is sent to any valid number, and the caller cannot tell a
        guardian's phone from a stranger's.
        """
        now = as_of or datetime.now(UTC)

        async with self._uow:
            sent, last_sent_at = await self._users.otp_sends_since(
                phone=phone, since=now - timedelta(hours=1)
            )
            allowed, reason = may_send_otp(
                sent_in_last_hour=sent, last_sent_at=last_sent_at, as_of=now
            )
            if not allowed:
                await self._audit.record(
                    school_id=school_id,
                    action="auth.otp_throttled",
                    outcome=AuditOutcome.DENIED,
                    note=f"{phone}: {reason}",
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
                await self._uow.commit()
                raise OtpRejected(reason)

            code = generate_otp()
            expires_at = now + OTP_TTL
            await self._users.create_otp_challenge(
                school_id=school_id,
                phone=phone,
                purpose=purpose,
                code_hash=hash_otp(code),
                expires_at=expires_at,
                ip=context.ip,
            )

            minutes = int(OTP_TTL.total_seconds() // 60)
            await self._sms.send(
                SmsMessage(
                    to=phone,
                    body=(
                        f"{code} is your BeyondSchools code. "
                        f"It expires in {minutes} minutes. Do not share it."
                    ),
                    reference=f"otp:{purpose.value}",
                )
            )

            await self._audit.record(
                school_id=school_id,
                action="auth.otp_sent",
                note=f"{phone} ({purpose.value})",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return expires_at

    async def verify_otp_code(
        self,
        *,
        school_id: UUID | None,
        phone: str,
        purpose: OtpPurpose,
        code: str,
        context: RequestContext,
        as_of: datetime | None = None,
    ) -> None:
        """Check a code and spend it.

        Spending it matters: a code that stays valid after use is a code that
        works twice, and the second use is never the parent.
        """
        now = as_of or datetime.now(UTC)

        async with self._uow:
            found = await self._users.latest_otp_challenge(
                school_id=school_id, phone=phone, purpose=purpose
            )
            facts, code_hash = found if found else (None, "")
            decision = evaluate_otp(
                challenge=facts,
                code_matches=bool(found) and verify_otp(code, code_hash),
                as_of=now,
            )

            if facts is not None:
                await self._users.record_otp_attempt(
                    facts.challenge_id,
                    attempts=decision.attempts,
                    consumed=decision.accepted,
                )

            if not decision.accepted:
                await self._audit.record(
                    school_id=school_id,
                    action="auth.otp_failed",
                    outcome=AuditOutcome.DENIED,
                    note=f"{phone}: {decision.outcome}",
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
                await self._uow.commit()
                spent = (
                    OtpOutcome.EXPIRED,
                    OtpOutcome.EXHAUSTED,
                    OtpOutcome.ALREADY_USED,
                )
                raise OtpRejected(
                    "that code can no longer be used, send another"
                    if decision.outcome in spent
                    else "that code is not right"
                )

            await self._uow.commit()

    async def login_with_otp(
        self,
        *,
        school_id: UUID,
        phone: str,
        code: str,
        context: RequestContext,
        as_of: datetime | None = None,
    ) -> Session:
        """Sign in with a code instead of a password.

        How a guardian gets in. The code is checked first and spent whether or
        not an account turns up, so a wrong code and an unknown number take the
        same path and reveal the same amount.
        """
        now = as_of or datetime.now(UTC)
        await self.verify_otp_code(
            school_id=school_id,
            phone=phone,
            purpose=OtpPurpose.LOGIN,
            code=code,
            context=context,
            as_of=now,
        )

        async with self._uow:
            user = await self._users.find_by_identifier(phone)
            if user is None or not user.is_active:
                await self._audit.record(
                    school_id=school_id,
                    action="auth.login_otp",
                    outcome=AuditOutcome.DENIED,
                    note=f"{phone}: no active account",
                    ip=context.ip,
                    user_agent=context.user_agent,
                )
                await self._uow.commit()
                raise InvalidCredentials("those details did not match an account")

            session = await self._open_session(user=user, context=context, now=now)
            await self._users.record_login_result(
                user.id, failed_logins=0, locked_until=None, succeeded=True
            )
            await self._audit.record(
                school_id=user.school_id,
                action="auth.login_otp",
                actor_user_id=user.id,
                actor_role=user.role.value,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
            return session

    async def describe(self, principal: Principal) -> Me:
        """Who the caller is, read from the database rather than the token.

        A token lives fifteen minutes, and somebody who corrects the spelling
        of their own name should not have to wait that long to see it.
        """
        user = await self._users.find_by_id(principal.user_id)
        if user is None or not user.is_active:
            raise InvalidCredentials("this account no longer exists")
        return Me(full_name=user.full_name, must_change_password=user.must_change_password)

    async def open_applicant_session(
        self,
        *,
        school_id: UUID,
        application_id: UUID,
        phone: str,
        context: RequestContext,
    ) -> Session:
        """A session for somebody filling in the public form.

        Scoped to one application by the claim, so every route they can reach
        takes it from the token and there is no id for them to change.

        The login is created once per phone number and kept. At enrolment it
        is promoted to the family's guardian account rather than replaced,
        which is how a parent who applied in November signs in to pay fees in
        January with what they already have.
        """
        async with self._uow:
            user = await self._users.find_by_identifier(phone)
            if user is None:
                user = await self._users.add_applicant(school_id=school_id, phone=phone)
            elif not user.is_active:
                raise AccountLocked("this account can no longer sign in")

            principal = Principal(
                user_id=user.id,
                school_id=school_id,
                role=Role.APPLICANT,
                application_id=application_id,
            )
            access = issue_access_token(principal)

            refresh = generate_token()
            now = datetime.now(UTC)
            refresh_expires_at = now + timedelta(seconds=settings.REFRESH_TOKEN_TTL_SECONDS)
            await self._users.store_refresh_token(
                user_id=user.id,
                school_id=school_id,
                token_hash=hash_token(refresh),
                expires_at=refresh_expires_at,
                user_agent=context.user_agent,
                ip=context.ip,
            )
            await self._audit.record(
                school_id=school_id,
                action="auth.applicant_session",
                actor_user_id=user.id,
                actor_role=Role.APPLICANT.value,
                entity_type="application",
                entity_id=application_id,
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()

            return Session(
                access_token=access.token,
                access_expires_at=access.expires_at,
                refresh_token=refresh,
                refresh_expires_at=refresh_expires_at,
                principal=principal,
                full_name=user.full_name,
                must_change_password=False,
            )

    # ----------------------------------------------------------------------
    # The handoff
    # ----------------------------------------------------------------------
    async def handoff(
        self,
        *,
        principal: Principal,
        student_id: UUID | None,
        context: RequestContext,
    ) -> IssuedToken:
        """Mint a short-lived token for aibeyondschools.co.tz.

        Students and guardians only. A bursar has no reason to cross over, and
        a token minted for staff would be a token the AI platform has to decide
        what to do with — better that it never exists.

        The audience differs from an access token's, so the AI platform cannot
        be handed an API token by mistake and this API cannot be handed a
        handoff token. Every crossing is audited, because a child's attempt
        history is theirs and somebody has to be able to say who opened it.
        """
        if principal.role is Role.STUDENT:
            # A student crosses as themselves, whatever the request asked for.
            child_id = principal.student_id
        elif principal.role is Role.GUARDIAN:
            if student_id is None:
                raise AuthError("say which child this is for")
            if principal.guardian_id is None or not await self._users.guardian_has_child(
                principal.guardian_id, student_id
            ):
                # Audited as a denial: an attempt to cross to a child one is
                # not linked to is the kind of thing somebody should see.
                async with self._uow:
                    await self._audit.record(
                        school_id=principal.school_id,
                        action="auth.ai_handoff",
                        actor_user_id=principal.user_id,
                        actor_role=principal.role.value,
                        entity_type="student",
                        entity_id=student_id,
                        outcome=AuditOutcome.DENIED,
                        note="guardian is not linked to this child",
                        ip=context.ip,
                        user_agent=context.user_agent,
                    )
                    await self._uow.commit()
                raise AuthError("that child is not linked to this account")
            child_id = student_id
        else:
            raise AuthError("only a student or a guardian crosses to the AI platform")

        if child_id is None:
            raise AuthError("this account is not attached to a student record")

        level_code = await self._users.current_level_code(child_id)
        token = issue_handoff_token(principal, student_id=child_id, level_code=level_code)

        async with self._uow:
            await self._audit.record(
                school_id=principal.school_id,
                action="auth.ai_handoff",
                actor_user_id=principal.user_id,
                actor_role=principal.role.value,
                entity_type="student",
                entity_id=child_id,
                note=f"audience={AUDIENCE_AI_PLATFORM} level={level_code or 'unset'}",
                ip=context.ip,
                user_agent=context.user_agent,
            )
            await self._uow.commit()
        return token

    # ----------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------
    async def _principal_for(self, user: AppUser) -> Principal:
        """Assemble the principal a token speaks for."""
        # A teacher's classes are deliberately absent: the token does not
        # carry them, so resolving them here would be a query at every sign-in
        # whose answer goes nowhere. The students service fills the scope in
        # per request, where it is used.

        # An applicant's scope is not on the user row, it is the open
        # application for their phone — so it has to be looked up again here.
        # Without this, rotating the refresh cookie returned a token with the
        # applicant role and no application, and every route on the public
        # form answered 403: a parent who closed the browser could not get
        # back to their own half-filled form.
        application_id = None
        if user.role is Role.APPLICANT and user.phone:
            open_application = await self._admissions.by_phone(user.phone, open_only=True)
            application_id = open_application.id if open_application else None

        return Principal(
            user_id=user.id,
            school_id=user.school_id,
            role=user.role,
            student_id=user.student_id,
            guardian_id=user.guardian_id,
            application_id=application_id,
        )

    async def _open_session(
        self, *, user: AppUser, context: RequestContext, now: datetime
    ) -> Session:
        principal = await self._principal_for(user)
        access = issue_access_token(principal)

        refresh = generate_token()
        refresh_expires_at = now + timedelta(seconds=settings.REFRESH_TOKEN_TTL_SECONDS)
        await self._users.store_refresh_token(
            user_id=user.id,
            school_id=user.school_id,
            token_hash=hash_token(refresh),
            expires_at=refresh_expires_at,
            user_agent=context.user_agent,
            ip=context.ip,
        )

        return Session(
            access_token=access.token,
            access_expires_at=access.expires_at,
            refresh_token=refresh,
            refresh_expires_at=refresh_expires_at,
            principal=principal,
            full_name=user.full_name,
            must_change_password=user.must_change_password,
        )
