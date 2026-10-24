"""Passwords, one-time codes, and the tokens both applications trust.

This module is the root of trust for the whole product. The school service
signs access tokens with RS256 and publishes the public half at a JWKS
endpoint; aibeyondschools.co.tz validates against that and never sees a
password. The one property everything else rests on is that a student can tap
a homework link and arrive already signed in — so the signing, the claims and
the key identifier all live in one file, where they can be read in one sitting.

Three choices worth stating:

**bcrypt, not passlib.** passlib is unmaintained and raises against bcrypt 5.x.
Calling bcrypt directly is less code, and the thing an abstraction would buy —
switching algorithms later — is already handled by bcrypt's hashes carrying
their own cost and salt.

**Long passphrases are pre-hashed.** bcrypt silently truncates at 72 bytes, so
"correct horse battery staple ..." and the same phrase with a different ending
would be the same password. SHA-256 first, base64 after, and the whole
passphrase counts.

**joserfc, not authlib.** `authlib.jose` is deprecated in favour of joserfc by
its own author. A deprecated module is a poor foundation for the thing that
decides who a request is.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import bcrypt
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, RSAKey

from app.core.config import settings
from app.domain.roles import Principal, Role

#: Cost factor. 12 is roughly a quarter of a second on the kind of droplet this
#: runs on — slow enough to make offline cracking expensive, fast enough that a
#: parent signing in on a phone does not notice.
BCRYPT_ROUNDS = 12

OTP_LENGTH = 6


def _prepare(password: str) -> bytes:
    """SHA-256 then base64, so no passphrase is silently truncated."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-time where it matters, and False rather than an exception when
    there is no hash to compare against.

    A user with no password — a guardian who only ever signs in by OTP — must
    not be a way in, and must not take a different amount of time to answer
    than a user with one.
    """
    if not password_hash:
        # Still do the work: an immediate False here would tell an attacker
        # which accounts have passwords set.
        bcrypt.hashpw(_prepare(password), bcrypt.gensalt(BCRYPT_ROUNDS))
        return False
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except ValueError:
        # A malformed hash in the database is a bug, not a successful login.
        return False


def generate_otp(length: int = OTP_LENGTH) -> str:
    """A numeric code, because it is read off a phone screen and typed back.

    Digits only and fixed length: letters in an SMS code produce support calls
    about whether the O is a zero.
    """
    return "".join(secrets.choice("0123456789") for _ in range(length))


def hash_otp(code: str) -> str:
    """OTPs are hashed too, with SHA-256 rather than bcrypt.

    They live for five minutes, they are six digits, and the whole keyspace
    can be walked in no time at all — so the hash is not what protects them.
    The attempt counter and the expiry are. Hashing them anyway keeps live
    codes out of a database backup, and SHA-256 keeps the verify fast enough
    to rate-limit rather than to be the rate limit.
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def verify_otp(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(code), code_hash)


def hash_token(token: str) -> str:
    """Refresh tokens are stored as a SHA-256 of the value in the cookie.

    A stolen database backup must not be a set of working sessions.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ==========================================================================
# Keys
# ==========================================================================
#: The audience of a token meant for this service's own API.
AUDIENCE_SMS = "beyondschools-sms"
#: The audience of a handoff token, which only the AI platform accepts.
AUDIENCE_AI_PLATFORM = "aibeyondschools"

#: How long a handoff token lives. Long enough to survive a redirect on a
#: weak connection, short enough that a link copied out of a browser history
#: is worthless by the time anyone finds it.
HANDOFF_TTL_SECONDS = 120


class TokenError(Exception):
    """A token that cannot be trusted. The reason is for the log, not the
    response: telling a caller *why* their token failed helps them forge the
    next one."""


@lru_cache
def _private_key() -> RSAKey:
    """The signing key, loaded once.

    Missing is a hard failure rather than a fallback. A service that silently
    signs with something else is worse than one that will not start.
    """
    path = Path(settings.JWT_PRIVATE_KEY_PATH)
    if not path.is_file():
        raise TokenError(
            f"no signing key at {path}. Run `make keys` locally, or mount the "
            "platform's secret in production."
        )
    return RSAKey.import_key(path.read_bytes())


@lru_cache
def _public_key() -> RSAKey:
    path = Path(settings.JWT_PUBLIC_KEY_PATH)
    if path.is_file():
        return RSAKey.import_key(path.read_bytes())
    # A service that holds the private key can always derive the public half;
    # this keeps a worker from needing both files mounted.
    return RSAKey.import_key(_private_key().as_pem(private=False))


def key_id() -> str:
    """The `kid` every token carries, and every JWKS entry is keyed by.

    An RFC 7638 thumbprint rather than a hand-written version string: it is
    derived from the key itself, so during a rotation the two keys cannot
    collide and nobody has to remember to bump a counter.
    """
    return str(_public_key().thumbprint())


def jwks() -> dict[str, list[dict[str, Any]]]:
    """What `/.well-known/jwks.json` serves.

    A set rather than a single key, because that is what makes rotation
    possible without downtime: publish the new key, wait for the old tokens to
    expire, then stop signing with the old one.
    """
    public = _public_key().as_dict(private=False)
    public["kid"] = key_id()
    public["use"] = "sig"
    public["alg"] = settings.JWT_ALGORITHM
    return {"keys": [public]}


def _key_set() -> KeySet:
    key = _public_key()
    key.ensure_kid()
    return KeySet([key])


# ==========================================================================
# Tokens
# ==========================================================================
@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    jti: UUID
    expires_at: datetime


def _claims(
    *,
    principal: Principal,
    audience: str,
    ttl_seconds: int,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], UUID, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    jti = uuid4()

    claims: dict[str, Any] = {
        "iss": settings.JWT_ISSUER,
        "sub": str(principal.user_id),
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(jti),
        # The tenant travels in the token. Every request resolves its school
        # from this claim, which is why a token from one school is useless
        # against another's data even if the route forgets to check.
        "school_id": str(principal.school_id) if principal.school_id else None,
        "role": principal.role.value,
    }
    if principal.student_id:
        claims["student_id"] = str(principal.student_id)
    if principal.guardian_id:
        claims["guardian_id"] = str(principal.guardian_id)
    if extra:
        claims.update(extra)
    return claims, jti, expires_at


def issue_access_token(principal: Principal) -> IssuedToken:
    """A short-lived token for this service's own API."""
    claims, jti, expires_at = _claims(
        principal=principal,
        audience=AUDIENCE_SMS,
        ttl_seconds=settings.ACCESS_TOKEN_TTL_SECONDS,
    )
    token = jwt.encode(
        {"alg": settings.JWT_ALGORITHM, "kid": key_id(), "typ": "JWT"},
        claims,
        _private_key(),
    )
    return IssuedToken(token, jti, expires_at)


def issue_handoff_token(
    principal: Principal,
    *,
    student_id: UUID,
    level_code: str | None = None,
) -> IssuedToken:
    """The token that carries one child across to aibeyondschools.co.tz.

    A different audience from the access token, deliberately: the AI platform
    must not accept a token minted for this API, and this API must not accept
    one minted for the AI platform.

    `student_id` is explicit rather than taken from the principal, because a
    guardian with three children has three possible answers and the caller has
    to say which. `level` rides along so the content served matches the class
    the child is actually in without a second call back.
    """
    extra: dict[str, Any] = {"student_id": str(student_id)}
    if level_code:
        extra["level"] = level_code
    claims, jti, expires_at = _claims(
        principal=principal,
        audience=AUDIENCE_AI_PLATFORM,
        ttl_seconds=HANDOFF_TTL_SECONDS,
        extra=extra,
    )
    token = jwt.encode(
        {"alg": settings.JWT_ALGORITHM, "kid": key_id(), "typ": "JWT"},
        claims,
        _private_key(),
    )
    return IssuedToken(token, jti, expires_at)


def decode_access_token(token: str, *, audience: str = AUDIENCE_SMS) -> Principal:
    """Validate a token and return who it speaks for.

    Everything that can be checked without a database is checked here:
    signature, algorithm, issuer, audience and expiry. What is deliberately
    *not* checked is whether the user still exists or is still active — that
    would be a query on every request, and the 15-minute lifetime is the
    answer instead. Anything that must take effect immediately (a dismissal, a
    stolen session) revokes the refresh token, and the access token is gone
    within the quarter hour.
    """
    try:
        decoded = jwt.decode(token, _key_set(), algorithms=[settings.JWT_ALGORITHM])
    except (JoseError, ValueError) as exc:
        raise TokenError(f"token did not validate: {exc}") from exc

    claims = decoded.claims
    registry = jwt.JWTClaimsRegistry(
        iss={"essential": True, "value": settings.JWT_ISSUER},
        aud={"essential": True, "value": audience},
        exp={"essential": True},
        sub={"essential": True},
    )
    try:
        registry.validate(claims)
    except JoseError as exc:
        raise TokenError(f"claims rejected: {exc}") from exc

    try:
        role = Role(claims["role"])
    except (KeyError, ValueError) as exc:
        raise TokenError("token carries no usable role") from exc

    school_id = claims.get("school_id")
    if school_id is None and role is not Role.PLATFORM_ADMIN:
        raise TokenError("token carries no school")

    def as_uuid(value: Any) -> UUID | None:
        return UUID(value) if value else None

    try:
        return Principal(
            user_id=UUID(claims["sub"]),
            school_id=as_uuid(school_id),
            role=role,
            student_id=as_uuid(claims.get("student_id")),
            guardian_id=as_uuid(claims.get("guardian_id")),
        )
    except ValueError as exc:
        raise TokenError("token carries a malformed id") from exc


def discovery_document() -> dict[str, Any]:
    """`/.well-known/openid-configuration`.

    Not a full OpenID Provider: there is no authorisation-code flow here, and
    this service is not trying to be an identity broker. It publishes what the
    AI platform needs to *validate* a token it was handed — the issuer, the
    key set and the algorithm — and says so plainly rather than advertising
    endpoints that do not exist.
    """
    issuer = settings.JWT_ISSUER.rstrip("/")
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "id_token_signing_alg_values_supported": [settings.JWT_ALGORITHM],
        "subject_types_supported": ["public"],
        "claims_supported": [
            "iss",
            "sub",
            "aud",
            "exp",
            "iat",
            "jti",
            "school_id",
            "role",
            "student_id",
            "guardian_id",
            "level",
        ],
        "response_types_supported": [],
        "grant_types_supported": [],
    }
