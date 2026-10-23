"""Request dependencies: the current user, the current school, a bound session.

Routes depend on `tenant_session`, never on `get_session` directly. That is what
keeps the row-level security guarantee true for every endpoint by default.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import bind_tenant
from app.db.session import get_session


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Validate the bearer token and return its claims.

    Claims carried: sub, school_id, role, student_id (students only).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    raise NotImplementedError("Wire to app.core.security.decode_access_token")


async def tenant_session(
    principal: Annotated[dict[str, Any], Depends(current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AsyncIterator[AsyncSession]:
    await bind_tenant(session, principal["school_id"])
    yield session
