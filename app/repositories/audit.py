"""Audit persistence.

Append-only, and the database agrees: the application's role has INSERT and
SELECT on `audit_event` and nothing else, so there is no code path — and no
console session as that role — that can rewrite history.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent, AuditOutcome


class AuditRepository(Protocol):
    async def record(
        self,
        *,
        school_id: UUID | None,
        action: str,
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
        on_behalf_of_user_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        changed: dict[str, Any] | None = None,
        note: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> UUID: ...


class SqlAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        school_id: UUID | None,
        action: str,
        actor_user_id: UUID | None = None,
        actor_role: str | None = None,
        on_behalf_of_user_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        changed: dict[str, Any] | None = None,
        note: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> UUID:
        event = AuditEvent(
            school_id=school_id,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            on_behalf_of_user_id=on_behalf_of_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome=outcome,
            changed=changed,
            note=note,
            ip=ip,
            user_agent=user_agent,
        )
        self._session.add(event)
        await self._session.flush()
        return event.id

    async def recent_for_entity(
        self, *, entity_type: str, entity_id: UUID, limit: int = 50
    ) -> list[AuditEvent]:
        """The history of one record, newest first.

        Used by the screens that have to answer "who changed this, and when" —
        a question a school asks about a child's status and about every
        payment, and one that a system holding children's records has to be
        able to answer under the Personal Data Protection Act.
        """
        return list(
            (
                await self._session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.entity_type == entity_type,
                        AuditEvent.entity_id == entity_id,
                    )
                    .order_by(AuditEvent.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
