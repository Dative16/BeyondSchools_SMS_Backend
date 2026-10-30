"""Fee structures, invoices and invoice lines.

Two things in here are deliberately not editable fields.

`invoice.billed` and `invoice.settled` are maintained by database triggers from
the lines and the payments, never assigned by application code. The temptation
in every school system is to let somebody type a balance into a form; a balance
that can be typed is a balance that stops reconciling, and by the time anyone
notices, nobody can say which number was ever right.

A published fee structure is also frozen. Invoices already raised against it
would otherwise change retrospectively, and a parent who photographs a bill
and comes back three weeks later must find the same figure on it.
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
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.invoicing import (
    FeeCategory,
    FeeFrequency,
    FeeStructureStatus,
    InvoiceLineKind,
)
from app.models.base import Base, TenantScoped, Timestamped, UUIDPrimaryKey, pg_enum


class FeeStructure(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """What one level costs in one year.

    Scoped to a stream as well as a level, because boarding and day streams of
    the same class pay differently and a single structure for both means
    either boarding fees on a day pupil's invoice or a bursar editing every
    invoice by hand.
    """

    __tablename__ = "fee_structure"
    #: Two partial indexes rather than one unique constraint over the three
    #: columns, because PostgreSQL treats NULL as distinct: a plain constraint
    #: on (year, level, stream) says nothing at all about the level-wide case,
    #: where the stream is NULL — and that is nearly every structure a school
    #: writes. Two level-wide structures for one level merge in the term run
    #: and bill the whole level the sum of both. See migration 0003.
    #:
    #: Archived rows are outside both, so a discarded draft does not block the
    #: structure written to replace it.
    __table_args__ = (
        Index(
            "uq_fee_structure_level_wide",
            "academic_year_id",
            "level_id",
            unique=True,
            postgresql_where=text("stream_id IS NULL AND status <> 'archived'"),
        ),
        Index(
            "uq_fee_structure_per_stream",
            "academic_year_id",
            "level_id",
            "stream_id",
            unique=True,
            postgresql_where=text("stream_id IS NOT NULL AND status <> 'archived'"),
        ),
    )

    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_year.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("level.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stream_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stream.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[FeeStructureStatus] = mapped_column(
        pg_enum(FeeStructureStatus, "fee_structure_status"),
        nullable=False,
        server_default=FeeStructureStatus.DRAFT.value,
    )
    #: Nothing can be invoiced from a draft, which is why the dashboard warns
    #: when a level's structure is still unpublished in week four of term.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)


class FeeItem(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """One line of a structure: what it is, how much, and how often."""

    __tablename__ = "fee_item"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_not_negative"),
        CheckConstraint(
            "term_index IS NULL OR term_index BETWEEN 1 AND 4", name="term_index_in_range"
        ),
    )

    fee_structure_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fee_structure.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    category: Mapped[FeeCategory] = mapped_column(
        pg_enum(FeeCategory, "fee_category"), nullable=False
    )
    frequency: Mapped[FeeFrequency] = mapped_column(
        pg_enum(FeeFrequency, "fee_frequency"), nullable=False
    )
    #: Set only for an item billed in one particular term.
    term_index: Mapped[int | None] = mapped_column(SmallInteger)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")


class InvoiceKind(StrEnum):
    TERM = "term"
    #: Raised at an offer, against an application rather than an enrolment.
    DEPOSIT = "deposit"
    ADHOC = "adhoc"


class Invoice(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """A bill.

    Attached to an enrolment where there is one, so arrears follow the child
    across years; attached to an application where there is not, because the
    deposit invoice is what converts the application into an enrolment and so
    necessarily precedes it. The check constraint says exactly that, and stops
    an invoice floating free of both.
    """

    __tablename__ = "invoice"
    __table_args__ = (
        UniqueConstraint("school_id", "reference", name="uq_invoice_school_id_reference"),
        CheckConstraint(
            "enrolment_id IS NOT NULL OR application_id IS NOT NULL",
            name="belongs_to_enrolment_or_application",
        ),
        CheckConstraint("billed >= 0 AND settled >= 0", name="amounts_not_negative"),
        # The arrears report: open invoices past their due date, by school.
        Index("ix_invoice_school_id_due_date", "school_id", "due_date"),
        Index("ix_invoice_school_id_student_id", "school_id", "student_id"),
    )

    #: Quoted by a parent paying over USSD, so it is short and has no
    #: characters that are ambiguous when read aloud.
    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[InvoiceKind] = mapped_column(
        pg_enum(InvoiceKind, "invoice_kind"), nullable=False, server_default=InvoiceKind.TERM.value
    )

    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="RESTRICT"), index=True
    )
    enrolment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("enrolment.id", ondelete="RESTRICT"), index=True
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("application.id", ondelete="RESTRICT"), index=True
    )
    term_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("term.id", ondelete="RESTRICT"), index=True
    )

    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="TZS")

    #: Maintained by trigger from invoice_line. Do not assign.
    billed: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    #: Maintained by trigger from payment. Do not assign.
    settled: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")

    #: Voiding rather than deleting: a bill a parent has already seen has to
    #: stay visible, with a reason, or the next statement makes no sense.
    is_void: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    void_reason: Mapped[str | None] = mapped_column(Text)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )

    note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    #: Set when the invoice was first sent to a guardian by SMS, so a reminder
    #: run can tell "never told" from "told and ignored".
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Invoice {self.reference} billed={self.billed} settled={self.settled}>"


class InvoiceLine(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """What an invoice is made of.

    Lines are append-only in practice: a mistake is corrected by a credit note
    line, not by editing the charge. That keeps the bill a parent photographed
    and the bill the school holds the same document.
    """

    __tablename__ = "invoice_line"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_not_negative"),
        Index("ix_invoice_line_invoice_id_sort_order", "invoice_id", "sort_order"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Always positive. The sign is the kind's business, not the typist's —
    #: see app.domain.invoicing.signed.
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    kind: Mapped[InvoiceLineKind] = mapped_column(
        pg_enum(InvoiceLineKind, "invoice_line_kind"),
        nullable=False,
        server_default=InvoiceLineKind.CHARGE.value,
    )
    fee_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fee_item.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )


class FeeDiscount(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """A standing reduction for one child: staff child, sibling, bursary.

    Stored against the student rather than written into each invoice, so that
    next term's invoice run applies it without anybody remembering to, and so
    that a bursary can be ended on a date instead of unpicked from six bills.
    """

    __tablename__ = "fee_discount"
    __table_args__ = (
        CheckConstraint(
            "(amount IS NOT NULL) <> (percent IS NOT NULL)",
            name="amount_or_percent_exactly_one",
        ),
        CheckConstraint(
            "percent IS NULL OR percent BETWEEN 1 AND 100", name="percent_in_range"
        ),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    percent: Mapped[int | None] = mapped_column(SmallInteger)
    #: Narrow the discount to one category, for "free tuition, pays transport".
    category: Mapped[FeeCategory | None] = mapped_column(pg_enum(FeeCategory, "fee_category"))
    starts_on: Mapped[date] = mapped_column(Date, nullable=False, server_default=text("now()"))
    ends_on: Mapped[date | None] = mapped_column(Date)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)
