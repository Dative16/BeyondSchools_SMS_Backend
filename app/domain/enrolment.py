"""Enrolment vocabulary and rules. Pure.

The distinction this module exists to protect: a student is a person, an
enrolment is that person in one class for one academic year. A school that
stores the current class on the student record cannot answer "what was her
Standard 4 report card" two years later, and every school eventually asks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID


class LevelStage(StrEnum):
    """The four stages of the 1+6+4+2/3 structure the country is moving to."""

    PRE_PRIMARY = "pre_primary"
    PRIMARY = "primary"
    LOWER_SECONDARY = "lower_secondary"
    UPPER_SECONDARY = "upper_secondary"


class StudentStatus(StrEnum):
    PROSPECTIVE = "prospective"
    ACTIVE = "active"
    ALUMNUS = "alumnus"
    TRANSFERRED_OUT = "transferred_out"
    WITHDRAWN = "withdrawn"
    DECEASED = "deceased"


class EnrolmentOutcome(StrEnum):
    """What happened to one child in one year.

    ACTIVE is the only one that means "still here". The database enforces at
    most one active enrolment per student with a partial unique index, which is
    cheaper than any amount of application-level care.
    """

    ACTIVE = "active"
    PROMOTED = "promoted"
    REPEATED = "repeated"
    GRADUATED = "graduated"
    TRANSFERRED_OUT = "transferred_out"
    WITHDRAWN = "withdrawn"


TERMINAL_OUTCOMES = frozenset(
    {
        EnrolmentOutcome.PROMOTED,
        EnrolmentOutcome.REPEATED,
        EnrolmentOutcome.GRADUATED,
        EnrolmentOutcome.TRANSFERRED_OUT,
        EnrolmentOutcome.WITHDRAWN,
    }
)


@dataclass(frozen=True, slots=True)
class EnrolmentFacts:
    """What the rules need to know about one enrolment."""

    enrolment_id: UUID
    student_id: UUID
    academic_year_id: UUID
    class_group_id: UUID
    outcome: EnrolmentOutcome
    enrolled_on: date


def is_open(enrolment: EnrolmentFacts) -> bool:
    return enrolment.outcome is EnrolmentOutcome.ACTIVE


def admission_number(fmt: str, *, year: int, sequence: int) -> str:
    """Render a school's admission number format.

    Schools write these differently and change their mind, so the format is a
    column on the school rather than a constant here: "MPS/{year}/{sequence:04d}"
    gives MPS/2027/0142. Only those two placeholders are supported, on purpose
    — a format that can reference anything else cannot be validated.
    """
    try:
        return fmt.format(year=year, sequence=sequence)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"admission number format {fmt!r} uses something other than year and sequence"
        ) from exc


def class_group_name(level_name: str, stream_name: str | None) -> str:
    """"Standard 4 Blue", or just "Standard 4" where a level has one stream."""
    return f"{level_name} {stream_name}" if stream_name else level_name


def age_on(date_of_birth: date, as_of: date) -> int:
    """Age in whole years.

    The day is an argument rather than a call to the clock: an admissions
    report run for January must not quietly use today's date.
    """
    years = as_of.year - date_of_birth.year
    if (as_of.month, as_of.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return max(years, 0)
