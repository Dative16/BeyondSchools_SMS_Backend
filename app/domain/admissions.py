"""The admissions funnel. Pure.

Six states a registrar can act on, and the two rules that decide whether a
registrar has to act at all: whether an application looks like a child who is
already enrolled, and whether it holds enough to be submitted.

The state machine is here rather than in the service because the illegal
transitions are the expensive ones — enrolling an application nobody offered a
place to, or offering twice and issuing two admission numbers — and here they
can be enumerated in a test without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class ApplicationState(StrEnum):
    STARTED = "started"
    SUBMITTED = "submitted"
    SCREENED = "screened"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    ENROLLED = "enrolled"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


#: What may follow what. DECLINED and WITHDRAWN are reachable from anywhere
#: before enrolment and lead nowhere, which is what makes them safe to use as
#: the "stop looking at this" states a queue filters out.
ALLOWED_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.STARTED: frozenset(
        {ApplicationState.SUBMITTED, ApplicationState.WITHDRAWN}
    ),
    ApplicationState.SUBMITTED: frozenset(
        {ApplicationState.SCREENED, ApplicationState.DECLINED, ApplicationState.WITHDRAWN}
    ),
    ApplicationState.SCREENED: frozenset(
        {ApplicationState.OFFERED, ApplicationState.DECLINED, ApplicationState.WITHDRAWN}
    ),
    # Acceptance is a payment arriving, never a button: see can_accept below.
    ApplicationState.OFFERED: frozenset(
        {ApplicationState.ACCEPTED, ApplicationState.DECLINED, ApplicationState.WITHDRAWN}
    ),
    ApplicationState.ACCEPTED: frozenset({ApplicationState.ENROLLED}),
    ApplicationState.ENROLLED: frozenset(),
    ApplicationState.DECLINED: frozenset(),
    ApplicationState.WITHDRAWN: frozenset(),
}

OPEN_STATES = frozenset(
    {
        ApplicationState.STARTED,
        ApplicationState.SUBMITTED,
        ApplicationState.SCREENED,
        ApplicationState.OFFERED,
        ApplicationState.ACCEPTED,
    }
)

#: States a registrar is expected to do something about, which is what the
#: review queue and the dashboard's "applications waiting" tile both count.
NEEDS_REVIEW_STATES = frozenset({ApplicationState.SUBMITTED, ApplicationState.SCREENED})


class TransitionRejected(Exception):
    """An illegal move. The service turns this into a 409; the rule itself does
    not know what a status code is."""


def transition(current: ApplicationState, target: ApplicationState) -> ApplicationState:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise TransitionRejected(f"an application cannot go from {current} to {target}")
    return target


@dataclass(frozen=True, slots=True)
class ApplicationDraft:
    """What the public form has collected so far.

    Every field is optional because the form saves as it goes: a parent on a 3G
    connection who loses the page at question nine must be able to come back by
    OTP and continue, and that only works if a half-filled application is a
    legal row rather than a validation error.
    """

    phone_verified: bool = False
    child_first_name: str | None = None
    child_surname: str | None = None
    date_of_birth: date | None = None
    sex: str | None = None
    level_applied_id: UUID | None = None
    guardian_name: str | None = None
    guardian_phone: str | None = None
    guardian_relationship: str | None = None


#: The shortest honest list. Every extra required field costs completed
#: applications, and a form that takes more than about four minutes on a phone
#: does not get finished — previous school, documents and the rest are
#: collected but never block submission.
REQUIRED_TO_SUBMIT = (
    "child_first_name",
    "child_surname",
    "date_of_birth",
    "sex",
    "level_applied_id",
    "guardian_name",
    "guardian_phone",
    "guardian_relationship",
)


def missing_to_submit(draft: ApplicationDraft) -> tuple[str, ...]:
    """What still has to be filled in before this can leave STARTED."""
    missing = [name for name in REQUIRED_TO_SUBMIT if getattr(draft, name) in (None, "")]
    if not draft.phone_verified:
        missing.insert(0, "phone_verified")
    return tuple(missing)


def can_submit(draft: ApplicationDraft) -> bool:
    return not missing_to_submit(draft)


@dataclass(frozen=True, slots=True)
class CandidateStudent:
    """An already-known child an application might be a duplicate of."""

    student_id: UUID
    first_name: str
    surname: str
    date_of_birth: date | None
    guardian_phones: frozenset[str]


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    student_id: UUID
    score: int
    reason: str


def _normalise_name(value: str) -> str:
    return " ".join(value.strip().lower().split())


def normalise_phone(raw: str) -> str:
    """Tanzanian mobile numbers, one way round.

    Parents write 0754118902, +255754118902 and 255754118902 for the same
    handset. Duplicate screening that compares the raw strings finds nothing,
    so everything is stored and compared as 0754118902.
    """
    digits = "".join(c for c in raw if c.isdigit())
    if digits.startswith("255"):
        digits = "0" + digits[3:]
    if len(digits) == 9 and not digits.startswith("0"):
        digits = "0" + digits
    return digits


def duplicate_score(
    *,
    first_name: str,
    surname: str,
    date_of_birth: date | None,
    guardian_phone: str | None,
    candidate: CandidateStudent,
) -> DuplicateMatch | None:
    """How likely is this application the child in `candidate`?

    Name plus date of birth plus guardian phone, scored rather than matched, so
    that a registrar sees "possible duplicate" and decides. Two things this
    deliberately does not do: it does not auto-merge at any score, and it does
    not treat a shared guardian phone on its own as a duplicate, because the
    commonest case by far is a sibling — one phone, several children, all of
    them real.
    """
    score = 0
    reasons: list[str] = []

    same_surname = _normalise_name(surname) == _normalise_name(candidate.surname)
    same_first = _normalise_name(first_name) == _normalise_name(candidate.first_name)

    if same_first and same_surname:
        score += 50
        reasons.append("same name")
    elif same_surname:
        score += 15
        reasons.append("same surname")

    if date_of_birth is not None and date_of_birth == candidate.date_of_birth:
        score += 35
        reasons.append("same date of birth")

    if guardian_phone and normalise_phone(guardian_phone) in candidate.guardian_phones:
        score += 20
        reasons.append("same guardian phone")

    if score == 0:
        return None

    return DuplicateMatch(candidate.student_id, score, ", ".join(reasons))


#: At or above this, the application is flagged and a registrar must accept or
#: merge it before an offer can be made. Below it, the match is recorded as a
#: note and nothing is blocked.
DUPLICATE_FLAG_THRESHOLD = 70


def likely_duplicates(matches: list[DuplicateMatch]) -> list[DuplicateMatch]:
    flagged = [m for m in matches if m.score >= DUPLICATE_FLAG_THRESHOLD]
    return sorted(flagged, key=lambda m: m.score, reverse=True)


def can_accept(*, state: ApplicationState, deposit_settled: bool) -> bool:
    """Acceptance is a deposit arriving, not a button a registrar presses.

    Payment is the signal that an application has become a pupil, so there is
    no accept button to press by mistake and no way for an unpaid place to look
    accepted in the funnel.
    """
    return state is ApplicationState.OFFERED and deposit_settled


# --------------------------------------------------------------------------
# The offer
# --------------------------------------------------------------------------
#: How long a place is held. Long enough for a family to find the deposit,
#: short enough that a school can plan its classes — and the reason the offer
#: carries a date at all is that an offer nobody ever declines is a seat
#: nobody can fill.
OFFER_VALID_DAYS = 21


def offer_expiry(*, offered_on: date, days: int = OFFER_VALID_DAYS) -> date:
    return offered_on + timedelta(days=days)


def offer_has_lapsed(*, expires_on: date | None, as_of: date) -> bool:
    """A lapsed offer is not automatically withdrawn.

    Deliberately: a deposit arriving a day late is a pupil, not an error, and
    a registrar who can see "expired three days ago" will take the money. This
    answers a question for a screen, not for a scheduled job.
    """
    return expires_on is not None and expires_on < as_of


@dataclass(frozen=True, slots=True)
class OfferTerms:
    """What a family is being asked to pay to hold the place."""

    admission_no: str
    deposit_due: Decimal
    expires_on: date


# --------------------------------------------------------------------------
# Becoming a pupil
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EnrolmentReadiness:
    """Whether an accepted application can become a child on the roll."""

    ready: bool
    problems: tuple[str, ...]


def readiness_to_enrol(
    *,
    state: ApplicationState,
    has_level: bool,
    has_class_group: bool,
    has_guardian_details: bool,
    admission_no: str | None,
) -> EnrolmentReadiness:
    """The last gate before a student record exists.

    Checked here rather than left to the service's sequence, because this is
    the step that creates a person: a child, a guardian, an enrolment and a
    login, in one transaction. Getting halfway through it is the one failure
    that leaves a school with a pupil nobody can find.
    """
    problems: list[str] = []

    if state is not ApplicationState.ACCEPTED:
        problems.append(
            "only an accepted application becomes an enrolment, and acceptance "
            "is the deposit arriving"
        )
    if not admission_no:
        problems.append("no admission number was issued")
    if not has_level:
        problems.append("no level was applied for")
    if not has_class_group:
        problems.append("no class has been chosen for this child")
    if not has_guardian_details:
        problems.append("the guardian's name and phone are needed")

    return EnrolmentReadiness(ready=not problems, problems=tuple(problems))


def reference(*, year: int, sequence: int, prefix: str = "APP") -> str:
    """Short and sayable over the phone: APP-2027-0043.

    A parent ringing the school says this; a UUID does not survive being read
    aloud twice.
    """
    return f"{prefix}-{year}-{sequence:04d}"
