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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enrolment import EnrolmentOutcome, StudentStatus
from app.domain.registration import Residency
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
    #:
    #: This stays on the student rather than moving to `student_health`: a
    #: class teacher has to know that a child cannot hear from the back of the
    #: room, whereas their medication list is none of that teacher's business
    #: until it is an emergency. Medical detail lives behind `health:read`.
    special_needs: Mapped[str | None] = mapped_column(Text)
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

    # Boarding and transport sit here rather than on the student because they
    # are per-year facts: a child can board in Standard 5 and be a day pupil
    # in Standard 6, and the fees differ. Deliberately thin — no beds, no
    # vehicles, no seat allocation. Those are products in their own right and
    # neither of them puts a student on the AI platform.
    residency: Mapped[Residency] = mapped_column(
        pg_enum(Residency, "residency"), nullable=False, server_default=Residency.DAY.value
    )
    #: The dormitory's name as the school says it. Enough to print a list.
    dormitory: Mapped[str | None] = mapped_column(String(64))
    uses_transport: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    transport_route: Mapped[str | None] = mapped_column(String(80))
    transport_stop: Mapped[str | None] = mapped_column(String(80))


class DocumentKind(StrEnum):
    """The papers a school asks for.

    Which of these a particular school *requires* is its own business and
    lives in `document_requirement`; this enum is only the vocabulary. Adding
    a member is a migration, which is the price of having the checklist be
    reportable — a school that needs something unusual records it as a custom
    field instead.
    """

    BIRTH_CERTIFICATE = "birth_certificate"
    #: Picha za pasipoti. Several, usually, and a school will not admit
    #: without them.
    PASSPORT_PHOTO = "passport_photo"
    #: Fomu ya usajili, signed and scanned back.
    REGISTRATION_FORM = "registration_form"
    REPORT_CARD = "report_card"
    TRANSFER_LETTER = "transfer_letter"
    LEAVING_CERTIFICATE = "leaving_certificate"
    IMMUNISATION_CARD = "immunisation_card"
    NATIONAL_ID = "national_id"
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


class StudentHealth(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """Medical detail, kept apart from the rest of the record.

    Its own table rather than more columns on `student`, for one reason that
    matters legally and one that matters practically.

    Legally: under the Personal Data Protection Act, 2022, data relating to a
    child's health is sensitive personal data. A separate table can be put
    behind its own capability (`health:read`), so a bursar chasing arrears
    cannot read a child's HIV status by opening their profile, and the access
    can be audited on its own.

    Practically: "taarifa za afya pale zinapohitajika" — health information
    where it is needed. Most children have no row here at all, and a table of
    mostly-null columns on every student invites somebody to export it.
    """

    __tablename__ = "student_health"
    __table_args__ = (
        UniqueConstraint("student_id", name="uq_student_health_student_id"),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blood_group: Mapped[str | None] = mapped_column(String(8))
    #: Free text, and it has to be: "peanuts, and penicillin brings on a rash"
    #: is the real shape of the answer a parent gives.
    allergies: Mapped[str | None] = mapped_column(Text)
    chronic_conditions: Mapped[str | None] = mapped_column(Text)
    medication: Mapped[str | None] = mapped_column(Text)
    #: What somebody should actually do. An asthma plan is useless as a
    #: diagnosis and valuable as an instruction.
    emergency_plan: Mapped[str | None] = mapped_column(Text)

    doctor_name: Mapped[str | None] = mapped_column(String(120))
    doctor_phone: Mapped[str | None] = mapped_column(String(20))
    #: NHIF, a private scheme, or nothing.
    insurance_scheme: Mapped[str | None] = mapped_column(String(80))
    insurance_number: Mapped[str | None] = mapped_column(String(64))

    note: Mapped[str | None] = mapped_column(Text)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )


class EmergencyContact(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """Taarifa za mawasiliano ya dharura: who to call, in what order.

    Not the same thing as a guardian, which is why it is not the same table. A
    neighbour, an aunt down the road or the father's employer can be the right
    person to call at eleven in the morning without being anybody's legal
    guardian, without receiving invoices, and without being allowed to collect
    the child.

    `priority` is what makes this useful in an emergency: a list of three
    numbers in no particular order is three numbers nobody knows which to ring
    first.
    """

    __tablename__ = "emergency_contact"
    __table_args__ = (
        Index(
            "uq_emergency_contact_one_first_call",
            "student_id",
            unique=True,
            postgresql_where=text("priority = 1"),
        ),
        Index("ix_emergency_contact_student_id_priority", "student_id", "priority"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="priority_range"),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    relationship: Mapped[str] = mapped_column(String(48), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    alt_phone: Mapped[str | None] = mapped_column(String(20))
    #: 1 is "ring this person first". One per child, enforced above.
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    #: "Works nights, try after 2pm" — the kind of thing that decides whether
    #: a call is answered.
    note: Mapped[str | None] = mapped_column(Text)
    #: Whether this person may take the child from the gate. Separate from
    #: being callable, because the two are genuinely different permissions.
    may_collect: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
