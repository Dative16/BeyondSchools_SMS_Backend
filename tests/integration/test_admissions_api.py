"""The admissions funnel, end to end through the real app.

The test that matters most is the whole path: a parent with no account applies,
a registrar screens and offers, a bursar records a deposit, and a child appears
on the roll with a guardian and a bill. Everything between those two points is
a state machine, and the tests around it are mostly about what it refuses.
"""

from decimal import Decimal
from uuid import UUID, uuid4

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

    # And the parent is told. A school running its intake on this system has
    # stopped phoning families by hand, so the enrolment is only finished when
    # the guardian has the admission number in their hand.
    told = (
        (
            await owner_session.execute(
                text(
                    "SELECT note, changed FROM audit_event WHERE school_id = :school "
                    "AND action = 'notification.enrolment_confirmed'"
                ),
                {"school": setup["school_id"]},
            )
        )
        .mappings()
        .all()
    )
    assert len(told) == 1, "exactly one message, to the guardian who applied"
    assert "0712998877" in told[0]["note"]
    assert "(sent)" in told[0]["note"]
    message = told[0]["changed"]["body"]
    assert "Happiness" in message
    assert body["application"]["admission_no"] in message, "the number they will be asked for"


async def test_a_parent_who_gives_a_different_contact_number_still_has_a_login(
    client, setup, fixed_code, owner_session
) -> None:
    """The case the form allows and nothing tested.

    A mother applies from her own handset and gives the father's number as the
    family contact. Both are legitimate and they are different numbers, and
    enrolment used to promote the typed one — which matched no account, so the
    family finished enrolment with no way to sign in and no way to pay.
    """
    parent = await applicant(client, setup, fixed_code, phone="0712998877")
    await fill_in(client, parent, setup, guardian_phone="0754118902")
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
        json={"amount": offered.json()["deposit_billed"], "receipt": "RCPT-0003"},
        headers=bursar,
    )
    enrolled = await client.post(
        f"/api/v1/admissions/{application_id}/enrol",
        json={"class_group_id": str(setup["class_group_id"])},
        headers=admin,
    )
    assert enrolled.status_code == 201, enrolled.text

    # The account that exists is the one an OTP proved, and it is the one that
    # becomes the family's login.
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
    assert row.linked, "promoted and tied to the guardian record"

    # The guardian record still carries the contact number the form gave,
    # because that is what a payment is matched back to.
    contact = (
        await owner_session.execute(
            text("SELECT phone FROM guardian WHERE school_id = :school_id"),
            {"school_id": setup["school_id"]},
        )
    ).scalar_one()
    assert contact == "0754118902"

    # And the message goes to the number that is the login, since it tells
    # them to sign in with it.
    note = (
        await owner_session.execute(
            text(
                "SELECT note FROM audit_event WHERE school_id = :school_id "
                "AND action = 'notification.enrolment_confirmed'"
            ),
            {"school_id": setup["school_id"]},
        )
    ).scalar_one()
    assert "0712998877" in note


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


# --------------------------------------------------------------------------
# Documents, while the form is still open
# --------------------------------------------------------------------------
PNG = bytes(
    [
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D,
        0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4, 0x89, 0x00, 0x00, 0x00,
        0x0A, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x60, 0x00, 0x00, 0x00,
        0x02, 0x00, 0x01, 0xFF, 0xFF, 0x03, 0x00, 0x00, 0x06, 0x00, 0x05, 0x57,
        0xBF, 0xAB, 0xD4, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
        0x42, 0x60, 0x82,
    ]
)


async def test_a_parent_can_attach_a_birth_certificate_from_the_form(
    client, setup, fixed_code
) -> None:
    """The best moment to get one is while a parent is sitting with the form
    open and the certificate in their hand. Every week after that is a phone
    call."""
    parent = await applicant(client, setup, fixed_code, phone="0754118951")

    ticket = await client.post(
        "/api/v1/apply/me/documents/upload-url",
        json={
            "kind": "birth_certificate",
            "content_type": "image/png",
            "size_bytes": len(PNG),
        },
        headers=parent,
    )
    assert ticket.status_code == 200, ticket.text
    body = ticket.json()
    assert "/application/" in body["object_key"], "stored under the application, not a pupil"

    # The parent's phone PUTs the bytes itself; this stands in for the phone.
    put = await client.put(body["url"], content=PNG, headers=body["headers"])
    assert put.status_code == 200, put.text

    confirmed = await client.post(
        "/api/v1/apply/me/documents",
        json={
            "kind": "birth_certificate",
            "object_key": body["object_key"],
            "file_name": "cheti.png",
        },
        headers=parent,
    )

    assert confirmed.status_code == 201, confirmed.text
    kinds = {row["kind"] for row in confirmed.json()["documents"]}
    assert "birth_certificate" in kinds


async def test_a_file_type_a_school_cannot_use_is_refused_before_it_is_sent(
    client, setup, fixed_code
) -> None:
    """Refused up front, so a parent on a weak connection has not already sent
    four megabytes of something nobody can open."""
    parent = await applicant(client, setup, fixed_code, phone="0754118952")

    response = await client.post(
        "/api/v1/apply/me/documents/upload-url",
        json={"kind": "birth_certificate", "content_type": "application/zip"},
        headers=parent,
    )

    assert response.status_code == 422
    assert "not a kind of file" in response.text


async def test_confirming_an_upload_that_never_arrived_is_refused(
    client, setup, fixed_code
) -> None:
    """A presigned PUT cannot enforce anything, so the confirm step asks
    storage what actually landed. Without it a record would exist for a file
    that is not there — which on a 3G connection is a real fraction of them."""
    parent = await applicant(client, setup, fixed_code, phone="0754118953")

    ticket = await client.post(
        "/api/v1/apply/me/documents/upload-url",
        json={"kind": "birth_certificate", "content_type": "image/png"},
        headers=parent,
    )

    confirmed = await client.post(
        "/api/v1/apply/me/documents",
        json={"kind": "birth_certificate", "object_key": ticket.json()["object_key"]},
        headers=parent,
    )

    assert confirmed.status_code == 422
    assert "did not arrive" in confirmed.text


async def test_a_stranger_cannot_ask_for_an_upload_url(client, setup) -> None:
    """There is no application id in any of these routes: the session is
    scoped to one form, so there is nothing for a caller to change."""
    response = await client.post(
        "/api/v1/apply/me/documents/upload-url",
        json={"kind": "birth_certificate", "content_type": "image/png"},
        headers=headers(setup["subdomain"]),
    )

    assert response.status_code in (401, 403)


async def test_what_a_parent_types_comes_back_when_they_return(
    client, setup, fixed_code
) -> None:
    """Three fields were write-only: the save request took them and the state
    response never sent them back. A parent who filled them in and came back
    the next day found the boxes empty, and either typed them again or decided
    the form had not saved."""
    parent = await applicant(client, setup, fixed_code, phone="0754118954")

    await client.patch(
        "/api/v1/apply/me",
        json={
            "guardian_name": "Neema Mollel",
            "guardian_national_id": "19850101-12345-00001-23",
            "guardian_address": "Mwenge, Kinondoni",
            "birth_certificate_no": "BC/2020/88213",
        },
        headers=parent,
    )

    # A fresh read, as a return visit would do.
    returned = await client.get("/api/v1/apply/me", headers=parent)

    application = returned.json()["application"]
    assert application["guardian_national_id"] == "19850101-12345-00001-23"
    assert application["guardian_address"] == "Mwenge, Kinondoni"
    assert application["birth_certificate_no"] == "BC/2020/88213"


async def test_a_storage_key_is_never_sent_to_the_form(
    client, setup, fixed_code
) -> None:
    """The document rows were untyped dicts and carried `file_url`, which is a
    storage key rather than anything openable. A public page reached by a
    stranger is not where an object key belongs."""
    parent = await applicant(client, setup, fixed_code, phone="0754118955")
    ticket = await client.post(
        "/api/v1/apply/me/documents/upload-url",
        json={"kind": "birth_certificate", "content_type": "image/png"},
        headers=parent,
    )
    body = ticket.json()
    await client.put(body["url"], content=PNG, headers=body["headers"])
    confirmed = await client.post(
        "/api/v1/apply/me/documents",
        json={"kind": "birth_certificate", "object_key": body["object_key"]},
        headers=parent,
    )

    document = confirmed.json()["documents"][0]
    assert "file_url" not in document
    assert document["kind"] == "birth_certificate"
    assert document["content_type"] == "image/png"


async def test_a_parent_who_closes_the_browser_comes_back_to_the_same_form(
    client, setup, fixed_code
) -> None:
    """The refresh cookie is the whole point of the form surviving a flat
    battery. Rotating it returned a token with the applicant role and no
    application, so every route on the public form answered 403 — a parent
    could not get back to their own half-filled form without a new code."""
    parent = await applicant(client, setup, fixed_code, phone="0754118960")
    await client.patch(
        "/api/v1/apply/me", json={"child_first_name": "Happiness"}, headers=parent
    )

    # The access token dies with the tab; only the cookie survives.
    refreshed = await client.post(
        "/api/v1/auth/refresh", headers=headers(setup["subdomain"])
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["principal"]["role"] == "applicant"

    resumed = await client.get(
        "/api/v1/apply/me",
        headers={
            "Authorization": f"Bearer {refreshed.json()['access_token']}",
            **headers(setup["subdomain"]),
        },
    )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["application"]["child_first_name"] == "Happiness"


async def test_a_form_says_separately_what_it_needs_and_what_the_school_wants(
    client, setup, fixed_code
) -> None:
    """Two different lists. `missing` is the school's checklist — a birth
    certificate, a photograph — and `missing_to_submit` is the shortest list
    the server will not accept a form without. A form that disabled its send
    button on the first one would put a document between a parent and a
    submission the server would have taken."""
    parent = await applicant(client, setup, fixed_code, phone="0754118961")

    empty = (await client.get("/api/v1/apply/me", headers=parent)).json()
    assert empty["can_submit"] is False
    assert "child_first_name" in empty["missing_to_submit"]

    filled = await client.patch(
        "/api/v1/apply/me",
        json={
            "child_first_name": "Happiness",
            "child_surname": "Mollel",
            "date_of_birth": "2020-11-30",
            "sex": "f",
            "level_applied_id": str(setup["level_id"]),
            "guardian_name": "Neema Mollel",
            "guardian_phone": "0754118961",
            "guardian_relationship": "mother",
        },
        headers=parent,
    )

    state = filled.json()
    assert state["can_submit"] is True
    assert state["missing_to_submit"] == []
    # The checklist is still incomplete, and that is fine.
    assert state["complete"] is False
    assert state["missing"], "the school still wants documents"

    sent = await client.post("/api/v1/apply/me/submit", headers=parent)
    assert sent.status_code == 200, sent.text

# --------------------------------------------------------------------------
# Admitting at the counter
# --------------------------------------------------------------------------
BY_HAND = "/api/v1/admissions/by-hand"


def _body(setup, **over):
    body = {
        "child": {
            "first_name": "Glory",
            "surname": "Mwakalebela",
            "date_of_birth": "2020-04-11",
            "sex": "f",
        },
        "class_group_id": str(setup["class_group_id"]),
        "guardian": {
            "full_name": "Asha Mwanjala",
            "phone": "0712445109",
            "relationship": "mother",
        },
    }
    body.update(over)
    return body


async def test_a_family_at_the_counter_leaves_with_everything_they_need(
    client, setup, owner_session
) -> None:
    """Six things, and the family cannot leave the room without any of them:
    the pupil, the enrolment, the guardian, the link, a login, and a bill."""
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])

    response = await client.post(BY_HAND, json=_body(setup), headers=admin)

    assert response.status_code == 201, response.text
    out = response.json()
    assert out["class_name"] == "Standard 1 Blue"
    assert out["admission_no"], "read out to the family at the desk"
    assert out["guardian_existed"] is False
    assert out["login"] == "created"
    # Tuition 300,000 for the term, plus the one-off admission fee and uniform
    # that the funnel would have put on a deposit invoice. Without them a
    # child admitted by hand would never be charged an admission fee at all.
    assert Decimal(out["billed"]) == Decimal("475000.00")

    enrolled = (
        await owner_session.execute(
            text(
                "SELECT s.status, s.admission_no, e.outcome, e.class_group_id "
                "  FROM student s JOIN enrolment e ON e.student_id = s.id "
                " WHERE s.id = :id"
            ),
            {"id": out["student_id"]},
        )
    ).mappings().one()
    assert enrolled["status"] == "active"
    assert enrolled["outcome"] == "active"
    assert enrolled["class_group_id"] == UUID(str(setup["class_group_id"]))

    link = (
        await owner_session.execute(
            text(
                "SELECT relationship, is_primary, receives_invoices "
                "  FROM guardian_student WHERE student_id = :id"
            ),
            {"id": out["student_id"]},
        )
    ).mappings().one()
    assert link["relationship"] == "mother"
    assert link["is_primary"] is True
    assert link["receives_invoices"] is True, "somebody has to get the bill"

    login = (
        await owner_session.execute(
            text("SELECT role, guardian_id FROM app_user WHERE phone = :phone"),
            {"phone": "0712445109"},
        )
    ).mappings().one()
    assert login["role"] == "guardian"
    assert login["guardian_id"] == UUID(out["guardian_id"]), "the login points at the family"

    told = (
        await owner_session.execute(
            text(
                "SELECT note FROM audit_event WHERE school_id = :school "
                "   AND action = 'notification.enrolment_confirmed'"
            ),
            {"school": setup["school_id"]},
        )
    ).mappings().all()
    assert len(told) == 1
    assert "0712445109" in told[0]["note"]

    # And they are on the roll, which is the screen the school actually works
    # from. A child in the database but not in this list is a child nobody
    # marks a register for.
    roll = await client.get(
        "/api/v1/students", params={"q": out["admission_no"]}, headers=admin
    )
    assert roll.status_code == 200, roll.text
    rows = roll.json()["items"]
    assert len(rows) == 1
    assert rows[0]["first_name"] == "Glory"
    assert rows[0]["class_name"] == "Standard 1 Blue"
    assert rows[0]["primary_guardian"] == "Asha Mwanjala"
    assert rows[0]["primary_guardian_phone"] == "0712445109", "who the bursar rings"
    assert Decimal(rows[0]["balance"]) == Decimal("475000.00"), "and what they owe"

    # The invoice is openable, because a receipt is entered against it.
    bill = await client.get(f"/api/v1/fees/invoices/{out['invoice_id']}", headers=admin)
    assert bill.status_code == 200, bill.text
    items = {line["item"] for line in bill.json()["lines"]}
    assert items == {"Tuition", "Admission fee", "Uniform set"}


async def test_a_second_child_joins_the_guardian_the_first_one_made(
    client, setup, owner_session
) -> None:
    """One parent, two children — not two parent records with one number
    between them, which is how a school ends up sending two of every SMS and
    showing half a balance."""
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])

    first = await client.post(BY_HAND, json=_body(setup), headers=admin)
    second = await client.post(
        BY_HAND,
        json=_body(
            setup,
            child={
                "first_name": "Baraka",
                "surname": "Mwakalebela",
                "date_of_birth": "2018-09-02",
                "sex": "m",
            },
        ),
        headers=admin,
    )

    assert second.status_code == 201, second.text
    assert second.json()["guardian_existed"] is True
    assert second.json()["guardian_id"] == first.json()["guardian_id"]

    count = await owner_session.scalar(
        text("SELECT count(*) FROM guardian WHERE phone = :phone"), {"phone": "0712445109"}
    )
    assert count == 1
    links = await owner_session.scalar(
        text(
            "SELECT count(*) FROM guardian_student WHERE guardian_id = :id"
        ),
        {"id": first.json()["guardian_id"]},
    )
    assert links == 2


async def test_the_name_typed_for_a_number_already_on_file_is_not_used(
    client, setup, owner_session
) -> None:
    """The record on file wins, which is why the form warns first.

    Worth a test of its own because it is a silent discard: nothing fails, and
    the registrar would have no way of knowing their spelling went nowhere.
    """
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    await client.post(BY_HAND, json=_body(setup), headers=admin)

    await client.post(
        BY_HAND,
        json=_body(
            setup,
            child={
                "first_name": "Baraka",
                "surname": "Mwakalebela",
                "date_of_birth": "2018-09-02",
                "sex": "m",
            },
            guardian={
                "full_name": "A. MWANJALA",
                "phone": "0712445109",
                "relationship": "mother",
            },
        ),
        headers=admin,
    )

    name = await owner_session.scalar(
        text("SELECT full_name FROM guardian WHERE phone = :phone"), {"phone": "0712445109"}
    )
    assert name == "Asha Mwanjala", "the first spelling the family gave is kept"


async def test_a_full_class_is_refused_by_name(client, setup, owner_session) -> None:
    """The browser's list of classes with places left goes stale the moment a
    second registrar admits somebody. The count that decides is read in the
    same transaction as the enrolment."""
    await owner_session.execute(
        text("UPDATE class_group SET capacity = 1 WHERE id = :id"),
        {"id": setup["class_group_id"]},
    )
    await owner_session.commit()
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])

    first = await client.post(BY_HAND, json=_body(setup), headers=admin)
    assert first.status_code == 201, first.text

    second = await client.post(
        BY_HAND,
        json=_body(
            setup,
            child={
                "first_name": "Baraka",
                "surname": "Juma",
                "date_of_birth": "2018-09-02",
                "sex": "m",
            },
            guardian={
                "full_name": "Juma Ally",
                "phone": "0754118902",
                "relationship": "father",
            },
        ),
        headers=admin,
    )

    assert second.status_code == 422
    assert "Standard 1 Blue is full" in second.json()["detail"]


async def test_a_level_with_no_published_fees_admits_nobody_and_leaves_nothing(
    client, setup, owner_session
) -> None:
    """The atomicity test, and the one that matters most.

    A level with no published structure cannot be billed, so the admission is
    refused — and the pupil record, the guardian and the enrolment created on
    the way to finding that out must all be gone. A child on the roll with no
    bill is invisible to every fee screen the school has.
    """
    level_id = await make_level(
        owner_session, school_id=setup["school_id"], code="STD2", name="Standard 2", order_index=2
    )
    group_id = await make_class_group(
        owner_session,
        school_id=setup["school_id"],
        academic_year_id=setup["year_id"],
        level_id=level_id,
        name="Standard 2 Blue",
    )
    await owner_session.commit()
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])

    response = await client.post(
        BY_HAND, json=_body(setup, class_group_id=str(group_id)), headers=admin
    )

    assert response.status_code == 422
    assert "no published fee structure" in response.json()["detail"]

    assert (
        await owner_session.scalar(
            text("SELECT count(*) FROM student WHERE school_id = :s"),
            {"s": setup["school_id"]},
        )
        == 0
    ), "no half-admitted pupil"
    assert (
        await owner_session.scalar(
            text("SELECT count(*) FROM guardian WHERE school_id = :s"),
            {"s": setup["school_id"]},
        )
        == 0
    ), "no guardian with no children"
    assert (
        await owner_session.scalar(
            text("SELECT count(*) FROM app_user WHERE phone = :p"), {"p": "0712445109"}
        )
        == 0
    ), "no login for a family that was not admitted"


async def test_a_teachers_own_child_does_not_cost_them_their_account(
    client, setup, owner_session
) -> None:
    """A class teacher whose child joins the school would otherwise be
    demoted to a guardian by the admission, losing the register and the marks
    their job depends on. They are linked as the child's guardian and keep
    their staff login."""
    await owner_session.execute(
        text("UPDATE app_user SET phone = :phone WHERE email = :email"),
        {"phone": "0754118902", "email": setup["teacher_email"]},
    )
    await owner_session.commit()
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])

    response = await client.post(
        BY_HAND,
        json=_body(
            setup,
            guardian={
                "full_name": "Grace Kessy",
                "phone": "0754118902",
                "relationship": "mother",
            },
        ),
        headers=admin,
    )

    assert response.status_code == 201, response.text
    assert response.json()["login"] == "left as class_teacher"

    role = await owner_session.scalar(
        text("SELECT role FROM app_user WHERE email = :email"),
        {"email": setup["teacher_email"]},
    )
    assert role == "class_teacher", "still has their job"

    # And the child is still theirs: the link is what the parent portal reads.
    linked = await owner_session.scalar(
        text(
            "SELECT count(*) FROM guardian_student gs JOIN guardian g ON g.id = gs.guardian_id "
            " WHERE g.phone = :phone"
        ),
        {"phone": "0754118902"},
    )
    assert linked == 1


async def test_the_number_already_on_file_is_shown_before_submitting(
    client, setup, owner_session
) -> None:
    """Whose number it is, and which children are theirs — enough for the
    registrar to ask the person in front of them whether it is the same
    family."""
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])
    await client.post(BY_HAND, json=_body(setup), headers=admin)

    found = await client.get(
        "/api/v1/guardians/at-number", params={"phone": "0712 445 109"}, headers=admin
    )

    assert found.status_code == 200, found.text
    body = found.json()
    assert body is not None, "spacing in a typed number must not hide a match"
    assert body["guardian"]["full_name"] == "Asha Mwanjala"
    assert [child["full_name"] for child in body["children"]] == ["Glory Mwakalebela"]
    assert body["children"][0]["class_name"] == "Standard 1 Blue"


async def test_a_number_nobody_uses_comes_back_empty(client, setup) -> None:
    """The ordinary answer, and it must not be an error: most numbers typed at
    an intake desk are new."""
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])

    found = await client.get(
        "/api/v1/guardians/at-number", params={"phone": "0689000111"}, headers=admin
    )

    assert found.status_code == 200
    assert found.json() is None


async def test_a_teacher_cannot_admit_a_child(client, setup) -> None:
    teacher = await staff_token(
        client, email=setup["teacher_email"], subdomain=setup["subdomain"]
    )

    response = await client.post(BY_HAND, json=_body(setup), headers=teacher)

    assert response.status_code == 403


async def test_a_birth_date_that_cannot_be_right_is_refused(client, setup) -> None:
    """A child born next year reaches every report, class list and age-banded
    fee the school ever produces."""
    admin = await staff_token(client, email=setup["admin_email"], subdomain=setup["subdomain"])

    response = await client.post(
        BY_HAND,
        json=_body(
            setup,
            child={
                "first_name": "Glory",
                "surname": "Mwakalebela",
                "date_of_birth": "2099-04-11",
                "sex": "f",
            },
        ),
        headers=admin,
    )

    assert response.status_code == 422
