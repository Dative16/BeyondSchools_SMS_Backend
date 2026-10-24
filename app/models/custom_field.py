"""Per-school custom fields, and the values schools put in them.

The bounded escape hatch. A school declares a field — a bed number, a bus
route, a baptism certificate reference — and the system stores it without
anybody shipping a migration. The boundary is the rule in
`app/domain/registration.py`: anything a rule, a charge, an access decision or
a report depends on gets a real column instead.

Two properties keep this from rotting into an entity-attribute-value swamp:
every field declares a `kind` that its values are checked against on write,
and `is_sensitive` marks the ones that must not appear in an ordinary export.
"""

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.registration import FieldKind, FieldScope
from app.models.base import Base, TenantScoped, Timestamped, UUIDPrimaryKey, pg_enum


class SchoolField(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """A field one school has decided it needs."""

    __tablename__ = "school_field"
    __table_args__ = (
        UniqueConstraint("school_id", "scope", "key", name="uq_school_field_school_id_scope_key"),
        CheckConstraint(
            "kind <> 'choice' OR array_length(options, 1) >= 2",
            name="choice_has_options",
        ),
    )

    #: Lands in an export header and in a URL, so it stays boring: letters,
    #: digits and underscores, checked in the domain.
    key: Mapped[str] = mapped_column(String(48), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    help_text: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[FieldKind] = mapped_column(pg_enum(FieldKind, "field_kind"), nullable=False)
    scope: Mapped[FieldScope] = mapped_column(pg_enum(FieldScope, "field_scope"), nullable=False)
    #: Only for a choice field.
    options: Mapped[list[str] | None] = mapped_column(ARRAY(String(80)))

    #: Required before an application can be submitted. Every field marked
    #: this way costs completed applications, so the public form should carry
    #: almost none of them.
    required_at_registration: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    #: Health, religion, anything a school should not be dropping into a
    #: spreadsheet. Kept out of ordinary exports.
    is_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")


class SchoolFieldValue(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """One school's answer for one subject.

    Four nullable subject columns and a check that exactly one is set. The
    alternative — a polymorphic (subject_type, subject_id) pair — cannot be
    given a foreign key, so a deleted student would leave values behind
    pointing at nothing.
    """

    __tablename__ = "school_field_value"
    __table_args__ = (
        CheckConstraint(
            "(student_id IS NOT NULL)::int + (guardian_id IS NOT NULL)::int + "
            "(enrolment_id IS NOT NULL)::int + (application_id IS NOT NULL)::int = 1",
            name="exactly_one_subject",
        ),
        Index(
            "uq_school_field_value_field_student",
            "field_id",
            "student_id",
            unique=True,
            postgresql_where=text("student_id IS NOT NULL"),
        ),
    )

    field_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("school_field.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True
    )
    guardian_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("guardian.id", ondelete="CASCADE"), index=True
    )
    enrolment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("enrolment.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("application.id", ondelete="CASCADE"), index=True
    )

    #: Checked against the field's `kind` before it is written, by
    #: app.domain.registration.coerce_field_value.
    value: Mapped[Any | None] = mapped_column(JSONB)
    set_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
