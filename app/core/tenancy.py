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


#: Where the bound school is remembered on the session, so that a commit can
#: put it back. See `rebind` below.
_BOUND_SCHOOL = "bs_bound_school_id"


async def bind_tenant(session: AsyncSession, school_id: UUID) -> None:
    """Pin this database session to one school for the rest of the transaction."""
    await session.execute(
        text("SELECT set_config(:key, :sid, true)"),
        {"key": SCHOOL_ID_SETTING, "sid": str(school_id)},
    )
    session.info[_BOUND_SCHOOL] = school_id


async def rebind(session: AsyncSession) -> None:
    """Re-apply the binding after a commit.

    `set_config(..., is_local => true)` lasts for the transaction, and a commit
    ends it — so a service that commits and then does more work would find
    itself unscoped, and its next write rejected by the very policy that is
    meant to be protecting it. The alternative, binding at session level, would
    survive the commit *and* survive the request: a pooled connection handed to
    the next caller still carrying a school is a leak that fails open.

    So the binding stays transaction-local, and the unit of work calls this
    after every commit. Fails closed if it is ever forgotten.
    """
    school_id = session.info.get(_BOUND_SCHOOL)
    if school_id is not None:
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


async def clear_tenant(session: AsyncSession) -> None:
    """Bind this session to no school, and leave it that way.

    For the one caller that has to write while unscoped: the platform admin
    signing in, whose own user row belongs to no school. Reads that only need
    to look at something before a tenant is known should use `unscoped`
    instead, which rolls back on the way out.
    """
    await session.execute(
        text("SELECT set_config(:key, '', true)"), {"key": SCHOOL_ID_SETTING}
    )


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
