"""Roles and what each one may do. Pure.

Permissions are checked in the service layer, never in a route, so the same
rule holds whether the call arrives from the staff app, the parent portal or
the AI platform's API. That is only possible because "may this principal do
this?" is a pure function of a role and a scope — no session, no request,
nothing to stand in for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    SCHOOL_ADMIN = "school_admin"
    HEAD_OF_ACADEMICS = "head_of_academics"
    BURSAR = "bursar"
    CLASS_TEACHER = "class_teacher"
    SUBJECT_TEACHER = "subject_teacher"
    GUARDIAN = "guardian"
    STUDENT = "student"
    APPLICANT = "applicant"


STAFF_ROLES = frozenset(
    {
        Role.SCHOOL_ADMIN,
        Role.HEAD_OF_ACADEMICS,
        Role.BURSAR,
        Role.CLASS_TEACHER,
        Role.SUBJECT_TEACHER,
    }
)


class Permission(StrEnum):
    """Named capabilities, not CRUD on tables.

    PAYMENT_RECORD_MANUAL rather than payment:create, because the interesting
    question is never "may this person write to a table" but "may this person
    move money", and the two drift apart the moment a second way to move money
    exists.
    """

    SCHOOL_MANAGE = "school:manage"
    USER_MANAGE = "user:manage"
    ACADEMIC_MANAGE = "academic:manage"
    STUDENT_READ = "student:read"
    STUDENT_MANAGE = "student:manage"
    GUARDIAN_MANAGE = "guardian:manage"
    ADMISSION_REVIEW = "admission:review"
    FEE_STRUCTURE_MANAGE = "fee_structure:manage"
    INVOICE_READ = "invoice:read"
    INVOICE_MANAGE = "invoice:manage"
    PAYMENT_READ = "payment:read"
    PAYMENT_RECORD_MANUAL = "payment:record_manual"
    PAYMENT_MATCH_SUSPENSE = "payment:match_suspense"
    MARK_READ = "mark:read"
    MARK_ENTER = "mark:enter"
    ATTENDANCE_TAKE = "attendance:take"
    TIMETABLE_MANAGE = "timetable:manage"
    ASSIGNMENT_MANAGE = "assignment:manage"
    PROMOTION_RUN = "promotion:run"
    REPORT_READ = "report:read"
    OWN_CHILDREN_READ = "own_children:read"
    OWN_RECORD_READ = "own_record:read"
    OWN_APPLICATION_READ = "own_application:read"

    # Platform level. Held by nobody inside a school, deliberately.
    SCHOOL_ONBOARD = "platform:school_onboard"
    IMPERSONATE = "platform:impersonate"


_P = Permission

#: The whole authorisation model, in one readable place. A role that is not a
#: key here holds nothing, so a newly added role is powerless until somebody
#: decides in a diff what it may do.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.PLATFORM_ADMIN: frozenset({_P.SCHOOL_ONBOARD, _P.IMPERSONATE, _P.SCHOOL_MANAGE}),
    Role.SCHOOL_ADMIN: frozenset(
        {
            _P.SCHOOL_MANAGE,
            _P.USER_MANAGE,
            _P.ACADEMIC_MANAGE,
            _P.STUDENT_READ,
            _P.STUDENT_MANAGE,
            _P.GUARDIAN_MANAGE,
            _P.ADMISSION_REVIEW,
            _P.FEE_STRUCTURE_MANAGE,
            _P.INVOICE_READ,
            _P.INVOICE_MANAGE,
            _P.PAYMENT_READ,
            _P.PAYMENT_RECORD_MANUAL,
            _P.PAYMENT_MATCH_SUSPENSE,
            _P.MARK_READ,
            _P.ATTENDANCE_TAKE,
            _P.TIMETABLE_MANAGE,
            _P.ASSIGNMENT_MANAGE,
            _P.PROMOTION_RUN,
            _P.REPORT_READ,
        }
    ),
    Role.HEAD_OF_ACADEMICS: frozenset(
        {
            _P.ACADEMIC_MANAGE,
            _P.STUDENT_READ,
            _P.STUDENT_MANAGE,
            _P.ADMISSION_REVIEW,
            _P.MARK_READ,
            _P.MARK_ENTER,
            _P.ATTENDANCE_TAKE,
            _P.TIMETABLE_MANAGE,
            _P.ASSIGNMENT_MANAGE,
            _P.PROMOTION_RUN,
            _P.REPORT_READ,
            _P.INVOICE_READ,
        }
    ),
    # The bursar sees money and nothing else. Marks and discipline records are
    # not read-only extras for this role; they are none of its business.
    Role.BURSAR: frozenset(
        {
            _P.STUDENT_READ,
            _P.GUARDIAN_MANAGE,
            _P.ADMISSION_REVIEW,
            _P.FEE_STRUCTURE_MANAGE,
            _P.INVOICE_READ,
            _P.INVOICE_MANAGE,
            _P.PAYMENT_READ,
            _P.PAYMENT_RECORD_MANUAL,
            _P.PAYMENT_MATCH_SUSPENSE,
            _P.REPORT_READ,
        }
    ),
    Role.CLASS_TEACHER: frozenset(
        {
            _P.STUDENT_READ,
            _P.MARK_READ,
            _P.MARK_ENTER,
            _P.ATTENDANCE_TAKE,
            _P.ASSIGNMENT_MANAGE,
            _P.REPORT_READ,
        }
    ),
    Role.SUBJECT_TEACHER: frozenset(
        {_P.STUDENT_READ, _P.MARK_READ, _P.MARK_ENTER, _P.ASSIGNMENT_MANAGE}
    ),
    Role.GUARDIAN: frozenset({_P.OWN_CHILDREN_READ}),
    Role.STUDENT: frozenset({_P.OWN_RECORD_READ}),
    Role.APPLICANT: frozenset({_P.OWN_APPLICATION_READ}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is asking.

    Built from the token's claims, never from a database row in the middle of a
    request: a principal that needs a query to assemble is a principal that
    gets assembled differently in two places.
    """

    user_id: UUID
    school_id: UUID | None
    role: Role
    student_id: UUID | None = None
    guardian_id: UUID | None = None
    #: Class groups this person is the class teacher of, or teaches a subject
    #: in. Empty for everyone who is not a teacher.
    class_group_ids: frozenset[UUID] = field(default_factory=frozenset)

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES


def permissions_of(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def can(principal: Principal, permission: Permission) -> bool:
    return permission in permissions_of(principal.role)


def may_see_student(principal: Principal, *, student_id: UUID, class_group_id: UUID) -> bool:
    """Reading a child's record: the narrowest and most-asked question here.

    A teacher is scoped to their own classes rather than the whole school. A
    Standard 2 teacher has no business reading a Form 2 pupil's file, and
    "every member of staff can see everything" is the shape of leak that ends
    up in a newspaper.

    A guardian's link to a child is a row in the database, so this cannot
    answer for a guardian from claims alone — the caller checks the link.
    """
    if principal.role in {Role.SCHOOL_ADMIN, Role.HEAD_OF_ACADEMICS, Role.BURSAR}:
        return True
    if principal.role in {Role.CLASS_TEACHER, Role.SUBJECT_TEACHER}:
        return class_group_id in principal.class_group_ids
    if principal.role is Role.STUDENT:
        return principal.student_id == student_id
    return False
