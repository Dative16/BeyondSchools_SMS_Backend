"""Guardians and their links to children.

A parent with three children at the school is one guardian and three links, not
three parents. Everything downstream depends on that: who receives one SMS
rather than three, who sees a combined balance across the family, and which
phone number a payment can be matched back to.
"""

import uuid
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, Timestamped, UUIDPrimaryKey, pg_enum


class Relationship(StrEnum):
    MOTHER = "mother"
    FATHER = "father"
    GRANDPARENT = "grandparent"
    AUNT_UNCLE = "aunt_uncle"
    SIBLING = "sibling"
    LEGAL_GUARDIAN = "legal_guardian"
    SPONSOR = "sponsor"
    OTHER = "other"


class Guardian(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    __tablename__ = "guardian"
    __table_args__ = (
        UniqueConstraint("school_id", "phone", name="uq_guardian_school_id_phone"),
        Index("ix_guardian_school_id_full_name", "school_id", "full_name"),
    )

    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Normalised to 0754118902. The phone is the identity here: it is how a
    #: guardian signs in, how a payment is traced back when a reference is
    #: missing, and how duplicate screening spots a sibling.
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    alt_phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(160))
    national_id: Mapped[str | None] = mapped_column(String(32))
    occupation: Mapped[str | None] = mapped_column(String(80))
    address: Mapped[str | None] = mapped_column(Text)
    #: Where SMS goes when a guardian asks for Kiswahili.
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="en")
    #: Honoured by every non-transactional message. A fee reminder is
    #: transactional; a newsletter is not.
    sms_opted_out: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class GuardianStudent(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """The link, and the four decisions that hang off it.

    One primary guardian per child, enforced by a partial unique index: "who do
    we call" must have exactly one answer, and a school that has two of them
    calls neither.
    """

    __tablename__ = "guardian_student"
    __table_args__ = (
        UniqueConstraint(
            "guardian_id", "student_id", name="uq_guardian_student_guardian_id_student_id"
        ),
        Index(
            "uq_guardian_student_one_primary_per_student",
            "student_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    guardian_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guardian.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relationship: Mapped[Relationship] = mapped_column(
        pg_enum(Relationship, "guardian_relationship"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    receives_invoices: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    receives_sms: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    may_collect: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
