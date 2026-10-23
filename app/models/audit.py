"""The audit trail.

Children's records and financial transactions are both sensitive personal data
under the Personal Data Protection Act, 2022, so "who looked at this and who
changed it" is a legal question here, not only an engineering one.

What gets a row: anything that moves money, anything that changes a child's
status, every platform-admin crossing into a school, and every failed
authorisation. Ordinary reads do not — an audit log that records every page
view is an audit log nobody can search.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKey, pg_enum


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"


class AuditEvent(UUIDPrimaryKey, Base):
    """Append-only. No updated_at, because nothing here is ever updated.

    `school_id` is nullable so that platform-level actions — onboarding a
    school, impersonating into one — are recorded too. The policy on this
    table lets a school read its own rows and nobody else's, and there is no
    delete path at all.
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_school_id_created_at", "school_id", "created_at"),
        Index("ix_audit_event_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_audit_event_actor_user_id_created_at", "actor_user_id", "created_at"),
    )

    school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("school.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    #: The role the actor held at the time. Roles change; the record of what
    #: this person was allowed to do when they did it must not.
    actor_role: Mapped[str | None] = mapped_column(String(32))
    #: Set when a platform admin is acting inside a school, which is the one
    #: case where the actor and the school do not belong together.
    on_behalf_of_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )

    #: "payment.recorded", "application.offered", "promotion.committed".
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(48))
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    outcome: Mapped[AuditOutcome] = mapped_column(
        pg_enum(AuditOutcome, "audit_outcome"),
        nullable=False,
        server_default=AuditOutcome.SUCCESS.value,
    )

    #: Only the fields that changed, never the whole row: a before-and-after of
    #: an entire student record in every edit makes the log unreadable and
    #: duplicates the sensitive data it is meant to protect.
    changed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
