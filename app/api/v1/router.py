"""v1 router: every endpoint hangs off here."""

from fastapi import APIRouter

from app.api.v1 import (
    academic,
    admissions,
    apply,
    auth,
    enrolments,
    fees,
    guardians,
    payments,
    schools,
    students,
)

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(schools.router, prefix="/schools", tags=["schools"])
api_router.include_router(academic.router, prefix="/academic", tags=["academic"])
api_router.include_router(students.router, prefix="/students", tags=["students"])
api_router.include_router(enrolments.router, prefix="/enrolments", tags=["students"])
api_router.include_router(guardians.router, prefix="/guardians", tags=["guardians"])
# The public form comes before the registrar's screens on purpose: it is the
# only part of this API a stranger can reach.
api_router.include_router(apply.router, prefix="/apply", tags=["apply"])
api_router.include_router(admissions.router, prefix="/admissions", tags=["admissions"])
api_router.include_router(fees.router, prefix="/fees", tags=["fees"])
api_router.include_router(payments.router, prefix="/payments", tags=["payments"])

# Not mounted, because they do not exist yet — and the honest account of that
# is in ROADMAP.md rather than in a comment here. In short: attendance, marks,
# report cards, the timetable, homework and the promotion engine have no table,
# no model, no repository and no service between them. That is the whole
# academic half of the product, and it is stage 2 of the roadmap, not a tail on
# the end of this one.
#
#   timetable, assignments, promotion, reports
