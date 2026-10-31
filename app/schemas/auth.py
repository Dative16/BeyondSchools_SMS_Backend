"""Auth request and response models.

These are the shapes the web app's `src/api/schema.d.ts` is generated from, so
the names here become the names there. Two conventions the whole API follows:
requests accept snake_case, and nothing that could identify a person appears in
a response that an unauthenticated caller can reach.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.admissions import normalise_phone
from app.domain.identity import MIN_PASSWORD_LENGTH
from app.domain.roles import Permission, Role


class LoginRequest(BaseModel):
    #: Email or phone, whichever the user thinks of as their login.
    identifier: str = Field(min_length=3, max_length=160)
    password: str = Field(min_length=1, max_length=256)
    #: Unticked means a shared handset, which in most Tanzanian households is
    #: the ordinary case: the session lasts a working day instead of a
    #: fortnight. Defaults to true so an existing caller is unaffected.
    remember: bool = True


class OtpStartRequest(BaseModel):
    phone: str = Field(min_length=9, max_length=20)
    purpose: Literal["login", "apply", "password_reset"] = "login"

    @field_validator("phone")
    @classmethod
    def normalise(cls, value: str) -> str:
        """Stored and compared as 0754118902, however it was typed.

        Parents write +255754118902, 255754118902 and 0754118902 for the same
        handset, and a rate limit that compares raw strings does not limit
        anything.
        """
        phone = normalise_phone(value)
        if len(phone) != 10 or not phone.startswith("0"):
            raise ValueError("that does not look like a Tanzanian mobile number")
        return phone


class OtpLoginRequest(OtpStartRequest):
    code: str = Field(min_length=4, max_length=8)


class OtpSentResponse(BaseModel):
    """What a code request answers.

    Never says whether the number is known to the school: the response is the
    same for a guardian's phone and a stranger's, because the difference is
    exactly what an attacker would be probing for.
    """

    sent: Literal[True] = True
    expires_at: datetime
    resend_after_seconds: int


class PasswordResetRequest(BaseModel):
    """Set a password by proving the phone, not by knowing the old one.

    Serves both screens that need it: somebody who has forgotten a password
    cannot supply the current one, and somebody setting their first never had
    one. The code can only have gone to the number already on the record.
    """

    phone: str = Field(min_length=9, max_length=20)
    code: str = Field(min_length=4, max_length=8)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class PrincipalResponse(BaseModel):
    """Who the caller is, as the staff app's shell needs them.

    Permissions are sent as a list rather than left for the client to derive
    from the role. The server owns the authorisation model; a client that
    recomputes it is a client that will eventually disagree with it, and the
    screens only use it to decide what to show.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    school_id: UUID | None
    full_name: str
    role: Role
    student_id: UUID | None = None
    guardian_id: UUID | None = None
    permissions: list[Permission]
    must_change_password: bool = False
    #: Only on `/auth/me`, and only so the portal can show a parent the number
    #: they sign in with. Absent from a login response, which does not read it.
    phone: str | None = None


class TokenResponse(BaseModel):
    """The access token and who it belongs to.

    The refresh token is absent on purpose: it is set as an httpOnly cookie
    and never passes through JavaScript, so a cross-site script cannot read a
    fortnight of access out of the page.
    """

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - a scheme name, not a secret
    expires_in: int
    expires_at: datetime
    principal: PrincipalResponse


class SchoolBrandResponse(BaseModel):
    """What the login page and the public application form may know.

    This is served unauthenticated, so it holds branding and nothing else — no
    fee settings, no counts, nothing about a single child.
    """

    id: UUID
    name: str
    subdomain: str
    brand_color: str
    logo_url: str | None = None
    locale: str
    timezone: str


class HandoffRequest(BaseModel):
    """Which child is crossing over.

    Required for a guardian, who may have several, and ignored for a student,
    who can only ever be themselves.
    """

    student_id: UUID | None = None


class HandoffResponse(BaseModel):
    """The token that carries a student across to aibeyondschools.co.tz."""

    token: str
    expires_at: datetime
    expires_in: int
    #: Where to send the browser. Built by the server so the client cannot
    #: aim a signed token at an address of its choosing.
    redirect_url: str


class JwksResponse(BaseModel):
    keys: list[dict[str, object]]
