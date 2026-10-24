"""Repository plumbing and the unit of work.

A repository is the only place a SQLAlchemy query is written. Services depend on
the protocols, not the implementations, so a service test can hand in a fake and
never touch PostgreSQL.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import rebind


class UnitOfWork(Protocol):
    """One transaction, several repositories, committed or rolled back together."""

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


class SqlAlchemyUnitOfWork:
    """The real one. Constructed in app/api/deps.py and injected into services."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None:
            await self.rollback()

    async def commit(self) -> None:
        """Commit, then put the tenant binding back.

        The binding is transaction-local by design — see
        `app.core.tenancy.rebind` — so a service that commits halfway through
        its work would carry on unscoped. Doing this here, once, is what lets
        every service commit as often as the sequence genuinely needs to
        without each one having to remember.
        """
        await self._session.commit()
        await rebind(self._session)

    async def rollback(self) -> None:
        await self._session.rollback()
        await rebind(self._session)
