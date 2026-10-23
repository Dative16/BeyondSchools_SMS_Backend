"""Initial schema: tenancy, identity, academic structure, people, fees

Everything Phase 1 needs, plus the four invariants that are cheaper to enforce
in PostgreSQL than to defend in Python:

1. A student has at most one active enrolment at a time.
2. One primary guardian per child, and one current academic year per school.
3. An invoice's billed and settled totals are maintained by trigger from its
   lines and its payments, and cannot be assigned by application code.
4. A payment's external_ref is unique per school, so a replayed webhook
   credits once.

The second half of this migration is row-level security. Every table that
carries school_id gets a policy, and `tests/integration/test_tenant_isolation.py`
compares the list of policies against `app.models.TENANT_TABLES` so a new
tenant table that nobody protected fails a test instead of leaking.

Revision ID: 0001
Revises:
Create Date: 2026-10-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.models import TENANT_TABLES

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------
# Enum types
#
# Created once, up front, rather than implicitly by the first table that uses
# one: `sex` appears on both student and application, and an implicit create
# would run twice and fail on the second.
# --------------------------------------------------------------------------
ENUMS: dict[str, tuple[str, ...]] = {
    "school_status": ("pending", "active", "suspended"),
    "level_stage": ("pre_primary", "primary", "lower_secondary", "upper_secondary"),
    "user_role": (
        "platform_admin",
        "school_admin",
        "head_of_academics",
        "bursar",
        "class_teacher",
        "subject_teacher",
        "guardian",
        "student",
        "applicant",
    ),
    "otp_purpose": ("apply", "login", "password_reset", "phone_change"),
    "sex": ("f", "m"),
    "student_status": (
        "prospective",
        "active",
        "alumnus",
        "transferred_out",
        "withdrawn",
        "deceased",
    ),
    "enrolment_outcome": (
        "active",
        "promoted",
        "repeated",
        "graduated",
        "transferred_out",
        "withdrawn",
    ),
    "document_kind": (
        "birth_certificate",
        "report_card",
        "transfer_letter",
        "photo",
        "medical",
        "other",
    ),
    "guardian_relationship": (
        "mother",
        "father",
        "grandparent",
        "aunt_uncle",
        "sibling",
        "legal_guardian",
        "sponsor",
        "other",
    ),
    "application_state": (
        "started",
        "submitted",
        "screened",
        "offered",
        "accepted",
        "enrolled",
        "declined",
        "withdrawn",
    ),
    "fee_structure_status": ("draft", "published", "archived"),
    "fee_category": (
        "tuition",
        "admission",
        "examination",
        "boarding",
        "transport",
        "uniform",
        "meals",
        "development",
        "other",
    ),
    "fee_frequency": ("per_term", "annual", "one_off"),
    "invoice_kind": ("term", "deposit", "adhoc"),
    "invoice_line_kind": ("charge", "discount", "credit_note", "arrears_brought_forward"),
    "payment_channel": (
        "mpesa",
        "airtel_money",
        "mixx",
        "halopesa",
        "bank",
        "card",
        "counter",
    ),
    "payment_state": ("pending", "settled", "failed", "reversed"),
    "reconciliation_status": (
        "matched",
        "missing_locally",
        "missing_remotely",
        "amount_mismatch",
        "resolved",
    ),
    "audit_outcome": ("success", "denied", "failed"),
}


def enum_col(name: str) -> postgresql.ENUM:
    """Reference an existing type. `create_type=False` keeps SQLAlchemy from
    emitting a second CREATE TYPE when a table is built."""
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def uuid_col() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _tenant_column() -> sa.Column:
    return sa.Column(
        "school_id",
        uuid_col(),
        sa.ForeignKey("school.id", ondelete="CASCADE"),
        nullable=False,
    )


APP_ROLE = settings.DB_APP_ROLE


def upgrade() -> None:
    bind = op.get_bind()

    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    _create_tables()
    _create_partial_indexes()
    _create_ledger_triggers()
    _create_tenancy_functions()
    _enable_row_level_security()
    _grant_to_app_role()


def downgrade() -> None:
    bind = op.get_bind()

    op.execute("DROP TRIGGER IF EXISTS trg_invoice_line_totals ON invoice_line")
    op.execute("DROP TRIGGER IF EXISTS trg_payment_totals ON payment")
    op.execute("DROP TRIGGER IF EXISTS trg_invoice_totals_are_derived ON invoice")
    op.execute("DROP FUNCTION IF EXISTS invoice_recalculate(uuid)")
    op.execute("DROP FUNCTION IF EXISTS invoice_line_totals()")
    op.execute("DROP FUNCTION IF EXISTS payment_totals()")
    op.execute("DROP FUNCTION IF EXISTS invoice_totals_are_derived()")
    op.execute("DROP FUNCTION IF EXISTS resolve_school_by_subdomain(text)")
    op.execute("DROP FUNCTION IF EXISTS current_school()")
    op.execute("DROP FUNCTION IF EXISTS platform_scope()")

    for table in (
        "audit_event",
        "reconciliation_exception",
        "payment",
        "invoice_line",
        "invoice",
        "fee_discount",
        "fee_item",
        "fee_structure",
        "student_document",
        "application",
        "guardian_student",
        "teaching_assignment",
        "subject_level",
        "subject",
        "enrolment",
        "class_group",
        "otp_challenge",
        "refresh_token",
        "app_user",
        "guardian",
        "student",
        "stream",
        "term",
        "academic_year_exit_level",
        "level",
        "academic_year",
        "school_sequence",
        "school",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    for name in ENUMS:
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)


# ==========================================================================
# Tables
# ==========================================================================
def _create_tables() -> None:
    op.create_table(
        "school",
        sa.Column("id", uuid_col(), primary_key=True),
        *_timestamps(),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("short_name", sa.String(32)),
        sa.Column("subdomain", sa.String(63), nullable=False),
        sa.Column("registration_number", sa.String(64)),
        sa.Column("status", enum_col("school_status"), nullable=False, server_default="pending"),
        sa.Column("motto", sa.String(200)),
        sa.Column("logo_url", sa.Text),
        sa.Column("brand_color", sa.String(9), nullable=False, server_default="#22C55E"),
        sa.Column("phone", sa.String(32)),
        sa.Column("email", sa.String(160)),
        sa.Column("address", sa.Text),
        sa.Column("region", sa.String(80)),
        sa.Column("district", sa.String(80)),
        sa.Column("currency", sa.String(3), nullable=False, server_default="TZS"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Africa/Dar_es_Salaam"),
        sa.Column("locale", sa.String(8), nullable=False, server_default="en"),
        sa.Column(
            "admission_no_format",
            sa.String(64),
            nullable=False,
            server_default="{year}/{sequence:04d}",
        ),
        sa.Column("deposit_percent", sa.SmallInteger, nullable=False, server_default="30"),
        sa.Column("invoice_grace_days", sa.SmallInteger, nullable=False, server_default="14"),
        sa.Column("arrears_blocks_results", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("arrears_blocks_promotion", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("arrears_threshold", sa.Numeric(14, 2)),
        sa.Column("ai_platform_tenant_ref", sa.String(128)),
        sa.UniqueConstraint("subdomain", name="uq_school_subdomain"),
    )

    op.create_table(
        "school_sequence",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("scope", sa.String(40), nullable=False, server_default=""),
        sa.Column("value", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "school_id", "name", "scope", name="uq_school_sequence_school_id_name_scope"
        ),
    )
    op.create_index("ix_school_sequence_school_id", "school_sequence", ["school_id"])

    op.create_table(
        "academic_year",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("starts_on", sa.Date, nullable=False),
        sa.Column("ends_on", sa.Date, nullable=False),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_closed", sa.Boolean, nullable=False, server_default="false"),
        sa.UniqueConstraint("school_id", "label", name="uq_academic_year_school_id_label"),
    )
    op.create_index("ix_academic_year_school_id", "academic_year", ["school_id"])

    op.create_table(
        "level",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("stage", enum_col("level_stage"), nullable=False),
        sa.Column("order_index", sa.Integer, nullable=False),
        sa.Column("next_level_id", uuid_col(), sa.ForeignKey("level.id", ondelete="SET NULL")),
        sa.Column("entry_age", sa.SmallInteger),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.UniqueConstraint("school_id", "code", name="uq_level_school_id_code"),
    )
    op.create_index("ix_level_school_id", "level", ["school_id"])
    op.create_index("ix_level_school_id_order_index", "level", ["school_id", "order_index"])

    op.create_table(
        "academic_year_exit_level",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        sa.Column(
            "academic_year_id",
            uuid_col(),
            sa.ForeignKey("academic_year.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "level_id", uuid_col(), sa.ForeignKey("level.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("note", sa.Text),
        sa.UniqueConstraint(
            "academic_year_id",
            "level_id",
            name="uq_academic_year_exit_level_academic_year_id_level_id",
        ),
    )
    op.create_index("ix_academic_year_exit_level_school_id", "academic_year_exit_level", ["school_id"])
    op.create_index(
        "ix_academic_year_exit_level_academic_year_id",
        "academic_year_exit_level",
        ["academic_year_id"],
    )
    op.create_index("ix_academic_year_exit_level_level_id", "academic_year_exit_level", ["level_id"])

    op.create_table(
        "term",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "academic_year_id",
            uuid_col(),
            sa.ForeignKey("academic_year.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("index", sa.SmallInteger, nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("starts_on", sa.Date, nullable=False),
        sa.Column("ends_on", sa.Date, nullable=False),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default="false"),
        sa.UniqueConstraint("academic_year_id", "index", name="uq_term_academic_year_id_index"),
    )
    op.create_index("ix_term_school_id", "term", ["school_id"])
    op.create_index("ix_term_academic_year_id", "term", ["academic_year_id"])

    op.create_table(
        "stream",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "level_id", uuid_col(), sa.ForeignKey("level.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("capacity", sa.SmallInteger),
        sa.Column("is_boarding", sa.Boolean, nullable=False, server_default="false"),
        sa.UniqueConstraint("level_id", "name", name="uq_stream_level_id_name"),
    )
    op.create_index("ix_stream_school_id", "stream", ["school_id"])
    op.create_index("ix_stream_level_id", "stream", ["level_id"])

    op.create_table(
        "student",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("admission_no", sa.String(40), nullable=False),
        sa.Column("first_name", sa.String(64), nullable=False),
        sa.Column("middle_name", sa.String(64)),
        sa.Column("surname", sa.String(64), nullable=False),
        sa.Column("date_of_birth", sa.Date, nullable=False),
        sa.Column("sex", enum_col("sex"), nullable=False),
        sa.Column("photo_url", sa.Text),
        sa.Column(
            "status", enum_col("student_status"), nullable=False, server_default="prospective"
        ),
        sa.Column("nationality", sa.String(48), nullable=False, server_default="Tanzanian"),
        sa.Column("birth_certificate_no", sa.String(64)),
        sa.Column("special_needs", sa.Text),
        sa.Column("blood_group", sa.String(8)),
        sa.Column("previous_school", sa.String(160)),
        sa.Column(
            "arrears_brought_forward", sa.Numeric(14, 2), nullable=False, server_default="0"
        ),
        sa.Column("ai_platform_user_ref", sa.String(128)),
        sa.Column("ai_level_synced_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("school_id", "admission_no", name="uq_student_school_id_admission_no"),
    )
    op.create_index("ix_student_school_id", "student", ["school_id"])
    op.create_index(
        "ix_student_school_id_surname_first_name",
        "student",
        ["school_id", "surname", "first_name"],
    )

    op.create_table(
        "guardian",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("full_name", sa.String(160), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("alt_phone", sa.String(20)),
        sa.Column("email", sa.String(160)),
        sa.Column("national_id", sa.String(32)),
        sa.Column("occupation", sa.String(80)),
        sa.Column("address", sa.Text),
        sa.Column("locale", sa.String(8), nullable=False, server_default="en"),
        sa.Column("sms_opted_out", sa.Boolean, nullable=False, server_default="false"),
        sa.UniqueConstraint("school_id", "phone", name="uq_guardian_school_id_phone"),
    )
    op.create_index("ix_guardian_school_id", "guardian", ["school_id"])
    op.create_index("ix_guardian_school_id_full_name", "guardian", ["school_id", "full_name"])

    op.create_table(
        "app_user",
        sa.Column("id", uuid_col(), primary_key=True),
        *_timestamps(),
        sa.Column("school_id", uuid_col(), sa.ForeignKey("school.id", ondelete="CASCADE")),
        sa.Column("full_name", sa.String(160), nullable=False),
        sa.Column("email", sa.String(160)),
        sa.Column("phone", sa.String(20)),
        sa.Column("role", enum_col("user_role"), nullable=False),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("must_change_password", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("staff_no", sa.String(32)),
        sa.Column("student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="CASCADE")),
        sa.Column("guardian_id", uuid_col(), sa.ForeignKey("guardian.id", ondelete="CASCADE")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("failed_logins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("ai_platform_user_ref", sa.String(128)),
    )
    op.create_index("ix_app_user_school_id", "app_user", ["school_id"])
    op.create_index("ix_app_user_student_id", "app_user", ["student_id"])
    op.create_index("ix_app_user_guardian_id", "app_user", ["guardian_id"])

    op.create_table(
        "refresh_token",
        sa.Column("id", uuid_col(), primary_key=True),
        sa.Column(
            "user_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("school_id", uuid_col(), sa.ForeignKey("school.id", ondelete="CASCADE")),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_reason", sa.String(64)),
        sa.Column(
            "replaced_by_id", uuid_col(), sa.ForeignKey("refresh_token.id", ondelete="SET NULL")
        ),
        sa.Column("user_agent", sa.Text),
        sa.Column("ip", postgresql.INET),
        sa.UniqueConstraint("token_hash", name="uq_refresh_token_token_hash"),
    )
    op.create_index("ix_refresh_token_user_id", "refresh_token", ["user_id"])
    op.create_index("ix_refresh_token_school_id", "refresh_token", ["school_id"])

    op.create_table(
        "otp_challenge",
        sa.Column("id", uuid_col(), primary_key=True),
        sa.Column("school_id", uuid_col(), sa.ForeignKey("school.id", ondelete="CASCADE")),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("purpose", enum_col("otp_purpose"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("ip", postgresql.INET),
    )
    op.create_index("ix_otp_challenge_school_id", "otp_challenge", ["school_id"])
    op.create_index(
        "ix_otp_challenge_school_id_phone_purpose",
        "otp_challenge",
        ["school_id", "phone", "purpose"],
    )

    op.create_table(
        "class_group",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "academic_year_id",
            uuid_col(),
            sa.ForeignKey("academic_year.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "level_id", uuid_col(), sa.ForeignKey("level.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("stream_id", uuid_col(), sa.ForeignKey("stream.id", ondelete="RESTRICT")),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column(
            "class_teacher_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.Column("room", sa.String(40)),
        sa.Column("capacity", sa.SmallInteger),
        sa.UniqueConstraint(
            "academic_year_id",
            "level_id",
            "stream_id",
            name="uq_class_group_academic_year_id_level_id_stream_id",
        ),
    )
    op.create_index("ix_class_group_school_id", "class_group", ["school_id"])
    op.create_index("ix_class_group_academic_year_id", "class_group", ["academic_year_id"])
    op.create_index("ix_class_group_level_id", "class_group", ["level_id"])
    op.create_index("ix_class_group_stream_id", "class_group", ["stream_id"])
    op.create_index("ix_class_group_class_teacher_id", "class_group", ["class_teacher_id"])

    op.create_table(
        "enrolment",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "academic_year_id",
            uuid_col(),
            sa.ForeignKey("academic_year.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "class_group_id",
            uuid_col(),
            sa.ForeignKey("class_group.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("outcome", enum_col("enrolment_outcome"), nullable=False, server_default="active"),
        sa.Column("enrolled_on", sa.Date, nullable=False),
        sa.Column("exited_on", sa.Date),
        sa.Column("exit_reason", sa.Text),
        sa.Column(
            "previous_enrolment_id", uuid_col(), sa.ForeignKey("enrolment.id", ondelete="SET NULL")
        ),
        sa.Column("promotion_run_id", uuid_col()),
        sa.Column("is_repeating", sa.Boolean, nullable=False, server_default="false"),
        sa.UniqueConstraint(
            "student_id", "academic_year_id", name="uq_enrolment_student_id_academic_year_id"
        ),
    )
    op.create_index("ix_enrolment_school_id", "enrolment", ["school_id"])
    op.create_index("ix_enrolment_student_id", "enrolment", ["student_id"])
    op.create_index("ix_enrolment_academic_year_id", "enrolment", ["academic_year_id"])
    op.create_index("ix_enrolment_class_group_id", "enrolment", ["class_group_id"])
    op.create_index("ix_enrolment_promotion_run_id", "enrolment", ["promotion_run_id"])
    op.create_index("ix_enrolment_class_group_id_outcome", "enrolment", ["class_group_id", "outcome"])

    op.create_table(
        "subject",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("is_core", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_examinable", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.UniqueConstraint("school_id", "code", name="uq_subject_school_id_code"),
    )
    op.create_index("ix_subject_school_id", "subject", ["school_id"])

    op.create_table(
        "subject_level",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        sa.Column(
            "subject_id", uuid_col(), sa.ForeignKey("subject.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "level_id", uuid_col(), sa.ForeignKey("level.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("periods_per_week", sa.SmallInteger),
        sa.UniqueConstraint("subject_id", "level_id", name="uq_subject_level_subject_id_level_id"),
    )
    op.create_index("ix_subject_level_school_id", "subject_level", ["school_id"])
    op.create_index("ix_subject_level_subject_id", "subject_level", ["subject_id"])
    op.create_index("ix_subject_level_level_id", "subject_level", ["level_id"])

    op.create_table(
        "teaching_assignment",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "teacher_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "subject_id", uuid_col(), sa.ForeignKey("subject.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "class_group_id",
            uuid_col(),
            sa.ForeignKey("class_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "subject_id",
            "class_group_id",
            name="uq_teaching_assignment_subject_id_class_group_id",
        ),
    )
    op.create_index("ix_teaching_assignment_school_id", "teaching_assignment", ["school_id"])
    op.create_index("ix_teaching_assignment_teacher_id", "teaching_assignment", ["teacher_id"])
    op.create_index("ix_teaching_assignment_subject_id", "teaching_assignment", ["subject_id"])
    op.create_index(
        "ix_teaching_assignment_class_group_id", "teaching_assignment", ["class_group_id"]
    )

    op.create_table(
        "guardian_student",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "guardian_id", uuid_col(), sa.ForeignKey("guardian.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("relationship", enum_col("guardian_relationship"), nullable=False),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("receives_invoices", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("receives_sms", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("may_collect", sa.Boolean, nullable=False, server_default="true"),
        sa.UniqueConstraint(
            "guardian_id", "student_id", name="uq_guardian_student_guardian_id_student_id"
        ),
    )
    op.create_index("ix_guardian_student_school_id", "guardian_student", ["school_id"])
    op.create_index("ix_guardian_student_guardian_id", "guardian_student", ["guardian_id"])
    op.create_index("ix_guardian_student_student_id", "guardian_student", ["student_id"])

    # The deposit_invoice_id foreign key is added after `invoice` exists: the
    # two tables point at each other, because an offer raises an invoice and a
    # deposit invoice belongs to an application.
    op.create_table(
        "application",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("state", enum_col("application_state"), nullable=False, server_default="started"),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True)),
        sa.Column("child_first_name", sa.String(64)),
        sa.Column("child_middle_name", sa.String(64)),
        sa.Column("child_surname", sa.String(64)),
        sa.Column("date_of_birth", sa.Date),
        sa.Column("sex", enum_col("sex")),
        sa.Column("birth_certificate_no", sa.String(64)),
        sa.Column("previous_school", sa.String(160)),
        sa.Column("special_needs", sa.Text),
        sa.Column(
            "level_applied_id", uuid_col(), sa.ForeignKey("level.id", ondelete="RESTRICT")
        ),
        sa.Column(
            "academic_year_id",
            uuid_col(),
            sa.ForeignKey("academic_year.id", ondelete="RESTRICT"),
        ),
        sa.Column("guardian_name", sa.String(160)),
        sa.Column("guardian_phone", sa.String(20)),
        sa.Column("guardian_email", sa.String(160)),
        sa.Column("guardian_relationship", enum_col("guardian_relationship")),
        sa.Column("guardian_national_id", sa.String(32)),
        sa.Column("guardian_address", sa.Text),
        sa.Column(
            "duplicate_of_student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="SET NULL")
        ),
        sa.Column("duplicate_score", sa.Integer),
        sa.Column("duplicate_reason", sa.String(200)),
        sa.Column("screening_note", sa.Text),
        sa.Column("screened_at", sa.DateTime(timezone=True)),
        sa.Column("screened_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("admission_no", sa.String(40)),
        sa.Column("offer_letter_url", sa.Text),
        sa.Column("offered_at", sa.DateTime(timezone=True)),
        sa.Column("offered_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("offer_expires_on", sa.Date),
        sa.Column("deposit_invoice_id", uuid_col()),
        sa.Column("deposit_due", sa.Numeric(14, 2)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("enrolled_at", sa.DateTime(timezone=True)),
        sa.Column("student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="SET NULL")),
        sa.Column("enrolment_id", uuid_col(), sa.ForeignKey("enrolment.id", ondelete="SET NULL")),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("declined_at", sa.DateTime(timezone=True)),
        sa.Column("decline_reason", sa.Text),
        sa.Column("source", sa.String(64)),
        sa.UniqueConstraint("school_id", "reference", name="uq_application_school_id_reference"),
    )
    op.create_index("ix_application_school_id", "application", ["school_id"])
    op.create_index("ix_application_level_applied_id", "application", ["level_applied_id"])
    op.create_index("ix_application_academic_year_id", "application", ["academic_year_id"])
    op.create_index(
        "ix_application_duplicate_of_student_id", "application", ["duplicate_of_student_id"]
    )
    op.create_index("ix_application_student_id", "application", ["student_id"])
    op.create_index("ix_application_deposit_invoice_id", "application", ["deposit_invoice_id"])
    op.create_index(
        "ix_application_school_id_state_created_at",
        "application",
        ["school_id", "state", "created_at"],
    )
    op.create_index("ix_application_school_id_phone", "application", ["school_id", "phone"])

    op.create_table(
        "student_document",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="CASCADE")),
        sa.Column(
            "application_id", uuid_col(), sa.ForeignKey("application.id", ondelete="CASCADE")
        ),
        sa.Column("kind", enum_col("document_kind"), nullable=False),
        sa.Column("file_url", sa.Text, nullable=False),
        sa.Column("file_name", sa.String(255)),
        sa.Column("content_type", sa.String(80)),
        sa.Column("size_bytes", sa.Integer),
        sa.Column("note", sa.Text),
        sa.Column("uploaded_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "student_id IS NOT NULL OR application_id IS NOT NULL",
            name="ck_student_document_belongs_to_student_or_application",
        ),
    )
    op.create_index("ix_student_document_school_id", "student_document", ["school_id"])
    op.create_index("ix_student_document_student_id", "student_document", ["student_id"])
    op.create_index("ix_student_document_application_id", "student_document", ["application_id"])

    op.create_table(
        "fee_structure",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "academic_year_id",
            uuid_col(),
            sa.ForeignKey("academic_year.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "level_id", uuid_col(), sa.ForeignKey("level.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("stream_id", uuid_col(), sa.ForeignKey("stream.id", ondelete="CASCADE")),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column(
            "status", enum_col("fee_structure_status"), nullable=False, server_default="draft"
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("published_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("note", sa.Text),
        sa.UniqueConstraint(
            "academic_year_id",
            "level_id",
            "stream_id",
            name="uq_fee_structure_academic_year_id_level_id_stream_id",
        ),
    )
    op.create_index("ix_fee_structure_school_id", "fee_structure", ["school_id"])
    op.create_index("ix_fee_structure_academic_year_id", "fee_structure", ["academic_year_id"])
    op.create_index("ix_fee_structure_level_id", "fee_structure", ["level_id"])
    op.create_index("ix_fee_structure_stream_id", "fee_structure", ["stream_id"])

    op.create_table(
        "fee_item",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "fee_structure_id",
            uuid_col(),
            sa.ForeignKey("fee_structure.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("category", enum_col("fee_category"), nullable=False),
        sa.Column("frequency", enum_col("fee_frequency"), nullable=False),
        sa.Column("term_index", sa.SmallInteger),
        sa.Column("is_mandatory", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("sort_order", sa.SmallInteger, nullable=False, server_default="0"),
        sa.CheckConstraint("amount >= 0", name="ck_fee_item_amount_not_negative"),
        sa.CheckConstraint(
            "term_index IS NULL OR term_index BETWEEN 1 AND 4",
            name="ck_fee_item_term_index_in_range",
        ),
    )
    op.create_index("ix_fee_item_school_id", "fee_item", ["school_id"])
    op.create_index("ix_fee_item_fee_structure_id", "fee_item", ["fee_structure_id"])

    op.create_table(
        "invoice",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("kind", enum_col("invoice_kind"), nullable=False, server_default="term"),
        sa.Column("student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="RESTRICT")),
        sa.Column("enrolment_id", uuid_col(), sa.ForeignKey("enrolment.id", ondelete="RESTRICT")),
        sa.Column(
            "application_id", uuid_col(), sa.ForeignKey("application.id", ondelete="RESTRICT")
        ),
        sa.Column("term_id", uuid_col(), sa.ForeignKey("term.id", ondelete="RESTRICT")),
        sa.Column("issued_on", sa.Date, nullable=False),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="TZS"),
        sa.Column("billed", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("settled", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("is_void", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("void_reason", sa.Text),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("voided_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("note", sa.Text),
        sa.Column("created_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("school_id", "reference", name="uq_invoice_school_id_reference"),
        sa.CheckConstraint(
            "enrolment_id IS NOT NULL OR application_id IS NOT NULL",
            name="ck_invoice_belongs_to_enrolment_or_application",
        ),
        sa.CheckConstraint(
            "billed >= 0 AND settled >= 0", name="ck_invoice_amounts_not_negative"
        ),
    )
    op.create_index("ix_invoice_school_id", "invoice", ["school_id"])
    op.create_index("ix_invoice_student_id", "invoice", ["student_id"])
    op.create_index("ix_invoice_enrolment_id", "invoice", ["enrolment_id"])
    op.create_index("ix_invoice_application_id", "invoice", ["application_id"])
    op.create_index("ix_invoice_term_id", "invoice", ["term_id"])
    op.create_index("ix_invoice_school_id_due_date", "invoice", ["school_id", "due_date"])
    op.create_index("ix_invoice_school_id_student_id", "invoice", ["school_id", "student_id"])

    op.create_foreign_key(
        "fk_application_deposit_invoice_id_invoice",
        "application",
        "invoice",
        ["deposit_invoice_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "invoice_line",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "invoice_id", uuid_col(), sa.ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("item", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("kind", enum_col("invoice_line_kind"), nullable=False, server_default="charge"),
        sa.Column("fee_item_id", uuid_col(), sa.ForeignKey("fee_item.id", ondelete="SET NULL")),
        sa.Column("note", sa.Text),
        sa.Column("sort_order", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("created_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.CheckConstraint("amount >= 0", name="ck_invoice_line_amount_not_negative"),
    )
    op.create_index("ix_invoice_line_school_id", "invoice_line", ["school_id"])
    op.create_index("ix_invoice_line_invoice_id", "invoice_line", ["invoice_id"])
    op.create_index(
        "ix_invoice_line_invoice_id_sort_order", "invoice_line", ["invoice_id", "sort_order"]
    )

    op.create_table(
        "fee_discount",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column(
            "student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2)),
        sa.Column("percent", sa.SmallInteger),
        sa.Column("category", enum_col("fee_category")),
        sa.Column("starts_on", sa.Date, nullable=False, server_default=sa.text("now()")),
        sa.Column("ends_on", sa.Date),
        sa.Column("approved_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("note", sa.Text),
        sa.CheckConstraint(
            "(amount IS NOT NULL) <> (percent IS NOT NULL)",
            name="ck_fee_discount_amount_or_percent_exactly_one",
        ),
        sa.CheckConstraint(
            "percent IS NULL OR percent BETWEEN 1 AND 100",
            name="ck_fee_discount_percent_in_range",
        ),
    )
    op.create_index("ix_fee_discount_school_id", "fee_discount", ["school_id"])
    op.create_index("ix_fee_discount_student_id", "fee_discount", ["student_id"])

    op.create_table(
        "payment",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("invoice_id", uuid_col(), sa.ForeignKey("invoice.id", ondelete="RESTRICT")),
        sa.Column("student_id", uuid_col(), sa.ForeignKey("student.id", ondelete="SET NULL")),
        sa.Column("external_ref", sa.String(128), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("applied", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="TZS"),
        sa.Column("channel", enum_col("payment_channel"), nullable=False),
        sa.Column("state", enum_col("payment_state"), nullable=False, server_default="settled"),
        sa.Column("provider", sa.String(32)),
        sa.Column("msisdn", sa.String(20)),
        sa.Column("payer_reference", sa.String(64)),
        sa.Column("payer_name", sa.String(160)),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("settled_on", sa.Date),
        sa.Column("raw", postgresql.JSONB),
        sa.Column("reason", sa.Text),
        sa.Column("is_manual", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("recorded_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("matched_at", sa.DateTime(timezone=True)),
        sa.Column("matched_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.Column("reversal_reason", sa.Text),
        sa.Column("receipt_no", sa.String(32)),
        sa.Column("receipt_sent_at", sa.DateTime(timezone=True)),
        # The idempotency guarantee. A replayed callback fails here, whatever
        # the service layer believes about having seen it before.
        sa.UniqueConstraint(
            "school_id", "external_ref", name="uq_payment_school_id_external_ref"
        ),
        sa.CheckConstraint("amount > 0", name="ck_payment_amount_positive"),
        sa.CheckConstraint(
            "applied >= 0 AND applied <= amount", name="ck_payment_applied_within_amount"
        ),
        sa.CheckConstraint(
            "is_manual = false OR recorded_by_id IS NOT NULL",
            name="ck_payment_manual_entry_names_its_author",
        ),
    )
    op.create_index("ix_payment_school_id", "payment", ["school_id"])
    op.create_index("ix_payment_invoice_id", "payment", ["invoice_id"])
    op.create_index("ix_payment_student_id", "payment", ["student_id"])
    op.create_index("ix_payment_msisdn", "payment", ["msisdn"])
    op.create_index("ix_payment_school_id_received_at", "payment", ["school_id", "received_at"])
    op.create_index("ix_payment_invoice_id_state", "payment", ["invoice_id", "state"])

    op.create_table(
        "reconciliation_exception",
        sa.Column("id", uuid_col(), primary_key=True),
        _tenant_column(),
        *_timestamps(),
        sa.Column("settlement_date", sa.Date, nullable=False),
        sa.Column("external_ref", sa.String(128), nullable=False),
        sa.Column("status", enum_col("reconciliation_status"), nullable=False),
        sa.Column("provider_amount", sa.Numeric(14, 2)),
        sa.Column("ledger_amount", sa.Numeric(14, 2)),
        sa.Column("payment_id", uuid_col(), sa.ForeignKey("payment.id", ondelete="SET NULL")),
        sa.Column("raw", postgresql.JSONB),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("resolution_note", sa.Text),
        sa.UniqueConstraint(
            "school_id",
            "settlement_date",
            "external_ref",
            name="uq_reconciliation_exception_date_external_ref",
        ),
    )
    op.create_index("ix_reconciliation_exception_school_id", "reconciliation_exception", ["school_id"])

    op.create_table(
        "audit_event",
        sa.Column("id", uuid_col(), primary_key=True),
        sa.Column("school_id", uuid_col(), sa.ForeignKey("school.id", ondelete="CASCADE")),
        sa.Column("actor_user_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")),
        sa.Column("actor_role", sa.String(32)),
        sa.Column(
            "on_behalf_of_user_id", uuid_col(), sa.ForeignKey("app_user.id", ondelete="SET NULL")
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(48)),
        sa.Column("entity_id", uuid_col()),
        sa.Column("outcome", enum_col("audit_outcome"), nullable=False, server_default="success"),
        sa.Column("changed", postgresql.JSONB),
        sa.Column("note", sa.Text),
        sa.Column("ip", postgresql.INET),
        sa.Column("user_agent", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index("ix_audit_event_school_id", "audit_event", ["school_id"])
    op.create_index("ix_audit_event_school_id_created_at", "audit_event", ["school_id", "created_at"])
    op.create_index(
        "ix_audit_event_entity_type_entity_id", "audit_event", ["entity_type", "entity_id"]
    )
    op.create_index(
        "ix_audit_event_actor_user_id_created_at", "audit_event", ["actor_user_id", "created_at"]
    )


# ==========================================================================
# The invariants that need a partial index
# ==========================================================================
def _create_partial_indexes() -> None:
    # A child has at most one active enrolment. Promotion closes the old row
    # and writes a new one; if a bug ever tries to leave both open, this is
    # where it stops.
    op.create_index(
        "uq_enrolment_one_active_per_student",
        "enrolment",
        ["student_id"],
        unique=True,
        postgresql_where=sa.text("outcome = 'active'"),
    )

    # "Who do we call about this child" must have exactly one answer.
    op.create_index(
        "uq_guardian_student_one_primary_per_student",
        "guardian_student",
        ["student_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )

    # Invoicing and promotion both pivot on "the current year".
    op.create_index(
        "uq_academic_year_one_current_per_school",
        "academic_year",
        ["school_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    # Email and phone are unique within a school where present. Most guardians
    # have no email address, so a partial index rather than a constraint.
    op.create_index(
        "uq_app_user_school_id_email",
        "app_user",
        ["school_id", "email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index(
        "uq_app_user_school_id_phone",
        "app_user",
        ["school_id", "phone"],
        unique=True,
        postgresql_where=sa.text("phone IS NOT NULL"),
    )

    # The bursar's suspense list: money that arrived with nothing to match it.
    op.create_index(
        "ix_payment_school_id_unmatched",
        "payment",
        ["school_id", "received_at"],
        postgresql_where=sa.text("invoice_id IS NULL"),
    )


# ==========================================================================
# The ledger: billed and settled are derived, and cannot be typed in
# ==========================================================================
def _create_ledger_triggers() -> None:
    # One function recalculates both totals for one invoice. It sets a
    # transaction-local flag while it works, which is what the guard trigger
    # below looks for: the only writes to billed and settled that are allowed
    # are the ones this function makes.
    op.execute(
        """
        CREATE FUNCTION invoice_recalculate(p_invoice_id uuid) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            IF p_invoice_id IS NULL THEN
                RETURN;
            END IF;

            PERFORM set_config('app.ledger_recalc', 'on', true);

            UPDATE invoice i
               SET billed = GREATEST(COALESCE((
                       SELECT SUM(CASE
                                    WHEN l.kind IN ('charge', 'arrears_brought_forward')
                                    THEN l.amount ELSE -l.amount
                                  END)
                         FROM invoice_line l
                        WHERE l.invoice_id = i.id), 0), 0),
                   settled = COALESCE((
                       SELECT SUM(p.applied)
                         FROM payment p
                        WHERE p.invoice_id = i.id
                          AND p.state = 'settled'
                          AND p.reversed_at IS NULL), 0)
             WHERE i.id = p_invoice_id;

            PERFORM set_config('app.ledger_recalc', 'off', true);
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION invoice_line_totals() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM invoice_recalculate(OLD.invoice_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM invoice_recalculate(NEW.invoice_id);
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION payment_totals() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM invoice_recalculate(OLD.invoice_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM invoice_recalculate(NEW.invoice_id);
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )

    # The guard. Without it, "maintained by trigger" would be a comment rather
    # than a fact, and the first support request that asks for a balance to be
    # corrected by hand would get one.
    op.execute(
        """
        CREATE FUNCTION invoice_totals_are_derived() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF COALESCE(current_setting('app.ledger_recalc', true), 'off') <> 'on' THEN
                NEW.billed := OLD.billed;
                NEW.settled := OLD.settled;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_invoice_line_totals
        AFTER INSERT OR UPDATE OR DELETE ON invoice_line
        FOR EACH ROW EXECUTE FUNCTION invoice_line_totals();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_totals
        AFTER INSERT OR UPDATE OR DELETE ON payment
        FOR EACH ROW EXECUTE FUNCTION payment_totals();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_invoice_totals_are_derived
        BEFORE UPDATE OF billed, settled ON invoice
        FOR EACH ROW EXECUTE FUNCTION invoice_totals_are_derived();
        """
    )


# ==========================================================================
# Tenancy
# ==========================================================================
def _create_tenancy_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION current_school() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.school_id', true), '')::uuid;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION platform_scope() RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT COALESCE(NULLIF(current_setting('app.platform_scope', true), ''), 'off') = 'on';
        $$;
        """
    )

    # Sign-in has to resolve a subdomain to a school before any tenant is
    # bound, and a policy permissive enough to allow that would hand every
    # school's settings to anyone who could reach the login route. So the
    # lookup is this function instead: SECURITY DEFINER, parameterised, and
    # returning exactly the fields a login page renders.
    op.execute(
        """
        CREATE FUNCTION resolve_school_by_subdomain(p_subdomain text)
        RETURNS TABLE (
            id uuid,
            name varchar,
            subdomain varchar,
            brand_color varchar,
            logo_url text,
            status school_status,
            timezone varchar,
            locale varchar
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT s.id, s.name, s.subdomain, s.brand_color, s.logo_url,
                   s.status, s.timezone, s.locale
              FROM school s
             WHERE s.subdomain = p_subdomain
               AND s.status <> 'suspended';
        $$;
        """
    )


#: Tables whose school_id is nullable, because the row can belong to the
#: platform rather than to a school. They get one extra clause so that sign-in
#: can find the platform admin before any tenant is bound.
NULLABLE_TENANT_TABLES = frozenset({"app_user", "refresh_token", "otp_challenge", "audit_event"})


def _enable_row_level_security() -> None:
    for table in TENANT_TABLES:
        if table == "school":
            predicate = "id = current_school() OR platform_scope()"
        elif table in NULLABLE_TENANT_TABLES:
            predicate = (
                "school_id = current_school() "
                "OR platform_scope() "
                "OR (school_id IS NULL AND current_school() IS NULL)"
            )
        else:
            predicate = "school_id = current_school() OR platform_scope()"

        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so that the policies apply even when the table's owner is the
        # one connecting — which is how a test proves they are real, and how a
        # mistaken production connection string fails loudly instead of
        # quietly returning everything.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant ON {table}
            USING ({predicate})
            WITH CHECK ({predicate});
            """
        )


def _grant_to_app_role() -> None:
    role = f'"{APP_ROLE}"'
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_school_by_subdomain(text) TO {role}")

    # The audit trail is append-only, and a grant is a better way to say so
    # than a convention: there is no code path, and no console session as the
    # application's role, that can rewrite history.
    op.execute(f"REVOKE UPDATE, DELETE ON audit_event FROM {role}")

    # alembic_version belongs to migrations, which run as the owner.
    op.execute(f"REVOKE ALL ON alembic_version FROM {role}")
