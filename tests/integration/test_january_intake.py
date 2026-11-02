"""The whole January intake, start to finish, through the API only.

This is the roadmap's definition of done for Stage 1: *a January 2027 intake
can be run start to finish on this system by somebody who is not a developer.*
Every other test in this suite checks one rule. This one checks that the rules
compose — that a school can be set up from nothing, a family can apply, a
child can be enrolled, billed, paid for and read by their own parent, without
a developer opening a database at any point.

Two things are deliberately done outside the API, because they are not Stage
1's job: the school row itself and the first staff login. A school is onboarded
by the platform, and somebody has to be able to sign in before they can set
anything up. Everything after that — the year, its terms, the levels, the
ladder, the classes, the subjects, the fee structure, the intake, the invoices,
the receipts, the corrections and the parent portal — goes through HTTP.

It is one long test on purpose. The failure it exists to catch is a seam
between two steps that each pass alone, and a seam cannot be tested in
isolation by definition.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.security import hash_password
from app.main import app
from tests.conftest import make_school

PASSWORD = "Shule2027!"  # noqa: S105 - test fixture data
OTP = "483927"


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


@pytest.fixture
def fixed_code(monkeypatch) -> str:
    monkeypatch.setattr("app.services.auth.generate_otp", lambda *a, **k: OTP)
    return OTP


@pytest.fixture
async def school(owner_session):
    """A school and one account that can sign in. Nothing else.

    No year, no levels, no classes, no fee structure, no subjects — the state a
    school is actually in on the day it is handed the system.
    """
    suffix = uuid4().hex[:6]
    school_id = await make_school(owner_session, name="Mwenge Primary", subdomain=f"jan{suffix}")
    subdomain = await owner_session.scalar(
        text("SELECT subdomain FROM school WHERE id = :id"), {"id": school_id}
    )
    emails = {
        "admin": f"admin-{suffix}@mwenge.ac.tz",
        "bursar": f"bursar-{suffix}@mwenge.ac.tz",
        "teacher": f"grace-{suffix}@mwenge.ac.tz",
    }
    for name, role, email in (
        ("Datius Mwanga", "school_admin", emails["admin"]),
        ("Salome Kessy", "bursar", emails["bursar"]),
        ("Grace Kessy", "class_teacher", emails["teacher"]),
    ):
        await owner_session.execute(
            text(
                "INSERT INTO app_user (id, school_id, full_name, email, role, password_hash, "
                " must_change_password, is_active, failed_logins, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school, :name, :email, :role, :hash, false, "
                " true, 0, now(), now())"
            ),
            {
                "school": school_id,
                "name": name,
                "email": email,
                "role": role,
                "hash": hash_password(PASSWORD),
            },
        )
    await owner_session.commit()

    yield {"id": school_id, "subdomain": subdomain, **{f"{k}_email": v for k, v in emails.items()}}

    await owner_session.rollback()
    await owner_session.execute(text("DELETE FROM school WHERE id = :id"), {"id": school_id})
    await owner_session.commit()


def tenant(subdomain: str) -> dict[str, str]:
    return {"X-School-Subdomain": subdomain}


async def sign_in(client, *, identifier: str, subdomain: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": identifier, "password": PASSWORD},
        headers=tenant(subdomain),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}", **tenant(subdomain)}


async def sign_in_by_code(client, *, phone: str, subdomain: str, code: str) -> dict[str, str]:
    """How a parent actually gets in.

    A guardian created at the counter has no password — the admission makes
    the login, not a password somebody would then have to deliver to them. A
    one-time code to the number the school already holds is the whole
    credential, which is also why that number is changed by the office.
    """
    start = await client.post(
        "/api/v1/auth/otp/start",
        json={"phone": phone, "purpose": "login"},
        headers=tenant(subdomain),
    )
    assert start.status_code == 200, start.text

    done = await client.post(
        "/api/v1/auth/otp/login",
        json={"phone": phone, "purpose": "login", "code": code},
        headers=tenant(subdomain),
    )
    assert done.status_code == 200, done.text
    assert done.json()["principal"]["role"] == "guardian"
    return {"Authorization": f"Bearer {done.json()['access_token']}", **tenant(subdomain)}


async def test_a_january_intake_runs_start_to_finish(
    client, school, fixed_code, owner_session
) -> None:
    """Fifteen steps, no SQL after the fixture."""
    sub = school["subdomain"]
    admin = await sign_in(client, identifier=school["admin_email"], subdomain=sub)
    bursar = await sign_in(client, identifier=school["bursar_email"], subdomain=sub)

    # ------------------------------------------------------------------ 1
    # The school says who it is, and where fees are paid. Payments are manual
    # in Phase 1, so the bank details are the only instruction it can give.
    settings = await client.patch(
        "/api/v1/schools/me",
        json={
            "admission_no_format": "MPS/{year}/{sequence:04d}",
            "deposit_percent": 30,
            "invoice_grace_days": 14,
            "bank_name": "NMB, Mwenge branch",
            "bank_account_name": "Mwenge Primary School",
            "bank_account_number": "2011 4409 118",
        },
        headers=admin,
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["bank_account_number"] == "2011 4409 118"

    # ------------------------------------------------------------------ 2
    # A field only this school needs. It appears, empty, on every pupil.
    field = await client.post(
        "/api/v1/schools/me/fields",
        json={
            "key": "bus_route",
            "label": "Bus route",
            "kind": "choice",
            "options": ["Kimara", "Mbezi"],
        },
        headers=admin,
    )
    assert field.status_code == 201, field.text

    # ------------------------------------------------------------------ 3
    # The year. It does not become current on creation: that is a separate
    # decision, and it needs terms first.
    year = await client.post(
        "/api/v1/academic/years",
        json={
            "label": "2027",
            "starts_on": "2027-01-11",
            "ends_on": "2027-12-03",
            "make_current": False,
        },
        headers=admin,
    )
    assert year.status_code == 201, year.text
    year_id = year.json()["id"]
    assert year.json()["is_current"] is False

    # ------------------------------------------------------------------ 4
    # Three terms, which decide when fees fall due and which term a mark
    # belongs to. Overlapping ones are refused.
    overlapping = await client.put(
        f"/api/v1/academic/years/{year_id}/terms",
        json={
            "terms": [
                {"index": 1, "label": "Term 1", "starts_on": "2027-01-11", "ends_on": "2027-05-10"},
                {"index": 2, "label": "Term 2", "starts_on": "2027-05-03", "ends_on": "2027-08-06"},
            ]
        },
        headers=admin,
    )
    assert overlapping.status_code == 422, "two terms covering one day is unanswerable"

    terms = await client.put(
        f"/api/v1/academic/years/{year_id}/terms",
        json={
            "terms": [
                {"index": 1, "label": "Term 1", "starts_on": "2027-01-11", "ends_on": "2027-04-09"},
                {"index": 2, "label": "Term 2", "starts_on": "2027-05-03", "ends_on": "2027-08-06"},
                {"index": 3, "label": "Term 3", "starts_on": "2027-09-06", "ends_on": "2027-12-03"},
            ]
        },
        headers=admin,
    )
    assert terms.status_code == 200, terms.text
    term_one = next(term for term in terms.json() if term["index"] == 1)

    moved = await client.post(f"/api/v1/academic/years/{year_id}/current", headers=admin)
    assert moved.status_code == 200, moved.text
    made_current = await client.post(
        f"/api/v1/academic/terms/{term_one['id']}/current", headers=admin
    )
    assert made_current.status_code == 200, made_current.text

    # ------------------------------------------------------------------ 5
    # The ladder. Two levels is enough to prove promotion has somewhere to go
    # and that the top of it has to be told what happens there.
    levels = {}
    for code, name, order in (("STD1", "Standard 1", 1), ("STD2", "Standard 2", 2)):
        created = await client.post(
            "/api/v1/academic/levels",
            json={"code": code, "name": name, "stage": "primary", "order_index": order},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        levels[code] = next(
            level["id"] for level in created.json()["levels"] if level["code"] == code
        )

    ladder = await client.get("/api/v1/academic/ladder", headers=admin)
    assert ladder.status_code == 200, ladder.text
    # Standard 2 has nothing above it and is not marked as leaving, which the
    # promotion run refuses to guess at.
    assert ladder.json()["problems"], "the top of the ladder is undefined and says so"

    exits = await client.put(
        f"/api/v1/academic/years/{year_id}/exit-levels",
        json={"levels": [{"level_id": levels["STD2"], "note": "sits the exam"}]},
        headers=admin,
    )
    assert exits.status_code == 200, exits.text
    assert exits.json()["problems"] == [], "with an exit recorded, the ladder is complete"

    # ------------------------------------------------------------------ 6
    # Classes, with a place limit. A class is where a child is enrolled.
    blue = await client.post(
        "/api/v1/academic/class-groups",
        json={"academic_year_id": year_id, "level_id": levels["STD1"], "capacity": 2},
        headers=admin,
    )
    assert blue.status_code == 201, blue.text
    blue_id = blue.json()["id"]
    assert blue.json()["name"] == "Standard 1"

    teacher_id = next(
        person["id"]
        for person in (await client.get("/api/v1/academic/teaching-staff", headers=admin)).json()
        if person["full_name"] == "Grace Kessy"
    )
    assigned = await client.put(
        f"/api/v1/academic/class-groups/{blue_id}/class-teacher",
        json={"teacher_id": teacher_id},
        headers=admin,
    )
    assert assigned.status_code == 200, assigned.text

    # ------------------------------------------------------------------ 7
    # A subject, so somebody could enter a mark later.
    subject = await client.post(
        "/api/v1/academic/subjects",
        json={
            "code": "KIS",
            "name": "Kiswahili",
            "is_core": True,
            "is_examinable": True,
            "levels": [{"level_id": levels["STD1"], "periods_per_week": 5}],
        },
        headers=admin,
    )
    assert subject.status_code == 201, subject.text

    # ------------------------------------------------------------------ 8
    # What Standard 1 costs. Nothing can be billed from a draft.
    structure = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": year_id,
            "level_id": levels["STD1"],
            "label": "Standard 1 2027",
            "items": [],
        },
        headers=bursar,
    )
    assert structure.status_code == 201, structure.text
    structure_id = structure.json()["structure"]["id"]

    empty = await client.post(
        f"/api/v1/fees/structures/{structure_id}/publish", headers=bursar
    )
    assert empty.status_code == 422, "an empty structure would bill every child nothing"

    items = await client.put(
        f"/api/v1/fees/structures/{structure_id}/items",
        json={
            "items": [
                {
                    "name": "Tuition",
                    "amount": "300000",
                    "category": "tuition",
                    "frequency": "per_term",
                },
                {
                    "name": "Admission fee",
                    "amount": "100000",
                    "category": "admission",
                    "frequency": "one_off",
                },
            ]
        },
        headers=bursar,
    )
    assert items.status_code == 200, items.text

    published = await client.post(
        f"/api/v1/fees/structures/{structure_id}/publish", headers=bursar
    )
    assert published.status_code == 200, published.text

    frozen = await client.put(
        f"/api/v1/fees/structures/{structure_id}/items",
        json={"items": [{"name": "Tuition", "amount": "1", "category": "tuition",
                         "frequency": "per_term"}]},
        headers=bursar,
    )
    assert frozen.status_code == 422, "a published structure cannot change retrospectively"

    # ------------------------------------------------------------------ 9
    # A family applies on their own phone. A verified number is the only
    # credential they have.
    start = await client.post(
        "/api/v1/apply/start", json={"phone": "0712998877"}, headers=tenant(sub)
    )
    assert start.status_code == 200, start.text
    verified = await client.post(
        "/api/v1/apply/verify",
        json={"phone": "0712998877", "code": fixed_code},
        headers=tenant(sub),
    )
    assert verified.status_code == 200, verified.text
    parent = {
        "Authorization": f"Bearer {verified.json()['access_token']}",
        **tenant(sub),
    }

    await client.patch(
        "/api/v1/apply/me",
        json={
            "child_first_name": "Happiness",
            "child_surname": "Mollel",
            "date_of_birth": "2020-11-30",
            "sex": "f",
            "level_applied_id": levels["STD1"],
            "guardian_name": "Neema Mollel",
            "guardian_phone": "0712998877",
            "guardian_relationship": "mother",
        },
        headers=parent,
    )
    submitted = await client.post("/api/v1/apply/me/submit", headers=parent)
    assert submitted.status_code == 200, submitted.text

    # ----------------------------------------------------------------- 10
    # Screened, offered, paid for, enrolled.
    application_id = (await client.get("/api/v1/admissions", headers=admin)).json()["items"][0][
        "id"
    ]
    await client.post(
        f"/api/v1/admissions/{application_id}/screen",
        json={"note": "No duplicate found"},
        headers=admin,
    )
    offered = await client.post(f"/api/v1/admissions/{application_id}/offer", headers=admin)
    assert offered.status_code == 200, offered.text
    # 100,000 joining in full, plus 30% of the 300,000 first term.
    assert Decimal(offered.json()["deposit_billed"]) == Decimal("190000.00")

    deposit = await client.post(
        f"/api/v1/admissions/{application_id}/deposit",
        json={
            "amount": offered.json()["deposit_billed"],
            "receipt": "RCPT-0001",
            "channel": "mpesa",
            "msisdn": "0712998877",
        },
        headers=bursar,
    )
    assert deposit.status_code == 200, deposit.text
    assert deposit.json()["application_state"] == "accepted"

    enrolled = await client.post(
        f"/api/v1/admissions/{application_id}/enrol",
        json={"class_group_id": blue_id, "residency": "day"},
        headers=admin,
    )
    assert enrolled.status_code == 201, enrolled.text
    applied_child = enrolled.json()["student_id"]

    # ----------------------------------------------------------------- 11
    # And a family at the counter who never used the form. That fills the
    # class, which has two places.
    by_hand = await client.post(
        "/api/v1/admissions/by-hand",
        json={
            "child": {
                "first_name": "Baraka",
                "surname": "Mwanjala",
                "date_of_birth": "2020-04-11",
                "sex": "m",
            },
            "class_group_id": blue_id,
            "guardian": {
                "full_name": "Asha Mwanjala",
                "phone": "0754118902",
                "relationship": "mother",
            },
        },
        headers=admin,
    )
    assert by_hand.status_code == 201, by_hand.text
    counter_child = by_hand.json()["student_id"]
    # The term's charges and the one-off joining items, since no deposit is
    # taken on this path.
    assert Decimal(by_hand.json()["billed"]) == Decimal("400000.00")
    assert by_hand.json()["login"] == "created"

    full = await client.post(
        "/api/v1/admissions/by-hand",
        json={
            "child": {
                "first_name": "Third",
                "surname": "Child",
                "date_of_birth": "2020-04-11",
                "sex": "f",
            },
            "class_group_id": blue_id,
            "guardian": {
                "full_name": "Somebody Else",
                "phone": "0689000111",
                "relationship": "father",
            },
        },
        headers=admin,
    )
    assert full.status_code == 422
    assert "is full" in full.json()["detail"]

    opened_up = await client.patch(
        f"/api/v1/academic/class-groups/{blue_id}", json={"capacity": 40}, headers=admin
    )
    assert opened_up.status_code == 200, opened_up.text

    # ----------------------------------------------------------------- 12
    # The term run. Previewed first, then raised, then run again to prove it
    # cannot double-bill.
    preview = await client.post(
        f"/api/v1/fees/terms/{term_one['id']}/run",
        json={"dry_run": True},
        headers=bursar,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["problems"] == []
    # The child admitted at the counter already has this term's invoice, so
    # only the one who came through the funnel is left to bill.
    assert preview.json()["raised"] == 1, preview.json()["decisions"]

    run = await client.post(
        f"/api/v1/fees/terms/{term_one['id']}/run",
        json={"dry_run": False},
        headers=bursar,
    )
    assert run.status_code == 200, run.text
    assert run.json()["raised"] == 1

    again = await client.post(
        f"/api/v1/fees/terms/{term_one['id']}/run",
        json={"dry_run": False},
        headers=bursar,
    )
    assert again.json()["raised"] == 0, "running it twice must not double-bill a school"

    # ----------------------------------------------------------------- 13
    # A receipt at the counter, entered against the bill.
    statement = await client.get(
        f"/api/v1/fees/students/{counter_child}/statement", headers=bursar
    )
    assert statement.status_code == 200, statement.text
    invoice_id = statement.json()["invoices"][0]["id"]
    owed = Decimal(statement.json()["balance"])
    assert owed == Decimal("400000.00")

    receipt = await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments",
        json={
            "amount": "150000",
            "receipt": "RCPT-0002",
            "channel": "counter",
            "received_on": "2027-01-15",
            "msisdn": None,
            "note": "Part payment at the office",
        },
        headers=bursar,
    )
    assert receipt.status_code == 201, receipt.text

    twice = await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments",
        json={
            "amount": "150000",
            "receipt": "RCPT-0002",
            "channel": "counter",
            "received_on": "2027-01-15",
            "msisdn": None,
            "note": "The same slip entered again",
        },
        headers=bursar,
    )
    assert twice.status_code == 422, "the same receipt number cannot be banked twice"

    after = await client.get(
        f"/api/v1/fees/students/{counter_child}/statement", headers=bursar
    )
    assert Decimal(after.json()["balance"]) == Decimal("250000.00")

    # ----------------------------------------------------------------- 14
    # The record is corrected: a second guardian, the school's own field, a
    # medical note, and a move to another class.
    second = await client.post(
        f"/api/v1/students/{counter_child}/guardians",
        json={
            "guardian": {"full_name": "Juma Mwanjala", "phone": "0755000222"},
            "link": {"relationship": "father", "is_primary": False},
        },
        headers=admin,
    )
    assert second.status_code == 201, second.text

    valued = await client.put(
        f"/api/v1/students/{counter_child}/fields",
        json={"values": {"bus_route": "Kimara"}},
        headers=admin,
    )
    assert valued.status_code in (200, 204), valued.text

    health = await client.put(
        f"/api/v1/students/{counter_child}/health",
        json={"allergies": "Peanuts", "emergency_plan": "Inhaler in the office"},
        headers=admin,
    )
    assert health.status_code == 200, health.text

    hidden = await client.get(f"/api/v1/students/{counter_child}/health", headers=bursar)
    assert hidden.status_code == 403, "a bursar chasing arrears has no business with it"

    green = await client.post(
        "/api/v1/academic/class-groups",
        json={"academic_year_id": year_id, "level_id": levels["STD2"], "capacity": 30},
        headers=admin,
    )
    assert green.status_code == 201, green.text

    enrolment_id = (
        await client.get(f"/api/v1/students/{counter_child}", headers=admin)
    ).json()["enrolment"]["id"]
    changed = await client.patch(
        f"/api/v1/enrolments/{enrolment_id}",
        json={"class_group_id": green.json()["id"]},
        headers=admin,
    )
    assert changed.status_code == 200, changed.text

    # ----------------------------------------------------------------- 15
    # And the family can read it. This is the end of the thread: a parent who
    # has paid at a counter signs in on their own phone and sees it.
    guardian = await sign_in_by_code(
        client, phone="0754118902", subdomain=sub, code=fixed_code
    )

    children = await client.get("/api/v1/guardians/me/children", headers=guardian)
    assert children.status_code == 200, children.text
    assert [child["full_name"] for child in children.json()] == ["Baraka Mwanjala"]
    assert Decimal(children.json()[0]["balance"]) == Decimal("250000.00")
    assert children.json()[0]["class_name"] == "Standard 2"

    theirs = await client.get(
        f"/api/v1/fees/students/{counter_child}/statement", headers=guardian
    )
    assert theirs.status_code == 200, "the question the portal exists to answer"

    someone_elses = await client.get(
        f"/api/v1/fees/students/{applied_child}/statement", headers=guardian
    )
    assert someone_elses.status_code == 404, "and only their own child"

    ledger = await client.get("/api/v1/fees/invoices", headers=guardian)
    assert ledger.status_code == 403, "reading their own bills is not reading the school's"

    # Two messages went out: one per enrolled child, to the number that will
    # be used to sign in.
    told = (
        (
            await owner_session.execute(
                text(
                    "SELECT note FROM audit_event WHERE school_id = :school "
                    "   AND action = 'notification.enrolment_confirmed' ORDER BY created_at"
                ),
                {"school": school["id"]},
            )
        )
        .scalars()
        .all()
    )
    assert len(told) == 2, told
    assert any("0712998877" in note for note in told)
    assert any("0754118902" in note for note in told)
