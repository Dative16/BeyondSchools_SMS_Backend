"""The standing cross-tenant test.

This one is load-bearing. One school seeing another school's children would end
the business, so these tests try the leak on purpose and must stay
red-sensitive: if someone disables RLS, forgets a policy on a new table, or
points the application's connection string at the owning role, it fails here
first.

Every read below goes through `app_session`, which connects as the restricted
role the application uses. Reads through `owner_session` would pass for the
wrong reason.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.core.tenancy import bind_tenant, platform_scope, unscoped
from app.models import TENANT_TABLES
from tests.conftest import make_school, make_student


@pytest.fixture
async def two_schools(owner_session):
    """School A with one child, school B with another."""
    a = await make_school(owner_session, name="Mwenge Primary", subdomain="mwenge")
    b = await make_school(owner_session, name="Tumaini Primary", subdomain="tumaini")
    await make_student(owner_session, school_id=a, admission_no="A/0001", first_name="Neema")
    await make_student(owner_session, school_id=b, admission_no="B/0001", first_name="Baraka")
    await owner_session.commit()
    yield a, b
    await owner_session.execute(
        text("DELETE FROM school WHERE id = ANY(:ids)"), {"ids": [a, b]}
    )
    await owner_session.commit()


async def test_a_bound_session_cannot_read_another_school(app_session, two_schools) -> None:
    """Bind to school A, query students, get only school A's rows."""
    school_a, _ = two_schools
    await bind_tenant(app_session, school_a)

    rows = (await app_session.execute(text("SELECT admission_no FROM student"))).scalars().all()

    assert rows == ["A/0001"]


async def test_a_query_without_a_where_clause_is_still_scoped(app_session, two_schools) -> None:
    """The point of the whole design: a forgotten filter leaks nothing."""
    _, school_b = two_schools
    await bind_tenant(app_session, school_b)

    count = await app_session.scalar(text("SELECT count(*) FROM student"))
    names = (await app_session.execute(text("SELECT first_name FROM student"))).scalars().all()

    assert count == 1
    assert names == ["Baraka"]


async def test_reading_by_a_known_id_from_another_school_returns_nothing(
    app_session, owner_session, two_schools
) -> None:
    """Knowing the primary key is not authorisation.

    The id could come from a guessed URL or an old bookmark; the policy has to
    stop it, because no amount of application code will be asked to.
    """
    school_a, school_b = two_schools
    other_id = await owner_session.scalar(
        text("SELECT id FROM student WHERE school_id = :sid"), {"sid": school_b}
    )
    await bind_tenant(app_session, school_a)

    found = await app_session.scalar(
        text("SELECT count(*) FROM student WHERE id = :id"), {"id": other_id}
    )

    assert found == 0


async def test_insert_with_a_foreign_school_id_is_rejected(
    app_session, two_schools
) -> None:
    """The WITH CHECK half of the policy, not just USING.

    Writing into another school is as bad as reading from one, and it is the
    half people forget: a policy with only USING lets a bug plant rows in
    somebody else's school that it then cannot see.
    """
    school_a, school_b = two_schools
    await bind_tenant(app_session, school_a)

    with pytest.raises((DBAPIError, ProgrammingError)) as caught:
        await app_session.execute(
            text(
                "INSERT INTO student (id, school_id, admission_no, first_name, surname, "
                " date_of_birth, sex, status, nationality, arrears_brought_forward, "
                " created_at, updated_at) "
                "VALUES (gen_random_uuid(), :sid, 'X/0001', 'Planted', 'Row', "
                " '2016-01-01', 'f', 'active', 'Tanzanian', 0, now(), now())"
            ),
            {"sid": school_b},
        )

    assert "row-level security" in str(caught.value).lower()
    await app_session.rollback()


async def test_an_unbound_session_reads_no_school_data(app_session, two_schools) -> None:
    """No tenant bound means no rows, not all rows.

    A missing dependency should fail closed. The failure mode this rules out is
    an endpoint that forgets `tenant_session` and quietly serves every school.
    """
    async with unscoped(app_session):
        count = await app_session.scalar(text("SELECT count(*) FROM student"))

    assert count == 0


async def test_platform_scope_sees_across_schools_and_rolls_back(
    app_session, two_schools
) -> None:
    """The audited escape hatch works, and cannot commit."""
    async with platform_scope(app_session):
        count = await app_session.scalar(text("SELECT count(*) FROM student"))
        assert count >= 2

    # Out of the block, the flag is gone with the transaction.
    count_after = await app_session.scalar(text("SELECT count(*) FROM student"))
    assert count_after == 0


async def test_the_application_does_not_connect_as_a_table_owner(app_session) -> None:
    """Policies do not apply to a table's owner, so this is the assumption the
    whole scheme rests on. It is checked rather than trusted because the
    failure is silent: everything works, and every school sees everything."""
    row = (
        await app_session.execute(
            text(
                "SELECT current_user, "
                "       pg_has_role(current_user, 'pg_read_all_data', 'member') AS reads_all, "
                "       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super, "
                "       (SELECT count(*) FROM pg_class c "
                "          JOIN pg_roles r ON r.oid = c.relowner "
                "         WHERE r.rolname = current_user AND c.relname = 'student') AS owns_student"
            )
        )
    ).one()

    assert row.is_super is False, "the app must not connect as a superuser"
    assert row.reads_all is False
    assert row.owns_student == 0, "the app must not own the tables it queries"


async def test_every_tenant_table_has_a_policy(owner_session) -> None:
    """A new table that holds school data and nobody protected fails here.

    This is the test that catches the mistake nobody makes deliberately:
    adding a table in a migration six months from now and forgetting the four
    lines of policy that go with it.
    """
    protected = set(
        (
            await owner_session.execute(
                text("SELECT tablename FROM pg_policies WHERE schemaname = 'public'")
            )
        )
        .scalars()
        .all()
    )

    missing = sorted(set(TENANT_TABLES) - protected)

    assert not missing, f"tables carrying school data with no RLS policy: {missing}"


async def test_row_level_security_is_forced_on_every_tenant_table(owner_session) -> None:
    """ENABLE is not enough on its own.

    With ENABLE alone, the owning role bypasses every policy — which is
    exactly what happens if a deployment is ever misconfigured to connect as
    the owner. FORCE closes that, and this test is what keeps it closed.
    """
    rows = (
        await owner_session.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE n.nspname = 'public' AND relname = ANY(:tables)"
            ),
            {"tables": list(TENANT_TABLES)},
        )
    ).all()

    assert len(rows) == len(TENANT_TABLES)
    not_enabled = [r.relname for r in rows if not r.relrowsecurity]
    not_forced = [r.relname for r in rows if not r.relforcerowsecurity]

    assert not not_enabled, f"row-level security not enabled: {not_enabled}"
    assert not not_forced, f"row-level security not forced: {not_forced}"
