"""The academic structure endpoints, through the real app and PostgreSQL.

What these cover that the unit tests cannot: who is allowed to change the
structure, that a partial update does not clear the settings it was not given,
and that one school's structure is invisible to another's token.
"""

from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.security import hash_password
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def _add_user(session, *, school_id, name, role, email=None, phone=None):
    user_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO app_user (id, school_id, full_name, email, phone, role, "
            " password_hash, must_change_password, is_active, failed_logins, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, :name, :email, :phone, :role, :hash, false, "
            " true, 0, now(), now())"
        ),
        {
            "id": user_id,
            "school_id": school_id,
            "name": name,
            "email": email,
            "phone": phone,
            "role": role,
            "hash": hash_password(PASSWORD),
        },
    )
    return user_id


@pytest.fixture
async def setup(owner_session):
    """A school with a year, three levels and some staff, plus a second school
    whose structure must stay invisible."""
    suffix = uuid4().hex[:6]
    school_id = await make_school(owner_session, name="Mwenge Primary", subdomain=f"acad{suffix}")
    other_id = await make_school(owner_session, name="Tumaini Primary", subdomain=f"oth{suffix}")

    subdomain = await owner_session.scalar(
        text("SELECT subdomain FROM school WHERE id = :id"), {"id": school_id}
    )
    other_subdomain = await owner_session.scalar(
        text("SELECT subdomain FROM school WHERE id = :id"), {"id": other_id}
    )

    admin_email = f"admin-{suffix}@mwenge.ac.tz"
    bursar_email = f"bursar-{suffix}@mwenge.ac.tz"
    head_email = f"head-{suffix}@mwenge.ac.tz"
    teacher_email = f"teacher-{suffix}@mwenge.ac.tz"
    await _add_user(
        owner_session, school_id=school_id, name="Datius", role="school_admin", email=admin_email
    )
    await _add_user(
        owner_session, school_id=school_id, name="Salome", role="bursar", email=bursar_email
    )
    await _add_user(
        owner_session, school_id=school_id, name="Agnes", role="head_of_academics", email=head_email
    )
    teacher_id = await _add_user(
        owner_session,
        school_id=school_id,
        name="Josephat",
        role="class_teacher",
        email=teacher_email,
    )

    year_id = await make_academic_year(owner_session, school_id=school_id, label="2027")
    std5 = await make_level(
        owner_session, school_id=school_id, code="STD5", name="Standard 5", order_index=5
    )
    std6 = await make_level(
        owner_session, school_id=school_id, code="STD6", name="Standard 6", order_index=6
    )
    std7 = await make_level(
        owner_session, school_id=school_id, code="STD7", name="Standard 7", order_index=7
    )

    # The other school gets its own level, which must never appear in ours.
    other_year = await make_academic_year(owner_session, school_id=other_id, label="2027")
    await make_level(owner_session, school_id=other_id, code="FORM1", name="Form 1", order_index=8)

    await owner_session.commit()

    yield {
        "school_id": school_id,
        "subdomain": subdomain,
        "other_subdomain": other_subdomain,
        "admin_email": admin_email,
        "bursar_email": bursar_email,
        "head_email": head_email,
        "teacher_id": teacher_id,
        "year_id": year_id,
        "other_year_id": other_year,
        "std5": std5,
        "std6": std6,
        "std7": std7,
    }

    await owner_session.rollback()
    await owner_session.execute(
        text("DELETE FROM school WHERE id = ANY(:ids)"), {"ids": [school_id, other_id]}
    )
    await owner_session.commit()


async def token_for(client: AsyncClient, *, email: str, subdomain: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": PASSWORD},
        headers={"X-School-Subdomain": subdomain},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def admin(client, setup) -> dict[str, str]:
    return await token_for(client, email=setup["admin_email"], subdomain=setup["subdomain"])


# --------------------------------------------------------------------------
# The school profile
# --------------------------------------------------------------------------
async def test_a_partial_update_leaves_the_other_settings_alone(client, setup, admin) -> None:
    """The bug this rules out: a settings form that posts three fields and
    silently resets the arrears switches to their defaults, so a school starts
    withholding report cards nobody asked it to withhold."""
    await client.patch(
        "/api/v1/schools/me",
        json={"arrears_blocks_results": True, "deposit_percent": 50},
        headers=admin,
    )

    response = await client.patch(
        "/api/v1/schools/me", json={"motto": "Elimu ni ufunguo"}, headers=admin
    )

    assert response.status_code == 200
    body = response.json()
    assert body["motto"] == "Elimu ni ufunguo"
    assert body["arrears_blocks_results"] is True, "an untouched setting must survive"
    assert body["deposit_percent"] == 50


async def test_a_bursar_may_read_the_school_but_not_change_it(client, setup) -> None:
    bursar = await token_for(client, email=setup["bursar_email"], subdomain=setup["subdomain"])

    readable = await client.get("/api/v1/schools/me", headers=bursar)
    writable = await client.patch("/api/v1/schools/me", json={"motto": "Mine now"}, headers=bursar)

    assert readable.status_code == 200
    assert writable.status_code == 403


async def test_a_deposit_outside_nought_to_a_hundred_is_refused(client, setup, admin) -> None:
    response = await client.patch(
        "/api/v1/schools/me", json={"deposit_percent": 140}, headers=admin
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------
async def test_opening_a_year_and_moving_the_school_into_it(client, setup, admin) -> None:
    created = await client.post(
        "/api/v1/academic/years",
        json={"label": "2028", "starts_on": "2028-01-10", "ends_on": "2028-12-08"},
        headers=admin,
    )
    assert created.status_code == 201
    new_year = created.json()
    assert new_year["is_current"] is False, "opening next year must not move the school into it"

    moved = await client.post(f"/api/v1/academic/years/{new_year['id']}/current", headers=admin)
    assert moved.status_code == 200

    years = (await client.get("/api/v1/academic/years", headers=admin)).json()
    current = [year["label"] for year in years if year["is_current"]]
    assert current == ["2028"], "exactly one year is current, and it is the new one"


async def test_a_backwards_year_is_refused_with_a_readable_reason(client, setup, admin) -> None:
    response = await client.post(
        "/api/v1/academic/years",
        json={"label": "2028", "starts_on": "2028-12-08", "ends_on": "2028-01-10"},
        headers=admin,
    )

    assert response.status_code == 422
    assert "end after it starts" in response.json()["detail"]


async def test_setting_a_years_terms(client, setup, admin) -> None:
    response = await client.put(
        f"/api/v1/academic/years/{setup['year_id']}/terms",
        json={
            "terms": [
                {"index": 1, "label": "Term 1", "starts_on": "2027-01-08", "ends_on": "2027-04-02"},
                {"index": 2, "label": "Term 2", "starts_on": "2027-04-19", "ends_on": "2027-08-06"},
                {"index": 3, "label": "Term 3", "starts_on": "2027-08-23", "ends_on": "2027-12-04"},
            ]
        },
        headers=admin,
    )

    assert response.status_code == 200
    assert [term["index"] for term in response.json()] == [1, 2, 3]


async def test_overlapping_terms_are_refused(client, setup, admin) -> None:
    response = await client.put(
        f"/api/v1/academic/years/{setup['year_id']}/terms",
        json={
            "terms": [
                {"index": 1, "starts_on": "2027-01-08", "ends_on": "2027-05-02"},
                {"index": 2, "starts_on": "2027-04-19", "ends_on": "2027-08-06"},
            ]
        },
        headers=admin,
    )

    assert response.status_code == 422
    assert "overlap" in response.json()["detail"]


async def test_terms_outside_their_year_are_refused(client, setup, admin) -> None:
    response = await client.put(
        f"/api/v1/academic/years/{setup['year_id']}/terms",
        json={"terms": [{"index": 1, "starts_on": "2026-11-01", "ends_on": "2027-04-02"}]},
        headers=admin,
    )

    assert response.status_code == 422
    assert "outside the academic year" in response.json()["detail"]


async def test_replacing_terms_updates_rather_than_duplicating(client, setup, admin) -> None:
    body = {"terms": [{"index": 1, "starts_on": "2027-01-08", "ends_on": "2027-04-02"}]}
    await client.put(f"/api/v1/academic/years/{setup['year_id']}/terms", json=body, headers=admin)

    body["terms"][0]["ends_on"] = "2027-04-09"
    again = await client.put(
        f"/api/v1/academic/years/{setup['year_id']}/terms", json=body, headers=admin
    )

    assert len(again.json()) == 1
    assert again.json()[0]["ends_on"] == "2027-04-09"


# --------------------------------------------------------------------------
# The ladder and the 2027 double cohort
# --------------------------------------------------------------------------
async def test_the_ladder_warns_when_the_top_cohort_has_nowhere_to_go(client, setup, admin) -> None:
    response = await client.get(
        "/api/v1/academic/ladder", params={"year_id": str(setup["year_id"])}, headers=admin
    )

    assert response.status_code == 200
    body = response.json()
    assert any("nowhere to go" in problem for problem in body["problems"])
    assert "No cohort" in body["summary"]


async def test_recording_both_2027_cohorts_graduates_both(client, setup, admin) -> None:
    """The year the six-year primary transition produces two exit cohorts at
    once. Standard 6 has a perfectly good next rung and must still graduate."""
    response = await client.put(
        f"/api/v1/academic/years/{setup['year_id']}/exit-levels",
        json={
            "levels": [
                {"level_id": str(setup["std7"]), "note": "Last of the seven-year cycle"},
                {"level_id": str(setup["std6"]), "note": "First to finish after six years"},
            ]
        },
        headers=admin,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["problems"] == []
    assert "both leave" in body["summary"]

    outcomes = {step["level_id"]: step["outcome"] for step in body["steps"]}
    assert outcomes[str(setup["std6"])] == "graduate"
    assert outcomes[str(setup["std7"])] == "graduate"
    assert outcomes[str(setup["std5"])] == "promote"


async def test_an_exit_level_from_another_school_is_refused(client, setup, admin) -> None:
    """A level id is something somebody can type. Without this check, one
    school could record another school's level as its own exit cohort."""
    other_level = uuid4()

    response = await client.put(
        f"/api/v1/academic/years/{setup['year_id']}/exit-levels",
        json={"levels": [{"level_id": str(other_level)}]},
        headers=admin,
    )

    assert response.status_code == 422
    assert "not levels at this school" in response.json()["detail"]


async def test_adding_a_level_answers_with_the_whole_ladder(client, setup, admin) -> None:
    response = await client.post(
        "/api/v1/academic/levels",
        json={"code": "STD4", "name": "Standard 4", "stage": "primary", "order_index": 4},
        headers=admin,
    )

    assert response.status_code == 201
    codes = [level["code"] for level in response.json()["levels"]]
    assert codes == ["STD4", "STD5", "STD6", "STD7"], "ordered by the ladder, not by insertion"


async def test_a_duplicate_order_index_is_reported_not_refused(client, setup, admin) -> None:
    """A school halfway through setting up its ladder legitimately has
    collisions; refusing would force it to add levels in one order only."""
    response = await client.post(
        "/api/v1/academic/levels",
        json={
            "code": "STD5B",
            "name": "Standard 5 Vocational",
            "stage": "primary",
            "order_index": 5,
        },
        headers=admin,
    )

    assert response.status_code == 201
    assert any("share order 5" in problem for problem in response.json()["problems"])


async def test_a_head_of_academics_may_change_the_ladder_and_a_bursar_may_not(
    client, setup
) -> None:
    head = await token_for(client, email=setup["head_email"], subdomain=setup["subdomain"])
    bursar = await token_for(client, email=setup["bursar_email"], subdomain=setup["subdomain"])
    body = {"code": "PP", "name": "Pre-primary", "stage": "pre_primary", "order_index": 0}

    allowed = await client.post("/api/v1/academic/levels", json=body, headers=head)
    refused = await client.post(
        "/api/v1/academic/levels", json={**body, "code": "PP2"}, headers=bursar
    )

    assert allowed.status_code == 201
    assert refused.status_code == 403


# --------------------------------------------------------------------------
# Classes
# --------------------------------------------------------------------------
async def test_a_class_gets_its_name_from_the_level_and_stream(client, setup, admin) -> None:
    """Derived in one place, so a roll, an invoice and an SMS all say
    "Standard 6 Blue"."""
    stream = await client.post(
        "/api/v1/academic/streams",
        json={"level_id": str(setup["std6"]), "name": "Blue", "capacity": 40},
        headers=admin,
    )
    assert stream.status_code == 201

    response = await client.post(
        "/api/v1/academic/class-groups",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["std6"]),
            "stream_id": stream.json()["id"],
        },
        headers=admin,
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Standard 6 Blue"
    assert response.json()["pupils"] == 0


async def test_a_stream_from_another_level_is_refused(client, setup, admin) -> None:
    stream = await client.post(
        "/api/v1/academic/streams",
        json={"level_id": str(setup["std5"]), "name": "Green"},
        headers=admin,
    )

    response = await client.post(
        "/api/v1/academic/class-groups",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["std6"]),
            "stream_id": stream.json()["id"],
        },
        headers=admin,
    )

    assert response.status_code == 422
    assert "does not belong to" in response.json()["detail"]


async def test_pupils_are_counted_from_active_enrolments(
    client, setup, admin, owner_session
) -> None:
    """Counted rather than stored: a stored count goes stale the first time a
    child transfers out."""
    group_id = await make_class_group(
        owner_session,
        school_id=setup["school_id"],
        academic_year_id=setup["year_id"],
        level_id=setup["std6"],
        name="Standard 6 Counted",
    )
    for n in range(3):
        student_id = await make_student(
            owner_session, school_id=setup["school_id"], admission_no=f"C/{uuid4().hex[:6]}/{n}"
        )
        await make_enrolment(
            owner_session,
            school_id=setup["school_id"],
            student_id=student_id,
            academic_year_id=setup["year_id"],
            class_group_id=group_id,
        )
    # One child who has left: still an enrolment row, not an active one.
    gone = await make_student(
        owner_session, school_id=setup["school_id"], admission_no=f"C/{uuid4().hex[:6]}/gone"
    )
    await make_enrolment(
        owner_session,
        school_id=setup["school_id"],
        student_id=gone,
        academic_year_id=setup["year_id"],
        class_group_id=group_id,
        outcome="transferred_out",
    )
    await owner_session.commit()

    response = await client.get(
        "/api/v1/academic/class-groups",
        params={"year_id": str(setup["year_id"])},
        headers=admin,
    )

    counted = {group["name"]: group["pupils"] for group in response.json()}
    assert counted["Standard 6 Counted"] == 3


async def _a_class(client, admin, setup, **over):
    body = {
        "academic_year_id": str(setup["year_id"]),
        "level_id": str(setup["std5"]),
        "capacity": 40,
    }
    body.update(over)
    created = await client.post("/api/v1/academic/class-groups", json=body, headers=admin)
    assert created.status_code == 201, created.text
    return created.json()


async def test_a_full_class_can_be_opened_up(client, setup, admin) -> None:
    """The counter admission form refuses a full class and tells the registrar
    to come here and raise it. Until this existed, that was advice with
    nowhere to act on."""
    group = await _a_class(client, admin, setup, capacity=1)

    changed = await client.patch(
        f"/api/v1/academic/class-groups/{group['id']}",
        json={"capacity": 45, "room": "Block B, Room 3"},
        headers=admin,
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["capacity"] == 45
    assert changed.json()["room"] == "Block B, Room 3"
    assert changed.json()["name"] == group["name"], "the name is not editable here"


async def test_a_capacity_below_the_headcount_is_recorded_not_refused(
    client, setup, admin, owner_session
) -> None:
    """A school that moves thirty-seven children into a room rated for
    thirty-five has done that. Refusing to record it would be insisting on a
    tidier world rather than describing this one — and nothing is lost: the
    class simply has no places left."""
    group = await _a_class(client, admin, setup, capacity=40)
    for index in range(3):
        student_id = await make_student(
            owner_session,
            school_id=setup["school_id"],
            admission_no=f"CAP/{uuid4().hex[:6]}/{index}",
        )
        await make_enrolment(
            owner_session,
            school_id=setup["school_id"],
            student_id=student_id,
            academic_year_id=setup["year_id"],
            class_group_id=UUID(group["id"]),
        )
    await owner_session.commit()

    changed = await client.patch(
        f"/api/v1/academic/class-groups/{group['id']}",
        json={"capacity": 2},
        headers=admin,
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["capacity"] == 2
    assert changed.json()["pupils"] == 3, "the real headcount, not the capacity"


async def test_a_class_in_a_closed_year_is_fixed(client, setup, admin, owner_session) -> None:
    """A closed year's classes are part of what a report card means."""
    group = await _a_class(client, admin, setup)
    await owner_session.execute(
        text("UPDATE academic_year SET is_closed = true WHERE id = :id"),
        {"id": setup["year_id"]},
    )
    await owner_session.commit()

    changed = await client.patch(
        f"/api/v1/academic/class-groups/{group['id']}",
        json={"capacity": 60},
        headers=admin,
    )

    assert changed.status_code == 422
    assert "closed" in changed.json()["detail"]


async def test_a_bursar_cannot_change_a_class(client, setup, admin) -> None:
    group = await _a_class(client, admin, setup)
    bursar = await token_for(client, email=setup["bursar_email"], subdomain=setup["subdomain"])

    changed = await client.patch(
        f"/api/v1/academic/class-groups/{group['id']}",
        json={"capacity": 60},
        headers=bursar,
    )

    assert changed.status_code == 403


# --------------------------------------------------------------------------
# Who can be put in front of a class
# --------------------------------------------------------------------------
async def test_the_teacher_picker_offers_only_people_the_save_would_accept(
    client, setup, admin
) -> None:
    """The same roles `teacher_can_be_assigned` checks. A picker that offered
    somebody the save then refused would be a form that lies."""
    staff = await client.get("/api/v1/academic/teaching-staff", headers=admin)

    assert staff.status_code == 200, staff.text
    roles = {person["role"] for person in staff.json()}
    assert roles <= {"class_teacher", "subject_teacher", "head_of_academics", "school_admin"}
    assert "bursar" not in roles, "a bursar cannot take a register"
    names = {person["full_name"] for person in staff.json()}
    assert {"Josephat", "Agnes", "Datius"} <= names
    assert "Salome" not in names


async def test_the_picker_says_who_already_has_a_class(client, setup, admin) -> None:
    """A teacher who already has a class can take a second, but the register
    then falls to one person twice a day. That has to be visible."""
    group = await _a_class(client, admin, setup)
    await client.put(
        f"/api/v1/academic/class-groups/{group['id']}/class-teacher",
        json={"teacher_id": str(setup["teacher_id"])},
        headers=admin,
    )

    staff = await client.get(
        "/api/v1/academic/teaching-staff",
        params={"year_id": str(setup["year_id"])},
        headers=admin,
    )

    held = {person["full_name"]: person["class_name"] for person in staff.json()}
    assert held["Josephat"] == group["name"]
    assert held["Agnes"] is None, "free to take one"


async def test_another_schools_staff_are_invisible(client, setup, admin, owner_session) -> None:
    """Row-level security, checked from the endpoint a picker actually uses."""
    await _add_user(
        owner_session,
        school_id=await owner_session.scalar(
            text("SELECT id FROM school WHERE subdomain = :s"),
            {"s": setup["other_subdomain"]},
        ),
        name="Someone Else",
        role="class_teacher",
        email=f"other-{uuid4().hex[:6]}@tumaini.ac.tz",
    )
    await owner_session.commit()

    staff = await client.get("/api/v1/academic/teaching-staff", headers=admin)

    assert "Someone Else" not in {person["full_name"] for person in staff.json()}


async def test_a_bursar_cannot_list_the_teaching_staff(client, setup) -> None:
    bursar = await token_for(client, email=setup["bursar_email"], subdomain=setup["subdomain"])

    staff = await client.get("/api/v1/academic/teaching-staff", headers=bursar)

    assert staff.status_code == 403


async def test_a_guardian_cannot_be_made_a_class_teacher(
    client, setup, admin, owner_session
) -> None:
    """An id from a form is an id somebody could have typed, and a class
    teacher may read every child in their class."""
    guardian_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO guardian (id, school_id, full_name, phone, locale, sms_opted_out, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, 'Asha', :phone, 'en', false, now(), now())"
        ),
        {
            "id": guardian_id,
            "school_id": setup["school_id"],
            "phone": f"0755{uuid4().int % 1000000:06d}",
        },
    )
    guardian_user = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO app_user (id, school_id, full_name, role, guardian_id, is_active, "
            " must_change_password, failed_logins, created_at, updated_at) "
            "VALUES (:id, :school_id, 'Asha', 'guardian', :guardian_id, true, false, 0, "
            " now(), now())"
        ),
        {"id": guardian_user, "school_id": setup["school_id"], "guardian_id": guardian_id},
    )
    group_id = await make_class_group(
        owner_session,
        school_id=setup["school_id"],
        academic_year_id=setup["year_id"],
        level_id=setup["std6"],
        name="Standard 6 Teacherless",
    )
    await owner_session.commit()

    response = await client.put(
        f"/api/v1/academic/class-groups/{group_id}/class-teacher",
        json={"teacher_id": str(guardian_user)},
        headers=admin,
    )

    assert response.status_code == 422
    assert "cannot be made a class teacher" in response.json()["detail"]


# --------------------------------------------------------------------------
# Subjects and who teaches them
# --------------------------------------------------------------------------
async def _a_subject(client, admin, setup, **over):
    body = {
        "code": "KIS",
        "name": "Kiswahili",
        "is_core": True,
        "is_examinable": True,
        "levels": [{"level_id": str(setup["std6"]), "periods_per_week": 5}],
    }
    body.update(over)
    created = await client.post("/api/v1/academic/subjects", json=body, headers=admin)
    assert created.status_code == 201, created.text
    return created.json()


async def test_the_subjects_list_carries_where_each_is_taught(client, setup, admin) -> None:
    """Kiswahili at Standard 2 and at Standard 6 are one subject against two
    syllabuses, so the periods belong to the pairing. The screen shows both,
    and one query answers for every subject rather than one apiece."""
    subject = await _a_subject(client, admin, setup)

    listed = await client.get("/api/v1/academic/subjects", headers=admin)

    assert listed.status_code == 200, listed.text
    row = next(each for each in listed.json() if each["id"] == subject["id"])
    assert [(level["level_name"], level["periods_per_week"]) for level in row["levels"]] == [
        ("Standard 6", 5)
    ]


async def test_renaming_a_subject_keeps_the_code_records_point_at(
    client, setup, admin
) -> None:
    """Renaming is safe by design. The code is the thing records point at,
    which is why it is worth saying so on the screen."""
    subject = await _a_subject(client, admin, setup)

    changed = await client.patch(
        f"/api/v1/academic/subjects/{subject['id']}",
        json={"name": "Kiswahili Sanifu"},
        headers=admin,
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "Kiswahili Sanifu"
    assert changed.json()["code"] == "KIS", "unchanged"


async def test_a_subject_is_retired_rather_than_deleted(client, setup, admin) -> None:
    """A report card from a closed year names its subjects. There is no delete
    at all — retiring keeps every old record and stops the subject appearing
    on new mark sheets."""
    subject = await _a_subject(client, admin, setup)

    retired = await client.patch(
        f"/api/v1/academic/subjects/{subject['id']}",
        json={"is_active": False},
        headers=admin,
    )

    assert retired.status_code == 200, retired.text
    assert retired.json()["is_active"] is False

    # Still listed, because old records still name it.
    listed = await client.get("/api/v1/academic/subjects", headers=admin)
    row = next(each for each in listed.json() if each["id"] == subject["id"])
    assert row["is_active"] is False

    gone = await client.delete(f"/api/v1/academic/subjects/{subject['id']}", headers=admin)
    assert gone.status_code == 405, "there is deliberately no delete"


async def test_moving_a_subject_replaces_its_levels(client, setup, admin) -> None:
    subject = await _a_subject(client, admin, setup)

    moved = await client.patch(
        f"/api/v1/academic/subjects/{subject['id']}",
        json={
            "levels": [
                {"level_id": str(setup["std5"]), "periods_per_week": 4},
                {"level_id": str(setup["std7"]), "periods_per_week": 6},
            ]
        },
        headers=admin,
    )
    assert moved.status_code == 200, moved.text

    listed = await client.get("/api/v1/academic/subjects", headers=admin)
    row = next(each for each in listed.json() if each["id"] == subject["id"])
    assert {level["level_name"] for level in row["levels"]} == {"Standard 5", "Standard 7"}


async def test_a_level_from_another_school_cannot_be_taught_here(
    client, setup, admin
) -> None:
    subject = await _a_subject(client, admin, setup)

    moved = await client.patch(
        f"/api/v1/academic/subjects/{subject['id']}",
        json={"levels": [{"level_id": str(uuid4()), "periods_per_week": 4}]},
        headers=admin,
    )

    assert moved.status_code == 422
    assert "not levels at this school" in moved.json()["detail"]


# --------------------------------------------------------------------------
# The fields a school adds for itself
# --------------------------------------------------------------------------
async def test_a_school_adds_a_field_of_its_own(client, setup, admin) -> None:
    """Every field added here appears, empty, on every pupil record."""
    created = await client.post(
        "/api/v1/schools/me/fields",
        json={
            "key": "bus_route",
            "label": "Bus route",
            "kind": "choice",
            "options": ["Kimara", "Mbezi", "Tegeta"],
            "help_text": "Only for pupils on the school bus.",
        },
        headers=admin,
    )

    assert created.status_code == 201, created.text
    assert created.json()["key"] == "bus_route"
    assert created.json()["scope"] == "student"
    assert created.json()["required_at_registration"] is False, (
        "a required field on the public form costs completed applications"
    )

    listed = await client.get("/api/v1/schools/me/fields", headers=admin)
    assert [field["label"] for field in listed.json()] == ["Bus route"]


async def test_a_choice_field_with_one_option_is_refused(client, setup, admin) -> None:
    """A choice of one is not a choice, and the database refuses it too."""
    created = await client.post(
        "/api/v1/schools/me/fields",
        json={"key": "house", "label": "House", "kind": "choice", "options": ["Simba"]},
        headers=admin,
    )

    assert created.status_code == 422
    assert "two options" in created.json()["detail"]


async def test_a_key_that_would_break_an_export_is_refused(client, setup, admin) -> None:
    """The key lands in an export header and in a URL, so it stays boring."""
    created = await client.post(
        "/api/v1/schools/me/fields",
        json={"key": "bus route!", "label": "Bus route", "kind": "text"},
        headers=admin,
    )

    assert created.status_code == 422
    assert "letters, digits and underscores" in created.json()["detail"]


async def test_two_fields_cannot_share_a_key(client, setup, admin) -> None:
    await client.post(
        "/api/v1/schools/me/fields",
        json={"key": "bed_number", "label": "Bed number", "kind": "text"},
        headers=admin,
    )

    again = await client.post(
        "/api/v1/schools/me/fields",
        json={"key": "bed_number", "label": "Bed", "kind": "text"},
        headers=admin,
    )

    assert again.status_code == 422
    assert "already has" in again.json()["detail"]


async def test_a_field_is_relabelled_but_never_rekeyed(client, setup, admin) -> None:
    """Values already recorded are stored under the key and typed by the kind,
    so changing either would reinterpret answers people have already given."""
    created = await client.post(
        "/api/v1/schools/me/fields",
        json={"key": "bus_route", "label": "Bus route", "kind": "text"},
        headers=admin,
    )
    field_id = created.json()["id"]

    changed = await client.patch(
        f"/api/v1/schools/me/fields/{field_id}",
        json={"label": "Bus route (morning)", "key": "something_else", "kind": "number"},
        headers=admin,
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["label"] == "Bus route (morning)"
    assert changed.json()["key"] == "bus_route", "ignored, not applied"
    assert changed.json()["kind"] == "text", "ignored, not applied"


async def test_a_field_is_retired_and_keeps_its_values(client, setup, admin) -> None:
    created = await client.post(
        "/api/v1/schools/me/fields",
        json={"key": "old_thing", "label": "Old thing", "kind": "text"},
        headers=admin,
    )

    retired = await client.patch(
        f"/api/v1/schools/me/fields/{created.json()['id']}",
        json={"is_active": False},
        headers=admin,
    )

    assert retired.status_code == 200, retired.text
    assert retired.json()["is_active"] is False


async def test_a_bursar_cannot_add_a_field_to_every_pupil_record(client, setup) -> None:
    bursar = await token_for(client, email=setup["bursar_email"], subdomain=setup["subdomain"])

    created = await client.post(
        "/api/v1/schools/me/fields",
        json={"key": "whatever", "label": "Whatever", "kind": "text"},
        headers=bursar,
    )

    assert created.status_code == 403


async def test_a_subject_can_be_taught_at_several_levels(client, setup, admin) -> None:
    response = await client.post(
        "/api/v1/academic/subjects",
        json={
            "code": "KIS",
            "name": "Kiswahili",
            "is_core": True,
            "levels": [
                {"level_id": str(setup["std5"]), "periods_per_week": 6},
                {"level_id": str(setup["std6"]), "periods_per_week": 5},
            ],
        },
        headers=admin,
    )

    assert response.status_code == 201
    assert response.json()["code"] == "KIS"


async def test_one_teacher_per_subject_per_class(client, setup, admin, owner_session) -> None:
    """Co-teaching exists, but two people able to enter the same mark means
    nobody can say whose it was — so the second attempt reads as a sentence
    rather than a constraint violation."""
    group_id = await make_class_group(
        owner_session,
        school_id=setup["school_id"],
        academic_year_id=setup["year_id"],
        level_id=setup["std6"],
        name="Standard 6 Taught",
    )
    await owner_session.commit()

    subject = await client.post(
        "/api/v1/academic/subjects",
        json={"code": "MTC", "name": "Mathematics", "is_core": True},
        headers=admin,
    )
    body = {
        "teacher_id": str(setup["teacher_id"]),
        "subject_id": subject.json()["id"],
        "class_group_id": str(group_id),
    }

    first = await client.post("/api/v1/academic/teaching-assignments", json=body, headers=admin)
    second = await client.post("/api/v1/academic/teaching-assignments", json=body, headers=admin)

    assert first.status_code == 201
    assert second.status_code == 422
    assert "already has a teacher" in second.json()["detail"]

    removed = await client.delete(
        f"/api/v1/academic/teaching-assignments/{first.json()['id']}", headers=admin
    )
    assert removed.status_code == 204


# --------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------
async def test_one_schools_token_sees_none_of_anothers_structure(client, setup) -> None:
    """The levels exist, in the same table, one row apart. The only thing
    keeping them apart is the policy."""
    admin = await token_for(client, email=setup["admin_email"], subdomain=setup["subdomain"])

    levels = (await client.get("/api/v1/academic/levels", headers=admin)).json()

    assert {level["code"] for level in levels} == {"STD5", "STD6", "STD7"}
    assert "FORM1" not in {level["code"] for level in levels}


async def test_a_year_belonging_to_another_school_is_a_404(client, setup, admin) -> None:
    """Not a 403: whether that id exists somewhere else on the platform is not
    something this school needs to learn."""
    response = await client.get(
        f"/api/v1/academic/years/{setup['other_year_id']}/terms", headers=admin
    )

    assert response.status_code == 404


async def test_a_guardian_may_not_read_the_academic_structure(client, setup, owner_session) -> None:
    guardian_id = uuid4()
    phone = f"0756{uuid4().int % 1000000:06d}"
    await owner_session.execute(
        text(
            "INSERT INTO guardian (id, school_id, full_name, phone, locale, sms_opted_out, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, 'Grace', :phone, 'en', false, now(), now())"
        ),
        {"id": guardian_id, "school_id": setup["school_id"], "phone": phone},
    )
    await owner_session.execute(
        text(
            "INSERT INTO app_user (id, school_id, full_name, phone, role, guardian_id, "
            " password_hash, is_active, must_change_password, failed_logins, "
            " created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, 'Grace', :phone, 'guardian', :guardian_id, "
            " :hash, true, false, 0, now(), now())"
        ),
        {
            "school_id": setup["school_id"],
            "phone": phone,
            "guardian_id": guardian_id,
            "hash": hash_password(PASSWORD),
        },
    )
    await owner_session.commit()

    guardian = await token_for(client, email=phone, subdomain=setup["subdomain"])
    response = await client.get("/api/v1/academic/levels", headers=guardian)

    assert response.status_code == 403
