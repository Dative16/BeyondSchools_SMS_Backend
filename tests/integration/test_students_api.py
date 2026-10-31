"""Students, enrolments and guardians through the real app.

What these cover that the unit tests cannot: who may see a child, that a
guardian's children are a question about rows rather than claims, and that
health sits behind its own capability rather than riding along with the rest
of a profile.
"""

from datetime import date, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.security import hash_password
from app.main import app
from tests.conftest import make_academic_year, make_class_group, make_level, make_school

PASSWORD = "Shule2027!"  # noqa: S105 - test fixture data


@pytest.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _add_user(session, *, school_id, name, role, email=None, phone=None, guardian_id=None):
    user_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO app_user (id, school_id, full_name, email, phone, role, "
            " password_hash, must_change_password, is_active, failed_logins, guardian_id, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, :name, :email, :phone, :role, :hash, false, true, 0, "
            " :guardian_id, now(), now())"
        ),
        {
            "id": user_id,
            "school_id": school_id,
            "name": name,
            "email": email,
            "phone": phone,
            "role": role,
            "hash": hash_password(PASSWORD),
            "guardian_id": guardian_id,
        },
    )
    return user_id


@pytest.fixture
async def setup(owner_session):
    suffix = uuid4().hex[:6]
    school_id = await make_school(owner_session, name="Mwenge", subdomain=f"stud{suffix}")
    subdomain = await owner_session.scalar(
        text("SELECT subdomain FROM school WHERE id = :id"), {"id": school_id}
    )

    year_id = await make_academic_year(owner_session, school_id=school_id)
    level_id = await make_level(owner_session, school_id=school_id, code="STD4", order_index=4)
    blue = await make_class_group(
        owner_session,
        school_id=school_id,
        academic_year_id=year_id,
        level_id=level_id,
        name="Standard 4 Blue",
    )
    green = await make_class_group(
        owner_session,
        school_id=school_id,
        academic_year_id=year_id,
        level_id=level_id,
        name="Standard 4 Green",
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
    teacher_id = await _add_user(
        owner_session,
        school_id=school_id,
        name="Grace",
        role="class_teacher",
        email=emails["teacher"],
    )
    # The teacher gets Blue, and only Blue.
    await owner_session.execute(
        text("UPDATE class_group SET class_teacher_id = :t WHERE id = :id"),
        {"t": teacher_id, "id": blue},
    )

    # One document requirement, so the checklist has something to say.
    await owner_session.execute(
        text(
            "INSERT INTO document_requirement (id, school_id, kind, label, is_required, "
            " boarders_only, ask_at_application, sort_order, is_active, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, 'birth_certificate', 'Birth certificate', "
            " true, false, true, 0, true, now(), now()), "
            " (gen_random_uuid(), :school_id, 'medical', 'Boarder health form', true, true, "
            " false, 1, true, now(), now())"
        ),
        {"school_id": school_id},
    )
    await owner_session.execute(
        text(
            "INSERT INTO school_field (id, school_id, key, label, kind, scope, options, "
            " required_at_registration, is_sensitive, is_active, sort_order, "
            " created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, 'uniform_size', 'Uniform size', 'choice', "
            " 'student', ARRAY['S','M','L'], false, false, true, 0, now(), now())"
        ),
        {"school_id": school_id},
    )
    await owner_session.commit()

    yield {
        "school_id": school_id,
        "subdomain": subdomain,
        "year_id": year_id,
        "level_id": level_id,
        "blue": blue,
        "green": green,
        "teacher_id": teacher_id,
        **{f"{k}_email": v for k, v in emails.items()},
    }

    await owner_session.rollback()
    await owner_session.execute(text("DELETE FROM school WHERE id = :id"), {"id": school_id})
    await owner_session.commit()


async def token(client, *, email, subdomain) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": PASSWORD},
        headers={"X-School-Subdomain": subdomain},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def admin(client, setup):
    return await token(client, email=setup["admin_email"], subdomain=setup["subdomain"])


async def admit(client, admin, *, first="Neema", surname="Mwanjala") -> str:
    response = await client.post(
        "/api/v1/students",
        json={
            "first_name": first,
            "surname": surname,
            "date_of_birth": "2018-06-08",
            "sex": "f",
        },
        headers=admin,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def enrol(client, admin, setup, student_id, *, class_group=None, residency="day") -> str:
    response = await client.post(
        "/api/v1/enrolments",
        json={
            "student_id": student_id,
            "academic_year_id": str(setup["year_id"]),
            "class_group_id": str(class_group or setup["blue"]),
            "residency": residency,
        },
        headers=admin,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------------------
# Admission numbers
# --------------------------------------------------------------------------
async def test_admission_numbers_come_from_a_sequence_not_the_form(client, setup, admin) -> None:
    """Issued server-side from a locked counter, so two registrars admitting
    in the same second cannot mint the same number."""
    first = await admit(client, admin, first="Neema")
    second = await admit(client, admin, first="Baraka")

    numbers = []
    for student_id in (first, second):
        response = await client.get(f"/api/v1/students/{student_id}", headers=admin)
        numbers.append(response.json()["student"]["admission_no"])

    assert len(set(numbers)) == 2
    assert all(number.endswith(("0001", "0002")) for number in numbers)


async def test_a_new_child_is_prospective_until_enrolled(client, setup, admin) -> None:
    student_id = await admit(client, admin)

    before = await client.get(f"/api/v1/students/{student_id}", headers=admin)
    assert before.json()["student"]["status"] == "prospective"

    await enrol(client, admin, setup, student_id)

    after = await client.get(f"/api/v1/students/{student_id}", headers=admin)
    assert after.json()["student"]["status"] == "active"
    assert after.json()["class_name"] == "Standard 4 Blue"


# --------------------------------------------------------------------------
# One active enrolment
# --------------------------------------------------------------------------
async def test_a_second_open_enrolment_is_refused_with_an_explanation(client, setup, admin) -> None:
    """Rather than quietly rewriting last year's row, which is the whole
    reason enrolments are separate records."""
    student_id = await admit(client, admin)
    await enrol(client, admin, setup, student_id)

    again = await client.post(
        "/api/v1/enrolments",
        json={
            "student_id": student_id,
            "academic_year_id": str(setup["year_id"]),
            "class_group_id": str(setup["green"]),
        },
        headers=admin,
    )

    assert again.status_code == 422
    assert "already has an open enrolment" in again.json()["detail"]


async def test_boarding_and_bus_are_per_year_facts(client, setup, admin) -> None:
    student_id = await admit(client, admin)
    enrolment_id = await enrol(client, admin, setup, student_id)

    response = await client.patch(
        f"/api/v1/enrolments/{enrolment_id}",
        json={
            "residency": "boarder",
            "dormitory": "Uhuru",
            "uses_transport": True,
            "transport_route": "Majengo",
            "transport_stop": "Majengo A",
        },
        headers=admin,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["residency"] == "boarder"
    assert body["dormitory"] == "Uhuru"
    assert body["transport_route"] == "Majengo"


async def test_transfer_out_closes_the_enrolment_and_keeps_the_record(client, setup, admin) -> None:
    """A school asked for a transfer letter two years later has to answer, and
    arrears do not vanish because a child left."""
    student_id = await admit(client, admin)
    await enrol(client, admin, setup, student_id)

    response = await client.post(
        f"/api/v1/students/{student_id}/transfer-out",
        json={"reason": "Family moved to Dodoma"},
        headers=admin,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "transferred_out"

    history = await client.get(f"/api/v1/students/{student_id}/enrolments", headers=admin)
    assert history.json()[0]["outcome"] == "transferred_out"
    assert history.json()[0]["exited_on"] is not None


# --------------------------------------------------------------------------
# The roll
# --------------------------------------------------------------------------
async def test_the_roll_searches_by_name_and_admission_number(client, setup, admin) -> None:
    await admit(client, admin, first="Neema", surname="Mwanjala")
    await admit(client, admin, first="Baraka", surname="Shayo")

    by_name = await client.get("/api/v1/students", params={"q": "Shayo"}, headers=admin)
    assert by_name.json()["total"] == 1

    number = by_name.json()["items"][0]["admission_no"]
    by_number = await client.get("/api/v1/students", params={"q": number}, headers=admin)
    assert by_number.json()["total"] == 1


async def test_the_roll_filters_by_boarding_and_transport(client, setup, admin) -> None:
    day = await admit(client, admin, first="Joyce")
    await enrol(client, admin, setup, day)
    boarder = await admit(client, admin, first="Amani")
    await enrol(client, admin, setup, boarder, residency="boarder")

    boarders = await client.get("/api/v1/students", params={"residency": "boarder"}, headers=admin)

    assert boarders.json()["total"] == 1
    assert boarders.json()["items"][0]["first_name"] == "Amani"


async def test_the_roll_is_paginated(client, setup, admin) -> None:
    for index in range(4):
        await admit(client, admin, first=f"Child{index}")

    page = await client.get("/api/v1/students", params={"size": 2, "page": 2}, headers=admin)

    body = page.json()
    assert body["total"] == 4
    assert len(body["items"]) == 2
    assert body["page"] == 2


async def test_a_teacher_sees_only_their_own_class(client, setup, admin) -> None:
    """Narrowed by the service, not by the client: a list endpoint that
    returns the school and trusts the caller to filter is one that leaks."""
    mine = await admit(client, admin, first="Mine")
    await enrol(client, admin, setup, mine, class_group=setup["blue"])
    theirs = await admit(client, admin, first="Theirs")
    await enrol(client, admin, setup, theirs, class_group=setup["green"])

    teacher = await token(client, email=setup["teacher_email"], subdomain=setup["subdomain"])
    response = await client.get("/api/v1/students", headers=teacher)

    names = [row["first_name"] for row in response.json()["items"]]
    assert names == ["Mine"]

    asking_for_another = await client.get(
        "/api/v1/students", params={"class_group_id": str(setup["green"])}, headers=teacher
    )
    assert asking_for_another.status_code == 403


async def test_a_teacher_cannot_open_a_child_from_another_class(client, setup, admin) -> None:
    theirs = await admit(client, admin, first="Theirs")
    await enrol(client, admin, setup, theirs, class_group=setup["green"])
    teacher = await token(client, email=setup["teacher_email"], subdomain=setup["subdomain"])

    response = await client.get(f"/api/v1/students/{theirs}", headers=teacher)

    assert response.status_code == 403
    assert "not in one of your classes" in response.json()["detail"]


# --------------------------------------------------------------------------
# Guardians
# --------------------------------------------------------------------------
async def test_one_guardian_three_children(client, setup, admin) -> None:
    """A parent with three children at the school is one guardian and three
    links. Without that, the family gets three fee reminders and no combined
    balance."""
    phone = f"0754{uuid4().int % 1000000:06d}"
    guardian_ids = set()
    for name in ("Neema", "Baraka", "Joyce"):
        student_id = await admit(client, admin, first=name, surname="Shayo")
        response = await client.post(
            f"/api/v1/students/{student_id}/guardians",
            json={
                "guardian": {"full_name": "Juma Shayo", "phone": phone},
                "link": {"relationship": "father", "is_primary": True},
            },
            headers=admin,
        )
        assert response.status_code == 201, response.text
        guardian_ids.add(response.json()["guardian_id"])

    assert len(guardian_ids) == 1, "matched on phone, so it is the same person"


async def test_a_phone_written_three_ways_is_one_guardian(client, setup, admin) -> None:
    first = await admit(client, admin, first="Neema")
    second = await admit(client, admin, first="Baraka")

    a = await client.post(
        f"/api/v1/students/{first}/guardians",
        json={
            "guardian": {"full_name": "Asha Mwanjala", "phone": "0754118902"},
            "link": {"relationship": "mother"},
        },
        headers=admin,
    )
    b = await client.post(
        f"/api/v1/students/{second}/guardians",
        json={
            "guardian": {"full_name": "Asha Mwanjala", "phone": "+255754118902"},
            "link": {"relationship": "mother"},
        },
        headers=admin,
    )

    assert a.json()["guardian_id"] == b.json()["guardian_id"]


async def test_one_primary_guardian_per_child(client, setup, admin) -> None:
    student_id = await admit(client, admin)
    for name, phone in (("Asha", "0754100001"), ("Said", "0754100002")):
        response = await client.post(
            f"/api/v1/students/{student_id}/guardians",
            json={
                "guardian": {"full_name": f"{name} Mwanjala", "phone": phone},
                "link": {"relationship": "mother", "is_primary": True},
            },
            headers=admin,
        )
        assert response.status_code == 201, response.text

    guardians = await client.get(f"/api/v1/students/{student_id}/guardians", headers=admin)

    primaries = [row for row in guardians.json() if row["is_primary"]]
    assert len(primaries) == 1
    assert primaries[0]["full_name"] == "Said Mwanjala", "the newest one takes over"


async def test_the_last_guardian_cannot_be_unlinked(client, setup, admin) -> None:
    """A child with nobody to contact is the one state this must not produce."""
    student_id = await admit(client, admin)
    linked = await client.post(
        f"/api/v1/students/{student_id}/guardians",
        json={
            "guardian": {"full_name": "Asha Mwanjala", "phone": "0754100003"},
            "link": {"relationship": "mother", "is_primary": True},
        },
        headers=admin,
    )

    response = await client.delete(
        f"/api/v1/students/{student_id}/guardians/{linked.json()['guardian_id']}",
        headers=admin,
    )

    assert response.status_code == 422
    assert "only guardian" in response.json()["detail"]


async def test_a_guardian_sees_their_own_children_and_nobody_elses(
    client, setup, admin, owner_session
) -> None:
    """Which children are a guardian's is a question about rows, so it cannot
    be answered from a token and is answered with a query every time."""
    mine = await admit(client, admin, first="Mine")
    theirs = await admit(client, admin, first="Theirs")

    phone = f"0755{uuid4().int % 1000000:06d}"
    linked = await client.post(
        f"/api/v1/students/{mine}/guardians",
        json={
            "guardian": {"full_name": "Asha Mwanjala", "phone": phone},
            "link": {"relationship": "mother", "is_primary": True},
        },
        headers=admin,
    )
    guardian_id = linked.json()["guardian_id"]
    await _add_user(
        owner_session,
        school_id=setup["school_id"],
        name="Asha Mwanjala",
        role="guardian",
        phone=phone,
        guardian_id=guardian_id,
    )
    await owner_session.commit()

    guardian = await token(client, email=phone, subdomain=setup["subdomain"])

    children = await client.get("/api/v1/guardians/me/children", headers=guardian)
    assert [child["full_name"] for child in children.json()] == ["Mine Mwanjala"]

    own = await client.get(f"/api/v1/students/{mine}", headers=guardian)
    assert own.status_code == 200

    # A 404, not a 403: a guardian probing ids must not be able to tell a
    # child who exists from one who does not.
    other = await client.get(f"/api/v1/students/{theirs}", headers=guardian)
    assert other.status_code == 404


async def _family(client, admin, setup, owner_session, *, first="Neema"):
    """A child on the roll, a guardian with a login, a pupil with a login."""
    student_id = await admit(client, admin, first=first)
    await enrol(client, admin, setup, student_id)
    phone = f"0755{uuid4().int % 1000000:06d}"
    linked = await client.post(
        f"/api/v1/students/{student_id}/guardians",
        json={
            "guardian": {"full_name": "Asha Mwanjala", "phone": phone},
            "link": {"relationship": "mother", "is_primary": True},
        },
        headers=admin,
    )
    await _add_user(
        owner_session,
        school_id=setup["school_id"],
        name="Asha Mwanjala",
        role="guardian",
        phone=phone,
        guardian_id=linked.json()["guardian_id"],
    )
    pupil_email = f"pupil-{uuid4().hex[:6]}@mwenge.ac.tz"
    await owner_session.execute(
        text(
            "INSERT INTO app_user (id, school_id, full_name, email, role, password_hash, "
            " must_change_password, is_active, failed_logins, student_id, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school, :name, :email, 'student', :hash, false, "
            " true, 0, :student, now(), now())"
        ),
        {
            "school": setup["school_id"],
            "name": f"{first} Mwanjala",
            "email": pupil_email,
            "hash": hash_password(PASSWORD),
            "student": student_id,
        },
    )
    await owner_session.commit()
    return {
        "student_id": student_id,
        "guardian_phone": phone,
        "pupil_email": pupil_email,
    }


# --------------------------------------------------------------------------
# The family's own portal
# --------------------------------------------------------------------------
async def test_a_pupil_can_read_their_own_record(
    client, setup, admin, owner_session
) -> None:
    """`may_see_student` has always allowed this and never got the chance to:
    the STUDENT_READ check above it rejected every student before the question
    was asked, so a pupil could not open their own file and the branch that
    allowed it was unreachable."""
    family = await _family(client, admin, setup, owner_session)
    pupil = await token(client, email=family["pupil_email"], subdomain=setup["subdomain"])

    own = await client.get(f"/api/v1/students/{family['student_id']}", headers=pupil)

    assert own.status_code == 200, own.text
    body = own.json()
    assert body["student"]["first_name"] == "Neema"
    assert body["class_name"], "their own class, which the school page shows"
    # And not the sensitive parts: the profile hands those out only on
    # HEALTH_READ, which a pupil does not have.
    assert body["health"] is None
    assert body["emergency_contacts"] == []


async def test_a_pupil_cannot_read_another_pupils_record(
    client, setup, admin, owner_session
) -> None:
    """A 404, not a 403: probing ids must not teach a pupil who exists."""
    family = await _family(client, admin, setup, owner_session)
    someone_else = await admit(client, admin, first="Baraka")
    pupil = await token(client, email=family["pupil_email"], subdomain=setup["subdomain"])

    other = await client.get(f"/api/v1/students/{someone_else}", headers=pupil)

    assert other.status_code == 404


async def test_a_guardian_can_read_their_childs_statement(
    client, setup, admin, owner_session
) -> None:
    """The parent portal's main button. It needed INVOICE_READ, which a
    guardian does not have, so the one question the portal exists to answer —
    what do I owe, and for which child — was forbidden to the person asking."""
    family = await _family(client, admin, setup, owner_session)
    guardian = await token(
        client, email=family["guardian_phone"], subdomain=setup["subdomain"]
    )

    statement = await client.get(
        f"/api/v1/fees/students/{family['student_id']}/statement", headers=guardian
    )

    assert statement.status_code == 200, statement.text
    assert statement.json()["student_id"] == str(family["student_id"])


async def test_a_pupil_can_read_their_own_statement(
    client, setup, admin, owner_session
) -> None:
    family = await _family(client, admin, setup, owner_session)
    pupil = await token(client, email=family["pupil_email"], subdomain=setup["subdomain"])

    statement = await client.get(
        f"/api/v1/fees/students/{family['student_id']}/statement", headers=pupil
    )

    assert statement.status_code == 200, statement.text


async def test_a_guardian_cannot_read_another_familys_statement(
    client, setup, admin, owner_session
) -> None:
    """The check is a query against the link table, not a claim in the token:
    which children are a guardian's is a question about rows."""
    family = await _family(client, admin, setup, owner_session)
    someone_else = await admit(client, admin, first="Baraka")
    guardian = await token(
        client, email=family["guardian_phone"], subdomain=setup["subdomain"]
    )

    statement = await client.get(
        f"/api/v1/fees/students/{someone_else}/statement", headers=guardian
    )

    assert statement.status_code == 404


async def test_a_guardian_still_cannot_read_the_whole_schools_invoices(
    client, setup, admin, owner_session
) -> None:
    """Widening the statement must not widen anything else. A parent reading
    their own child's bills is not a parent reading the school's ledger."""
    family = await _family(client, admin, setup, owner_session)
    guardian = await token(
        client, email=family["guardian_phone"], subdomain=setup["subdomain"]
    )

    for path in (
        "/api/v1/fees/invoices",
        "/api/v1/fees/arrears",
        "/api/v1/fees/collection",
        "/api/v1/students",
    ):
        refused = await client.get(path, headers=guardian)
        assert refused.status_code == 403, f"{path} answered {refused.status_code}"


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------
async def test_a_bursar_cannot_read_a_childs_health(client, setup, admin) -> None:
    """Children's health data is sensitive personal data under the Personal
    Data Protection Act, 2022. A bursar chasing arrears has no business with
    it."""
    student_id = await admit(client, admin)
    await client.put(
        f"/api/v1/students/{student_id}/health",
        json={"allergies": "Peanuts", "emergency_plan": "Inhaler in the office"},
        headers=admin,
    )

    bursar = await token(client, email=setup["bursar_email"], subdomain=setup["subdomain"])

    direct = await client.get(f"/api/v1/students/{student_id}/health", headers=bursar)
    assert direct.status_code == 403

    profile = await client.get(f"/api/v1/students/{student_id}", headers=bursar)
    assert profile.status_code == 200, "the rest of the record is still readable"
    assert profile.json()["health"] is None


async def test_a_class_teacher_can_read_health_for_their_own_class(client, setup, admin) -> None:
    """An asthma plan is no use locked in an office."""
    student_id = await admit(client, admin)
    await enrol(client, admin, setup, student_id, class_group=setup["blue"])
    await client.put(
        f"/api/v1/students/{student_id}/health",
        json={"chronic_conditions": "Asthma"},
        headers=admin,
    )

    teacher = await token(client, email=setup["teacher_email"], subdomain=setup["subdomain"])
    response = await client.get(f"/api/v1/students/{student_id}/health", headers=teacher)

    assert response.status_code == 200
    assert response.json()["chronic_conditions"] == "Asthma"


async def test_reading_health_is_audited_and_the_values_are_not_copied_into_the_log(
    client, setup, admin, owner_session
) -> None:
    """Who opened a child's health record is a question a school may have to
    answer. What it says is not something to duplicate into an audit row."""
    student_id = await admit(client, admin)
    await client.put(
        f"/api/v1/students/{student_id}/health",
        json={"allergies": "Penicillin"},
        headers=admin,
    )
    await client.get(f"/api/v1/students/{student_id}/health", headers=admin)

    rows = (
        await owner_session.execute(
            text(
                "SELECT action, changed FROM audit_event "
                " WHERE entity_id = :id AND action LIKE 'health.%' "
                " ORDER BY created_at"
            ),
            {"id": student_id},
        )
    ).all()

    actions = [row.action for row in rows]
    assert "health.updated" in actions
    assert "health.read" in actions
    serialised = str([row.changed for row in rows])
    assert "Penicillin" not in serialised


# --------------------------------------------------------------------------
# Emergency contacts
# --------------------------------------------------------------------------
async def test_emergency_contacts_are_ordered_and_one_is_first(client, setup, admin) -> None:
    student_id = await admit(client, admin)

    response = await client.put(
        f"/api/v1/students/{student_id}/emergency-contacts",
        json={
            "contacts": [
                {
                    "full_name": "Asha Mwanjala",
                    "relationship": "Mother",
                    "phone": "0754118902",
                    "priority": 1,
                },
                {
                    "full_name": "Neighbour Mbwana",
                    "relationship": "Neighbour",
                    "phone": "0715440118",
                    "priority": 2,
                    "note": "Works nights, try after 2pm",
                },
            ]
        },
        headers=admin,
    )

    assert response.status_code == 200
    assert [contact["priority"] for contact in response.json()] == [1, 2]


async def test_two_contacts_cannot_share_a_priority(client, setup, admin) -> None:
    """Three numbers in no particular order is three numbers nobody knows
    which to ring first."""
    student_id = await admit(client, admin)

    response = await client.put(
        f"/api/v1/students/{student_id}/emergency-contacts",
        json={
            "contacts": [
                {
                    "full_name": "Asha Mwanjala",
                    "relationship": "Mother",
                    "phone": "0754118902",
                    "priority": 1,
                },
                {
                    "full_name": "Said Mwanjala",
                    "relationship": "Father",
                    "phone": "0715440118",
                    "priority": 1,
                },
            ]
        },
        headers=admin,
    )

    assert response.status_code == 422
    assert "same priority" in response.json()["detail"]


async def test_somebody_has_to_be_first_to_call(client, setup, admin) -> None:
    student_id = await admit(client, admin)

    response = await client.put(
        f"/api/v1/students/{student_id}/emergency-contacts",
        json={
            "contacts": [
                {
                    "full_name": "Rehema Mbwana",
                    "relationship": "Aunt",
                    "phone": "0754118902",
                    "priority": 2,
                },
            ]
        },
        headers=admin,
    )

    assert response.status_code == 422
    assert "first to call" in response.json()["detail"]


# --------------------------------------------------------------------------
# The checklist and the school's own fields
# --------------------------------------------------------------------------
async def test_the_checklist_names_what_is_missing(client, setup, admin) -> None:
    student_id = await admit(client, admin)

    response = await client.get(f"/api/v1/students/{student_id}/checklist", headers=admin)

    body = response.json()
    assert body["complete"] is False
    assert "Birth certificate" in body["missing"]
    assert "At least one guardian" in body["missing"]
    assert "An emergency contact" in body["missing"]


async def test_a_boarders_checklist_asks_for_more_than_a_day_pupils(client, setup, admin) -> None:
    day = await admit(client, admin, first="Day")
    await enrol(client, admin, setup, day, residency="day")
    boarder = await admit(client, admin, first="Boarder")
    await enrol(client, admin, setup, boarder, residency="boarder")

    day_list = await client.get(f"/api/v1/students/{day}/checklist", headers=admin)
    boarder_list = await client.get(f"/api/v1/students/{boarder}/checklist", headers=admin)

    assert "Boarder health form" not in day_list.json()["missing"]
    assert "Boarder health form" in boarder_list.json()["missing"]


def tiny_png() -> bytes:
    """A real 1x1 PNG, so the declared content type is not a lie.

    Built byte by byte rather than pasted as an escaped literal: the confirm
    step asks storage what actually arrived, so a test that uploaded something
    which is not a PNG would be testing the wrong thing.
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    signature = bytes([137, 80, 78, 71, 13, 10, 26, 10])
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(bytes([0, 255, 255, 255]))
    return (
        signature + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")
    )


async def upload_document(client, admin, student_id, *, kind="birth_certificate"):
    """The two steps a client actually takes: PUT to storage, then confirm."""
    png = tiny_png()
    ticket = await client.post(
        f"/api/v1/students/{student_id}/documents/upload-url",
        json={"kind": kind, "content_type": "image/png", "size_bytes": len(png)},
        headers=admin,
    )
    assert ticket.status_code == 200, ticket.text
    body = ticket.json()

    put = await client.put(body["url"], content=png, headers=body["headers"])
    assert put.status_code == 200, put.text

    confirmed = await client.post(
        f"/api/v1/students/{student_id}/documents",
        json={"kind": kind, "object_key": body["object_key"], "file_name": "cheti.png"},
        headers=admin,
    )
    return confirmed, png


async def test_a_document_goes_straight_to_storage_and_is_verified_on_confirm(
    client, setup, admin
) -> None:
    """The bytes never pass through the API: a four-megabyte photograph through
    an application container ties up a worker for the length of a 3G upload,
    and an intake day is several hundred of them.

    What the confirm step records is what storage says arrived, not what the
    client said it would send — a presigned PUT cannot enforce a length.
    """
    student_id = await admit(client, admin)

    confirmed, png = await upload_document(client, admin, student_id)

    assert confirmed.status_code == 201, confirmed.text
    document = confirmed.json()
    assert document["size_bytes"] == len(png)
    assert document["content_type"] == "image/png"
    # The stored reference is an object key, not a URL anybody can follow.
    assert document["file_url"].startswith(f"school/{setup['school_id']}/student/")


async def test_the_object_key_is_chosen_by_the_server(client, setup, admin) -> None:
    """A client that could choose its own key is a client that could overwrite
    another school's object."""
    student_id = await admit(client, admin)

    ticket = await client.post(
        f"/api/v1/students/{student_id}/documents/upload-url",
        json={"kind": "passport_photo", "content_type": "image/jpeg"},
        headers=admin,
    )

    key = ticket.json()["object_key"]
    assert key.startswith(f"school/{setup['school_id']}/student/{student_id}/passport_photo/")


async def test_reading_a_document_needs_a_fresh_short_lived_link(
    client, setup, admin
) -> None:
    """The bucket is private so that opening a child's birth certificate is an
    act with a permission attached, rather than a URL that works forever."""
    student_id = await admit(client, admin)
    confirmed, png = await upload_document(client, admin, student_id)
    document_id = confirmed.json()["id"]

    link = await client.get(
        f"/api/v1/students/{student_id}/documents/{document_id}/url", headers=admin
    )
    assert link.status_code == 200
    assert link.json()["expires_in"] <= 900

    fetched = await client.get(link.json()["url"])
    assert fetched.status_code == 200
    assert fetched.content == png

    tampered = await client.get(
        link.json()["url"].split("?")[0] + "?expires=1&signature=deadbeef"
    )
    assert tampered.status_code == 403


async def test_an_upload_the_school_cannot_store_is_refused_up_front(
    client, setup, admin
) -> None:
    student_id = await admit(client, admin)

    executable = await client.post(
        f"/api/v1/students/{student_id}/documents/upload-url",
        json={"kind": "birth_certificate", "content_type": "application/x-msdownload"},
        headers=admin,
    )
    oversized = await client.post(
        f"/api/v1/students/{student_id}/documents/upload-url",
        json={
            "kind": "birth_certificate",
            "content_type": "image/jpeg",
            "size_bytes": 40 * 1024 * 1024,
        },
        headers=admin,
    )

    assert executable.status_code == 422
    assert "not a kind of file" in executable.json()["detail"]
    assert oversized.status_code == 422
    assert "larger than 8 MB" in oversized.json()["detail"]


async def test_confirming_an_upload_that_never_arrived_is_refused(
    client, setup, admin
) -> None:
    """Otherwise a record exists for a document nobody can open, and a
    checklist clears itself without a file behind it."""
    student_id = await admit(client, admin)

    response = await client.post(
        f"/api/v1/students/{student_id}/documents",
        json={
            "kind": "birth_certificate",
            "object_key": f"school/{setup['school_id']}/student/{student_id}/bc/nothing.jpg",
        },
        headers=admin,
    )

    assert response.status_code == 422
    assert "did not arrive" in response.json()["detail"]


async def test_recording_a_document_clears_its_line(client, setup, admin) -> None:
    student_id = await admit(client, admin)

    await upload_document(client, admin, student_id)
    checklist = await client.get(f"/api/v1/students/{student_id}/checklist", headers=admin)

    assert "Birth certificate" not in checklist.json()["missing"]


async def test_a_custom_field_is_checked_against_its_declared_kind(client, setup, admin) -> None:
    """The difference between a bounded custom field and a free-text dump
    nobody can report on."""
    student_id = await admit(client, admin)

    good = await client.put(
        f"/api/v1/students/{student_id}/fields",
        json={"values": {"uniform_size": "M"}},
        headers=admin,
    )
    bad = await client.put(
        f"/api/v1/students/{student_id}/fields",
        json={"values": {"uniform_size": "XXL"}},
        headers=admin,
    )
    unknown = await client.put(
        f"/api/v1/students/{student_id}/fields",
        json={"values": {"favourite_colour": "blue"}},
        headers=admin,
    )

    assert good.status_code == 200
    assert good.json()["uniform_size"] == "M"
    assert bad.status_code == 422
    assert "S, M, L" in bad.json()["detail"]
    assert unknown.status_code == 422
    assert "no field called favourite_colour" in unknown.json()["detail"]


async def test_setting_a_field_twice_overwrites_rather_than_duplicating(
    client, setup, admin
) -> None:
    student_id = await admit(client, admin)

    await client.put(
        f"/api/v1/students/{student_id}/fields",
        json={"values": {"uniform_size": "S"}},
        headers=admin,
    )
    second = await client.put(
        f"/api/v1/students/{student_id}/fields",
        json={"values": {"uniform_size": "L"}},
        headers=admin,
    )

    assert second.json() == {"uniform_size": "L"}


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------
async def test_a_child_at_another_school_is_a_404(client, setup, admin, owner_session) -> None:
    other = await make_school(owner_session, name="Tumaini", subdomain=f"oth{uuid4().hex[:6]}")
    student_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO student (id, school_id, admission_no, first_name, surname, "
            " date_of_birth, sex, status, nationality, arrears_brought_forward, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, 'T/0001', 'Elsewhere', 'Child', :dob, 'f', 'active', "
            " 'Tanzanian', 0, now(), now())"
        ),
        {"id": student_id, "school_id": other, "dob": date(2018, 1, 1)},
    )
    await owner_session.commit()

    response = await client.get(f"/api/v1/students/{student_id}", headers=admin)

    assert response.status_code == 404

    await owner_session.execute(text("DELETE FROM school WHERE id = :id"), {"id": other})
    await owner_session.commit()


# --------------------------------------------------------------------------
# Filtering the roll by what a family owes
# --------------------------------------------------------------------------
async def _bill(owner_session, *, school_id, student_id, enrolment_id, billed, settled, due):
    """One invoice with a known balance and a known due date."""
    invoice_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO invoice (id, school_id, reference, kind, student_id, "
            " enrolment_id, issued_on, due_date, currency, billed, settled, is_void, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, :ref, 'term', :student, :enrolment, "
            " current_date - 30, :due, 'TZS', 0, 0, false, now(), now())"
        ),
        {
            "id": invoice_id,
            "school_id": school_id,
            "ref": f"INV-{uuid4().hex[:8]}",
            "student": student_id,
            "enrolment": enrolment_id,
            "due": due,
        },
    )
    # billed and settled are derived, so they are written as a line and a payment.
    await owner_session.execute(
        text(
            "INSERT INTO invoice_line (id, school_id, invoice_id, item, amount, kind, "
            " sort_order, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, :invoice, 'Tuition', :amount, "
            " 'charge', 0, now(), now())"
        ),
        {"school_id": school_id, "invoice": invoice_id, "amount": billed},
    )
    if settled:
        await owner_session.execute(
            text(
                "INSERT INTO payment (id, school_id, invoice_id, external_ref, amount, "
                " applied, currency, channel, state, received_at, is_manual, "
                " created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :invoice, :ref, :amount, "
                " :amount, 'TZS', 'mpesa', 'settled', now(), false, now(), now())"
            ),
            {
                "school_id": school_id,
                "invoice": invoice_id,
                "ref": f"MOCK-{uuid4().hex[:8]}",
                "amount": settled,
            },
        )
    return invoice_id


async def test_a_balance_that_is_not_yet_due_is_not_arrears(
    client, setup, admin, owner_session
) -> None:
    """The distinction the old `in_arrears` boolean collapsed: it matched any
    unpaid balance, so a child billed yesterday was listed beside one who had
    owed since February. That is the difference between a reminder and an
    accusation, and a bursar ringing the second list does not want the first."""
    paid = await admit(client, admin, first="Paid")
    owing = await admit(client, admin, first="Owing")
    overdue = await admit(client, admin, first="Overdue")

    not_yet_due = date.today() + timedelta(days=10)
    past_due = date.today() - timedelta(days=5)

    for student_id, billed, settled, due in (
        (paid, "300000", "300000", not_yet_due),
        (owing, "300000", "100000", not_yet_due),
        (overdue, "300000", "100000", past_due),
    ):
        enrolment = await client.post(
            "/api/v1/enrolments",
            json={
                "student_id": student_id,
                "academic_year_id": str(setup["year_id"]),
                "class_group_id": str(setup["blue"]),
            },
            headers=admin,
        )
        await _bill(
            owner_session,
            school_id=setup["school_id"],
            student_id=student_id,
            enrolment_id=enrolment.json()["id"],
            billed=billed,
            settled=settled,
            due=due,
        )
    await owner_session.commit()

    async def names(fee_status: str) -> set[str]:
        response = await client.get(
            "/api/v1/students", params={"fee_status": fee_status}, headers=admin
        )
        assert response.status_code == 200, response.text
        return {row["first_name"] for row in response.json()["items"]}

    assert await names("settled") == {"Paid"}
    assert await names("part_paid") == {"Owing"}
    assert await names("in_arrears") == {"Overdue"}


async def test_a_child_with_no_invoice_at_all_is_not_settled(
    client, setup, admin
) -> None:
    """Unbilled and settled look the same from a balance of zero, and lumping
    them together would hide a whole class the term run missed."""
    await admit(client, admin, first="Unbilled")

    settled = await client.get(
        "/api/v1/students", params={"fee_status": "settled"}, headers=admin
    )

    assert "Unbilled" not in {row["first_name"] for row in settled.json()["items"]}


async def test_a_date_of_birth_in_the_future_is_refused(client, setup, admin) -> None:
    """It was accepted. A child born in 2099 came out as age 0 on every screen
    that showed one, and the field is not one anybody looks at twice."""
    student_id = await admit(client, admin)

    response = await client.patch(
        f"/api/v1/students/{student_id}",
        json={"date_of_birth": "2099-01-01"},
        headers=admin,
    )

    assert response.status_code == 422
    assert "future" in response.text


async def test_a_child_cannot_be_admitted_with_an_impossible_birth_date(
    client, setup, admin
) -> None:
    response = await client.post(
        "/api/v1/students",
        json={
            "first_name": "Neema",
            "surname": "Mwanjala",
            "date_of_birth": "1066-10-14",
            "sex": "f",
        },
        headers=admin,
    )

    assert response.status_code == 422
