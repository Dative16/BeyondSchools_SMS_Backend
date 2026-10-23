"""Students, enrolments and documents.

The spine of the system. A `student` row is a person and lives for the child's
whole time at the school; an `enrolment` row is that person in one class for
one academic year. Promotion writes a new enrolment rather than mutating the
student, so a child's whole history — every class, every repeat, every transfer
— is readable without an audit log, and a report card from two years ago can
still say which class it was earned in.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enrolment import EnrolmentOutcome, StudentStatus
from app.models.base import Base, TenantScoped, Timestamped, UUIDPrimaryKey, pg_enum


class Sex(StrEnum):
    FEMALE = "f"
    MALE = "m"


class Student(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    __tablename__ = "student"
    __table_args__ = (
        UniqueConstraint("school_id", "admission_no", name="uq_student_school_id_admission_no"),
        # The search the roll screen runs on every keystroke.
        Index("ix_student_school_id_surname_first_name", "school_id", "surname", "first_name"),
    )

    admission_no: Mapped[str] = mapped_column(String(40), nullable=False)
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(64))
    surname: Mapped[str] = mapped_column(String(64), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[Sex] = mapped_column(pg_enum(Sex, "sex"), nullable=False)
    photo_url: Mapped[str | None] = mapped_column(Text)

    status: Mapped[StudentStatus] = mapped_column(
        pg_enum(StudentStatus, "student_status"),
        nullable=False,
        server_default=StudentStatus.PROSPECTIVE.value,
    )
    nationality: Mapped[str] = mapped_column(String(48), nullable=False, server_default="Tanzanian")
    birth_certificate_no: Mapped[str | None] = mapped_column(String(64))
    #: Free text on purpose. A school that needs to record "wears a hearing aid
    #: in the left ear" should not have to wait for us to add an enum member.
    special_needs: Mapped[str | None] = mapped_column(Text)
    blood_group: Mapped[str | None] = mapped_column(String(8))
    previous_school: Mapped[str | None] = mapped_column(String(160))

    #: Unpaid fees that followed the child past a closed enrolment. Carried on
    #: the student rather than left on the old enrolment, so that a year can be
    #: closed without money disappearing from the arrears report.
    arrears_brought_forward: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default="0"
    )

    #: aibeyondschools.co.tz's own id for this child, written back the first
    #: time they cross over, plus when we last told it their level.
    ai_platform_user_ref: Mapped[str | None] = mapped_column(String(128))
    ai_level_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Student {self.admission_no} {self.first_name} {self.surname}>"


class Enrolment(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """One child, one class, one year.

    Two indexes here are doing real work. The partial unique one is the
    invariant: a child has at most one *active* enrolment at a time, whatever
    an application bug tries. The pair on (student, year) stops the same child
    being enrolled twice in one year, which is the shape a double-submitted
    admission takes.
    """

    __tablename__ = "enrolment"
    __table_args__ = (
        UniqueConstraint(
            "student_id", "academic_year_id", name="uq_enrolment_student_id_academic_year_id"
        ),
        Index(
            "uq_enrolment_one_active_per_student",
            "student_id",
            unique=True,
            postgresql_where=text("outcome = 'active'"),
        ),
        Index("ix_enrolment_class_group_id_outcome", "class_group_id", "outcome"),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), nullable=False, index=True
    )
    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_year.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    class_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("class_group.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    outcome: Mapped[EnrolmentOutcome] = mapped_column(
        pg_enum(EnrolmentOutcome, "enrolment_outcome"),
        nullable=False,
        server_default=EnrolmentOutcome.ACTIVE.value,
    )
    enrolled_on: Mapped[date] = mapped_column(Date, nullable=False)
    exited_on: Mapped[date | None] = mapped_column(Date)
    exit_reason: Mapped[str | None] = mapped_column(Text)

    #: Where this child came from, which makes a repeat visible as a repeat
    #: rather than as two unrelated rows.
    previous_enrolment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("enrolment.id", ondelete="SET NULL")
    )
    #: Stamped by the promotion run that created or closed this row. Every row
    #: carrying it is what makes a whole run reversible in one operation — the
    #: foreign key arrives with the promotion engine in Phase 2.
    promotion_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)

    is_repeating: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class DocumentKind(StrEnum):
    BIRTH_CERTIFICATE = "birth_certificate"
    REPORT_CARD = "report_card"
    TRANSFER_LETTER = "transfer_letter"
    PHOTO = "photo"
    MEDICAL = "medical"
    OTHER = "other"


class StudentDocument(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """A photograph of a piece of paper.

    Parents photograph a birth certificate on a phone; they do not scan it. The
    row is attached to an application *or* a student, because documents arrive
    before a child exists in the system and have to survive the conversion.
    """

    __tablename__ = "student_document"
    __table_args__ = (
        CheckConstraint(
            "student_id IS NOT NULL OR application_id IS NOT NULL",
            name="belongs_to_student_or_application",
        ),
    )

    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("application.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DocumentKind] = mapped_column(
        pg_enum(DocumentKind, "document_kind"), nullable=False
    )
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(80))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
