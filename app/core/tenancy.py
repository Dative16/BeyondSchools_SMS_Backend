"""Tenant isolation.

The rule this file exists to enforce: no query in application code ever filters
by school_id by hand. The tenant is resolved once per request, pushed into the
PostgreSQL session as `app.school_id`, and the row-level security policies in
the migrations do the rest. A developer who forgets a WHERE clause gets an
empty result set, not another school's children.

Both settings are set with `is_local => true`, so they last for the current
transaction only and cannot leak to the next request that borrows the same
pooled connection.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SCHOOL_ID_SETTING = "app.school_id"
#: The deliberate escape hatch. Every policy in the schema reads it, so
#: changing its name means changing the migrations too.
PLATFORM_SCOPE_SETTING = "app.platform_scope"


async def bind_tenant(session: AsyncSession, school_id: UUID) -> None:
    """Pin this database session to one school for the rest of the transaction."""
    await session.execute(
        text("SELECT set_config(:key, :sid, true)"),
        {"key": SCHOOL_ID_SETTING, "sid": str(school_id)},
    )


@asynccontextmanager
async def platform_scope(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Cross-tenant access for platform admin work only.

    Every use is audited. Do not reach for this to make a query convenient.

    Two things make it survivable. It rolls back on the way out, so nothing
    written under it can be committed by accident; and because the flag is
    transaction-local, forgetting to close the block cannot leave a connection
    unscoped in the pool.
    """
    await session.execute(
        text("SELECT set_config(:key, 'on', true)"), {"key": PLATFORM_SCOPE_SETTING}
    )
    try:
        yield session
    finally:
        await session.rollback()


@asynccontextmanager
async def unscoped(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """A session bound to no school at all.

    Used by the parts of sign-in that run before a tenant is known. Reads under
    it see only rows that belong to no school — the platform admin's own user
    row — because every other policy requires a bound tenant.

    Resolving a subdomain to a school is not done this way: it goes through
    `resolve_school_by_subdomain`, a SECURITY DEFINER function created in the
    initial migration that returns a login page's worth of fields and nothing
    else. A policy that let an unscoped session read the `school` table would
    hand every tenant's settings to anyone who could reach the login route.
    """
    await session.execute(
        text("SELECT set_config(:key, '', true)"), {"key": SCHOOL_ID_SETTING}
    )
    try:
        yield session
    finally:
        await session.rollback()
