"""Self-registration: one table for the whole funnel.

An application is not a student yet, which is why it holds the child's and the
guardian's details as plain columns instead of pointing at `student` and
`guardian` rows. Those rows are created once, at enrolment, when a deposit has
actually been paid — so the roll never fills up with children who applied and
never came, and the duplicate check has something to compare against.

The flat shape also makes the form resumable. A parent on a 3G connection who
loses the page at question nine comes back by OTP to a half-filled row, and a
half-filled row is legal here: `app.domain.admissions` decides when there is
enough to submit.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.admissions import ApplicationState
from app.models.base import Base, TenantScoped, Timestamped, UUIDPrimaryKey, pg_enum
from app.models.guardian import Relationship
from app.models.student import Sex


class Application(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    __tablename__ = "application"
    __table_args__ = (
        UniqueConstraint("school_id", "reference", name="uq_application_school_id_reference"),
        # The review queue's query, and the dashboard tile's count.
        Index("ix_application_school_id_state_created_at", "school_id", "state", "created_at"),
        # A returning parent is found by the phone they verified.
        Index("ix_application_school_id_phone", "school_id", "phone"),
    )

    #: Short and sayable over the phone: "APP-2027-0043".
    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[ApplicationState] = mapped_column(
        pg_enum(ApplicationState, "application_state"),
        nullable=False,
        server_default=ApplicationState.STARTED.value,
    )

    #: The verified phone is the application's identity before anything else is
    #: collected. Verifying it first is what kills most junk applications.
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    child_first_name: Mapped[str | None] = mapped_column(String(64))
    child_middle_name: Mapped[str | None] = mapped_column(String(64))
    child_surname: Mapped[str | None] = mapped_column(String(64))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[Sex | None] = mapped_column(pg_enum(Sex, "sex"))
    birth_certificate_no: Mapped[str | None] = mapped_column(String(64))
    previous_school: Mapped[str | None] = mapped_column(String(160))
    special_needs: Mapped[str | None] = mapped_column(Text)

    level_applied_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("level.id", ondelete="RESTRICT"), index=True
    )
    academic_year_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("academic_year.id", ondelete="RESTRICT"), index=True
    )

    guardian_name: Mapped[str | None] = mapped_column(String(160))
    guardian_phone: Mapped[str | None] = mapped_column(String(20))
    guardian_email: Mapped[str | None] = mapped_column(String(160))
    guardian_relationship: Mapped[Relationship | None] = mapped_column(
        pg_enum(Relationship, "guardian_relationship")
    )
    guardian_national_id: Mapped[str | None] = mapped_column(String(32))
    guardian_address: Mapped[str | None] = mapped_column(Text)

    # --- screening ---------------------------------------------------------
    #: The child this application may be a duplicate of, with the score and
    #: the reason that produced the flag. Never acted on automatically: a
    #: registrar accepts or merges, because the commonest high score is a
    #: sibling, and a sibling is not a duplicate.
    duplicate_of_student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="SET NULL"), index=True
    )
    duplicate_score: Mapped[int | None] = mapped_column(Integer)
    duplicate_reason: Mapped[str | None] = mapped_column(String(200))
    screening_note: Mapped[str | None] = mapped_column(Text)
    screened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    screened_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )

    # --- offer -------------------------------------------------------------
    #: Issued at the offer, which is the point an admission number is burnt.
    admission_no: Mapped[str | None] = mapped_column(String(40))
    offer_letter_url: Mapped[str | None] = mapped_column(Text)
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    offer_expires_on: Mapped[date | None] = mapped_column(Date)
    #: `use_alter` because application and invoice point at each other: an
    #: offer raises the deposit invoice, and that invoice belongs to the
    #: application. Without it, SQLAlchemy cannot order the two CREATE TABLEs.
    deposit_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "invoice.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_application_deposit_invoice_id_invoice",
        ),
        index=True,
    )
    deposit_due: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    # --- conversion --------------------------------------------------------
    #: Acceptance is the deposit arriving, never a button somebody presses.
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="SET NULL"), index=True
    )
    enrolment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("enrolment.id", ondelete="SET NULL")
    )

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decline_reason: Mapped[str | None] = mapped_column(Text)
    #: Where the family heard about the school. The marketing question every
    #: head teacher asks in month two, cheap to collect now.
    source: Mapped[str | None] = mapped_column(String(64))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Application {self.reference} {self.state}>"
