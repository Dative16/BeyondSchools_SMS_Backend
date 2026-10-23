"""The ledger.

One table, and two rules that the schema itself enforces.

`(school_id, external_ref)` is unique. Aggregators replay callbacks — not
occasionally, routinely — and a replay that credits twice is money a school
cannot account for. The second insert fails at the database, whatever the
service layer believes.

A payment is never edited into existence. It arrives from a verified callback,
or from a manual entry that records who made it and why, and `applied` moves
`invoice.settled` by trigger. There is no path that sets a balance directly.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.payments import Channel, PaymentState
from app.models.base import Base, TenantScoped, Timestamped, UUIDPrimaryKey, pg_enum


class Payment(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """Money that arrived.

    `invoice_id` is nullable because money arrives that nobody can place: a
    parent pays with no reference from a number the school does not hold. That
    is a suspense entry — recorded, visible, and waiting for a bursar to match
    it. Dropping such a payment on the floor, or guessing an invoice for it, is
    how a month-end stops balancing.
    """

    __tablename__ = "payment"
    __table_args__ = (
        # The idempotency guarantee, in the only place it cannot be forgotten.
        UniqueConstraint("school_id", "external_ref", name="uq_payment_school_id_external_ref"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("applied >= 0 AND applied <= amount", name="applied_within_amount"),
        CheckConstraint(
            "is_manual = false OR recorded_by_id IS NOT NULL",
            name="manual_entry_names_its_author",
        ),
        # The bursar's suspense list, and the reconciliation job's day window.
        Index(
            "ix_payment_school_id_unmatched",
            "school_id",
            "received_at",
            postgresql_where=text("invoice_id IS NULL"),
        ),
        Index("ix_payment_school_id_received_at", "school_id", "received_at"),
        Index("ix_payment_invoice_id_state", "invoice_id", "state"),
    )

    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice.id", ondelete="RESTRICT"), index=True
    )
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="SET NULL"), index=True
    )

    #: The aggregator's transaction id. For a manual entry, a receipt number
    #: prefixed so it cannot collide with a provider's.
    external_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    #: How much of `amount` went against the invoice. The remainder is an
    #: overpayment held as credit for the family.
    applied: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="TZS")

    channel: Mapped[Channel] = mapped_column(pg_enum(Channel, "payment_channel"), nullable=False)
    state: Mapped[PaymentState] = mapped_column(
        pg_enum(PaymentState, "payment_state"),
        nullable=False,
        server_default=PaymentState.SETTLED.value,
    )
    provider: Mapped[str | None] = mapped_column(String(32))
    msisdn: Mapped[str | None] = mapped_column(String(20), index=True)
    #: What the parent typed as a reference, if anything. The first of the
    #: three ways a payment finds its invoice.
    payer_reference: Mapped[str | None] = mapped_column(String(64))
    payer_name: Mapped[str | None] = mapped_column(String(160))

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    #: The provider's own settlement date, which is what the nightly
    #: reconciliation compares against — not the moment we heard about it.
    settled_on: Mapped[date | None] = mapped_column(Date)

    #: The callback exactly as it arrived. Kept because the first argument with
    #: an aggregator is always about what they actually sent.
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Why this is in suspense, or why it was rejected.
    reason: Mapped[str | None] = mapped_column(Text)

    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    recorded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    matched_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    #: A reversal is a state change plus a reason, never a deletion. The
    #: trigger that maintains invoice.settled watches this.
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_reason: Mapped[str | None] = mapped_column(Text)

    receipt_no: Mapped[str | None] = mapped_column(String(32))
    receipt_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Payment {self.external_ref} {self.amount} {self.state}>"


class ReconciliationStatus(StrEnum):
    MATCHED = "matched"
    #: In the aggregator's report, not in our ledger.
    MISSING_LOCALLY = "missing_locally"
    #: In our ledger, not in the aggregator's report.
    MISSING_REMOTELY = "missing_remotely"
    AMOUNT_MISMATCH = "amount_mismatch"
    RESOLVED = "resolved"


class ReconciliationException(UUIDPrimaryKey, TenantScoped, Timestamped, Base):
    """One line of the nightly settlement comparison that did not agree.

    Raised in both directions, because both directions happen and only one of
    them is the one people expect. A month-end that does not balance destroys
    the bursar's trust in every other number in the system, so the drift is
    surfaced the morning after rather than found in week five.
    """

    __tablename__ = "reconciliation_exception"
    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "settlement_date",
            "external_ref",
            name="uq_reconciliation_exception_date_external_ref",
        ),
    )

    settlement_date: Mapped[date] = mapped_column(Date, nullable=False)
    external_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ReconciliationStatus] = mapped_column(
        pg_enum(ReconciliationStatus, "reconciliation_status"), nullable=False
    )
    provider_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    ledger_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment.id", ondelete="SET NULL")
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    resolution_note: Mapped[str | None] = mapped_column(Text)
