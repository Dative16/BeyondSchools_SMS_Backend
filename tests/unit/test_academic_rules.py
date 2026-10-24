"""The calendar and the ladder, tested without a database.

The ladder tests carry most of the weight. December 2027 moves two primary
cohorts out of every school in the country at once, and a promotion engine that
has only ever been run against a single-exit ladder is a promotion engine
nobody has tested. Every case below is one that year produces.
"""

from datetime import date
from uuid import uuid4

from app.domain.academic_year import (
    LadderOutcome,
    Level,
    TermDraft,
    YearDraft,
    describe_exit_cohorts,
    ladder_problems,
    next_level,
    next_term,
    term_for,
    validate_terms,
    validate_year,
)
from app.domain.enrolment import LevelStage

YEAR = YearDraft(label="2027", starts_on=date(2027, 1, 8), ends_on=date(2027, 12, 4))

TERMS = [
    TermDraft(1, date(2027, 1, 8), date(2027, 4, 2), "Term 1"),
    TermDraft(2, date(2027, 4, 19), date(2027, 8, 6), "Term 2"),
    TermDraft(3, date(2027, 8, 23), date(2027, 12, 4), "Term 3"),
]


def level(code: str, order: int, **overrides) -> Level:
    defaults = {
        "level_id": uuid4(),
        "code": code,
        "name": code.replace("STD", "Standard "),
        "stage": LevelStage.PRIMARY,
        "order_index": order,
        "next_level_id": None,
        "is_active": True,
    }
    return Level(**{**defaults, **overrides})


def primary_ladder() -> list[Level]:
    """Standard 1 to Standard 7, the shape every primary school has in 2027."""
    return [level(f"STD{n}", n) for n in range(1, 8)]


# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------
def test_a_sensible_year_has_no_problems() -> None:
    assert validate_year(YEAR) == ()


def test_a_year_must_end_after_it_starts() -> None:
    backwards = YearDraft("2027", date(2027, 12, 4), date(2027, 1, 8))

    assert "end after it starts" in " ".join(validate_year(backwards))


def test_an_eighteen_month_year_is_treated_as_a_typo() -> None:
    """Not merely odd: every "current year" assumption downstream would quietly
    stretch with it."""
    stretched = YearDraft("2027", date(2027, 1, 8), date(2028, 11, 4))

    assert "typo" in " ".join(validate_year(stretched))


def test_three_ordinary_terms_are_accepted() -> None:
    assert validate_terms(TERMS, year=YEAR) == ()


def test_overlapping_terms_are_refused() -> None:
    """The one that matters. A mark, an invoice or a register in the shared week
    could belong to either term, and nothing downstream can tell which."""
    overlapping = [
        TERMS[0],
        TermDraft(2, date(2027, 3, 20), date(2027, 8, 6)),
        TERMS[2],
    ]

    assert "overlap" in " ".join(validate_terms(overlapping, year=YEAR))


def test_a_term_outside_the_year_is_refused() -> None:
    straying = [*TERMS[:2], TermDraft(3, date(2027, 8, 23), date(2028, 2, 1))]

    assert "outside the academic year" in " ".join(validate_terms(straying, year=YEAR))


def test_term_numbers_cannot_skip() -> None:
    gapped = [TERMS[0], TermDraft(3, date(2027, 4, 19), date(2027, 8, 6))]

    assert "numbered 1, 2, 3" in " ".join(validate_terms(gapped, year=YEAR))


def test_term_numbers_have_to_follow_the_dates() -> None:
    """Term 2 starting before Term 1 would make "next term" a question with two
    answers."""
    muddled = [
        TermDraft(1, date(2027, 4, 19), date(2027, 8, 6)),
        TermDraft(2, date(2027, 1, 8), date(2027, 4, 2)),
    ]

    assert "do not follow the term dates" in " ".join(validate_terms(muddled, year=YEAR))


def test_a_year_needs_at_least_one_term() -> None:
    assert validate_terms([], year=YEAR) == ("a year needs at least one term",)


def test_a_date_resolves_to_its_term_and_the_holidays_resolve_to_nothing() -> None:
    assert term_for(date(2027, 2, 14), TERMS) == TERMS[0]
    assert term_for(date(2027, 4, 10), TERMS) is None, "the Easter holidays"
    assert term_for(date(2027, 12, 25), TERMS) is None


def test_the_next_term_is_found_by_date() -> None:
    assert next_term(TERMS[0], TERMS) == TERMS[1]
    assert next_term(TERMS[2], TERMS) is None


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------
def test_the_ordinary_case_is_the_level_above() -> None:
    levels = primary_ladder()
    std4 = levels[3]

    step = next_level(level=std4, levels=levels, exit_level_ids=frozenset())

    assert step.outcome is LadderOutcome.PROMOTE
    assert step.next_level_id == levels[4].level_id


def test_an_exit_level_graduates_even_though_a_level_sits_above_it() -> None:
    """The 2027 case, and the reason exit levels are rows on the academic year.

    Standard 6 has a perfectly good next rung. Checking the ladder first would
    promote a cohort that is supposed to be sitting the PSLE.
    """
    levels = primary_ladder()
    std6, std7 = levels[5], levels[6]

    step = next_level(
        level=std6, levels=levels, exit_level_ids=frozenset({std6.level_id, std7.level_id})
    )

    assert step.outcome is LadderOutcome.GRADUATE
    assert step.next_level_id is None
    assert "exit level" in step.reason


def test_both_2027_cohorts_graduate_and_everybody_below_still_promotes() -> None:
    levels = primary_ladder()
    exits = frozenset({levels[5].level_id, levels[6].level_id})

    outcomes = {
        lvl.code: next_level(level=lvl, levels=levels, exit_level_ids=exits).outcome
        for lvl in levels
    }

    assert outcomes["STD5"] is LadderOutcome.PROMOTE
    assert outcomes["STD6"] is LadderOutcome.GRADUATE
    assert outcomes["STD7"] is LadderOutcome.GRADUATE


def test_an_explicit_override_beats_the_order_index() -> None:
    """For a school whose ladder is not a straight line — a Form 1 that splits
    into a general and a vocational stream, for instance."""
    levels = primary_ladder()
    target = levels[4]
    std2 = level("STD2", 2, next_level_id=target.level_id)
    levels[1] = std2

    step = next_level(level=std2, levels=levels, exit_level_ids=frozenset())

    assert step.next_level_id == target.level_id


def test_an_override_pointing_at_a_deleted_level_is_undefined_not_a_guess() -> None:
    levels = primary_ladder()
    dangling = level("STD2", 2, next_level_id=uuid4())
    levels[1] = dangling

    step = next_level(level=dangling, levels=levels, exit_level_ids=frozenset())

    assert step.outcome is LadderOutcome.UNDEFINED


def test_the_top_of_the_ladder_without_an_exit_level_is_undefined() -> None:
    """Rather than silently graduating them: a school that forgot to record the
    cohort should be told, not guessed at."""
    levels = primary_ladder()

    step = next_level(level=levels[6], levels=levels, exit_level_ids=frozenset())

    assert step.outcome is LadderOutcome.UNDEFINED
    assert "not marked as an exit level" in step.reason


def test_an_inactive_level_is_skipped() -> None:
    levels = primary_ladder()
    levels[4] = level("STD5", 5, is_active=False)

    step = next_level(level=levels[3], levels=levels, exit_level_ids=frozenset())

    assert step.next_level_id == levels[5].level_id


# --------------------------------------------------------------------------
# What is wrong with a ladder
# --------------------------------------------------------------------------
def test_a_well_formed_ladder_has_no_problems() -> None:
    levels = primary_ladder()

    assert ladder_problems(levels, frozenset({levels[6].level_id})) == ()


def test_two_levels_sharing_an_order_is_reported() -> None:
    levels = primary_ladder()
    levels.append(level("STD4B", 4))

    problems = ladder_problems(levels, frozenset({levels[6].level_id}))

    assert any("share order 4" in p for p in problems)


def test_an_override_pointing_downwards_is_reported() -> None:
    """It would move children sideways or backwards, round the school forever."""
    levels = primary_ladder()
    levels[4] = level("STD5", 5, next_level_id=levels[1].level_id)

    problems = ladder_problems(levels, frozenset({levels[6].level_id}))

    assert any("not above it" in p for p in problems)


def test_a_missing_exit_level_is_reported() -> None:
    levels = primary_ladder()

    problems = ladder_problems(levels, frozenset())

    assert any("nowhere to go" in p for p in problems)


def test_the_summary_says_plainly_what_is_leaving() -> None:
    levels = primary_ladder()
    std6, std7 = levels[5], levels[6]

    assert "No cohort" in describe_exit_cohorts(levels, frozenset())
    assert describe_exit_cohorts(levels, frozenset({std7.level_id})).startswith("Standard 7")

    both = describe_exit_cohorts(levels, frozenset({std6.level_id, std7.level_id}))
    assert "both leave" in both
    assert "transition" in both, "two exits is right in 2027 and should not read as a fault"
