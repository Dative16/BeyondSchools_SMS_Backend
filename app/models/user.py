"""Identity: who may sign in, and what they are currently holding.

This service is the OIDC issuer for both apps, so these three tables are the
root of trust for aibeyondschools.co.tz as well as for the staff app. Two
consequences show up in the schema:

* A refresh token is stored as a hash, never in the clear. A stolen database
  backup must not be a set of working sessions.
* A student's and a guardian's login is the same `app_user` row shape as a
  bursar's, pointed at a student or guardian record. That is what lets a child
  tap a homework link and land on the AI platform already signed in, with no
  second credential to forget over the holidays.

The table is `app_user` rather than `user`, which is reserved in PostgreSQL.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.roles import Role
from app.models.base import Base, Timestamped, UUIDPrimaryKey, pg_enum


class AppUser(UUIDPrimaryKey, Timestamped, Base):
    """A person who can sign in.

    `school_id` is nullable only for the platform admin, who belongs to no
    school; every other row carries one and is covered by the same policy as
    the rest of the schema.
    """

    __tablename__ = "app_user"
    __table_args__ = (
        # Email and phone are each unique within a school, where present. A
        # partial index rather than a plain unique constraint, because most
        # guardians have no email address and NULLs would otherwise collide in
        # some PostgreSQL configurations.
        Index(
            "uq_app_user_school_id_email",
            "school_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index(
            "uq_app_user_school_id_phone",
            "school_id",
            "phone",
            unique=True,
            postgresql_where=text("phone IS NOT NULL"),
        ),
    )

    school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("school.id", ondelete="CASCADE"), index=True
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(20))
    role: Mapped[Role] = mapped_column(pg_enum(Role, "user_role"), nullable=False)

    #: Null for a guardian or student who only ever signs in by OTP.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    staff_no: Mapped[str | None] = mapped_column(String(32))
    #: The seat this login speaks for. Exactly one is set for a student or a
    #: guardian, and neither for staff.
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True
    )
    guardian_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("guardian.id", ondelete="CASCADE"), index=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Consecutive failed sign-ins, reset on success. A lock-out is cheap here
    #: and expensive to retrofit after the first password-spraying attempt.
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The subject claim the AI platform knows this person by, written back on
    #: first crossing so the two accounts stay linked.
    ai_platform_user_ref: Mapped[str | None] = mapped_column(String(128))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AppUser {self.full_name} {self.role}>"


class RefreshToken(UUIDPrimaryKey, Base):
    """One row per live session, rotated on every refresh.

    Rotation means a refresh token is single-use: presenting one that has
    already been exchanged is evidence the token was copied, so the whole
    family is revoked rather than just that row. `replaced_by_id` is what makes
    the family walkable.
    """

    __tablename__ = "refresh_token"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_refresh_token_token_hash"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("school.id", ondelete="CASCADE"), index=True
    )
    #: SHA-256 of the token. The token itself exists only in the cookie.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_token.id", ondelete="SET NULL")
    )
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)


class OtpPurpose(StrEnum):
    APPLY = "apply"
    LOGIN = "login"
    PASSWORD_RESET = "password_reset"  # noqa: S105 - an enum member, not a secret
    PHONE_CHANGE = "phone_change"


class OtpChallenge(UUIDPrimaryKey, Base):
    """A one-time code sent by SMS.

    This is the front door for everybody who has no password: an applicant
    returning to a half-finished form, a guardian checking a balance, a parent
    opening a results link during the holidays. It is also the cheapest defence
    against junk applications, which is why the public form verifies a phone
    number before it collects anything else.
    """

    __tablename__ = "otp_challenge"
    __table_args__ = (
        Index("ix_otp_challenge_school_id_phone_purpose", "school_id", "phone", "purpose"),
    )

    school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("school.id", ondelete="CASCADE"), index=True
    )
    #: Normalised to 0754118902 by app.domain.admissions.normalise_phone, so
    #: that the same handset cannot hold three separate challenges.
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    purpose: Mapped[OtpPurpose] = mapped_column(pg_enum(OtpPurpose, "otp_purpose"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Counted so that a six-digit code cannot be brute-forced over SMS.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ip: Mapped[str | None] = mapped_column(INET)
