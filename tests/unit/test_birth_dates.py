"""Dates of birth that cannot be right.

Not a validation nicety. A date of birth is read by the age-banded fee rules,
the class list, the admissions screen that decides whether a child is old
enough for Standard 1, and every report card for the next seven years. A typed
year is wrong in all of them at once, and nobody looks at the field again.
"""

from datetime import date

import pytest

from app.domain.enrolment import EARLIEST_PLAUSIBLE_BIRTH_YEAR, age_on, date_of_birth_problem

TODAY = date(2027, 1, 8)


def test_an_ordinary_date_passes() -> None:
    assert date_of_birth_problem(date(2018, 6, 8), today=TODAY) is None


def test_today_passes_because_a_newborn_is_not_an_error() -> None:
    """A nursery registering a baby on the day it was born is a real case."""
    assert date_of_birth_problem(TODAY, today=TODAY) is None


def test_tomorrow_does_not() -> None:
    problem = date_of_birth_problem(date(2027, 1, 9), today=TODAY)

    assert problem is not None
    assert "future" in problem


def test_a_year_typed_as_2099_is_refused() -> None:
    """The one this was written for: it was accepted, and produced a child
    whose age came out as zero on every screen that showed it."""
    assert date_of_birth_problem(date(2099, 1, 1), today=TODAY) is not None


def test_a_transposed_year_is_refused() -> None:
    """1091 for 1910, or 2001 typed as 1001."""
    problem = date_of_birth_problem(date(1091, 4, 2), today=TODAY)

    assert problem is not None
    assert str(EARLIEST_PLAUSIBLE_BIRTH_YEAR) in problem


def test_an_adult_learner_is_not_refused() -> None:
    """The bound is a guard against a typed year, not a judgement about who
    may enrol. A 60-year-old on an evening programme is not a typing mistake."""
    assert date_of_birth_problem(date(1962, 3, 14), today=TODAY) is None


@pytest.mark.parametrize(
    ("born", "as_of", "expected"),
    [
        (date(2018, 6, 8), date(2026, 6, 7), 7),
        (date(2018, 6, 8), date(2026, 6, 8), 8),
        (date(2020, 2, 29), date(2027, 2, 28), 6),
        (date(2020, 2, 29), date(2027, 3, 1), 7),
    ],
)
def test_age_counts_completed_years(born: date, as_of: date, expected: int) -> None:
    """The same arithmetic the web app does in `src/domain/student.ts`. The two
    have to agree: a profile that says eight while a report says seven is a
    profile a class teacher stops trusting."""
    assert age_on(born, as_of) == expected
