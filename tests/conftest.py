"""Shared fixtures.

Tests run against a real PostgreSQL, never SQLite: row-level security is the
thing most worth testing here and SQLite has none.

Two connections, and the difference is the whole point. `owner_session`
connects as the role that owns the tables, which RLS does not apply to — it is
how a test arranges two schools' data. `app_session` connects as the restricted
role the application itself uses, which is where the policies bite. A test that
proves isolation must read through the second one.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from app.core.config import settings


async def _ensure_app_role() -> None:
    """Create the restricted role if it is missing.

    Locally, `docker/initdb/01-app-role.sql` has already done this. In CI the
    database is a bare postgres service, so the role has to be made here — and
    without it every test would pass for the wrong reason, connecting as the
    owner and never exercising a single policy.
    """
    engine = create_async_engine(str(settings.MIGRATION_DATABASE_URL), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": settings.DB_APP_ROLE},
            )
            if not exists:
                await conn.execute(
                    text(
                        f'CREATE ROLE "{settings.DB_APP_ROLE}" LOGIN '
                        f"PASSWORD '{settings.DB_APP_PASSWORD}' "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE"
                    )
                )
            await conn.execute(
                text(f'GRANT USAGE ON SCHEMA public TO "{settings.DB_APP_ROLE}"')
            )
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """One schema per test session, built the way production is built.

    `alembic upgrade head` rather than `create_all`: the policies, triggers and
    grants live in the migration, so a schema created from the models alone
    would pass the tests that matter for a reason that does not exist in
    production.
    """
    asyncio.run(_ensure_app_role())
    config = Config("alembic.ini")
    command.upgrade(config, "head")


@pytest.fixture
async def owner_session() -> AsyncIterator[AsyncSession]:
    """Connects as the table owner. Arranges data; proves nothing."""
    engine = create_async_engine(str(settings.MIGRATION_DATABASE_URL))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()


@pytest.fixture
async def app_session() -> AsyncIterator[AsyncSession]:
    """Connects as the application's restricted role. Policies apply."""
    engine = create_async_engine(str(settings.DATABASE_URL))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()


async def make_school(session: AsyncSession, *, name: str, subdomain: str) -> UUID:
    """The smallest school a test can hang data off.

    The subdomain is suffixed, because the demo seed holds "mwenge" and a test
    that only passes against an empty database is a test that fails on the
    machine of whoever ran `make seed` first.
    """
    school_id = uuid4()
    subdomain = f"{subdomain}-{school_id.hex[:8]}"
    await session.execute(
        text(
            "INSERT INTO school (id, name, subdomain, status, created_at, updated_at) "
            "VALUES (:id, :name, :subdomain, 'active', now(), now())"
        ),
        {"id": school_id, "name": name, "subdomain": subdomain},
    )
    return school_id


async def make_academic_year(
    session: AsyncSession, *, school_id: UUID, label: str = "2027", is_current: bool = True
) -> UUID:
    year_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO academic_year "
            "(id, school_id, label, starts_on, ends_on, is_current, is_closed, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, :label, :starts, :ends, :current, false, now(), now())"
        ),
        {
            "id": year_id,
            "school_id": school_id,
            "label": label,
            "starts": date(int(label), 1, 8),
            "ends": date(int(label), 12, 4),
            "current": is_current,
        },
    )
    return year_id


async def make_level(
    session: AsyncSession,
    *,
    school_id: UUID,
    code: str = "STD4",
    name: str = "Standard 4",
    order_index: int = 4,
) -> UUID:
    level_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO level "
            "(id, school_id, code, name, stage, order_index, is_active, created_at, updated_at) "
            "VALUES (:id, :school_id, :code, :name, 'primary', :order_index, true, now(), now())"
        ),
        {
            "id": level_id,
            "school_id": school_id,
            "code": code,
            "name": name,
            "order_index": order_index,
        },
    )
    return level_id


async def make_class_group(
    session: AsyncSession,
    *,
    school_id: UUID,
    academic_year_id: UUID,
    level_id: UUID,
    name: str = "Standard 4 Blue",
) -> UUID:
    group_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO class_group "
            "(id, school_id, academic_year_id, level_id, name, created_at, updated_at) "
            "VALUES (:id, :school_id, :year, :level, :name, now(), now())"
        ),
        {
            "id": group_id,
            "school_id": school_id,
            "year": academic_year_id,
            "level": level_id,
            "name": name,
        },
    )
    return group_id


async def make_enrolment(
    session: AsyncSession,
    *,
    school_id: UUID,
    student_id: UUID,
    academic_year_id: UUID,
    class_group_id: UUID,
    outcome: str = "active",
) -> UUID:
    enrolment_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO enrolment "
            "(id, school_id, student_id, academic_year_id, class_group_id, outcome, "
            " enrolled_on, is_repeating, created_at, updated_at) "
            "VALUES (:id, :school_id, :student, :year, :group, :outcome, :on, false, "
            " now(), now())"
        ),
        {
            "id": enrolment_id,
            "school_id": school_id,
            "student": student_id,
            "year": academic_year_id,
            "group": class_group_id,
            "outcome": outcome,
            "on": date(2027, 1, 8),
        },
    )
    return enrolment_id


async def make_invoice(
    session: AsyncSession,
    *,
    school_id: UUID,
    student_id: UUID,
    enrolment_id: UUID,
    reference: str = "INV-0001",
) -> UUID:
    invoice_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO invoice "
            "(id, school_id, reference, kind, student_id, enrolment_id, issued_on, "
            " due_date, currency, billed, settled, is_void, created_at, updated_at) "
            "VALUES (:id, :school_id, :ref, 'term', :student, :enrolment, :issued, "
            " :due, 'TZS', 0, 0, false, now(), now())"
        ),
        {
            "id": invoice_id,
            "school_id": school_id,
            "ref": reference,
            "student": student_id,
            "enrolment": enrolment_id,
            "issued": date(2027, 1, 8),
            "due": date(2027, 1, 22),
        },
    )
    return invoice_id


async def add_invoice_line(
    session: AsyncSession,
    *,
    school_id: UUID,
    invoice_id: UUID,
    item: str,
    amount: str,
    kind: str = "charge",
) -> UUID:
    line_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO invoice_line "
            "(id, school_id, invoice_id, item, amount, kind, sort_order, "
            " created_at, updated_at) "
            "VALUES (:id, :school_id, :invoice, :item, :amount, :kind, 0, now(), now())"
        ),
        {
            "id": line_id,
            "school_id": school_id,
            "invoice": invoice_id,
            "item": item,
            "amount": amount,
            "kind": kind,
        },
    )
    return line_id


async def add_payment(
    session: AsyncSession,
    *,
    school_id: UUID,
    invoice_id: UUID | None,
    external_ref: str,
    amount: str,
    applied: str | None = None,
    state: str = "settled",
    channel: str = "mpesa",
) -> UUID:
    payment_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO payment "
            "(id, school_id, invoice_id, external_ref, amount, applied, currency, "
            " channel, state, received_at, is_manual, created_at, updated_at) "
            "VALUES (:id, :school_id, :invoice, :ref, :amount, :applied, 'TZS', "
            " :channel, :state, now(), false, now(), now())"
        ),
        {
            "id": payment_id,
            "school_id": school_id,
            "invoice": invoice_id,
            "ref": external_ref,
            "amount": amount,
            "applied": applied if applied is not None else amount,
            "channel": channel,
            "state": state,
        },
    )
    return payment_id


async def make_student(
    session: AsyncSession,
    *,
    school_id: UUID,
    admission_no: str,
    first_name: str = "Neema",
    surname: str = "Mwanjala",
) -> UUID:
    student_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO student "
            "(id, school_id, admission_no, first_name, surname, date_of_birth, sex, "
            " status, nationality, arrears_brought_forward, created_at, updated_at) "
            "VALUES (:id, :school_id, :adm, :first, :surname, :dob, 'f', "
            " 'active', 'Tanzanian', 0, now(), now())"
        ),
        {
            "id": student_id,
            "school_id": school_id,
            "adm": admission_no,
            "first": first_name,
            "surname": surname,
            "dob": date(2016, 3, 14),
        },
    )
    return student_id
