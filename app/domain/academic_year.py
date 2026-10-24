"""The calendar and the ladder. Pure.

Two things live here, and both are data rather than code on purpose.

The **calendar** is a year with terms inside it. Invoicing and reporting both
pivot on "the current term", so the rules that decide whether a set of terms is
coherent — inside the year, in order, not overlapping — are worth getting right
once, here, where every case can be enumerated in a test.

The **ladder** is what the promotion engine will ask: given this level, what
comes next? Tanzania is halfway through shortening primary from seven years to
six, so in 2027 one school holds two exit cohorts at once — the pupils who were
in Standard Six in 2026 sit the PSLE from Standard Seven, and the pupils who
were in Standard Five sit the same examination from Standard Six. Code that
assumes Standard 7 is the exit level is wrong that year. So the answer comes
from `order_index`, an optional explicit override, and the exit levels recorded
against the academic year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from app.domain.enrolment import LevelStage

# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------
#: Tanzanian schools bill and report per term. Three is the norm; four is
#: allowed because a few schools split the long term and the database's check
#: constraint has to agree with something.
MAX_TERMS_PER_YEAR = 4


@dataclass(frozen=True, slots=True)
class TermDraft:
    """A term as it is being proposed, before anything is written."""

    index: int
    starts_on: date
    ends_on: date
    label: str = ""


@dataclass(frozen=True, slots=True)
class YearDraft:
    label: str
    starts_on: date
    ends_on: date


def validate_year(year: YearDraft) -> tuple[str, ...]:
    """What is wrong with a proposed academic year, if anything."""
    problems: list[str] = []
    if year.ends_on <= year.starts_on:
        problems.append("the year has to end after it starts")
    if (year.ends_on - year.starts_on).days > 500:
        # A year that spans 18 months is a typo, and it would quietly break
        # every "current year" assumption downstream.
        problems.append("an academic year longer than 500 days is probably a typo")
    if not year.label.strip():
        problems.append("the year needs a label, such as 2027")
    return tuple(problems)


def validate_terms(terms: list[TermDraft], *, year: YearDraft) -> tuple[str, ...]:
    """Whether a set of terms fits inside its year and makes sense together.

    The overlap check is the one that matters. Two terms covering the same
    week means a mark, an invoice or a register can belong to either, and
    nothing downstream can tell which.
    """
    problems: list[str] = []

    if not terms:
        return ("a year needs at least one term",)
    if len(terms) > MAX_TERMS_PER_YEAR:
        problems.append(f"a year cannot hold more than {MAX_TERMS_PER_YEAR} terms")

    indexes = [t.index for t in terms]
    if len(set(indexes)) != len(indexes):
        problems.append("two terms share the same number")
    if sorted(indexes) != list(range(1, len(terms) + 1)):
        problems.append("terms have to be numbered 1, 2, 3 with nothing missing")

    for term in terms:
        if term.ends_on <= term.starts_on:
            problems.append(f"term {term.index} has to end after it starts")
        if term.starts_on < year.starts_on or term.ends_on > year.ends_on:
            problems.append(f"term {term.index} falls outside the academic year")

    in_order = sorted(terms, key=lambda t: t.starts_on)
    for earlier, later in zip(in_order, in_order[1:], strict=False):
        if later.starts_on <= earlier.ends_on:
            problems.append(
                f"terms {earlier.index} and {later.index} overlap"
            )
    if [t.index for t in in_order] != sorted(indexes):
        problems.append("term numbers do not follow the term dates")

    return tuple(dict.fromkeys(problems))


def term_for(day: date, terms: list[TermDraft]) -> TermDraft | None:
    """Which term a date falls in, or None for the holidays.

    The day is an argument. A statement produced for last month has to be read
    against last month, and a report run in January for December must not
    silently use today.
    """
    for term in terms:
        if term.starts_on <= day <= term.ends_on:
            return term
    return None


def next_term(current: TermDraft, terms: list[TermDraft]) -> TermDraft | None:
    """The term after this one, by date rather than by number."""
    later = sorted(
        (t for t in terms if t.starts_on > current.starts_on), key=lambda t: t.starts_on
    )
    return later[0] if later else None


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Level:
    """A rung, as the rules need to see it."""

    level_id: UUID
    code: str
    name: str
    stage: LevelStage
    order_index: int
    #: An explicit override for schools whose ladder is not a straight line.
    next_level_id: UUID | None = None
    is_active: bool = True


class LadderOutcome(StrEnum):
    PROMOTE = "promote"
    #: The child has finished with this school: they sit an examination and
    #: leave. Which levels these are is a property of the *year*, not the
    #: level, which is the whole point of this module.
    GRADUATE = "graduate"
    #: The ladder runs out with no exit level recorded. A configuration
    #: mistake rather than an outcome, and one worth refusing to guess at.
    UNDEFINED = "undefined"


@dataclass(frozen=True, slots=True)
class LadderStep:
    outcome: LadderOutcome
    next_level_id: UUID | None
    reason: str


def next_level(
    *,
    level: Level,
    levels: list[Level],
    exit_level_ids: frozenset[UUID],
) -> LadderStep:
    """What follows this level for a child being promoted this year.

    Order of precedence, and each step exists because of a real case:

    1. **An exit level wins.** In 2027 both Standard 6 and Standard 7 are exit
       levels at the same school, and Standard 6 still has a perfectly good
       next rung. Checking the ladder first would promote a cohort that is
       supposed to be sitting the PSLE.
    2. **An explicit override next.** A school whose Form 1 splits into a
       general and a vocational stream sets it by hand.
    3. **Then `order_index`.** The ordinary case: the next active level up.
    """
    if level.level_id in exit_level_ids:
        return LadderStep(
            LadderOutcome.GRADUATE,
            None,
            f"{level.name} is an exit level for this academic year",
        )

    if level.next_level_id is not None:
        target = next(
            (c for c in levels if c.level_id == level.next_level_id),
            None,
        )
        if target is None:
            return LadderStep(
                LadderOutcome.UNDEFINED,
                None,
                f"{level.name} points at a level that no longer exists",
            )
        return LadderStep(
            LadderOutcome.PROMOTE, target.level_id, f"{level.name} is set to feed {target.name}"
        )

    higher = sorted(
        (
            candidate
            for candidate in levels
            if candidate.order_index > level.order_index and candidate.is_active
        ),
        key=lambda candidate: candidate.order_index,
    )
    if not higher:
        return LadderStep(
            LadderOutcome.UNDEFINED,
            None,
            f"{level.name} is the top of the ladder but is not marked as an exit level",
        )
    return LadderStep(
        LadderOutcome.PROMOTE, higher[0].level_id, f"the level above {level.name}"
    )


def ladder_problems(levels: list[Level], exit_level_ids: frozenset[UUID]) -> tuple[str, ...]:
    """Everything about a school's ladder that will bite at promotion time.

    Run when a level is saved and shown in the academic settings screen, so
    that the December run is not the first time anybody finds out. Each of
    these has a December consequence: a duplicate order index makes "the level
    above" ambiguous, a missing exit level strands a cohort, and an override
    pointing downwards loops a child round the school forever.
    """
    problems: list[str] = []
    active = [level for level in levels if level.is_active]

    if not active:
        return ("the school has no active levels",)

    seen: dict[int, str] = {}
    for level in active:
        if level.order_index in seen:
            problems.append(
                f"{level.name} and {seen[level.order_index]} share order {level.order_index}, "
                "so which one comes next is ambiguous"
            )
        seen[level.order_index] = level.name

    by_id = {level.level_id: level for level in levels}
    for level in active:
        if level.next_level_id is None:
            continue
        target = by_id.get(level.next_level_id)
        if target is None:
            problems.append(f"{level.name} points at a level that no longer exists")
        elif target.order_index <= level.order_index:
            problems.append(
                f"{level.name} points at {target.name}, which is not above it — "
                "a promotion run would move children sideways or backwards"
            )

    top = max(active, key=lambda level: level.order_index)
    if top.level_id not in exit_level_ids:
        problems.append(
            f"{top.name} is the top of the ladder and no exit level is recorded for "
            "this year, so its pupils have nowhere to go"
        )

    return tuple(dict.fromkeys(problems))


def describe_exit_cohorts(levels: list[Level], exit_level_ids: frozenset[UUID]) -> str:
    """One line for the academic settings screen.

    Two exit levels in a year is unusual enough to look like a mistake and, in
    2027, is exactly right — so the screen says which cohorts are leaving
    rather than leaving somebody to work it out from two checkboxes.
    """
    leaving = sorted(
        (level for level in levels if level.level_id in exit_level_ids),
        key=lambda level: level.order_index,
    )
    if not leaving:
        return "No cohort is set to leave at the end of this year."
    names = ", ".join(level.name for level in leaving)
    if len(leaving) == 1:
        return f"{names} leaves at the end of this year."
    return (
        f"{names} both leave at the end of this year — "
        "two cohorts exiting together, as the six-year primary transition requires."
    )
