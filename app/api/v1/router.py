"""v1 router: every endpoint hangs off here."""

from fastapi import APIRouter

from app.api.v1 import auth

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# Each lands with its phase:
#   1.3  schools, academic
#   1.4  students, enrolments, guardians
#   1.5  admissions
#   1.6  fees, payments, webhooks
#   2    timetable, assignments, promotion, reports
