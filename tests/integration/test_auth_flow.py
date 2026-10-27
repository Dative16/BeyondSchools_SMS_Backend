"""Sign-in, end to end, through the real app and a real PostgreSQL.

These go through HTTP rather than calling the service directly, because the
things most likely to be wrong live at the edges: the refresh cookie's flags,
which session a route is scoped by, and whether a 401 leaks the difference
between a wrong password and an account that does not exist.

The app's own engine connects as the restricted role, so every policy in the
schema applies to everything these tests do.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.security import hash_password
from app.domain.identity import MAX_FAILED_LOGINS
from app.main import app
from tests.conftest import (
    make_academic_year,
    make_class_group,
    make_enrolment,
    make_level,
    make_school,
    make_student,
)

PASSWORD = "Shule2027!"  # noqa: S105 - test fixture data


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


async def _add_user(
    session,
    *,
    school_id,
    full_name: str,
    role: str,
    email: str | None = None,
    phone: str | None = None,
    password: str | None = PASSWORD,
    guardian_id=None,
    student_id=None,
):
    user_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO app_user (id, school_id, full_name, email, phone, role, "
            " password_hash, must_change_password, is_active, failed_logins, "
            " guardian_id, student_id, created_at, updated_at) "
            "VALUES (:id, :school_id, :name, :email, :phone, :role, :hash, false, "
            " true, 0, :guardian_id, :student_id, now(), now())"
        ),
        {
            "id": user_id,
            "school_id": school_id,
            "name": full_name,
            "email": email,
            "phone": phone,
            "role": role,
            "hash": hash_password(password) if password else None,
            "guardian_id": guardian_id,
            "student_id": student_id,
        },
    )
    return user_id


@pytest.fixture
async def school(owner_session):
    """Two schools, so that "can a user sign in through another school's
    subdomain" is a question the tests can actually ask."""
    suffix = uuid4().hex[:6]
    school_id = await make_school(owner_session, name="Mwenge Primary", subdomain=f"auth{suffix}")
    other_id = await make_school(owner_session, name="Tumaini Primary", subdomain=f"other{suffix}")

    subdomain = await owner_session.scalar(
        text("SELECT subdomain FROM school WHERE id = :id"), {"id": school_id}
    )
    other_subdomain = await owner_session.scalar(
        text("SELECT subdomain FROM school WHERE id = :id"), {"id": other_id}
    )

    bursar_email = f"bursar-{suffix}@mwenge.ac.tz"
    await _add_user(
        owner_session,
        school_id=school_id,
        full_name="Salome Kessy",
        role="bursar",
        email=bursar_email,
    )

    # A guardian with one child, for the OTP and handoff paths.
    guardian_id = uuid4()
    phone = f"0755{suffix[:6].translate(str.maketrans('abcdef', '123456'))}"
    await owner_session.execute(
        text(
            "INSERT INTO guardian (id, school_id, full_name, phone, locale, "
            " sms_opted_out, created_at, updated_at) "
            "VALUES (:id, :school_id, 'Asha Mwanjala', :phone, 'en', false, now(), now())"
        ),
        {"id": guardian_id, "school_id": school_id, "phone": phone},
    )
    await _add_user(
        owner_session,
        school_id=school_id,
        full_name="Asha Mwanjala",
        role="guardian",
        phone=phone,
        password=None,
        guardian_id=guardian_id,
    )

    year_id = await make_academic_year(owner_session, school_id=school_id)
    level_id = await make_level(owner_session, school_id=school_id)
    group_id = await make_class_group(
        owner_session, school_id=school_id, academic_year_id=year_id, level_id=level_id
    )
    student_id = await make_student(
        owner_session, school_id=school_id, admission_no=f"A/{suffix}"
    )
    await make_enrolment(
        owner_session,
        school_id=school_id,
        student_id=student_id,
        academic_year_id=year_id,
        class_group_id=group_id,
    )
    await owner_session.execute(
        text(
            "INSERT INTO guardian_student (id, school_id, guardian_id, student_id, "
            " relationship, is_primary, receives_invoices, receives_sms, may_collect, "
            " created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, :guardian, :student, 'mother', "
            " true, true, true, true, now(), now())"
        ),
        {"school_id": school_id, "guardian": guardian_id, "student": student_id},
    )
    await owner_session.commit()

    yield {
        "id": school_id,
        "subdomain": subdomain,
        "other_subdomain": other_subdomain,
        "bursar_email": bursar_email,
        "guardian_phone": phone,
        "student_id": student_id,
    }

    await owner_session.rollback()
    await owner_session.execute(
        text("DELETE FROM school WHERE id = ANY(:ids)"), {"ids": [school_id, other_id]}
    )
    await owner_session.commit()


def headers(subdomain: str, *, refresh: str | None = None) -> dict[str, str]:
    """The school a request is for, and optionally a refresh cookie.

    Sent as a header rather than through httpx's per-request `cookies=`, which
    is deprecated: the point of these tests is often to replay a *specific*
    old cookie, not whatever the client happens to be holding.
    """
    sent = {"X-School-Subdomain": subdomain}
    if refresh is not None:
        sent["Cookie"] = f"bs_refresh={refresh}"
    return sent


# --------------------------------------------------------------------------
# Branding, before anybody has signed in
# --------------------------------------------------------------------------
async def test_the_login_page_can_read_branding_without_a_token(client, school) -> None:
    response = await client.get("/api/v1/auth/school", headers=headers(school["subdomain"]))

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Mwenge Primary"
    # Nothing about money or children may appear here.
    assert set(body) == {
        "id",
        "name",
        "subdomain",
        "brand_color",
        "logo_url",
        "locale",
        "timezone",
    }


async def test_an_unknown_school_is_a_404_either_way(client) -> None:
    response = await client.get("/api/v1/auth/school", headers=headers("no-such-school"))

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
async def test_a_bursar_signs_in_and_gets_their_permissions(client, school) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["principal"]["role"] == "bursar"
    assert "payment:record_manual" in body["principal"]["permissions"]
    # The bursar sees money and nothing else: marks are none of their business.
    assert "mark:enter" not in body["principal"]["permissions"]


async def test_the_refresh_cookie_is_httponly_and_scoped(client, school) -> None:
    """A refresh token readable by JavaScript is a fortnight of access one
    cross-site script away."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )

    cookie = response.headers["set-cookie"]
    assert "bs_refresh=" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/api/v1/auth" in cookie
    assert "SameSite=lax" in cookie.lower().replace("samesite=lax", "SameSite=lax")


async def test_a_wrong_password_and_an_unknown_user_answer_the_same(client, school) -> None:
    wrong = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": "not-it"},
        headers=headers(school["subdomain"]),
    )
    unknown = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "nobody@mwenge.ac.tz", "password": "not-it"},
        headers=headers(school["subdomain"]),
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


async def test_a_user_cannot_sign_in_through_another_schools_subdomain(
    client, school
) -> None:
    """The tenancy guarantee, at the front door. The lookup runs with no
    `school_id` filter, exactly like every other query here — the only thing
    stopping it is that the session was pinned to the other school first."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["other_subdomain"]),
    )

    assert response.status_code == 401


async def test_repeated_failures_lock_the_account(client, school) -> None:
    for _ in range(MAX_FAILED_LOGINS):
        await client.post(
            "/api/v1/auth/login",
            json={"identifier": school["bursar_email"], "password": "not-it"},
            headers=headers(school["subdomain"]),
        )

    # Now even the right password is refused, and with a status that tells the
    # client to stop retrying.
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )

    assert response.status_code == 423


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
async def test_me_is_scoped_by_the_token_not_the_subdomain(client, school) -> None:
    """A client that is not on a school's subdomain — the staff app in local
    development, or a native app — still has to work."""
    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )
    token = login.json()["access_token"]

    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Salome Kessy"


async def test_no_token_is_a_401_with_a_challenge(client) -> None:
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


async def test_refreshing_rotates_the_token_and_reuse_cuts_every_session(
    client, school
) -> None:
    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )
    first = login.cookies["bs_refresh"]

    rotated = await client.post(
        "/api/v1/auth/refresh",
        headers=headers(school["subdomain"], refresh=first),
    )
    assert rotated.status_code == 200
    second = rotated.cookies["bs_refresh"]
    assert second != first, "a refresh token is single use"

    replayed = await client.post(
        "/api/v1/auth/refresh",
        headers=headers(school["subdomain"], refresh=first),
    )
    assert replayed.status_code == 401

    # The replacement is dead too: two parties held the first one, so the whole
    # family goes.
    after = await client.post(
        "/api/v1/auth/refresh",
        headers=headers(school["subdomain"], refresh=second),
    )
    assert after.status_code == 401


async def test_signing_out_twice_is_not_an_error(client, school) -> None:
    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )
    cookie = login.cookies["bs_refresh"]

    first = await client.post(
        "/api/v1/auth/logout",
        headers=headers(school["subdomain"], refresh=cookie),
    )
    second = await client.post(
        "/api/v1/auth/logout",
        headers=headers(school["subdomain"], refresh=cookie),
    )

    assert first.status_code == second.status_code == 204


# --------------------------------------------------------------------------
# One-time codes and the handoff
# --------------------------------------------------------------------------
@pytest.fixture
def fixed_code(monkeypatch) -> str:
    """Pin the generated code so a test can type it back.

    Patched where the service looks it up rather than where it is defined,
    which is the only place that matters.
    """
    code = "424242"
    monkeypatch.setattr("app.services.auth.generate_otp", lambda *a, **k: code)
    return code


async def test_a_guardian_signs_in_with_a_code(client, school, fixed_code) -> None:
    sent = await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": school["guardian_phone"], "purpose": "login"},
        headers=headers(school["subdomain"]),
    )
    assert sent.status_code == 200
    assert sent.json()["sent"] is True

    response = await client.post(
        "/api/v1/auth/otp/login",
        json={
            "phone": school["guardian_phone"],
            "purpose": "login",
            "code": fixed_code,
        },
        headers=headers(school["subdomain"]),
    )

    assert response.status_code == 200
    assert response.json()["principal"]["role"] == "guardian"


async def test_a_code_sent_to_a_stranger_looks_the_same_as_one_to_a_parent(
    client, school, fixed_code
) -> None:
    """A 404 here would turn this route into a way to ask which parents a
    school has."""
    known = await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": school["guardian_phone"], "purpose": "login"},
        headers=headers(school["subdomain"]),
    )
    unknown = await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": "0789000111", "purpose": "login"},
        headers=headers(school["subdomain"]),
    )

    assert known.status_code == unknown.status_code == 200
    assert known.json().keys() == unknown.json().keys()


async def test_a_code_cannot_be_used_twice(client, school, fixed_code) -> None:
    await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": school["guardian_phone"], "purpose": "login"},
        headers=headers(school["subdomain"]),
    )
    body = {"phone": school["guardian_phone"], "purpose": "login", "code": fixed_code}

    first = await client.post(
        "/api/v1/auth/otp/login", json=body, headers=headers(school["subdomain"])
    )
    second = await client.post(
        "/api/v1/auth/otp/login", json=body, headers=headers(school["subdomain"])
    )

    assert first.status_code == 200
    assert second.status_code == 401


async def test_resending_immediately_is_throttled(client, school, fixed_code) -> None:
    await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": school["guardian_phone"], "purpose": "login"},
        headers=headers(school["subdomain"]),
    )
    again = await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": school["guardian_phone"], "purpose": "login"},
        headers=headers(school["subdomain"]),
    )

    assert again.status_code == 429


async def test_a_guardian_crosses_to_the_ai_platform_for_their_own_child(
    client, school, fixed_code
) -> None:
    """The property the whole project rests on, and the check that keeps it
    safe: a guardian may only cross for a child they are linked to."""
    await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": school["guardian_phone"], "purpose": "login"},
        headers=headers(school["subdomain"]),
    )
    login = await client.post(
        "/api/v1/auth/otp/login",
        json={
            "phone": school["guardian_phone"],
            "purpose": "login",
            "code": fixed_code,
        },
        headers=headers(school["subdomain"]),
    )
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    allowed = await client.post(
        "/api/v1/auth/ai-handoff",
        json={"student_id": str(school["student_id"])},
        headers=auth,
    )
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["expires_in"] <= 120, "a crossing token must be short-lived"
    assert body["token"] in body["redirect_url"]

    refused = await client.post(
        "/api/v1/auth/ai-handoff",
        json={"student_id": str(uuid4())},
        headers=auth,
    )
    assert refused.status_code == 403

    unnamed = await client.post("/api/v1/auth/ai-handoff", json={}, headers=auth)
    assert unnamed.status_code == 403


async def test_staff_do_not_cross_to_the_ai_platform(client, school) -> None:
    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.post(
        "/api/v1/auth/ai-handoff",
        json={"student_id": str(school["student_id"])},
        headers=auth,
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------
# What the AI platform reads
# --------------------------------------------------------------------------
async def test_the_key_set_and_discovery_document_are_public(client) -> None:
    """How aibeyondschools.co.tz validates a token without sharing a secret.
    Both addresses are published, so neither may move."""
    keys = await client.get("/.well-known/jwks.json")
    discovery = await client.get("/.well-known/openid-configuration")

    assert keys.status_code == 200
    assert keys.json()["keys"][0]["kty"] == "RSA"
    assert "d" not in keys.json()["keys"][0]
    assert discovery.status_code == 200
    assert discovery.json()["jwks_uri"].endswith("/.well-known/jwks.json")


# --------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------
async def test_every_sign_in_and_refusal_is_recorded(client, school, owner_session) -> None:
    """Children's records and financial transactions are both sensitive
    personal data, so who got in — and who was turned away — is a legal
    question here, not only an operational one."""
    await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )
    await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": "not-it"},
        headers=headers(school["subdomain"]),
    )

    rows = (
        await owner_session.execute(
            text(
                "SELECT action, outcome FROM audit_event "
                " WHERE school_id = :school_id AND action = 'auth.login' "
                " ORDER BY created_at"
            ),
            {"school_id": school["id"]},
        )
    ).all()

    outcomes = [str(r.outcome) for r in rows]
    assert "success" in outcomes
    assert "denied" in outcomes


async def test_an_expired_access_token_is_refused(client, school, monkeypatch) -> None:
    """The 15-minute lifetime is what makes a per-request database lookup
    unnecessary, so it has to actually be enforced."""
    from app.core import security

    login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": school["bursar_email"], "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )
    principal = security.decode_access_token(login.json()["access_token"])

    # Mint one that expired a minute ago.
    stale = security.jwt.encode(
        {"alg": "RS256", "kid": security.key_id(), "typ": "JWT"},
        {
            "iss": security.settings.JWT_ISSUER,
            "sub": str(principal.user_id),
            "aud": security.AUDIENCE_SMS,
            "iat": int((datetime.now(UTC) - timedelta(minutes=30)).timestamp()),
            "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()),
            "school_id": str(principal.school_id),
            "role": principal.role.value,
        },
        security._private_key(),
    )

    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {stale}"}
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------
# Setting a password without knowing the old one
# --------------------------------------------------------------------------
async def _give_the_bursar_a_phone(owner_session, school, phone: str) -> None:
    await owner_session.execute(
        text("UPDATE app_user SET phone = :phone WHERE email = :email"),
        {"phone": phone, "email": school["bursar_email"]},
    )
    await owner_session.commit()


async def _send_reset_code(client, school, phone: str) -> None:
    sent = await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": phone, "purpose": "password_reset"},
        headers=headers(school["subdomain"]),
    )
    assert sent.status_code == 200, sent.text


async def test_a_forgotten_password_can_be_reset_with_a_code(
    client, school, fixed_code, owner_session
) -> None:
    """`/auth/password` needs the current password, which is exactly what
    somebody who has forgotten it cannot supply. Until now there was no way
    back into an account at all."""
    phone = "0754330001"
    await _give_the_bursar_a_phone(owner_session, school, phone)
    await _send_reset_code(client, school, phone)

    reset = await client.post(
        "/api/v1/auth/password/reset",
        json={"phone": phone, "code": fixed_code, "new_password": "NewShule2027!"},
        headers=headers(school["subdomain"]),
    )
    assert reset.status_code == 204, reset.text

    signed_in = await client.post(
        "/api/v1/auth/login",
        json={"identifier": phone, "password": "NewShule2027!"},
        headers=headers(school["subdomain"]),
    )
    assert signed_in.status_code == 200, signed_in.text


async def test_a_reset_clears_a_lock_out(
    client, school, fixed_code, owner_session
) -> None:
    """Somebody who guessed wrong five times and then reset would otherwise
    still be locked out of the password they had just chosen, which reads as
    the reset not having worked."""
    phone = "0754330002"
    await _give_the_bursar_a_phone(owner_session, school, phone)
    await owner_session.execute(
        text(
            "UPDATE app_user SET failed_logins = 5, "
            " locked_until = now() + interval '15 minutes' WHERE phone = :phone"
        ),
        {"phone": phone},
    )
    await owner_session.commit()

    locked = await client.post(
        "/api/v1/auth/login",
        json={"identifier": phone, "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )
    assert locked.status_code == 423

    await _send_reset_code(client, school, phone)
    await client.post(
        "/api/v1/auth/password/reset",
        json={"phone": phone, "code": fixed_code, "new_password": "AfterReset27!"},
        headers=headers(school["subdomain"]),
    )

    after = await client.post(
        "/api/v1/auth/login",
        json={"identifier": phone, "password": "AfterReset27!"},
        headers=headers(school["subdomain"]),
    )
    assert after.status_code == 200, after.text


async def test_a_reset_ends_every_other_session(
    client, school, fixed_code, owner_session
) -> None:
    """If somebody else was in the account, this is what puts them out. The
    screen says so, so it had better be true."""
    phone = "0754330003"
    await _give_the_bursar_a_phone(owner_session, school, phone)

    intruder = await client.post(
        "/api/v1/auth/login",
        json={"identifier": phone, "password": PASSWORD},
        headers=headers(school["subdomain"]),
    )
    cookie = intruder.cookies.get("bs_refresh")
    assert cookie

    await _send_reset_code(client, school, phone)
    await client.post(
        "/api/v1/auth/password/reset",
        json={"phone": phone, "code": fixed_code, "new_password": "ResetAgain27!"},
        headers=headers(school["subdomain"]),
    )

    still_alive = await client.post(
        "/api/v1/auth/refresh",
        headers=headers(school["subdomain"]),
        cookies={"bs_refresh": cookie},
    )
    assert still_alive.status_code == 401


async def test_a_wrong_code_and_an_unknown_number_answer_the_same(
    client, school, fixed_code
) -> None:
    """Otherwise this page becomes a way to ask which parents a school has."""
    await _send_reset_code(client, school, "0754330004")
    wrong_code = await client.post(
        "/api/v1/auth/password/reset",
        json={"phone": "0754330004", "code": "000000", "new_password": "Whatever27!"},
        headers=headers(school["subdomain"]),
    )

    await _send_reset_code(client, school, "0754339999")
    no_such_number = await client.post(
        "/api/v1/auth/password/reset",
        json={"phone": "0754339999", "code": fixed_code, "new_password": "Whatever27!"},
        headers=headers(school["subdomain"]),
    )

    assert wrong_code.status_code == no_such_number.status_code == 401


async def test_a_short_password_is_refused(client, school, fixed_code) -> None:
    """Three screens now tell somebody what the minimum is, so all three have
    to say the number the server enforces."""
    await _send_reset_code(client, school, "0754330005")

    response = await client.post(
        "/api/v1/auth/password/reset",
        json={"phone": "0754330005", "code": fixed_code, "new_password": "short1!"},
        headers=headers(school["subdomain"]),
    )

    assert response.status_code == 422


async def test_a_reset_code_lasts_longer_than_a_login_code(client, school) -> None:
    """A reset is a longer errand: read the code, choose a password, type it
    twice, on a phone. Five minutes is the window in which people give up and
    ring the office instead."""
    login = await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": "0754330006", "purpose": "login"},
        headers=headers(school["subdomain"]),
    )
    reset = await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": "0754330007", "purpose": "password_reset"},
        headers=headers(school["subdomain"]),
    )

    assert login.json()["expires_at"] < reset.json()["expires_at"]


async def test_a_shared_handset_gets_a_shorter_session(client, school) -> None:
    """Unticking "keep me signed in" is the ordinary case on a phone three
    people use: a fortnight is a fortnight any of them can read a fee
    statement."""
    kept = await client.post(
        "/api/v1/auth/login",
        json={
            "identifier": school["bursar_email"],
            "password": PASSWORD,
            "remember": True,
        },
        headers=headers(school["subdomain"]),
    )
    shared = await client.post(
        "/api/v1/auth/login",
        json={
            "identifier": school["bursar_email"],
            "password": PASSWORD,
            "remember": False,
        },
        headers=headers(school["subdomain"]),
    )

    assert kept.status_code == shared.status_code == 200
    # The cookie must not outlive the token it carries, or a browser sends a
    # dead one and the person is signed out at a moment nobody chose.
    assert _cookie_max_age(shared) < _cookie_max_age(kept)


def _cookie_max_age(response) -> int:
    header = next(
        value for key, value in response.headers.multi_items() if key == "set-cookie"
    )
    return int(header.split("Max-Age=")[1].split(";")[0])


async def test_the_last_attempts_before_a_lock_out_are_warned_about(
    client, school
) -> None:
    """A real person mistyping on a phone keyboard is otherwise locked out with
    no warning at all. Nothing is said earlier: a countdown from five tells a
    password sprayer how far it has got."""
    said = []
    for _ in range(4):
        response = await client.post(
            "/api/v1/auth/login",
            json={"identifier": school["bursar_email"], "password": "wrong-one"},
            headers=headers(school["subdomain"]),
        )
        said.append(response.json()["detail"])

    assert "locks" not in said[0]
    assert "locks" not in said[1]
    assert "2 more attempts" in said[2]
    assert "One more attempt" in said[3]
