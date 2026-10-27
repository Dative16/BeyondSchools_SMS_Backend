"""Sign-in rules. Pure.

Three decisions live here, and each one is the kind that is expensive to get
wrong and cheap to test exhaustively: whether a password attempt is allowed,
whether a one-time code is accepted, and what to do when a refresh token is
presented twice.

Nothing in this module reads a clock. `as_of` is always an argument, which is
what lets a test walk a lock-out to its expiry without sleeping, and what stops
a report run for last month quietly using today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
#: Named rather than written into the one place that checks it, because three
#: screens now tell somebody what it is and all three have to say the same
#: number as the server enforces.
MIN_PASSWORD_LENGTH = 10

#: Failed attempts before an account is locked. Five is low enough to make
#: password spraying useless and high enough that a parent mistyping on a
#: phone keyboard still gets in.
MAX_FAILED_LOGINS = 5

#: How long a lock-out lasts. A fixed window, not an escalating one: a school
#: has no helpdesk, and an hour-long lock on a bursar at 8 a.m. on invoice day
#: costs more than it protects.
LOCKOUT_DURATION = timedelta(minutes=15)


#: Warn only from here. Telling every failed attempt how many are left hands
#: somebody spraying passwords a progress bar; saying nothing until the last
#: two leaves a real person, mistyping on a phone keyboard, about to be locked
#: out with no warning at all. Two is where the warning is worth more to them
#: than to an attacker who has already had three goes.
WARN_FROM_ATTEMPTS_LEFT = 2


def attempts_left(failed_logins: int) -> int:
    """How many tries remain before the lock-out."""
    return max(MAX_FAILED_LOGINS - failed_logins, 0)


def lockout_warning(failed_logins: int) -> str | None:
    """The sentence to add to a rejection, or nothing.

    Nothing is the usual answer. A message that counts down from five is a
    message that tells a password sprayer exactly how far it has got.
    """
    left = attempts_left(failed_logins)
    if left <= 0 or left > WARN_FROM_ATTEMPTS_LEFT:
        return None
    minutes = int(LOCKOUT_DURATION.total_seconds() // 60)
    tries = "one more attempt" if left == 1 else f"{left} more attempts"
    return f"{tries} and this account locks for {minutes} minutes"


class LoginOutcome(StrEnum):
    ALLOWED = "allowed"
    #: The credentials were wrong. Never distinguished in a response from
    #: "no such user": which accounts exist is not public information.
    REJECTED = "rejected"
    LOCKED = "locked"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class UserFacts:
    """What the rules need to know about the account being signed into."""

    user_id: UUID
    is_active: bool
    failed_logins: int
    locked_until: datetime | None
    has_password: bool


@dataclass(frozen=True, slots=True)
class LoginDecision:
    outcome: LoginOutcome
    #: What `failed_logins` should become. Zero on success, so a near-miss
    #: history does not follow someone around forever.
    failed_logins: int
    locked_until: datetime | None
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome is LoginOutcome.ALLOWED


def evaluate_login(
    *, user: UserFacts | None, password_matches: bool, as_of: datetime
) -> LoginDecision:
    """Decide whether this attempt gets in, and what the account looks like after.

    The ordering matters. An unknown user and a wrong password produce the same
    outcome, and a locked account is reported as locked rather than rejected
    only to the account's own owner — the API layer collapses both into one
    message. What this function must never do is let a lock-out be bypassed by
    a correct password, which is why the lock is checked before the match.
    """
    if user is None:
        return LoginDecision(LoginOutcome.REJECTED, 0, None, "no such user")

    if not user.is_active:
        return LoginDecision(
            LoginOutcome.INACTIVE, user.failed_logins, user.locked_until, "account disabled"
        )

    if user.locked_until is not None and user.locked_until > as_of:
        return LoginDecision(
            LoginOutcome.LOCKED,
            user.failed_logins,
            user.locked_until,
            "too many failed attempts",
        )

    if not user.has_password:
        # A guardian who only ever signs in by one-time code. Not an error and
        # not a way in: they are sent to the OTP route instead.
        return LoginDecision(LoginOutcome.REJECTED, user.failed_logins, None, "no password set")

    if password_matches:
        return LoginDecision(LoginOutcome.ALLOWED, 0, None, "password matched")

    # A lock-out that began in the past is spent, so the count restarts rather
    # than carrying a stale tally into the next window.
    tally = user.failed_logins + 1 if user.locked_until is None else 1
    if tally >= MAX_FAILED_LOGINS:
        return LoginDecision(
            LoginOutcome.LOCKED,
            tally,
            as_of + LOCKOUT_DURATION,
            f"{tally} consecutive failures",
        )
    return LoginDecision(LoginOutcome.REJECTED, tally, None, "password did not match")


# --------------------------------------------------------------------------
# One-time codes
# --------------------------------------------------------------------------
#: A code lives five minutes. Long enough for an SMS to arrive on a congested
#: network, short enough that a message left on a shared phone goes stale.
OTP_TTL = timedelta(minutes=5)

#: Except a password reset, which is a longer errand: read the code, choose a
#: password, type it twice, on a phone, possibly after walking somewhere with
#: signal. Five minutes is the window in which people give up and ring the
#: office instead.
PASSWORD_RESET_TTL = timedelta(minutes=30)

#: Guesses allowed per code. Six digits is a million possibilities, so the
#: attempt counter is what makes the code safe, not its length.
MAX_OTP_ATTEMPTS = 5

#: Codes per phone per hour, across every purpose. Caps both the nuisance of
#: an SMS flood sent at somebody's number and the bill for sending it.
MAX_OTP_SENDS_PER_HOUR = 5

OTP_RESEND_INTERVAL = timedelta(seconds=60)


class OtpPurpose(StrEnum):
    """Why a code was sent.

    Lives here rather than on the model because every layer names it — the
    route, the service and the table — and the endpoints are not allowed to
    import `app.models` at all.
    """

    APPLY = "apply"
    LOGIN = "login"
    PASSWORD_RESET = "password_reset"  # noqa: S105 - an enum member, not a secret
    PHONE_CHANGE = "phone_change"


def otp_ttl(purpose: OtpPurpose) -> timedelta:
    """How long a code of this kind lives."""
    return PASSWORD_RESET_TTL if purpose is OtpPurpose.PASSWORD_RESET else OTP_TTL


class OtpOutcome(StrEnum):
    ACCEPTED = "accepted"
    MISMATCH = "mismatch"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"
    ALREADY_USED = "already_used"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class OtpFacts:
    challenge_id: UUID
    expires_at: datetime
    attempts: int
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class OtpDecision:
    outcome: OtpOutcome
    attempts: int
    reason: str

    @property
    def accepted(self) -> bool:
        return self.outcome is OtpOutcome.ACCEPTED


def evaluate_otp(
    *, challenge: OtpFacts | None, code_matches: bool, as_of: datetime
) -> OtpDecision:
    """Decide whether a submitted code is accepted.

    Expiry and the attempt count are checked before the code itself: a code
    whose window has closed must be a dead end whether or not the digits are
    right, and an exhausted challenge must stop counting rather than let a
    sixth guess through.
    """
    if challenge is None:
        return OtpDecision(OtpOutcome.NOT_FOUND, 0, "no code outstanding for this number")

    if challenge.consumed_at is not None:
        return OtpDecision(OtpOutcome.ALREADY_USED, challenge.attempts, "code already used")

    if challenge.expires_at <= as_of:
        return OtpDecision(OtpOutcome.EXPIRED, challenge.attempts, "code expired")

    if challenge.attempts >= MAX_OTP_ATTEMPTS:
        return OtpDecision(
            OtpOutcome.EXHAUSTED, challenge.attempts, "too many incorrect attempts"
        )

    if code_matches:
        return OtpDecision(OtpOutcome.ACCEPTED, challenge.attempts + 1, "code matched")

    return OtpDecision(
        OtpOutcome.MISMATCH, challenge.attempts + 1, "code did not match"
    )


def may_send_otp(
    *, sent_in_last_hour: int, last_sent_at: datetime | None, as_of: datetime
) -> tuple[bool, str]:
    """Whether another code may be sent to this number now.

    Two limits rather than one: a minute between codes, so the "resend" button
    cannot be leant on, and a ceiling per hour, so a number cannot be used as
    a target. Both are counted per phone rather than per application, because
    the cost and the nuisance both land on the handset.
    """
    if last_sent_at is not None and as_of - last_sent_at < OTP_RESEND_INTERVAL:
        wait = OTP_RESEND_INTERVAL - (as_of - last_sent_at)
        return False, f"another code can be sent in {int(wait.total_seconds())}s"
    if sent_in_last_hour >= MAX_OTP_SENDS_PER_HOUR:
        return False, "too many codes sent to this number in the last hour"
    return True, "ok"


# --------------------------------------------------------------------------
# Refresh tokens
# --------------------------------------------------------------------------
class RefreshOutcome(StrEnum):
    ROTATE = "rotate"
    #: The token was already exchanged. That is evidence it was copied, so the
    #: whole family is revoked rather than just this row.
    REUSE_DETECTED = "reuse_detected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class RefreshFacts:
    token_id: UUID
    user_id: UUID
    #: The school this session belongs to. Carried so that the audit entry a
    #: refresh writes lands in the right school's trail — a row with no school
    #: is rejected outright by the policy on a session bound to one.
    school_id: UUID | None
    expires_at: datetime
    revoked_at: datetime | None
    replaced_by_id: UUID | None


@dataclass(frozen=True, slots=True)
class RefreshDecision:
    outcome: RefreshOutcome
    reason: str
    #: True when every live session for this user should be cut, not just one.
    revoke_family: bool = False

    @property
    def allowed(self) -> bool:
        return self.outcome is RefreshOutcome.ROTATE


def evaluate_refresh(*, token: RefreshFacts | None, as_of: datetime) -> RefreshDecision:
    """Decide what to do with a presented refresh token.

    Rotation makes each token single-use, which turns a stolen cookie from a
    fortnight of access into a race the thief has to win. The important case is
    the second one: a token presented after it has already been exchanged means
    two parties hold it, and the only safe response is to end every session for
    that user and make them sign in again.
    """
    if token is None:
        return RefreshDecision(RefreshOutcome.NOT_FOUND, "unknown refresh token")

    if token.replaced_by_id is not None:
        return RefreshDecision(
            RefreshOutcome.REUSE_DETECTED,
            "refresh token presented twice; it has been copied",
            revoke_family=True,
        )

    if token.revoked_at is not None:
        return RefreshDecision(RefreshOutcome.REVOKED, "refresh token was revoked")

    if token.expires_at <= as_of:
        return RefreshDecision(RefreshOutcome.EXPIRED, "refresh token expired")

    return RefreshDecision(RefreshOutcome.ROTATE, "ok")
