"""v1 router: every endpoint hangs off here."""

from fastapi import APIRouter

from app.api.v1 import academic, auth, enrolments, guardians, schools, students

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(schools.router, prefix="/schools", tags=["schools"])
api_router.include_router(academic.router, prefix="/academic", tags=["academic"])
api_router.include_router(students.router, prefix="/students", tags=["students"])
api_router.include_router(enrolments.router, prefix="/enrolments", tags=["students"])
api_router.include_router(guardians.router, prefix="/guardians", tags=["guardians"])

# Each lands with its phase:
#   1.5  admissions
#   1.6  fees, payments, webhooks
#   2    timetable, assignments, promotion, reports
