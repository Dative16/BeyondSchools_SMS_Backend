"""Academic structure: years, terms, levels, streams, classes, subjects.

The load-bearing decision in this file is that the level ladder is data. The
country is halfway through shortening primary from seven years to six, and in
2027 one school holds two exit cohorts at once: the pupils who were in Standard
Six in 2026 sit the PSLE from Standard Seven, and the pupils who were in
Standard Five sit the same examination from Standard Six. Code that assumes
Standard 7 is the exit level is wrong that year, so what exits is rows in
`academic_year_exit_level`, and what comes next is `level.order_index` with an
optional explicit override.
"""

import uuid
from datetime import date

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enrolment import LevelStage
from app.models.base import Base, TenantScoped, Timestamped, UUIDPrimaryKey, pg_enum


class AcademicYear(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    __tablename__ = "academic_year"
    __table_args__ = (
        UniqueConstraint("school_id", "label", name="uq_academic_year_school_id_label"),
        # One current year per school, enforced where it cannot be forgotten.
        # Invoicing and promotion both pivot on "the current year"; two of them
        # would mean a term's invoices raised against the wrong one.
        Index(
            "uq_academic_year_one_current_per_school",
            "school_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    label: Mapped[str] = mapped_column(String(16), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: Closed once the promotion run for this year has committed and been
    #: accepted; a closed year takes no new marks, invoices or enrolments.
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class AcademicYearExitLevel(UUIDPrimaryKey, TenantScoped, Base):
    """A level whose pupils leave the school at the end of this year.

    A row per cohort, not a constant: 2027 has two of them for every primary
    school in the country, and 2028 has one again.
    """

    __tablename__ = "academic_year_exit_level"
    __table_args__ = (
        UniqueConstraint(
            "academic_year_id",
            "level_id",
            name="uq_academic_year_exit_level_academic_year_id_level_id",
        ),
    )

    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_year.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("level.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: "Last cohort through the seven-year primary cycle", so that whoever
    #: reads the promotion proposals in December knows why.
    note: Mapped[str | None] = mapped_column(Text)


class Term(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """Tanzanian schools bill and report per term, not per semester."""

    __tablename__ = "term"
    __table_args__ = (
        UniqueConstraint("academic_year_id", "index", name="uq_term_academic_year_id_index"),
    )

    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_year.id", ondelete="CASCADE"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class Level(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """Standard 1, Standard 2, Form 1 — a rung on the ladder.

    `order_index` is what the promotion engine increments, and
    `next_level_id` overrides it where a school's ladder is not a straight
    line. Both are data a school admin can change without a deployment, which
    is the whole point.
    """

    __tablename__ = "level"
    __table_args__ = (
        UniqueConstraint("school_id", "code", name="uq_level_school_id_code"),
        Index("ix_level_school_id_order_index", "school_id", "order_index"),
    )

    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[LevelStage] = mapped_column(pg_enum(LevelStage, "level_stage"), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    next_level_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("level.id", ondelete="SET NULL")
    )
    #: Typical age on entry, used only to flag an admission that looks wrong.
    entry_age: Mapped[int | None] = mapped_column(SmallInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class Stream(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """Standard 3 Blue and Standard 3 Green are separate class groups."""

    __tablename__ = "stream"
    __table_args__ = (UniqueConstraint("level_id", "name", name="uq_stream_level_id_name"),)

    level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("level.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    capacity: Mapped[int | None] = mapped_column(SmallInteger)
    #: Boarding streams carry different fee items from day streams, which is
    #: why a fee structure can be scoped to one.
    is_boarding: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class ClassGroup(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """The thing a timetable, a register and an assignment all attach to.

    A class group belongs to one academic year. Standard 4 Blue in 2027 and
    Standard 4 Blue in 2028 are different rows holding different children, so
    last year's register stays readable after this year's has been taken.
    """

    __tablename__ = "class_group"
    __table_args__ = (
        UniqueConstraint(
            "academic_year_id",
            "level_id",
            "stream_id",
            name="uq_class_group_academic_year_id_level_id_stream_id",
        ),
    )

    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_year.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("level.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    stream_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stream.id", ondelete="RESTRICT"), index=True
    )
    #: Denormalised "Standard 4 Blue". Written by the service from
    #: app.domain.enrolment.class_group_name so that a roll, an invoice and an
    #: SMS all say the same thing.
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    class_teacher_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), index=True
    )
    room: Mapped[str | None] = mapped_column(String(40))
    capacity: Mapped[int | None] = mapped_column(SmallInteger)


class Subject(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """Kiswahili at Standard 2 and at Form 3 are the same subject against a
    different syllabus, so the subject is one row and the levels it is taught
    at are rows in `subject_level`."""

    __tablename__ = "subject"
    __table_args__ = (UniqueConstraint("school_id", "code", name="uq_subject_school_id_code"),)

    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Core subjects are the ones the timetable builder tries to place in
    #: morning periods: Maths, English, Kiswahili, Science.
    is_core: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_examinable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class SubjectLevel(UUIDPrimaryKey, TenantScoped, Base):
    __tablename__ = "subject_level"
    __table_args__ = (
        UniqueConstraint("subject_id", "level_id", name="uq_subject_level_subject_id_level_id"),
    )

    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subject.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("level.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Periods per week the syllabus allocates. The timetable builder scores a
    #: draft against this rather than enforcing it.
    periods_per_week: Mapped[int | None] = mapped_column(SmallInteger)


class TeachingAssignment(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """Who teaches what to whom. Drives the timetable and who may enter marks.

    One teacher per subject per class group: co-teaching exists, but modelling
    it here would mean two people able to enter the same mark, and nobody able
    to say whose it was.
    """

    __tablename__ = "teaching_assignment"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "class_group_id",
            name="uq_teaching_assignment_subject_id_class_group_id",
        ),
    )

    teacher_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subject.id", ondelete="CASCADE"), nullable=False, index=True
    )
    class_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("class_group.id", ondelete="CASCADE"), nullable=False, index=True
    )
