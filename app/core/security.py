"""Passwords and one-time codes.

Tokens live next to these in Phase 1's identity work; this module is kept to
the primitives, so that nothing above it has to know which algorithm is in use
or how a secret is compared.

Two choices worth stating:

**bcrypt, not passlib.** passlib is unmaintained and raises against bcrypt 5.x.
Calling bcrypt directly is less code, and the thing an abstraction would buy —
switching algorithms later — is already handled by bcrypt's hashes carrying
their own cost and salt.

**Long passphrases are pre-hashed.** bcrypt silently truncates at 72 bytes, so
"correct horse battery staple ..." and the same phrase with a different ending
would be the same password. SHA-256 first, base64 after, and the whole
passphrase counts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

import bcrypt

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
