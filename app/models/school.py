"""The tenant root.

Every other table in this schema carries this table's id. `School` itself is
not `TenantScoped` — it is the thing tenancy is scoped *to* — but it still sits
behind a policy so that a session bound to one school cannot read another
school's name, branding or settings.
"""

import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamped, UUIDPrimaryKey, pg_enum


class SchoolStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class School(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "school"
    __table_args__ = (UniqueConstraint("subdomain", name="uq_school_subdomain"),)

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(32))
    #: Resolves the tenant on every request that does not carry a token yet —
    #: the public application form, most importantly.
    subdomain: Mapped[str] = mapped_column(String(63), nullable=False)
    registration_number: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[SchoolStatus] = mapped_column(
        pg_enum(SchoolStatus, "school_status"),
        nullable=False,
        server_default=SchoolStatus.PENDING.value,
    )

    motto: Mapped[str | None] = mapped_column(String(200))
    logo_url: Mapped[str | None] = mapped_column(Text)
    #: Each school's own accent, which is why the screens read it as a token
    #: instead of hard-coding the brand green.
    brand_color: Mapped[str] = mapped_column(String(9), nullable=False, server_default="#22C55E")

    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(160))
    address: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(80))
    district: Mapped[str | None] = mapped_column(String(80))

    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="TZS")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="Africa/Dar_es_Salaam"
    )
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="en")

    #: "MPS/{year}/{sequence:04d}". Schools write these differently and change
    #: their minds, so the format is data; app.domain.enrolment renders it.
    admission_no_format: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="{year}/{sequence:04d}"
    )

    #: How much of the first term a place costs to hold. See
    #: app.domain.invoicing.deposit_due.
    deposit_percent: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="30"
    )
    invoice_grace_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="14"
    )

    # Whether arrears withhold a report card or a promotion is a head
    # teacher's decision, not ours, and it is still open for the pilot school.
    # Both default to off: a system that silently withholds a child's results
    # is worse than one that has to be told to.
    arrears_blocks_results: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    arrears_blocks_promotion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    #: Arrears below this are ignored by whichever of the two switches is on.
    arrears_threshold: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    #: Set once this school's students are provisioned on aibeyondschools.co.tz.
    ai_platform_tenant_ref: Mapped[str | None] = mapped_column(String(128))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<School {self.subdomain}>"


class SchoolSequence(UUIDPrimaryKey, Base):
    """Per-school counters that must not collide under concurrency.

    Admission numbers and invoice references are issued by locking one of these
    rows, not by counting existing records: two registrars offering places in
    the same second would otherwise mint the same admission number, and the
    loser would see an integrity error instead of a pupil.
    """

    __tablename__ = "school_sequence"
    __table_args__ = (
        UniqueConstraint(
            "school_id", "name", "scope", name="uq_school_sequence_school_id_name_scope"
        ),
    )

    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("school.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: "admission_no", "invoice_ref", "application_ref".
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Usually an academic year's label, so each year starts at 1 again.
    scope: Mapped[str] = mapped_column(String(40), nullable=False, server_default="")
    value: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
