"""Passwords, codes and tokens.

These are the primitives both applications trust, so the tests are about the
properties rather than the implementation: that a long passphrase is not
truncated, that a token minted for one audience is useless against the other,
and that a tampered token does not validate.
"""

from uuid import uuid4

import pytest

from app.core.security import (
    AUDIENCE_AI_PLATFORM,
    TokenError,
    decode_access_token,
    discovery_document,
    generate_otp,
    generate_token,
    hash_otp,
    hash_password,
    hash_token,
    issue_access_token,
    issue_handoff_token,
    jwks,
    key_id,
    verify_otp,
    verify_password,
)
from app.domain.roles import Principal, Role


def principal(role: Role = Role.BURSAR, **overrides) -> Principal:
    defaults = {
        "user_id": uuid4(),
        "school_id": uuid4(),
        "role": role,
    }
    return Principal(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def test_a_password_verifies_against_its_own_hash() -> None:
    hashed = hash_password("Shule2027!")

    assert verify_password("Shule2027!", hashed)
    assert not verify_password("shule2027!", hashed)


def test_the_same_password_hashes_differently_each_time() -> None:
    """Salted, so two staff who choose the same password do not have matching
    rows in a stolen backup."""
    assert hash_password("Shule2027!") != hash_password("Shule2027!")


def test_a_long_passphrase_is_not_truncated() -> None:
    """bcrypt silently stops at 72 bytes. Without pre-hashing, these two would
    be the same password, which is the whole reason that step exists."""
    base = "correct horse battery staple " * 3
    first = f"{base}alpha"
    second = f"{base}omega"

    assert len(first) > 72
    assert not verify_password(second, hash_password(first))
    assert verify_password(first, hash_password(first))


def test_an_account_with_no_hash_cannot_be_signed_into() -> None:
    assert not verify_password("anything", None)
    assert not verify_password("anything", "")


def test_a_corrupt_hash_is_a_failure_not_a_crash() -> None:
    """A malformed row is a bug to investigate, not a 500 in front of a user,
    and certainly not a successful login."""
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_an_empty_password_is_refused_outright() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        hash_password("")


# --------------------------------------------------------------------------
# One-time codes
# --------------------------------------------------------------------------
def test_a_generated_code_is_six_digits() -> None:
    for _ in range(20):
        code = generate_otp()
        assert len(code) == 6
        assert code.isdigit(), "letters in an SMS code produce support calls"


def test_a_code_verifies_against_its_hash() -> None:
    code = generate_otp()

    assert verify_otp(code, hash_otp(code))
    assert not verify_otp("000000" if code != "000000" else "111111", hash_otp(code))


def test_tokens_are_stored_as_hashes() -> None:
    token = generate_token()

    assert hash_token(token) != token
    assert len(hash_token(token)) == 64
    assert hash_token(token) == hash_token(token)


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
def test_an_access_token_round_trips() -> None:
    who = principal(Role.BURSAR)

    issued = issue_access_token(who)
    back = decode_access_token(issued.token)

    assert back.user_id == who.user_id
    assert back.school_id == who.school_id
    assert back.role is Role.BURSAR


def test_a_student_token_carries_the_student_record() -> None:
    student_id = uuid4()
    who = principal(Role.STUDENT, student_id=student_id)

    back = decode_access_token(issue_access_token(who).token)

    assert back.student_id == student_id


def test_this_api_refuses_a_handoff_token() -> None:
    """The audience separation the whole handoff rests on. The AI platform must
    not accept an API token, and this API must not accept one minted for the AI
    platform — otherwise a two-minute crossing token becomes a key to the
    school's own data."""
    who = principal(Role.STUDENT, student_id=uuid4())
    handoff = issue_handoff_token(who, student_id=who.student_id, level_code="STD4")

    with pytest.raises(TokenError):
        decode_access_token(handoff.token)

    accepted = decode_access_token(handoff.token, audience=AUDIENCE_AI_PLATFORM)
    assert accepted.user_id == who.user_id


def test_the_ai_platform_refuses_an_api_token() -> None:
    who = principal(Role.STUDENT, student_id=uuid4())
    access = issue_access_token(who)

    with pytest.raises(TokenError):
        decode_access_token(access.token, audience=AUDIENCE_AI_PLATFORM)


def test_a_handoff_token_is_short_lived() -> None:
    who = principal(Role.GUARDIAN, guardian_id=uuid4())
    child = uuid4()

    handoff = issue_handoff_token(who, student_id=child, level_code="STD2")
    access = issue_access_token(who)

    assert handoff.expires_at < access.expires_at


def test_a_tampered_token_does_not_validate() -> None:
    issued = issue_access_token(principal())
    head, payload, signature = issued.token.split(".")

    for broken in (
        f"{head}.{payload}.{signature[:-4]}AAAA",
        f"{head}.{payload[:-4]}AAAA.{signature}",
        f"{head}.{payload}.",
    ):
        with pytest.raises(TokenError):
            decode_access_token(broken)


def test_nonsense_is_not_a_token() -> None:
    for rubbish in ("", "x", "a.b.c", "Bearer something"):
        with pytest.raises(TokenError):
            decode_access_token(rubbish)


def test_a_token_with_no_school_is_refused_unless_it_is_the_platform_admin() -> None:
    """Every ordinary token carries a tenant, because that claim is what every
    request scopes itself by."""
    with pytest.raises(TokenError):
        decode_access_token(issue_access_token(principal(school_id=None)).token)

    platform = principal(Role.PLATFORM_ADMIN, school_id=None)
    assert decode_access_token(issue_access_token(platform).token).school_id is None


# --------------------------------------------------------------------------
# Published keys
# --------------------------------------------------------------------------
def test_the_jwks_publishes_a_public_key_and_nothing_private() -> None:
    published = jwks()

    assert len(published["keys"]) == 1
    key = published["keys"][0]
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["kid"] == key_id()
    # The private exponent and the primes must never leave the service.
    for private_field in ("d", "p", "q", "dp", "dq", "qi"):
        assert private_field not in key


def test_the_key_id_is_stable_and_matches_the_tokens() -> None:
    import json
    from base64 import urlsafe_b64decode

    header_segment = issue_access_token(principal()).token.split(".")[0]
    padded = header_segment + "=" * (-len(header_segment) % 4)
    header = json.loads(urlsafe_b64decode(padded))

    assert header["kid"] == key_id()
    assert header["alg"] == "RS256"


def test_the_discovery_document_advertises_nothing_that_does_not_exist() -> None:
    """It is not an OpenID Provider and should not claim to be: a relying party
    that tries an authorisation-code flow against it would get nowhere."""
    doc = discovery_document()

    assert doc["jwks_uri"].endswith("/.well-known/jwks.json")
    assert doc["id_token_signing_alg_values_supported"] == ["RS256"]
    assert doc["grant_types_supported"] == []
    assert doc["response_types_supported"] == []
    assert "school_id" in doc["claims_supported"]
