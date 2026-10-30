"""Fees end to end: structures, a term run, corrections and arrears.

The tests that matter are the ones about money not moving when it should not:
a published structure that cannot be edited, a run that cannot double-bill, a
part-paid invoice that cannot be voided, and a balance that cannot be typed in.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.security import hash_password
from app.main import app
from tests.conftest import (
    add_invoice_line,
    make_academic_year,
    make_class_group,
    make_enrolment,
    make_invoice,
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
    """A school with two children on the roll and one published structure."""
    suffix = uuid4().hex[:6]
    school_id = await make_school(owner_session, name="Mwenge", subdomain=f"fee{suffix}")
    subdomain = await owner_session.scalar(
        text("SELECT subdomain FROM school WHERE id = :id"), {"id": school_id}
    )

    year_id = await make_academic_year(owner_session, school_id=school_id, label="2027")
    term_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO term (id, school_id, academic_year_id, index, label, starts_on, "
            " ends_on, is_current, created_at, updated_at) "
            "VALUES (:id, :school_id, :year, 1, 'Term 1', '2027-01-08', '2027-04-02', "
            " true, now(), now())"
        ),
        {"id": term_id, "school_id": school_id, "year": year_id},
    )
    level_id = await make_level(
        owner_session, school_id=school_id, code="STD4", name="Standard 4", order_index=4
    )
    other_level = await make_level(
        owner_session, school_id=school_id, code="STD5", name="Standard 5", order_index=5
    )
    class_group_id = await make_class_group(
        owner_session,
        school_id=school_id,
        academic_year_id=year_id,
        level_id=level_id,
        name="Standard 4 Blue",
    )

    students = []
    for index in range(2):
        student_id = await make_student(
            owner_session,
            school_id=school_id,
            admission_no=f"M/{suffix}/{index}",
            first_name=f"Child{index}",
        )
        await make_enrolment(
            owner_session,
            school_id=school_id,
            student_id=student_id,
            academic_year_id=year_id,
            class_group_id=class_group_id,
        )
        students.append(student_id)

    emails = {
        "bursar": f"bursar-{suffix}@mwenge.ac.tz",
        "teacher": f"teacher-{suffix}@mwenge.ac.tz",
    }
    await _add_user(
        owner_session,
        school_id=school_id,
        name="Salome",
        role="bursar",
        email=emails["bursar"],
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
        "term_id": term_id,
        "level_id": level_id,
        "other_level": other_level,
        "class_group_id": class_group_id,
        "students": students,
        **{f"{k}_email": v for k, v in emails.items()},
    }

    await owner_session.rollback()
    await owner_session.execute(text("DELETE FROM school WHERE id = :id"), {"id": school_id})
    await owner_session.commit()


def headers(subdomain: str) -> dict[str, str]:
    return {"X-School-Subdomain": subdomain}


async def token(client, *, email, subdomain) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": PASSWORD},
        headers=headers(subdomain),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}", **headers(subdomain)}


@pytest.fixture
async def bursar(client, setup) -> dict[str, str]:
    return await token(client, email=setup["bursar_email"], subdomain=setup["subdomain"])


async def publish_structure(client, bursar, setup, *, level_id=None, items=None):
    body = {
        "academic_year_id": str(setup["year_id"]),
        "level_id": str(level_id or setup["level_id"]),
        "label": "Standard 4 2027",
        "items": items
        if items is not None
        else [
            {
                "name": "Tuition",
                "amount": "240000",
                "category": "tuition",
                "frequency": "per_term",
            },
            {
                "name": "Examination",
                "amount": "40000",
                "category": "examination",
                "frequency": "per_term",
            },
        ],
    }
    created = await client.post("/api/v1/fees/structures", json=body, headers=bursar)
    assert created.status_code == 201, created.text
    structure_id = created.json()["structure"]["id"]

    published = await client.post(
        f"/api/v1/fees/structures/{structure_id}/publish", headers=bursar
    )
    assert published.status_code == 200, published.text
    return structure_id


# --------------------------------------------------------------------------
# Fee structures
# --------------------------------------------------------------------------
async def test_a_structure_starts_as_a_draft(client, setup, bursar) -> None:
    """Nothing can be invoiced from a draft."""
    created = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "Standard 4 2027",
            "items": [
                {
                    "name": "Tuition",
                    "amount": "240000",
                    "category": "tuition",
                    "frequency": "per_term",
                }
            ],
        },
        headers=bursar,
    )

    assert created.status_code == 201
    assert created.json()["structure"]["status"] == "draft"


async def test_an_empty_structure_cannot_be_published(client, setup, bursar) -> None:
    """A term run would bill every child nothing and look as though it
    worked, which is worse than failing."""
    created = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "Empty",
            "items": [],
        },
        headers=bursar,
    )

    published = await client.post(
        f"/api/v1/fees/structures/{created.json()['structure']['id']}/publish",
        headers=bursar,
    )

    assert published.status_code == 422
    assert "no fee items cannot be published" in published.json()["detail"]


async def test_a_published_structure_is_frozen(client, setup, bursar) -> None:
    """Invoices already raised would otherwise change retrospectively, and a
    parent who photographed a bill must find the same figure on it three weeks
    later."""
    structure_id = await publish_structure(client, bursar, setup)

    response = await client.put(
        f"/api/v1/fees/structures/{structure_id}/items",
        json={
            "items": [
                {
                    "name": "Tuition",
                    "amount": "1",
                    "category": "tuition",
                    "frequency": "per_term",
                }
            ]
        },
        headers=bursar,
    )

    assert response.status_code == 422
    assert "cannot be edited" in response.json()["detail"]


async def test_a_draft_can_be_edited_and_lists_its_total(client, setup, bursar) -> None:
    created = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "Standard 4 2027",
            "items": [],
        },
        headers=bursar,
    )
    structure_id = created.json()["structure"]["id"]

    await client.put(
        f"/api/v1/fees/structures/{structure_id}/items",
        json={
            "items": [
                {
                    "name": "Tuition",
                    "amount": "240000",
                    "category": "tuition",
                    "frequency": "per_term",
                },
                {
                    "name": "Meals",
                    "amount": "90000",
                    "category": "meals",
                    "frequency": "per_term",
                },
            ]
        },
        headers=bursar,
    )
    listed = await client.get("/api/v1/fees/structures", headers=bursar)

    row = next(r for r in listed.json() if r["id"] == structure_id)
    assert row["item_count"] == 2
    assert Decimal(row["total_amount"]) == Decimal("330000.00")


async def test_a_draft_can_be_read_back_item_by_item(client, setup, bursar) -> None:
    """Without this a draft could be reopened only by whoever still had the
    create response in front of them, so closing the tab lost the work."""
    created = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "Standard 4 2027",
            "items": [
                {
                    "name": "Tuition",
                    "amount": "240000",
                    "category": "tuition",
                    "frequency": "per_term",
                },
                {
                    "name": "Registration",
                    "amount": "40000",
                    "category": "admission",
                    "frequency": "one_off",
                },
            ],
        },
        headers=bursar,
    )
    structure_id = created.json()["structure"]["id"]

    read = await client.get(f"/api/v1/fees/structures/{structure_id}", headers=bursar)

    assert read.status_code == 200, read.text
    items = {item["name"]: item for item in read.json()["items"]}
    assert set(items) == {"Tuition", "Registration"}
    assert Decimal(items["Tuition"]["amount"]) == Decimal("240000.00")
    # The frequency comes back, because it is what decides whether a line
    # lands on a term invoice or only on the one a child joins with.
    assert items["Registration"]["frequency"] == "one_off"


async def test_a_structure_says_how_many_children_it_would_bill(
    client, setup, bursar
) -> None:
    """The figure on an irreversible button. Two children are on the roll at
    this level, so publishing it affects two and no more."""
    created = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "Standard 4 2027",
            "items": [],
        },
        headers=bursar,
    )
    structure_id = created.json()["structure"]["id"]
    assert created.json()["enrolled_count"] == 2

    # And the same number on the list, which is the other place it is read.
    listed = await client.get("/api/v1/fees/structures", headers=bursar)
    row = next(r for r in listed.json() if r["id"] == structure_id)
    assert row["enrolled_count"] == 2


async def test_a_structure_for_an_empty_level_says_nobody(client, setup, bursar) -> None:
    """Standard 5 exists as a level and has no class group, so no child is in
    it. Publishing prices for it is harmless and the screen should say so
    rather than imply a run will bill somebody."""
    created = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["other_level"]),
            "label": "Standard 5 2027",
            "items": [],
        },
        headers=bursar,
    )

    assert created.json()["enrolled_count"] == 0


async def test_one_level_cannot_be_priced_twice(client, setup, bursar) -> None:
    """Two structures for one level bill every child in it from both.

    The unique constraint on (year, level, stream) was meant to stop this and
    did not: PostgreSQL treats NULL as distinct in a unique index, and a
    level-wide structure has a NULL stream — so the constraint applied to the
    stream-scoped case only, which is the rare one. Every structure a school
    actually writes was unconstrained.

    What it costs if it gets through: the term run keys published items by
    (level, stream), so two level-wide structures merge into one list and the
    whole level is billed the sum of both.
    """
    await publish_structure(client, bursar, setup)

    second = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "Standard 4 2027, again",
            "items": [
                {
                    "name": "Tuition",
                    "amount": "240000",
                    "category": "tuition",
                    "frequency": "per_term",
                }
            ],
        },
        headers=bursar,
    )

    assert second.status_code == 422, second.text
    assert "already" in second.json()["detail"]


async def test_a_stream_may_still_be_priced_differently(
    client, setup, bursar, owner_session
) -> None:
    """The level-wide structure and a boarders' structure are not duplicates of
    each other. Refusing the second would take away the one thing a stream
    scope is for."""
    await publish_structure(client, bursar, setup)
    stream_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO stream (id, school_id, level_id, name, is_boarding, "
            " created_at, updated_at) "
            "VALUES (:id, :school, :level, 'Boarding', true, now(), now())"
        ),
        {"id": stream_id, "school": setup["school_id"], "level": setup["level_id"]},
    )
    await owner_session.commit()

    scoped = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "stream_id": str(stream_id),
            "label": "Standard 4 boarders 2027",
            "items": [
                {
                    "name": "Boarding",
                    "amount": "450000",
                    "category": "boarding",
                    "frequency": "per_term",
                }
            ],
        },
        headers=bursar,
    )

    assert scoped.status_code == 201, scoped.text


async def test_a_discarded_draft_does_not_block_a_second_attempt(
    client, setup, bursar
) -> None:
    """Otherwise a bursar who started one against the wrong level could never
    start the right one."""
    first = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "First attempt",
            "items": [],
        },
        headers=bursar,
    )
    await client.post(
        f"/api/v1/fees/structures/{first.json()['structure']['id']}/archive",
        headers=bursar,
    )

    second = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "Second attempt",
            "items": [],
        },
        headers=bursar,
    )

    assert second.status_code == 201, second.text


async def test_a_draft_can_be_discarded_and_leaves_the_list(client, setup, bursar) -> None:
    """A level that is "not ready to bill" must mean exactly that, so a draft
    started against the wrong level has to be removable."""
    created = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["level_id"]),
            "label": "Started by mistake",
            "items": [],
        },
        headers=bursar,
    )
    structure_id = created.json()["structure"]["id"]

    discarded = await client.post(
        f"/api/v1/fees/structures/{structure_id}/archive", headers=bursar
    )

    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["structure"]["status"] == "archived"

    listed = await client.get("/api/v1/fees/structures", headers=bursar)
    assert all(row["id"] != structure_id for row in listed.json())


async def test_a_published_structure_cannot_be_discarded(client, setup, bursar) -> None:
    """Invoices have been raised from it. A price an invoice was raised from
    must stay findable, which is the same rule that freezes editing."""
    structure_id = await publish_structure(client, bursar, setup)

    discarded = await client.post(
        f"/api/v1/fees/structures/{structure_id}/archive", headers=bursar
    )

    assert discarded.status_code == 422
    assert "cannot be discarded" in discarded.json()["detail"]


async def test_discarding_a_draft_does_not_disturb_the_run(client, setup, bursar) -> None:
    """A draft discarded somewhere else in the school must leave the run
    exactly as it was. It reads published rows only, so a level nobody is
    enrolled in losing its draft changes nothing about the level that is."""
    await publish_structure(client, bursar, setup)
    spare = await client.post(
        "/api/v1/fees/structures",
        json={
            "academic_year_id": str(setup["year_id"]),
            "level_id": str(setup["other_level"]),
            "label": "Standard 5, abandoned",
            "items": [],
        },
        headers=bursar,
    )
    await client.post(
        f"/api/v1/fees/structures/{spare.json()['structure']['id']}/archive",
        headers=bursar,
    )

    run = await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": True},
        headers=bursar,
    )

    assert run.status_code == 200, run.text
    assert run.json()["raised"] == 2, "both children still priced"
    assert run.json()["problems"] == []


# --------------------------------------------------------------------------
# The term run
# --------------------------------------------------------------------------
async def test_a_preview_writes_nothing(client, setup, bursar) -> None:
    await publish_structure(client, bursar, setup)

    preview = await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": True},
        headers=bursar,
    )
    invoices = await client.get(
        "/api/v1/fees/invoices", params={"term_id": str(setup["term_id"])}, headers=bursar
    )

    assert preview.json()["raised"] == 2
    assert Decimal(preview.json()["total_billed"]) == Decimal("560000.00")
    assert invoices.json()["total"] == 0, "a preview must not raise an invoice"


async def test_the_run_raises_one_invoice_per_child(client, setup, bursar) -> None:
    await publish_structure(client, bursar, setup)

    run = await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": False},
        headers=bursar,
    )
    invoices = await client.get(
        "/api/v1/fees/invoices", params={"term_id": str(setup["term_id"])}, headers=bursar
    )

    assert run.json()["raised"] == 2
    assert invoices.json()["total"] == 2
    assert Decimal(invoices.json()["items"][0]["billed"]) == Decimal("280000.00")


async def test_running_it_twice_does_not_double_bill(client, setup, bursar) -> None:
    """A run over 500 children will be interrupted eventually, and running it
    again has to be the safe recovery."""
    await publish_structure(client, bursar, setup)
    await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": False},
        headers=bursar,
    )

    again = await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": False},
        headers=bursar,
    )
    invoices = await client.get(
        "/api/v1/fees/invoices", params={"term_id": str(setup["term_id"])}, headers=bursar
    )

    assert again.json()["raised"] == 0
    assert again.json()["skipped"] == 2
    assert invoices.json()["total"] == 2


async def test_a_level_with_no_published_structure_is_named_in_the_problems(
    client, setup, bursar, owner_session
) -> None:
    """The case the preview exists to surface: billing nothing would look like
    success."""
    await publish_structure(client, bursar, setup)

    # A second class at a level nobody has published fees for.
    other_group = await make_class_group(
        owner_session,
        school_id=setup["school_id"],
        academic_year_id=setup["year_id"],
        level_id=setup["other_level"],
        name="Standard 5 Blue",
    )
    stranded = await make_student(
        owner_session, school_id=setup["school_id"], admission_no=f"M/{uuid4().hex[:6]}"
    )
    await make_enrolment(
        owner_session,
        school_id=setup["school_id"],
        student_id=stranded,
        academic_year_id=setup["year_id"],
        class_group_id=other_group,
    )
    await owner_session.commit()

    preview = await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": True},
        headers=bursar,
    )

    problems = preview.json()["problems"]
    assert len(problems) == 1
    assert problems[0]["student_id"] == str(stranded)
    assert "no published fee structure" in problems[0]["reason"]


async def test_a_standing_discount_lands_on_the_invoice(
    client, setup, bursar, owner_session
) -> None:
    """Held against the child, so next term's run applies it without anybody
    remembering to."""
    await publish_structure(client, bursar, setup)
    await owner_session.execute(
        text(
            "INSERT INTO fee_discount (id, school_id, student_id, label, percent, "
            " starts_on, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, :student, 'Staff child', 50, "
            " '2027-01-01', now(), now())"
        ),
        {"school_id": setup["school_id"], "student": setup["students"][0]},
    )
    await owner_session.commit()

    await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": False, "issued_on": "2027-01-08"},
        headers=bursar,
    )
    invoices = await client.get(
        "/api/v1/fees/invoices",
        params={"student_id": str(setup["students"][0])},
        headers=bursar,
    )
    detail = await client.get(
        f"/api/v1/fees/invoices/{invoices.json()['items'][0]['id']}", headers=bursar
    )

    kinds = {line["kind"] for line in detail.json()["lines"]}
    assert "discount" in kinds
    # 280,000 of charges less half of it.
    assert Decimal(detail.json()["billed"]) == Decimal("140000.00")


async def test_a_class_teacher_cannot_run_a_term_or_read_invoices(
    client, setup
) -> None:
    teacher = await token(client, email=setup["teacher_email"], subdomain=setup["subdomain"])

    run = await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": True},
        headers=teacher,
    )
    invoices = await client.get("/api/v1/fees/invoices", headers=teacher)

    assert run.status_code == 403
    assert invoices.status_code == 403


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------
async def invoice_for(client, bursar, setup, index: int = 0) -> str:
    await publish_structure(client, bursar, setup)
    # Issued on the first day of term, so the due date — and therefore every
    # ageing assertion below — does not depend on the day the suite runs.
    await client.post(
        f"/api/v1/fees/terms/{setup['term_id']}/run",
        json={"dry_run": False, "issued_on": "2027-01-08"},
        headers=bursar,
    )
    invoices = await client.get(
        "/api/v1/fees/invoices",
        params={"student_id": str(setup["students"][index])},
        headers=bursar,
    )
    return invoices.json()["items"][0]["id"]


async def test_a_payment_moves_the_balance(client, setup, bursar) -> None:
    invoice_id = await invoice_for(client, bursar, setup)

    paid = await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments",
        json={"amount": "100000", "receipt": "RCPT-1", "channel": "mpesa"},
        headers=bursar,
    )

    assert paid.status_code == 201
    assert Decimal(paid.json()["settled"]) == Decimal("100000.00")
    assert Decimal(paid.json()["balance"]) == Decimal("180000.00")


async def test_the_same_receipt_twice_is_refused(client, setup, bursar) -> None:
    """Entering the same receipt twice must be caught rather than doubling a
    credit."""
    invoice_id = await invoice_for(client, bursar, setup)
    body = {"amount": "50000", "receipt": "RCPT-SAME", "channel": "counter"}

    first = await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments", json=body, headers=bursar
    )
    second = await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments", json=body, headers=bursar
    )

    assert first.status_code == 201
    assert second.status_code == 422
    assert "already been recorded" in second.json()["detail"]


async def test_a_balance_cannot_be_typed_in_through_the_api(
    client, setup, bursar, owner_session
) -> None:
    """There is no endpoint that sets a balance, and the database would
    discard it anyway. This checks the whole path, not just the trigger."""
    invoice_id = await invoice_for(client, bursar, setup)

    await owner_session.execute(
        text("UPDATE invoice SET settled = billed WHERE id = CAST(:id AS uuid)"),
        {"id": invoice_id},
    )
    await owner_session.commit()

    detail = await client.get(f"/api/v1/fees/invoices/{invoice_id}", headers=bursar)

    assert Decimal(detail.json()["settled"]) == Decimal("0.00")


async def test_a_part_paid_invoice_cannot_be_voided(client, setup, bursar) -> None:
    """A payment attached to a bill that officially does not exist is worse
    than the mistake it was meant to fix."""
    invoice_id = await invoice_for(client, bursar, setup)
    await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments",
        json={"amount": "50000", "receipt": "RCPT-2"},
        headers=bursar,
    )

    voided = await client.post(
        f"/api/v1/fees/invoices/{invoice_id}/void",
        json={"reason": "Raised against the wrong term"},
        headers=bursar,
    )

    assert voided.status_code == 422
    assert "credit note" in voided.json()["detail"]


async def test_an_unpaid_invoice_can_be_voided_and_leaves_the_record(
    client, setup, bursar
) -> None:
    """Voiding rather than deleting: a bill a parent has already seen has to
    stay visible, with a reason."""
    invoice_id = await invoice_for(client, bursar, setup)

    voided = await client.post(
        f"/api/v1/fees/invoices/{invoice_id}/void",
        json={"reason": "Child left before term started"},
        headers=bursar,
    )
    detail = await client.get(f"/api/v1/fees/invoices/{invoice_id}", headers=bursar)

    assert voided.status_code == 204
    assert detail.json()["is_void"] is True
    assert detail.json()["void_reason"] == "Child left before term started"
    assert detail.json()["status"] == "void"


async def test_a_credit_note_reduces_the_bill_without_rewriting_it(
    client, setup, bursar
) -> None:
    """The original charge stays, so the bill a parent photographed and the
    bill the school holds remain one document."""
    invoice_id = await invoice_for(client, bursar, setup)

    credited = await client.post(
        f"/api/v1/fees/invoices/{invoice_id}/credit-note",
        json={"amount": "40000", "note": "Sibling discount applied late"},
        headers=bursar,
    )
    detail = await client.get(f"/api/v1/fees/invoices/{invoice_id}", headers=bursar)

    assert credited.status_code == 200
    assert Decimal(credited.json()["billed"]) == Decimal("240000.00")
    charges = [row for row in detail.json()["lines"] if row["kind"] == "charge"]
    assert sum(Decimal(row["amount"]) for row in charges) == Decimal("280000.00")


async def test_a_credit_note_cannot_exceed_the_bill(client, setup, bursar) -> None:
    invoice_id = await invoice_for(client, bursar, setup)

    response = await client.post(
        f"/api/v1/fees/invoices/{invoice_id}/credit-note",
        json={"amount": "900000", "note": "Too much"},
        headers=bursar,
    )

    assert response.status_code == 422
    assert "cannot exceed" in response.json()["detail"]


# --------------------------------------------------------------------------
# Money nobody could place
# --------------------------------------------------------------------------
async def test_suspense_money_is_matched_by_a_bursar(
    client, setup, bursar, owner_session
) -> None:
    """A decision, not a guess. Nothing picks between two siblings with open
    balances."""
    invoice_id = await invoice_for(client, bursar, setup)
    payment_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO payment (id, school_id, invoice_id, external_ref, amount, "
            " applied, currency, channel, state, msisdn, received_at, is_manual, reason, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, NULL, :ref, 150000, 0, 'TZS', 'mpesa', 'settled', "
            " '0712445109', now(), false, 'no open invoice matched this payment', "
            " now(), now())"
        ),
        {"id": payment_id, "school_id": setup["school_id"], "ref": f"MOCK-{uuid4().hex[:10]}"},
    )
    await owner_session.commit()

    waiting = await client.get("/api/v1/payments/suspense", headers=bursar)
    assert len(waiting.json()) == 1

    matched = await client.post(
        f"/api/v1/payments/{payment_id}/match",
        json={"invoice_id": invoice_id},
        headers=bursar,
    )

    assert matched.status_code == 200
    assert Decimal(matched.json()["settled"]) == Decimal("150000.00")

    empty = await client.get("/api/v1/payments/suspense", headers=bursar)
    assert empty.json() == []


async def test_a_payment_already_against_an_invoice_cannot_be_rematched(
    client, setup, bursar
) -> None:
    invoice_id = await invoice_for(client, bursar, setup)
    paid = await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments",
        json={"amount": "50000", "receipt": "RCPT-3"},
        headers=bursar,
    )
    assert paid.status_code == 201

    payments = await client.get(f"/api/v1/fees/invoices/{invoice_id}", headers=bursar)
    payment_id = payments.json()["payments"][0]["id"]

    response = await client.post(
        f"/api/v1/payments/{payment_id}/match",
        json={"invoice_id": invoice_id},
        headers=bursar,
    )

    assert response.status_code == 422
    assert "already against an invoice" in response.json()["detail"]


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
async def test_arrears_are_read_against_a_given_day(client, setup, bursar) -> None:
    """A month-end report run in May for April must not quietly use today, or
    the ageing buckets are wrong and the one number a bursar is judged on stops
    reconciling."""
    await invoice_for(client, bursar, setup)

    before_due = await client.get(
        "/api/v1/fees/arrears", params={"as_of": "2027-01-10"}, headers=bursar
    )
    just_after = await client.get(
        "/api/v1/fees/arrears", params={"as_of": "2027-02-10"}, headers=bursar
    )
    much_later = await client.get(
        "/api/v1/fees/arrears", params={"as_of": "2027-06-01"}, headers=bursar
    )

    assert before_due.json()["total"] == 0, "nothing is overdue before its due date"
    assert just_after.json()["total"] == 2
    assert just_after.json()["items"][0]["bucket"] == "1-30"
    assert much_later.json()["items"][0]["bucket"] == "over-60"


async def test_arrears_are_one_row_per_child(
    client, setup, bursar, owner_session
) -> None:
    """A bursar chasing arrears rings a family, so three overdue bills for one
    child is one row and one phone call."""
    await invoice_for(client, bursar, setup)
    # A second overdue bill for the same child.
    second = await make_invoice(
        owner_session,
        school_id=setup["school_id"],
        student_id=setup["students"][0],
        enrolment_id=await owner_session.scalar(
            text("SELECT id FROM enrolment WHERE student_id = :s"),
            {"s": setup["students"][0]},
        ),
        reference=f"INV/EXTRA/{uuid4().hex[:6]}",
    )
    await add_invoice_line(
        owner_session,
        school_id=setup["school_id"],
        invoice_id=second,
        item="Uniform",
        amount="35000",
    )
    await owner_session.commit()

    arrears = await client.get(
        "/api/v1/fees/arrears", params={"as_of": "2027-03-01"}, headers=bursar
    )

    body = arrears.json()
    assert body["total"] == 2, "two children, not three invoices"
    owing = next(r for r in body["items"] if r["student_id"] == str(setup["students"][0]))
    assert owing["overdue_invoices"] == 2
    assert Decimal(owing["balance"]) == Decimal("315000.00")


async def test_the_collection_tile_adds_up(client, setup, bursar) -> None:
    invoice_id = await invoice_for(client, bursar, setup)
    await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments",
        json={"amount": "140000", "receipt": "RCPT-4"},
        headers=bursar,
    )

    collection = await client.get("/api/v1/fees/collection", headers=bursar)

    body = collection.json()
    assert Decimal(body["billed"]) == Decimal("560000.00")
    assert Decimal(body["settled"]) == Decimal("140000.00")
    assert Decimal(body["outstanding"]) == Decimal("420000.00")
    assert body["collected_percent"] == 25


async def test_a_statement_shows_every_bill_a_child_has_had(
    client, setup, bursar
) -> None:
    invoice_id = await invoice_for(client, bursar, setup)
    await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments",
        json={"amount": "80000", "receipt": "RCPT-5"},
        headers=bursar,
    )

    statement = await client.get(
        f"/api/v1/fees/students/{setup['students'][0]}/statement", headers=bursar
    )

    body = statement.json()
    assert len(body["invoices"]) == 1
    assert Decimal(body["billed"]) == Decimal("280000.00")
    assert Decimal(body["settled"]) == Decimal("80000.00")
    assert Decimal(body["balance"]) == Decimal("200000.00")


async def test_a_voided_invoice_does_not_count_towards_a_balance(
    client, setup, bursar
) -> None:
    """It stays on the statement, with its reason, and counts for nothing."""
    invoice_id = await invoice_for(client, bursar, setup)
    await client.post(
        f"/api/v1/fees/invoices/{invoice_id}/void",
        json={"reason": "Raised in error"},
        headers=bursar,
    )

    statement = await client.get(
        f"/api/v1/fees/students/{setup['students'][0]}/statement", headers=bursar
    )

    body = statement.json()
    assert len(body["invoices"]) == 1, "the record stays visible"
    assert body["invoices"][0]["is_void"] is True
    assert Decimal(body["balance"]) == Decimal("0.00")


# --------------------------------------------------------------------------
# What the dashboard reads
# --------------------------------------------------------------------------
async def test_the_arrears_tile_separates_the_long_overdue(
    client, setup, bursar
) -> None:
    """"61 students, 9 over 60 days" is two different problems. A school whose
    arrears are all under a month has a timing problem; one with a tail past
    sixty days has a collection problem, and the tile has to tell them apart.

    Both bills fall due on 2027-01-22. Read on 10 February they are nineteen
    days late; read on 1 June they are past sixty.
    """
    await invoice_for(client, bursar, setup)

    recent = await client.get(
        "/api/v1/fees/collection", params={"as_of": "2027-02-10"}, headers=bursar
    )
    stale = await client.get(
        "/api/v1/fees/collection", params={"as_of": "2027-06-01"}, headers=bursar
    )

    assert recent.json()["students_in_arrears"] == 2
    assert recent.json()["students_over_60_days"] == 0
    assert stale.json()["students_in_arrears"] == 2
    assert stale.json()["students_over_60_days"] == 2


async def test_the_tiles_and_the_list_beneath_them_read_the_same_day(
    client, setup, bursar
) -> None:
    """Two different arrears figures on one screen is how a bursar stops
    believing either of them."""
    await invoice_for(client, bursar, setup)

    tiles = await client.get(
        "/api/v1/fees/collection", params={"as_of": "2027-02-10"}, headers=bursar
    )
    listed = await client.get(
        "/api/v1/fees/arrears", params={"as_of": "2027-02-10"}, headers=bursar
    )

    assert tiles.json()["students_in_arrears"] == listed.json()["total"]


async def test_nothing_overdue_before_the_due_date(client, setup, bursar) -> None:
    await invoice_for(client, bursar, setup)

    body = (
        await client.get(
            "/api/v1/fees/collection", params={"as_of": "2027-01-10"}, headers=bursar
        )
    ).json()

    assert body["students_in_arrears"] == 0
    assert body["students_over_60_days"] == 0
    assert Decimal(body["overdue"]) == Decimal("0.00")


async def test_the_payments_feed_names_the_child_not_the_invoice(
    client, setup, bursar
) -> None:
    """A bursar recognises "Child0 Mwanjala, Standard 4 Blue" and recognises
    nothing at all about INV/Term1/0001."""
    invoice_id = await invoice_for(client, bursar, setup)
    await client.post(
        f"/api/v1/payments/invoices/{invoice_id}/payments",
        json={"amount": "120000", "receipt": "RCPT-FEED", "channel": "mpesa"},
        headers=bursar,
    )

    feed = await client.get("/api/v1/payments/recent", headers=bursar)

    row = feed.json()[0]
    assert row["student_name"].startswith("Child0")
    assert row["class_name"] == "Standard 4 Blue"
    assert Decimal(row["amount"]) == Decimal("120000.00")
    assert row["channel"] == "mpesa"


async def test_the_feed_is_newest_first(client, setup, bursar) -> None:
    invoice_id = await invoice_for(client, bursar, setup)
    for index, amount in enumerate(("10000", "20000", "30000")):
        await client.post(
            f"/api/v1/payments/invoices/{invoice_id}/payments",
            json={"amount": amount, "receipt": f"RCPT-{index}"},
            headers=bursar,
        )

    feed = await client.get("/api/v1/payments/recent", headers=bursar)

    assert [Decimal(row["amount"]) for row in feed.json()] == [
        Decimal("30000.00"),
        Decimal("20000.00"),
        Decimal("10000.00"),
    ]


async def test_the_feed_is_behind_payment_read(client, setup) -> None:
    """A teacher who can see one child's balance has no business reading the
    school's takings."""
    teacher = await token(client, email=setup["teacher_email"], subdomain=setup["subdomain"])

    response = await client.get("/api/v1/payments/recent", headers=teacher)

    assert response.status_code == 403


async def test_an_invoice_says_who_it_is_for(client, setup, bursar) -> None:
    """The header reads "Frank Lyimo · Standard 6 Blue · due 31 January". The
    detail response carried none of that, so the screen had to fetch the child
    separately — a second request and a second moment, with the name and the
    balance read from different seconds of a term run."""
    invoice_id = await invoice_for(client, bursar, setup)

    detail = await client.get(f"/api/v1/fees/invoices/{invoice_id}", headers=bursar)

    body = detail.json()
    assert body["student_name"].startswith("Child0")
    assert body["class_name"] == "Standard 4 Blue"
    assert body["term_label"] == "Term 1"
    assert body["admission_no"]


async def test_an_invoice_with_no_term_comes_back_with_nulls_not_an_error(
    client, setup, bursar, owner_session
) -> None:
    """A deposit is raised at an offer, before there is a term to bill in. The
    joins have to be outer ones, or the detail screen 500s on the first bill a
    school ever raises."""
    enrolment_id = await owner_session.scalar(
        text("SELECT id FROM enrolment WHERE student_id = :s"),
        {"s": setup["students"][0]},
    )
    invoice_id = uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO invoice (id, school_id, reference, kind, student_id, "
            " enrolment_id, term_id, issued_on, due_date, currency, billed, settled, "
            " is_void, created_at, updated_at) "
            "VALUES (:id, :school_id, :ref, 'deposit', :student, :enrolment, NULL, "
            " current_date, current_date + 14, 'TZS', 0, 0, false, now(), now())"
        ),
        {
            "id": invoice_id,
            "school_id": setup["school_id"],
            "ref": f"DEP-{uuid4().hex[:6]}",
            "student": setup["students"][0],
            "enrolment": enrolment_id,
        },
    )
    await owner_session.commit()

    detail = await client.get(f"/api/v1/fees/invoices/{invoice_id}", headers=bursar)

    assert detail.status_code == 200, detail.text
    assert detail.json()["term_label"] is None
    assert detail.json()["academic_year"] is None
    assert detail.json()["student_name"].startswith("Child0")
