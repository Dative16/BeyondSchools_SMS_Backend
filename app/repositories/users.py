"""Identity persistence: users, sessions and one-time codes.

Every query the sign-in path needs, and nothing else. The protocol at the top
is what `app/services/auth.py` depends on, which is what lets the service's
tests hand in a fake and run without PostgreSQL.

One shape worth noticing: nothing here decides anything. `find_by_identifier`
returns facts, `evaluate_login` in `app/domain/identity.py` judges them, and
this module writes down whatever was judged. A repository that started
returning "is this login allowed" would put the rule in the one layer that
cannot be tested without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.identity import OtpFacts, OtpPurpose, RefreshFacts, UserFacts
from app.domain.roles import Role
from app.models.user import AppUser, OtpChallenge, RefreshToken


class UserRecord(Protocol):
    """What the service needs off a user row besides the rules' own facts."""

    id: UUID
    school_id: UUID | None
    full_name: str
    email: str | None
    phone: str | None
    role: Role
    password_hash: str | None
    student_id: UUID | None
    guardian_id: UUID | None
    must_change_password: bool


class UserRepository(Protocol):
    async def find_by_identifier(self, identifier: str) -> AppUser | None: ...

    async def find_by_id(self, user_id: UUID) -> AppUser | None: ...

    async def facts_for(self, user: AppUser) -> UserFacts: ...

    async def record_login_result(
        self, user_id: UUID, *, failed_logins: int, locked_until: datetime | None, succeeded: bool
    ) -> None: ...

    async def class_group_ids_for(self, user_id: UUID) -> frozenset[UUID]: ...

    async def store_refresh_token(
        self,
        *,
        user_id: UUID,
        school_id: UUID | None,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None,
        ip: str | None,
    ) -> UUID: ...

    async def find_refresh_token(self, token_hash: str) -> RefreshFacts | None: ...

    async def rotate_refresh_token(
        self,
        *,
        old_token_id: UUID,
        new_token_hash: str,
        expires_at: datetime,
        user_agent: str | None,
        ip: str | None,
    ) -> UUID: ...

    async def revoke_refresh_token(self, token_id: UUID, *, reason: str) -> None: ...

    async def revoke_all_for_user(self, user_id: UUID, *, reason: str) -> int: ...

    async def create_otp_challenge(
        self,
        *,
        school_id: UUID | None,
        phone: str,
        purpose: OtpPurpose,
        code_hash: str,
        expires_at: datetime,
        ip: str | None,
    ) -> UUID: ...

    async def latest_otp_challenge(
        self, *, school_id: UUID | None, phone: str, purpose: OtpPurpose
    ) -> tuple[OtpFacts, str] | None: ...

    async def otp_sends_since(
        self, *, phone: str, since: datetime
    ) -> tuple[int, datetime | None]: ...

    async def record_otp_attempt(
        self, challenge_id: UUID, *, attempts: int, consumed: bool
    ) -> None: ...

    async def current_level_code(self, student_id: UUID) -> str | None: ...
    async def add_applicant(self, *, school_id: UUID, phone: str) -> AppUser: ...
    async def promote_applicant_to_guardian(
        self, *, phone: str, guardian_id: UUID, full_name: str
    ) -> UUID | None: ...

    async def guardian_has_child(self, guardian_id: UUID, student_id: UUID) -> bool: ...


class SqlUserRepository:
    """SQLAlchemy implementation.

    Every method runs on the session the caller handed in, which is already
    bound to a school — so none of these queries filters by `school_id` and
    none of them needs to. The exceptions are the ones that run before a
    tenant is known, and they say so.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- users ------------------------------------------------------------
    async def find_by_identifier(self, identifier: str) -> AppUser | None:
        """Find a user by email or phone, whichever they typed.

        One lookup for both, because a school's staff think of themselves as
        having "a login", not an email login and a phone login. The identifier
        is matched case-insensitively on email and exactly on phone, which the
        caller has already normalised.
        """
        lowered = identifier.strip().lower()
        stmt = select(AppUser).where(
            (AppUser.email.is_not(None) & (AppUser.email.ilike(lowered)))
            | (AppUser.phone == identifier.strip())
        )
        return (await self._session.execute(stmt.limit(1))).scalar_one_or_none()

    async def find_by_id(self, user_id: UUID) -> AppUser | None:
        return await self._session.get(AppUser, user_id)

    async def facts_for(self, user: AppUser) -> UserFacts:
        return UserFacts(
            user_id=user.id,
            is_active=user.is_active,
            failed_logins=user.failed_logins,
            locked_until=user.locked_until,
            has_password=bool(user.password_hash),
        )

    async def record_login_result(
        self,
        user_id: UUID,
        *,
        failed_logins: int,
        locked_until: datetime | None,
        succeeded: bool,
    ) -> None:
        values: dict[str, object] = {
            "failed_logins": failed_logins,
            "locked_until": locked_until,
        }
        if succeeded:
            values["last_login_at"] = _now()
        await self._session.execute(
            update(AppUser).where(AppUser.id == user_id).values(**values)
        )

    async def class_group_ids_for(self, user_id: UUID) -> frozenset[UUID]:
        """The classes a teacher may see children in.

        Both ways a teacher is attached to a class: as its class teacher, and
        through a subject they are assigned to teach it. The union is what
        `app.domain.roles.may_see_student` checks against, and it is resolved
        once at sign-in rather than on every request.
        """
        rows = await self._session.execute(
            text(
                "SELECT id AS class_group_id FROM class_group WHERE class_teacher_id = :uid "
                "UNION "
                "SELECT class_group_id FROM teaching_assignment WHERE teacher_id = :uid"
            ),
            {"uid": user_id},
        )
        return frozenset(row.class_group_id for row in rows)

    # --- sessions ---------------------------------------------------------
    async def store_refresh_token(
        self,
        *,
        user_id: UUID,
        school_id: UUID | None,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None,
        ip: str | None,
    ) -> UUID:
        token = RefreshToken(
            user_id=user_id,
            school_id=school_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
        self._session.add(token)
        await self._session.flush()
        return token.id

    async def find_refresh_token(self, token_hash: str) -> RefreshFacts | None:
        """Looked up by hash, never by value: the value only ever exists in the
        caller's cookie."""
        row = (
            await self._session.execute(
                select(
                    RefreshToken.id,
                    RefreshToken.user_id,
                    RefreshToken.school_id,
                    RefreshToken.expires_at,
                    RefreshToken.revoked_at,
                    RefreshToken.replaced_by_id,
                ).where(RefreshToken.token_hash == token_hash)
            )
        ).one_or_none()
        if row is None:
            return None
        return RefreshFacts(
            token_id=row.id,
            user_id=row.user_id,
            school_id=row.school_id,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            replaced_by_id=row.replaced_by_id,
        )

    async def rotate_refresh_token(
        self,
        *,
        old_token_id: UUID,
        new_token_hash: str,
        expires_at: datetime,
        user_agent: str | None,
        ip: str | None,
    ) -> UUID:
        """Write the replacement and point the old row at it.

        The link is what makes reuse detectable: a token presented after it has
        been replaced is a token somebody else also has.
        """
        old = await self._session.get(RefreshToken, old_token_id)
        if old is None:  # pragma: no cover - the caller has just read it
            raise LookupError("refresh token vanished mid-rotation")

        replacement = RefreshToken(
            user_id=old.user_id,
            school_id=old.school_id,
            token_hash=new_token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
        self._session.add(replacement)
        await self._session.flush()

        old.replaced_by_id = replacement.id
        old.revoked_at = _now()
        old.revoked_reason = "rotated"
        await self._session.flush()
        return replacement.id

    async def revoke_refresh_token(self, token_id: UUID, *, reason: str) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == token_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now(), revoked_reason=reason)
        )

    async def revoke_all_for_user(self, user_id: UUID, *, reason: str) -> int:
        """Cut every live session for one person.

        Called on reuse detection and on sign-out-everywhere. Returns how many
        were cut, which is what the audit entry records.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=_now(), revoked_reason=reason)
            ),
        )
        return result.rowcount or 0

    # --- one-time codes ---------------------------------------------------
    async def create_otp_challenge(
        self,
        *,
        school_id: UUID | None,
        phone: str,
        purpose: OtpPurpose,
        code_hash: str,
        expires_at: datetime,
        ip: str | None,
    ) -> UUID:
        challenge = OtpChallenge(
            school_id=school_id,
            phone=phone,
            purpose=purpose,
            code_hash=code_hash,
            expires_at=expires_at,
            ip=ip,
        )
        self._session.add(challenge)
        await self._session.flush()
        return challenge.id

    async def latest_otp_challenge(
        self, *, school_id: UUID | None, phone: str, purpose: OtpPurpose
    ) -> tuple[OtpFacts, str] | None:
        """The most recent code for this number and purpose, with its hash.

        Only the newest one is considered. A parent who pressed resend has two
        codes in their inbox and will type the second; accepting the first
        would make "resend" a way to keep an expired code alive.
        """
        row = (
            await self._session.execute(
                select(
                    OtpChallenge.id,
                    OtpChallenge.expires_at,
                    OtpChallenge.attempts,
                    OtpChallenge.consumed_at,
                    OtpChallenge.code_hash,
                )
                .where(OtpChallenge.phone == phone, OtpChallenge.purpose == purpose)
                .order_by(OtpChallenge.created_at.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        facts = OtpFacts(
            challenge_id=row.id,
            expires_at=row.expires_at,
            attempts=row.attempts,
            consumed_at=row.consumed_at,
        )
        return facts, row.code_hash

    async def otp_sends_since(
        self, *, phone: str, since: datetime
    ) -> tuple[int, datetime | None]:
        """How many codes this number has had, and when the last one went.

        Counted across every purpose: the limit protects the handset, and the
        handset does not care why it is being messaged.
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT count(*) AS sends, max(created_at) AS last_sent "
                    "  FROM otp_challenge "
                    " WHERE phone = :phone AND created_at >= :since"
                ),
                {"phone": phone, "since": since},
            )
        ).one()
        return row.sends, row.last_sent

    async def guardian_has_child(self, guardian_id: UUID, student_id: UUID) -> bool:
        """Whether this guardian is linked to this child.

        The one authorisation question that cannot be answered from a token: a
        guardian's children are rows, not claims. Checked on every crossing to
        the AI platform, because a signed token aimed at somebody else's child
        would be the worst kind of bug in this system.
        """
        found = await self._session.scalar(
            text(
                "SELECT 1 FROM guardian_student "
                " WHERE guardian_id = :guardian_id AND student_id = :student_id"
            ),
            {"guardian_id": guardian_id, "student_id": student_id},
        )
        return found is not None

    async def current_level_code(self, student_id: UUID) -> str | None:
        """The level a child is currently enrolled in.

        Read through the active enrolment, never from anything on the student:
        a student does not carry a class. Sent with a handoff token so the AI
        platform serves Standard 4 content to a Standard 4 pupil without
        calling back.
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT l.code "
                    "  FROM enrolment e "
                    "  JOIN class_group cg ON cg.id = e.class_group_id "
                    "  JOIN level l ON l.id = cg.level_id "
                    " WHERE e.student_id = :student_id AND e.outcome = 'active' "
                    " LIMIT 1"
                ),
                {"student_id": student_id},
            )
        ).one_or_none()
        return row.code if row else None

    async def record_otp_attempt(
        self, challenge_id: UUID, *, attempts: int, consumed: bool
    ) -> None:
        values: dict[str, object] = {"attempts": attempts}
        if consumed:
            values["consumed_at"] = _now()
        await self._session.execute(
            update(OtpChallenge).where(OtpChallenge.id == challenge_id).values(**values)
        )

    async def add_applicant(self, *, school_id: UUID, phone: str) -> AppUser:
        """A login for somebody with no account and a verified phone number.

        No password: they sign in by one-time code, which is the only
        credential a parent filling in the public form has. The row exists
        because a session needs a user to hang off — and because at enrolment
        it is promoted to a guardian login rather than replaced, which is what
        makes "one password, never two" true for the family.
        """
        user = AppUser(
            school_id=school_id,
            full_name="Applicant",
            phone=phone,
            role=Role.APPLICANT,
            is_active=True,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def promote_applicant_to_guardian(
        self, *, phone: str, guardian_id: UUID, full_name: str
    ) -> UUID | None:
        """Turn the account that filled in the form into the family's login.

        The same phone, the same row, a wider role. A parent who applied in
        November and signs in to pay fees in January uses what they already
        have, and a school never has two accounts for one handset.
        """
        row = (
            await self._session.execute(
                select(AppUser).where(AppUser.phone == phone, AppUser.role == Role.APPLICANT)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.role = Role.GUARDIAN
        row.guardian_id = guardian_id
        row.full_name = full_name
        await self._session.flush()
        return row.id


def _now() -> datetime:
    return datetime.now(UTC)
