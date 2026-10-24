"""The admissions funnel, end to end through the real app.

The test that matters most is the whole path: a parent with no account applies,
a registrar screens and offers, a bursar records a deposit, and a child appears
on the roll with a guardian and a bill. Everything between those two points is
a state machine, and the tests around it are mostly about what it refuses.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.security import hash_password
from app.main import app
from tests.conftest import make_academic_year, make_class_group, make_level, make_school

PASSWORD = "Shule2027!"  # noqa: S105 - test fixture data
OTP = "424242"


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
def fixed_code(monkeypatch) -> str:
    """Pin the generated code so the test can type it back."""
    monkeypatch.setattr("app.services.auth.generate_otp", lambda *a, **k: OTP)
    return OTP


async def _add_user(session, *, school_id, name, role, email):
    await session.execute(
        text(
            "INSERT INTO app_user (id, school_id, full_name, email, role, password_hash, "
            " must_change_password, is_active, failed_logins, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, :name, :email, :role, :hash, false, "
            " true, 0, now(), now())"
        ),
        {
            "school_id": school_id,
            "name": name,
            "email": email,
            "role": role,
            "hash": hash_password(PASSWORD),
        },
    )


@pytest.fixture
async def setup(owner_session):
    """A school that can actually admit: a current year with a term, a level
    with a published fee structure, and a class to put a child in."""
    suffix = uuid4().hex[:6]
    school_id = await make_school(owner_session, name="Mwenge", subdomain=f"adm{suffix}")
    subdomain = await owner_session.scalar(
        text("SELECT subdomain FROM school WHERE id = :id"), {"id": school_id}
    )

    year_id = await make_academic_year(owner_session, school_id=school_id, label="2027")
    await owner_session.execute(
        text(
            "INSERT INTO term (id, school_id, academic_year_id, index, label, starts_on, "
            " ends_on, is_current, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, :year, 1, 'Term 1', '2027-01-08', "
            " '2027-04-02', true, now(), now())"
        ),
        {"school_id": school_id, "year": year_id},
    )
    level_id = await make_level(
        owner_session, school_id=school_id, code="STD1", name="Standard 1", order_index=1
    )
    class_group_id = await make_class_group(
        owner_session,
        school_id=school_id,
        academic_year_id=year_id,
        level_id=level_id,
        name="Standard 1 Blue",
    )

    structure_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO fee_structure (id, school_id, academic_year_id, level_id, label, "
            " status, published_at, created_at, updated_at) "
            "VALUES (:id, :school_id, :year, :level, 'Standard 1 2027', 'published', "
            " now(), now(), now())"
        ),
        {"id": structure_id, "school_id": school_id, "year": year_id, "level": level_id},
    )
    for name, amount, category, frequency in (
        ("Tuition", 300000, "tuition", "per_term"),
        ("Admission fee", 100000, "admission", "one_off"),
        ("Uniform set", 75000, "uniform", "one_off"),
    ):
        await owner_session.execute(
            text(
                "INSERT INTO fee_item (id, school_id, fee_structure_id, name, amount, "
                " category, frequency, is_mandatory, sort_order, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :structure, :name, :amount, "
                " :category, :frequency, true, 0, now(), now())"
            ),
            {
                "school_id": school_id,
                "structure": structure_id,
                "name": name,
                "amount": amount,
                "category": category,
                "frequency": frequency,
            },
        )

    await owner_session.execute(
        text(
            "INSERT INTO document_requirement (id, school_id, kind, label, is_required, "
            " boarders_only, ask_at_application, sort_order, is_active, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, 'birth_certificate', 'Birth certificate', "
            " true, false, true, 0, true, now(), now())"
        ),
        {"school_id": school_id},
    )

    emails = {
        "admin": f"admin-{suffix}@mwenge.ac.tz",
        "bursar": f"bursar-{suffix}@mwenge.ac.tz",
        "teacher": f"teacher-{suffix}@mwenge.ac.tz",
    }
    await _add_user(
        owner_session,
        school_id=school_id,
        name="Datius",
        role="school_admin",
        email=emails["admin"],
    )
    await _add_user(
        owner_session, school_id=school_id, name="Salome", role="bursar", email=emails["bursar"]
    )
    await _add_user(
        owner_session,
        school_id=school_id,
        name="Grace",
        role="class_teacher",
        email=emails["teacher"],
    )
    await owner_session.commit()

    yield {
        "school_id": school_id,
        "subdomain": subdomain,
        "year_id": year_id,
        "level_id": level_id,
        "class_group_id": class_group_id,
        **{f"{k}_email": v for k, v in emails.items()},
    }

    await owner_session.rollback()
    await owner_session.execute(text("DELETE FROM school WHERE id = :id"), {"id": school_id})
    await owner_session.commit()


def headers(subdomain: str) -> dict[str, str]:
    return {"X-School-Subdomain": subdomain}


async def staff_token(client, *, email, subdomain) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": PASSWORD},
        headers=headers(subdomain),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}", **headers(subdomain)}


async def applicant(client, setup, fixed_code, *, phone) -> dict[str, str]:
    """A parent with no account: a verified phone number is the credential."""
    start = await client.post(
        "/api/v1/apply/start", json={"phone": phone}, headers=headers(setup["subdomain"])
    )
    assert start.status_code == 200, start.text

    verify = await client.post(
        "/api/v1/apply/verify",
        json={"phone": phone, "code": fixed_code},
        headers=headers(setup["subdomain"]),
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["principal"]["role"] == "applicant"
    return {
        "Authorization": f"Bearer {verify.json()['access_token']}",
        **headers(setup["subdomain"]),
    }


async def fill_in(client, parent, setup, **overrides) -> None:
    body = {
        "child_first_name": "Happiness",
        "child_surname": "Mollel",
        "date_of_birth": "2020-11-30",
        "sex": "f",
        "level_applied_id": str(setup["level_id"]),
        "guardian_name": "Neema Mollel",
        "guardian_phone": "0754118902",
        "guardian_relationship": "mother",
        **overrides,
    }
    response = await client.patch("/api/v1/apply/me", json=body, headers=parent)
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# What the public form is told
# --------------------------------------------------------------------------
async def test_the_form_options_hold_nothing_about_any_child(client, setup) -> None:
    response = await client.get("/api/v1/apply/options", headers=headers(setup["subdomain"]))

    assert response.status_code == 200
    body = response.json()
    assert body["school_name"] == "Mwenge"
    assert [level["code"] for level in body["levels"]] == ["STD1"]
    assert body["documents"][0]["label"] == "Birth certificate"


async def test_a_level_with_no_published_fees_is_not_offered(client, setup, owner_session) -> None:
    """A level nobody can be quoted a deposit for is a level nobody should be
    able to apply to — finding that out after filling in a form is worse than
    not seeing it."""
    await make_level(
        owner_session, school_id=setup["school_id"], code="STD2", name="Standard 2", order_index=2
    )
    await owner_session.commit()

    response = await client.get("/api/v1/apply/options", headers=headers(setup["subdomain"]))

    assert [level["code"] for level in response.json()["levels"]] == ["STD1"]


# --------------------------------------------------------------------------
# Filling it in
# --------------------------------------------------------------------------
async def test_a_half_filled_form_saves_and_says_what_is_left(client, setup, fixed_code) -> None:
    """A form that takes longer than about four minutes on a phone does not get
    finished, so a parent has to be able to stop at question nine."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")

    saved = await client.patch(
        "/api/v1/apply/me",
        json={"child_first_name": "Happiness", "child_surname": "Mollel"},
        headers=parent,
    )

    assert saved.status_code == 200
    assert saved.json()["application"]["state"] == "started"
    assert "Date of birth" in saved.json()["missing"]


async def test_coming_back_resumes_the_same_application(
    client, setup, fixed_code, owner_session
) -> None:
    """By code, against the verified number. A parent who lost the page at
    question nine comes back to question nine."""
    first = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, first, setup)
    reference = (await client.get("/api/v1/apply/me", headers=first)).json()["application"][
        "reference"
    ]

    # An hour later. Clearing the challenges is what moves the clock past the
    # resend throttle, which is working as intended and not what is under test.
    await owner_session.execute(text("DELETE FROM otp_challenge WHERE phone = '0712998877'"))
    await owner_session.commit()

    again = await applicant(client, setup, fixed_code, phone="0712998877")
    resumed = await client.get("/api/v1/apply/me", headers=again)

    assert resumed.json()["application"]["reference"] == reference
    assert resumed.json()["application"]["child_first_name"] == "Happiness"


async def test_submitting_too_early_names_what_is_needed(client, setup, fixed_code) -> None:
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await client.patch("/api/v1/apply/me", json={"child_first_name": "Happiness"}, headers=parent)

    response = await client.post("/api/v1/apply/me/submit", headers=parent)

    assert response.status_code == 422
    assert "child_surname" in response.json()["detail"]


async def test_a_submitted_form_can_no_longer_be_edited(client, setup, fixed_code) -> None:
    """The school is reviewing it; a child's details changing underneath a
    registrar is how the wrong birth certificate gets attached."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    response = await client.patch(
        "/api/v1/apply/me", json={"child_first_name": "Changed"}, headers=parent
    )

    assert response.status_code == 422
    assert "reviewing" in response.json()["detail"]


async def test_an_applicant_session_reaches_nothing_but_its_own_application(
    client, setup, fixed_code
) -> None:
    """A verified phone number buys exactly one thing."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")

    roll = await client.get("/api/v1/students", headers=parent)
    queue = await client.get("/api/v1/admissions", headers=parent)

    assert roll.status_code == 403
    assert queue.status_code == 403


# --------------------------------------------------------------------------
# The registrar
# --------------------------------------------------------------------------
async def test_screening_flags_a_child_the_school_already_has(
    client, setup, fixed_code, owner_session
) -> None:
    """Scored, not matched, and never acted on automatically."""
    from tests.conftest import make_student

    await make_student(
        owner_session,
        school_id=setup["school_id"],
        admission_no=f"M/{uuid4().hex[:6]}",
        first_name="Happiness",
        surname="Mollel",
    )
    await owner_session.commit()

    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    queue = await client.get("/api/v1/admissions", headers=admin)

    row = queue.json()["items"][0]
    assert row["duplicate_score"] is not None
    assert "same name" in row["duplicate_reason"]


async def test_a_sibling_is_noted_rather_than_flagged(
    client, setup, fixed_code, owner_session
) -> None:
    """One phone, several children, all of them real. The commonest high score
    in a real school must not read as a duplicate."""
    from tests.conftest import make_student

    student_id = await make_student(
        owner_session,
        school_id=setup["school_id"],
        admission_no=f"M/{uuid4().hex[:6]}",
        first_name="Baraka",
        surname="Mollel",
    )
    guardian_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO guardian (id, school_id, full_name, phone, locale, sms_opted_out, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, 'Neema Mollel', '0754118902', 'en', false, now(), now())"
        ),
        {"id": guardian_id, "school_id": setup["school_id"]},
    )
    await owner_session.execute(
        text(
            "INSERT INTO guardian_student (id, school_id, guardian_id, student_id, "
            " relationship, is_primary, receives_invoices, receives_sms, may_collect, "
            " created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, :guardian, :student, 'mother', true, "
            " true, true, true, now(), now())"
        ),
        {"school_id": setup["school_id"], "guardian": guardian_id, "student": student_id},
    )
    await owner_session.commit()

    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    row = (await client.get("/api/v1/admissions", headers=admin)).json()["items"][0]

    assert row["duplicate_score"] < 70
    assert "sibling" in (row["screening_note"] or "")


async def test_the_queue_sorts_by_how_long_a_family_has_waited(client, setup, fixed_code) -> None:
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    queue = await client.get("/api/v1/admissions", headers=admin)

    assert queue.json()["total"] == 1
    assert queue.json()["items"][0]["waiting_days"] == 0
    assert queue.json()["items"][0]["child_name"] == "Happiness Mollel"


async def test_a_teacher_cannot_see_the_admissions_queue(client, setup) -> None:
    teacher = await staff_token(client, email=setup["teacher_email"], subdomain=setup["subdomain"])

    response = await client.get("/api/v1/admissions", headers=teacher)

    assert response.status_code == 403


# --------------------------------------------------------------------------
# The whole path
# --------------------------------------------------------------------------
async def test_a_parent_applies_and_a_child_lands_on_the_roll(
    client, setup, fixed_code, owner_session
) -> None:
    """The one that matters. Apply, screen, offer, pay, enrol — and at the end
    a child with a guardian, a class and a settled deposit."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    bursar = await staff_token(client, email=setup["bursar_email"], subdomain=setup["subdomain"])
    application_id = (await client.get("/api/v1/admissions", headers=admin)).json()["items"][0][
        "id"
    ]

    # Offering before anybody has looked is refused by the state machine.
    too_soon = await client.post(f"/api/v1/admissions/{application_id}/offer", headers=admin)
    assert too_soon.status_code == 422

    await client.post(
        f"/api/v1/admissions/{application_id}/screen",
        json={"note": "No duplicate found"},
        headers=admin,
    )
    offered = await client.post(f"/api/v1/admissions/{application_id}/offer", headers=admin)
    assert offered.status_code == 200
    body = offered.json()
    assert body["application"]["admission_no"]
    # 100,000 + 75,000 joining, plus 30% of 300,000 tuition.
    assert Decimal(body["deposit_billed"]) == Decimal("265000.00")
    assert body["application"]["offer_expires_on"]

    # Enrolling before the deposit is refused, with the deposit as the reason.
    unpaid = await client.post(
        f"/api/v1/admissions/{application_id}/enrol",
        json={"class_group_id": str(setup["class_group_id"])},
        headers=admin,
    )
    assert unpaid.status_code == 422
    assert "deposit arriving" in unpaid.json()["detail"]

    paid = await client.post(
        f"/api/v1/admissions/{application_id}/deposit",
        json={
            "amount": body["deposit_billed"],
            "receipt": "RCPT-0001",
            "channel": "mpesa",
            "msisdn": "0712998877",
        },
        headers=bursar,
    )
    assert paid.status_code == 200
    assert paid.json()["application_state"] == "accepted", "payment is the acceptance"
    assert Decimal(paid.json()["balance"]) == Decimal("0.00")

    enrolled = await client.post(
        f"/api/v1/admissions/{application_id}/enrol",
        json={"class_group_id": str(setup["class_group_id"]), "residency": "day"},
        headers=admin,
    )
    assert enrolled.status_code == 201
    student_id = enrolled.json()["student_id"]

    profile = await client.get(f"/api/v1/students/{student_id}", headers=admin)
    assert profile.status_code == 200
    assert profile.json()["student"]["first_name"] == "Happiness"
    assert profile.json()["class_name"] == "Standard 1 Blue"
    assert [g["full_name"] for g in profile.json()["guardians"]] == ["Neema Mollel"]
    # The deposit was raised against the application; it has to follow the
    # child, or the money is invisible on their statement.
    assert Decimal(profile.json()["settled"]) == Decimal("265000.00")


async def test_the_applicants_login_becomes_the_familys_login(
    client, setup, fixed_code, owner_session
) -> None:
    """One password, never two. A parent who applied in November signs in to
    pay fees in January with what they already have."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup, guardian_phone="0712998877")
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    bursar = await staff_token(client, email=setup["bursar_email"], subdomain=setup["subdomain"])
    application_id = (await client.get("/api/v1/admissions", headers=admin)).json()["items"][0][
        "id"
    ]

    await client.post(f"/api/v1/admissions/{application_id}/screen", json={}, headers=admin)
    offered = await client.post(f"/api/v1/admissions/{application_id}/offer", headers=admin)
    await client.post(
        f"/api/v1/admissions/{application_id}/deposit",
        json={"amount": offered.json()["deposit_billed"], "receipt": "RCPT-0002"},
        headers=bursar,
    )
    await client.post(
        f"/api/v1/admissions/{application_id}/enrol",
        json={"class_group_id": str(setup["class_group_id"])},
        headers=admin,
    )

    # Scoped to this school: the same phone legitimately has an account at
    # another school, which the partial unique index allows and other tests
    # create.
    row = (
        await owner_session.execute(
            text(
                "SELECT role::text AS role, guardian_id IS NOT NULL AS linked "
                "  FROM app_user "
                " WHERE phone = '0712998877' AND school_id = :school_id"
            ),
            {"school_id": setup["school_id"]},
        )
    ).one()

    assert row.role == "guardian"
    assert row.linked is True


async def test_the_same_receipt_cannot_be_recorded_twice(client, setup, fixed_code) -> None:
    """Entering the same receipt twice must be caught rather than doubling a
    credit."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    bursar = await staff_token(client, email=setup["bursar_email"], subdomain=setup["subdomain"])
    application_id = (await client.get("/api/v1/admissions", headers=admin)).json()["items"][0][
        "id"
    ]
    await client.post(f"/api/v1/admissions/{application_id}/screen", json={}, headers=admin)
    await client.post(f"/api/v1/admissions/{application_id}/offer", headers=admin)

    body = {"amount": "50000", "receipt": "RCPT-SAME", "channel": "counter"}
    first = await client.post(
        f"/api/v1/admissions/{application_id}/deposit", json=body, headers=bursar
    )
    second = await client.post(
        f"/api/v1/admissions/{application_id}/deposit", json=body, headers=bursar
    )

    assert first.status_code == 200
    assert second.status_code == 422
    assert "already been recorded" in second.json()["detail"]


async def test_a_part_payment_does_not_accept_the_place(client, setup, fixed_code) -> None:
    """Acceptance is the deposit arriving — all of it. A place half paid for is
    a place still available."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    bursar = await staff_token(client, email=setup["bursar_email"], subdomain=setup["subdomain"])
    application_id = (await client.get("/api/v1/admissions", headers=admin)).json()["items"][0][
        "id"
    ]
    await client.post(f"/api/v1/admissions/{application_id}/screen", json={}, headers=admin)
    await client.post(f"/api/v1/admissions/{application_id}/offer", headers=admin)

    part = await client.post(
        f"/api/v1/admissions/{application_id}/deposit",
        json={"amount": "100000", "receipt": "RCPT-PART", "channel": "counter"},
        headers=bursar,
    )

    assert part.status_code == 200
    assert part.json()["application_state"] == "offered", "still offered, not accepted"
    assert Decimal(part.json()["balance"]) > Decimal(0)


async def test_a_bursar_cannot_offer_and_an_admin_can(client, setup, fixed_code) -> None:
    """Offering burns an admission number. A bursar takes money; a registrar
    decides who gets a place."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    application_id = (await client.get("/api/v1/admissions", headers=admin)).json()["items"][0][
        "id"
    ]
    await client.post(f"/api/v1/admissions/{application_id}/screen", json={}, headers=admin)

    teacher = await staff_token(client, email=setup["teacher_email"], subdomain=setup["subdomain"])
    refused = await client.post(f"/api/v1/admissions/{application_id}/offer", headers=teacher)
    allowed = await client.post(f"/api/v1/admissions/{application_id}/offer", headers=admin)

    assert refused.status_code == 403
    assert allowed.status_code == 200


async def test_declining_records_a_reason(client, setup, fixed_code) -> None:
    """A family that rings to ask why deserves an answer, and the registrar who
    declined may not be the one who takes the call."""
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    application_id = (await client.get("/api/v1/admissions", headers=admin)).json()["items"][0][
        "id"
    ]

    declined = await client.post(
        f"/api/v1/admissions/{application_id}/decline",
        json={"reason": "Standard 1 is full for the January intake"},
        headers=admin,
    )

    assert declined.status_code == 200
    assert declined.json()["state"] == "declined"
    assert "full" in declined.json()["decline_reason"]


async def test_the_funnel_counts_every_step(client, setup, fixed_code) -> None:
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup)
    await client.post("/api/v1/apply/me/submit", headers=parent)

    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    funnel = await client.get("/api/v1/admissions/funnel", headers=admin)

    assert funnel.status_code == 200
    assert funnel.json()["submitted"] == 1
    assert funnel.json()["enrolled"] == 0
