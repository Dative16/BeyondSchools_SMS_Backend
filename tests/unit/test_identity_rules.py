"""The sign-in rules, tested without a database or a clock.

Every case here is one somebody will eventually hit: a lock-out that has to
hold against a correct password, a one-time code that must not work twice, a
refresh token presented by two parties at once. They are cheap to pin down
because `app/domain/identity.py` takes facts and a date and returns a decision.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.identity import (
    LOCKOUT_DURATION,
    MAX_FAILED_LOGINS,
    MAX_OTP_ATTEMPTS,
    MAX_OTP_SENDS_PER_HOUR,
    LoginOutcome,
    OtpFacts,
    OtpOutcome,
    RefreshFacts,
    RefreshOutcome,
    UserFacts,
    evaluate_login,
    evaluate_otp,
    evaluate_refresh,
    may_send_otp,
)

NOW = datetime(2027, 1, 20, 7, 30, tzinfo=UTC)


def user(**overrides) -> UserFacts:
    defaults = {
        "user_id": uuid4(),
        "is_active": True,
        "failed_logins": 0,
        "locked_until": None,
        "has_password": True,
    }
    return UserFacts(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def test_a_correct_password_is_allowed_and_clears_the_tally() -> None:
    decision = evaluate_login(
        user=user(failed_logins=3), password_matches=True, as_of=NOW
    )

    assert decision.allowed
    assert decision.failed_logins == 0, "a near-miss history must not follow someone around"
    assert decision.locked_until is None


def test_an_unknown_user_and_a_wrong_password_look_identical() -> None:
    """Which accounts exist at a school is not public information."""
    unknown = evaluate_login(user=None, password_matches=False, as_of=NOW)
    wrong = evaluate_login(user=user(), password_matches=False, as_of=NOW)

    assert unknown.outcome is LoginOutcome.REJECTED
    assert wrong.outcome is LoginOutcome.REJECTED


def test_failures_accumulate_until_the_account_locks() -> None:
    facts = user()
    outcomes = []
    for _ in range(MAX_FAILED_LOGINS):
        decision = evaluate_login(user=facts, password_matches=False, as_of=NOW)
        outcomes.append(decision.outcome)
        facts = user(failed_logins=decision.failed_logins, locked_until=decision.locked_until)

    assert outcomes[:-1] == [LoginOutcome.REJECTED] * (MAX_FAILED_LOGINS - 1)
    assert outcomes[-1] is LoginOutcome.LOCKED
    assert facts.locked_until == NOW + LOCKOUT_DURATION


def test_a_lock_holds_even_against_the_right_password() -> None:
    """The case that makes the lock worth having. If a correct password got in
    during a lock-out, the lock would only slow down an attacker who was
    guessing wrong."""
    locked = user(failed_logins=MAX_FAILED_LOGINS, locked_until=NOW + timedelta(minutes=5))

    decision = evaluate_login(user=locked, password_matches=True, as_of=NOW)

    assert decision.outcome is LoginOutcome.LOCKED


def test_a_spent_lock_lets_the_right_password_through() -> None:
    expired = user(failed_logins=MAX_FAILED_LOGINS, locked_until=NOW - timedelta(seconds=1))

    decision = evaluate_login(user=expired, password_matches=True, as_of=NOW)

    assert decision.allowed


def test_a_spent_lock_restarts_the_tally_rather_than_locking_immediately() -> None:
    """Otherwise one wrong attempt after a lock-out expires would re-lock the
    account, and a parent who mistypes once would be out for the day."""
    expired = user(failed_logins=MAX_FAILED_LOGINS, locked_until=NOW - timedelta(seconds=1))

    decision = evaluate_login(user=expired, password_matches=False, as_of=NOW)

    assert decision.outcome is LoginOutcome.REJECTED
    assert decision.failed_logins == 1


def test_a_disabled_account_is_not_merely_rejected() -> None:
    """A school that has disabled somebody wants them told, not left guessing
    that they mistyped."""
    decision = evaluate_login(
        user=user(is_active=False), password_matches=True, as_of=NOW
    )

    assert decision.outcome is LoginOutcome.INACTIVE


def test_an_account_with_no_password_cannot_be_signed_into_with_one() -> None:
    """A guardian who only uses one-time codes. Not an error, and not a way in
    — an empty password must not match an empty hash."""
    decision = evaluate_login(
        user=user(has_password=False), password_matches=True, as_of=NOW
    )

    assert decision.outcome is LoginOutcome.REJECTED


# --------------------------------------------------------------------------
# One-time codes
# --------------------------------------------------------------------------
def challenge(**overrides) -> OtpFacts:
    defaults = {
        "challenge_id": uuid4(),
        "expires_at": NOW + timedelta(minutes=5),
        "attempts": 0,
        "consumed_at": None,
    }
    return OtpFacts(**{**defaults, **overrides})


def test_the_right_code_is_accepted_and_counted() -> None:
    decision = evaluate_otp(challenge=challenge(), code_matches=True, as_of=NOW)

    assert decision.accepted
    assert decision.attempts == 1


def test_a_wrong_code_counts_against_the_attempt_limit() -> None:
    decision = evaluate_otp(challenge=challenge(attempts=2), code_matches=False, as_of=NOW)

    assert decision.outcome is OtpOutcome.MISMATCH
    assert decision.attempts == 3


def test_an_expired_code_fails_even_when_the_digits_are_right() -> None:
    expired = challenge(expires_at=NOW - timedelta(seconds=1))

    decision = evaluate_otp(challenge=expired, code_matches=True, as_of=NOW)

    assert decision.outcome is OtpOutcome.EXPIRED


def test_a_used_code_cannot_be_used_again() -> None:
    """A six-digit code left in an inbox must not be a second way in."""
    used = challenge(consumed_at=NOW - timedelta(minutes=1))

    decision = evaluate_otp(challenge=used, code_matches=True, as_of=NOW)

    assert decision.outcome is OtpOutcome.ALREADY_USED


def test_guesses_run_out() -> None:
    """Six digits is a million possibilities, so the attempt counter is what
    makes the code safe rather than its length."""
    exhausted = challenge(attempts=MAX_OTP_ATTEMPTS)

    decision = evaluate_otp(challenge=exhausted, code_matches=True, as_of=NOW)

    assert decision.outcome is OtpOutcome.EXHAUSTED


def test_no_outstanding_code_is_not_an_accepted_code() -> None:
    decision = evaluate_otp(challenge=None, code_matches=True, as_of=NOW)

    assert decision.outcome is OtpOutcome.NOT_FOUND


def test_resend_is_refused_within_the_minute() -> None:
    allowed, reason = may_send_otp(
        sent_in_last_hour=1, last_sent_at=NOW - timedelta(seconds=20), as_of=NOW
    )

    assert not allowed
    assert "40s" in reason


def test_resend_is_allowed_after_the_minute() -> None:
    allowed, _ = may_send_otp(
        sent_in_last_hour=1, last_sent_at=NOW - timedelta(seconds=61), as_of=NOW
    )

    assert allowed


def test_a_number_cannot_be_used_as_a_target() -> None:
    allowed, reason = may_send_otp(
        sent_in_last_hour=MAX_OTP_SENDS_PER_HOUR,
        last_sent_at=NOW - timedelta(minutes=5),
        as_of=NOW,
    )

    assert not allowed
    assert "last hour" in reason


# --------------------------------------------------------------------------
# Refresh tokens
# --------------------------------------------------------------------------
def token(**overrides) -> RefreshFacts:
    defaults = {
        "token_id": uuid4(),
        "user_id": uuid4(),
        "school_id": uuid4(),
        "expires_at": NOW + timedelta(days=7),
        "revoked_at": None,
        "replaced_by_id": None,
    }
    return RefreshFacts(**{**defaults, **overrides})


def test_a_live_token_rotates() -> None:
    decision = evaluate_refresh(token=token(), as_of=NOW)

    assert decision.allowed
    assert not decision.revoke_family


def test_a_token_presented_twice_cuts_every_session() -> None:
    """The important case. A token that has already been exchanged means two
    parties hold it, and the only safe answer is to end every session for that
    user rather than the one row."""
    replaced = token(replaced_by_id=uuid4())

    decision = evaluate_refresh(token=replaced, as_of=NOW)

    assert decision.outcome is RefreshOutcome.REUSE_DETECTED
    assert decision.revoke_family


def test_a_revoked_token_is_refused_without_cutting_the_rest() -> None:
    decision = evaluate_refresh(token=token(revoked_at=NOW), as_of=NOW)

    assert decision.outcome is RefreshOutcome.REVOKED
    assert not decision.revoke_family


def test_an_expired_token_is_refused() -> None:
    decision = evaluate_refresh(token=token(expires_at=NOW), as_of=NOW)

    assert decision.outcome is RefreshOutcome.EXPIRED


def test_an_unknown_token_is_refused() -> None:
    decision = evaluate_refresh(token=None, as_of=NOW)

    assert decision.outcome is RefreshOutcome.NOT_FOUND
