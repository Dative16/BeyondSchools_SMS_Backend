"""Every table in the schema, imported once.

Alembic's autogenerate and the tenancy test both walk `Base.metadata`, so a
model that is not imported here is invisible to both: it will not appear in a
migration, and it will not be checked for a row-level security policy. Adding a
model file without adding it here is the one mistake in this package that
fails quietly.
"""

# OtpPurpose lives in app.domain.identity: every layer names it, and the
# endpoints are not allowed to import app.models at all.
from app.domain.identity import OtpPurpose
from app.models.academic import (
    AcademicYear,
    AcademicYearExitLevel,
    ClassGroup,
    Level,
    Stream,
    Subject,
    SubjectLevel,
    TeachingAssignment,
    Term,
)
from app.models.admission import Application
from app.models.audit import AuditEvent, AuditOutcome
from app.models.base import Base
from app.models.fees import (
    FeeDiscount,
    FeeItem,
    FeeStructure,
    Invoice,
    InvoiceKind,
    InvoiceLine,
)
from app.models.guardian import Guardian, GuardianStudent, Relationship
from app.models.payment import (
    Payment,
    ReconciliationException,
    ReconciliationStatus,
)
from app.models.school import School, SchoolSequence, SchoolStatus
from app.models.student import DocumentKind, Enrolment, Sex, Student, StudentDocument
from app.models.user import AppUser, OtpChallenge, RefreshToken

#: Tables that hold one school's data and are therefore covered by a row-level
#: security policy. `school` is here too: a session bound to one school has no
#: business reading another school's name or settings.
#:
#: `tests/integration/test_tenant_isolation.py` compares this list against
#: `pg_policies`, so a new tenant table that nobody protected fails a test
#: rather than leaking in production.
TENANT_TABLES: tuple[str, ...] = (
    "school",
    "school_sequence",
    "academic_year",
    "academic_year_exit_level",
    "term",
    "level",
    "stream",
    "class_group",
    "subject",
    "subject_level",
    "teaching_assignment",
    "app_user",
    "refresh_token",
    "otp_challenge",
    "student",
    "enrolment",
    "student_document",
    "guardian",
    "guardian_student",
    "application",
    "fee_structure",
    "fee_item",
    "invoice",
    "invoice_line",
    "fee_discount",
    "payment",
    "reconciliation_exception",
    "audit_event",
)

__all__ = [
    "AcademicYear",
    "AcademicYearExitLevel",
    "AppUser",
    "Application",
    "AuditEvent",
    "AuditOutcome",
    "Base",
    "ClassGroup",
    "DocumentKind",
    "Enrolment",
    "FeeDiscount",
    "FeeItem",
    "FeeStructure",
    "Guardian",
    "GuardianStudent",
    "Invoice",
    "InvoiceKind",
    "InvoiceLine",
    "Level",
    "OtpChallenge",
    "OtpPurpose",
    "Payment",
    "ReconciliationException",
    "ReconciliationStatus",
    "RefreshToken",
    "Relationship",
    "School",
    "SchoolSequence",
    "SchoolStatus",
    "Sex",
    "Stream",
    "Student",
    "StudentDocument",
    "Subject",
    "SubjectLevel",
    "TENANT_TABLES",
    "TeachingAssignment",
    "Term",
]
