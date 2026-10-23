"""Table plumbing: the declarative base, keys, timestamps and `school_id`.

The one thing worth reading closely is `TenantScoped`. Every table that mixes
it in gets a `school_id` column *and* a row-level security policy in the
migration, which together are what let the rest of the codebase stop thinking
about tenancy. A new table that holds school data and does not mix this in is a
leak waiting to happen, so `tests/integration/test_tenant_isolation.py` walks
the metadata and fails on any table that carries `school_id` without a policy.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, MetaData, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Predictable constraint names. Without this, Alembic autogenerate produces
# migrations that cannot drop the constraints an earlier migration created.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """A native PostgreSQL enum that stores the *value*, not the member name.

    The types themselves are created once, by hand, at the top of the initial
    migration — `create_type=False` keeps SQLAlchemy from trying to create them
    a second time the first time a table that uses one is built.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_type=False,
        values_callable=lambda e: [m.value for m in e],
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def money(nullable: bool = False, default: str | None = "0") -> Mapped[Any]:
    """Every amount in this schema is NUMERIC(14, 2). Never a float: a float
    cannot hold 0.1 exactly, and a bursar's trial balance has to come out to
    the shilling."""
    from sqlalchemy import Numeric

    return mapped_column(Numeric(14, 2), nullable=nullable, server_default=default)


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = uuid_pk()


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantScoped:
    """The tenant key. Mixing this in is what puts a table behind RLS.

    Note what is *not* here: a relationship back to School. Services never
    traverse from a row to its school, because they are already pinned to one;
    the column exists for the database's benefit, not the application's.
    """

    @declared_attr
    def school_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            PgUUID(as_uuid=True),
            ForeignKey("school.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
