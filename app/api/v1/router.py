"""v1 router: every endpoint hangs off here."""

from fastapi import APIRouter

api_router = APIRouter()

# from app.api.v1 import (auth, schools, academic, students, enrolments, guardians,
#                         admissions, fees, payments, webhooks, promotion,
#                         assignments, timetable, reports)
# api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
# api_router.include_router(students.router, prefix="/students", tags=["students"])
