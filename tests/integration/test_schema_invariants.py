"""The invariants the database enforces, tested against the database.

`tests/unit/` covers the rules that are pure functions. This file covers the
four that are deliberately *not* in Python, because being right in Python only
helps while every write goes through Python:

* an invoice's billed and settled totals are derived, and cannot be assigned;
* a payment's external_ref is unique per school, so a replay credits once;
* a child has at most one active enrolment;
* a child has at most one primary guardian, and a school one current year.

Each test writes as the table owner, which bypasses row-level security on
purpose — isolation is tested in `test_tenant_isolation.py`, and mixing the two
concerns makes a failure here ambiguous.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import (
    add_invoice_line,
    add_payment,
    make_academic_year,
    make_class_group,
    make_enrolment,
    make_invoice,
    make_level,
    make_school,
    make_student,
)


@pytest.fixture
async def school(owner_session):
    """A school with a year, a level, a class and one enrolled child."""
    school_id = await make_school(owner_session, name="Mwenge Primary", subdomain="mwenge-inv")
    year_id = await make_academic_year(owner_session, school_id=school_id)
    level_id = await make_level(owner_session, school_id=school_id)
    group_id = await make_class_group(
        owner_session, school_id=school_id, academic_year_id=year_id, level_id=level_id
    )
    student_id = await make_student(owner_session, school_id=school_id, admission_no=f"M/{uuid4().hex[:8]}")
    enrolment_id = await make_enrolment(
        owner_session,
        school_id=school_id,
        student_id=student_id,
        academic_year_id=year_id,
        class_group_id=group_id,
    )
    await owner_session.commit()

    yield {
        "school_id": school_id,
        "year_id": year_id,
        "level_id": level_id,
        "group_id": group_id,
        "student_id": student_id,
        "enrolment_id": enrolment_id,
    }

    await owner_session.rollback()
    await owner_session.execute(
        text("DELETE FROM school WHERE id = :id"), {"id": school_id}
    )
    await owner_session.commit()


async def _totals(session, invoice_id) -> tuple[Decimal, Decimal]:
    row = (
        await session.execute(
            text("SELECT billed, settled FROM invoice WHERE id = :id"), {"id": invoice_id}
        )
    ).one()
    return row.billed, row.settled


# --------------------------------------------------------------------------
# An invoice's totals are derived
# --------------------------------------------------------------------------
async def test_billed_follows_the_lines(owner_session, school) -> None:
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Tuition, Term 1",
        amount="280000",
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Examination",
        amount="40000",
    )

    billed, settled = await _totals(owner_session, invoice_id)

    assert billed == Decimal("320000.00")
    assert settled == Decimal("0.00")


async def test_a_discount_line_subtracts(owner_session, school) -> None:
    """The sign comes from the kind, never from the typist."""
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
        reference="INV-0002",
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Tuition, Term 1",
        amount="280000",
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Staff child discount",
        amount="80000",
        kind="discount",
    )

    billed, _ = await _totals(owner_session, invoice_id)

    assert billed == Decimal("200000.00")


async def test_deleting_a_line_lowers_the_total(owner_session, school) -> None:
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
        reference="INV-0003",
    )
    line_id = await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Transport",
        amount="60000",
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Tuition, Term 1",
        amount="280000",
    )

    await owner_session.execute(
        text("DELETE FROM invoice_line WHERE id = :id"), {"id": line_id}
    )

    billed, _ = await _totals(owner_session, invoice_id)
    assert billed == Decimal("280000.00")


async def test_settled_follows_the_payments(owner_session, school) -> None:
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
        reference="INV-0004",
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Tuition, Term 1",
        amount="320000",
    )
    await add_payment(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        external_ref="MP-0001",
        amount="180000",
    )

    billed, settled = await _totals(owner_session, invoice_id)

    assert billed == Decimal("320000.00")
    assert settled == Decimal("180000.00")


async def test_a_reversed_payment_stops_counting(owner_session, school) -> None:
    """A reversal is a state change with a reason, never a deletion — and the
    balance has to move back."""
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
        reference="INV-0005",
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Tuition, Term 1",
        amount="320000",
    )
    payment_id = await add_payment(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        external_ref="MP-0002",
        amount="320000",
    )
    assert (await _totals(owner_session, invoice_id))[1] == Decimal("320000.00")

    await owner_session.execute(
        text(
            "UPDATE payment SET state = 'reversed', reversed_at = now(), "
            "       reversal_reason = 'charged back by the aggregator' "
            " WHERE id = :id"
        ),
        {"id": payment_id},
    )

    _, settled = await _totals(owner_session, invoice_id)
    assert settled == Decimal("0.00")


async def test_a_pending_payment_does_not_settle_anything(owner_session, school) -> None:
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
        reference="INV-0006",
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Tuition, Term 1",
        amount="320000",
    )
    await add_payment(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        external_ref="MP-0003",
        amount="100000",
        state="pending",
    )

    _, settled = await _totals(owner_session, invoice_id)
    assert settled == Decimal("0.00")


async def test_a_balance_cannot_be_typed_in(owner_session, school) -> None:
    """The one that makes the comment on the column true.

    Every school system is eventually asked to "just correct the balance".
    This proves there is no way to: the UPDATE succeeds and changes nothing,
    so a correction has to be a credit note or a payment, both of which leave
    a trail.
    """
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
        reference="INV-0007",
    )
    await add_invoice_line(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        item="Tuition, Term 1",
        amount="320000",
    )

    await owner_session.execute(
        text("UPDATE invoice SET billed = 0, settled = 320000 WHERE id = :id"),
        {"id": invoice_id},
    )

    billed, settled = await _totals(owner_session, invoice_id)
    assert billed == Decimal("320000.00")
    assert settled == Decimal("0.00")


async def test_other_invoice_fields_still_update(owner_session, school) -> None:
    """The guard is narrow: it protects two columns, not the whole row."""
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
        reference="INV-0008",
    )

    await owner_session.execute(
        text(
            "UPDATE invoice SET is_void = true, void_reason = 'raised against the wrong term' "
            " WHERE id = :id"
        ),
        {"id": invoice_id},
    )

    voided = await owner_session.scalar(
        text("SELECT is_void FROM invoice WHERE id = :id"), {"id": invoice_id}
    )
    assert voided is True


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------
async def test_a_replayed_callback_cannot_credit_twice(owner_session, school) -> None:
    """Aggregators replay. This is what makes the second one harmless."""
    invoice_id = await make_invoice(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        enrolment_id=school["enrolment_id"],
        reference="INV-0009",
    )
    await add_payment(
        owner_session,
        school_id=school["school_id"],
        invoice_id=invoice_id,
        external_ref="SELCOM-77421",
        amount="150000",
    )

    with pytest.raises(IntegrityError):
        await add_payment(
            owner_session,
            school_id=school["school_id"],
            invoice_id=invoice_id,
            external_ref="SELCOM-77421",
            amount="150000",
        )
    await owner_session.rollback()


async def test_two_schools_may_hold_the_same_external_ref(owner_session, school) -> None:
    """Uniqueness is per school, not global: two aggregator accounts can
    legitimately issue the same transaction id."""
    other_school = await make_school(
        owner_session, name="Tumaini Primary", subdomain="tumaini-inv"
    )
    await add_payment(
        owner_session,
        school_id=school["school_id"],
        invoice_id=None,
        external_ref="SHARED-REF-1",
        amount="50000",
    )
    await add_payment(
        owner_session,
        school_id=other_school,
        invoice_id=None,
        external_ref="SHARED-REF-1",
        amount="50000",
    )
    await owner_session.commit()

    count = await owner_session.scalar(
        text("SELECT count(*) FROM payment WHERE external_ref = 'SHARED-REF-1'")
    )
    assert count == 2

    await owner_session.execute(
        text("DELETE FROM school WHERE id = :id"), {"id": other_school}
    )
    await owner_session.commit()


async def test_a_manual_payment_must_name_its_author(owner_session, school) -> None:
    """Money entered by hand without a name attached is the start of every
    fee dispute nobody can settle."""
    with pytest.raises(IntegrityError):
        await owner_session.execute(
            text(
                "INSERT INTO payment "
                "(id, school_id, external_ref, amount, applied, currency, channel, "
                " state, received_at, is_manual, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, 'CASH-1', 50000, 50000, 'TZS', "
                " 'counter', 'settled', now(), true, now(), now())"
            ),
            {"school_id": school["school_id"]},
        )
    await owner_session.rollback()


# --------------------------------------------------------------------------
# One active enrolment, one primary guardian, one current year
# --------------------------------------------------------------------------
async def test_a_child_cannot_hold_two_active_enrolments(owner_session, school) -> None:
    next_year = await make_academic_year(
        owner_session, school_id=school["school_id"], label="2028", is_current=False
    )
    next_group = await make_class_group(
        owner_session,
        school_id=school["school_id"],
        academic_year_id=next_year,
        level_id=school["level_id"],
        name="Standard 5 Blue",
    )

    with pytest.raises(IntegrityError):
        await make_enrolment(
            owner_session,
            school_id=school["school_id"],
            student_id=school["student_id"],
            academic_year_id=next_year,
            class_group_id=next_group,
        )
    await owner_session.rollback()


async def test_promotion_closes_one_year_before_opening_the_next(
    owner_session, school
) -> None:
    """The legal version of the previous test: close, then open."""
    next_year = await make_academic_year(
        owner_session, school_id=school["school_id"], label="2028", is_current=False
    )
    next_group = await make_class_group(
        owner_session,
        school_id=school["school_id"],
        academic_year_id=next_year,
        level_id=school["level_id"],
        name="Standard 5 Blue",
    )
    await owner_session.execute(
        text("UPDATE enrolment SET outcome = 'promoted', exited_on = now() WHERE id = :id"),
        {"id": school["enrolment_id"]},
    )

    new_enrolment = await make_enrolment(
        owner_session,
        school_id=school["school_id"],
        student_id=school["student_id"],
        academic_year_id=next_year,
        class_group_id=next_group,
    )

    assert new_enrolment is not None
    history = await owner_session.scalar(
        text("SELECT count(*) FROM enrolment WHERE student_id = :id"),
        {"id": school["student_id"]},
    )
    assert history == 2, "the old enrolment is kept, not overwritten"


async def test_a_child_has_one_primary_guardian(owner_session, school) -> None:
    guardians = []
    for name, phone in (("Asha Mwanjala", "0754118902"), ("Juma Mwanjala", "0754118903")):
        guardian_id = await owner_session.scalar(
            text(
                "INSERT INTO guardian "
                "(id, school_id, full_name, phone, locale, sms_opted_out, "
                " created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :name, :phone, 'en', false, "
                " now(), now()) RETURNING id"
            ),
            {"school_id": school["school_id"], "name": name, "phone": phone},
        )
        guardians.append(guardian_id)

    async def link(guardian_id, is_primary: bool):
        await owner_session.execute(
            text(
                "INSERT INTO guardian_student "
                "(id, school_id, guardian_id, student_id, relationship, is_primary, "
                " receives_invoices, receives_sms, may_collect, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :guardian, :student, 'mother', "
                " :primary, true, true, true, now(), now())"
            ),
            {
                "school_id": school["school_id"],
                "guardian": guardian_id,
                "student": school["student_id"],
                "primary": is_primary,
            },
        )

    await link(guardians[0], True)

    with pytest.raises(IntegrityError):
        await link(guardians[1], True)
    await owner_session.rollback()


async def test_a_school_has_one_current_academic_year(owner_session, school) -> None:
    """Invoicing and promotion both pivot on "the current year"; two of them
    means a term's invoices raised against the wrong one."""
    with pytest.raises(IntegrityError):
        await make_academic_year(
            owner_session, school_id=school["school_id"], label="2028", is_current=True
        )
    await owner_session.rollback()
