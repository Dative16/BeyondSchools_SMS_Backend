"""The admissions funnel's rules, tested without a database.

Two of these carry most of the weight. The state machine, because the illegal
moves are the expensive ones — enrolling a child nobody offered a place to, or
offering twice and burning two admission numbers. And the duplicate scoring,
because the commonest high score is a sibling, and a system that treated every
shared phone number as a duplicate would have a registrar merging two real
children.
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.admissions import (
    DUPLICATE_FLAG_THRESHOLD,
    NEEDS_REVIEW_STATES,
    OPEN_STATES,
    ApplicationDraft,
    ApplicationState,
    CandidateStudent,
    TransitionRejected,
    can_accept,
    can_submit,
    duplicate_score,
    likely_duplicates,
    missing_to_submit,
    normalise_phone,
    offer_expiry,
    offer_has_lapsed,
    readiness_to_enrol,
    reference,
    transition,
)

TODAY = date(2026, 11, 20)


def complete_draft(**overrides) -> ApplicationDraft:
    defaults = {
        "phone_verified": True,
        "child_first_name": "Happiness",
        "child_surname": "Mollel",
        "date_of_birth": date(2020, 11, 30),
        "sex": "f",
        "level_applied_id": uuid4(),
        "guardian_name": "Neema Mollel",
        "guardian_phone": "0754118902",
        "guardian_relationship": "mother",
    }
    return ApplicationDraft(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------
def test_the_ordinary_path_through_the_funnel() -> None:
    state = ApplicationState.STARTED
    for target in (
        ApplicationState.SUBMITTED,
        ApplicationState.SCREENED,
        ApplicationState.OFFERED,
        ApplicationState.ACCEPTED,
        ApplicationState.ENROLLED,
    ):
        state = transition(state, target)

    assert state is ApplicationState.ENROLLED


def test_a_place_cannot_be_offered_before_anybody_has_looked() -> None:
    """Offering burns an admission number, and the school should be able to
    say who decided the child was new."""
    with pytest.raises(TransitionRejected, match="submitted to offered"):
        transition(ApplicationState.SUBMITTED, ApplicationState.OFFERED)


def test_a_child_cannot_be_enrolled_without_being_accepted() -> None:
    """Acceptance is the deposit arriving, so this is what stops a pupil
    joining the roll without one."""
    with pytest.raises(TransitionRejected):
        transition(ApplicationState.OFFERED, ApplicationState.ENROLLED)


def test_an_enrolled_application_is_finished() -> None:
    for target in ApplicationState:
        with pytest.raises(TransitionRejected):
            transition(ApplicationState.ENROLLED, target)


def test_a_declined_application_does_not_come_back() -> None:
    with pytest.raises(TransitionRejected):
        transition(ApplicationState.DECLINED, ApplicationState.SCREENED)


def test_anything_open_can_be_declined_or_withdrawn() -> None:
    for state in (
        ApplicationState.SUBMITTED,
        ApplicationState.SCREENED,
        ApplicationState.OFFERED,
    ):
        assert transition(state, ApplicationState.DECLINED) is ApplicationState.DECLINED
        assert transition(state, ApplicationState.WITHDRAWN) is ApplicationState.WITHDRAWN


def test_the_queue_shows_what_somebody_has_to_act_on() -> None:
    assert frozenset(
        {ApplicationState.SUBMITTED, ApplicationState.SCREENED}
    ) == NEEDS_REVIEW_STATES
    assert ApplicationState.ENROLLED not in OPEN_STATES
    assert ApplicationState.DECLINED not in OPEN_STATES


# --------------------------------------------------------------------------
# What a four-minute form must collect
# --------------------------------------------------------------------------
def test_a_complete_draft_can_be_submitted() -> None:
    assert can_submit(complete_draft())
    assert missing_to_submit(complete_draft()) == ()


def test_an_unverified_phone_is_named_first() -> None:
    """It is the credential the whole form hangs off."""
    missing = missing_to_submit(complete_draft(phone_verified=False))

    assert missing[0] == "phone_verified"


def test_the_required_list_is_short_on_purpose() -> None:
    """Every extra required field costs completed applications. Documents, a
    previous school and the school's own questions are collected but never
    block submission."""
    assert len(missing_to_submit(ApplicationDraft())) == 9


def test_an_empty_string_counts_as_missing() -> None:
    """A form that posts "" for an untouched field must not pass."""
    assert "child_surname" in missing_to_submit(complete_draft(child_surname=""))


# --------------------------------------------------------------------------
# Phone numbers
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "written",
    ["0754118902", "+255754118902", "255754118902", "754118902"],
)
def test_every_way_a_parent_writes_a_number_normalises_to_one(written: str) -> None:
    """Matching on the raw string would give one family three guardian
    records, three fee reminders and no combined balance."""
    assert normalise_phone(written) == "0754118902"


# --------------------------------------------------------------------------
# Duplicate screening
# --------------------------------------------------------------------------
def candidate(**overrides) -> CandidateStudent:
    defaults = {
        "student_id": uuid4(),
        "first_name": "Happiness",
        "surname": "Mollel",
        "date_of_birth": date(2020, 11, 30),
        "guardian_phones": frozenset({"0754118902"}),
    }
    return CandidateStudent(**{**defaults, **overrides})


def test_the_same_child_scores_high_enough_to_flag() -> None:
    match = duplicate_score(
        first_name="Happiness",
        surname="Mollel",
        date_of_birth=date(2020, 11, 30),
        guardian_phone="0754118902",
        candidate=candidate(),
    )

    assert match is not None
    assert match.score >= DUPLICATE_FLAG_THRESHOLD
    assert likely_duplicates([match]) == [match]


def test_a_sibling_is_not_a_duplicate() -> None:
    """The commonest high score in a real school: one phone, several children,
    all of them real. A shared surname and phone must not be enough on its
    own, or a registrar ends up merging two children."""
    match = duplicate_score(
        first_name="Baraka",
        surname="Mollel",
        date_of_birth=date(2018, 2, 4),
        guardian_phone="0754118902",
        candidate=candidate(),
    )

    assert match is not None
    assert match.score < DUPLICATE_FLAG_THRESHOLD
    assert likely_duplicates([match]) == []


def test_the_same_name_and_birthday_flags_even_with_a_different_phone() -> None:
    """A family that changed numbers, or a second parent applying."""
    match = duplicate_score(
        first_name="Happiness",
        surname="Mollel",
        date_of_birth=date(2020, 11, 30),
        guardian_phone="0788000111",
        candidate=candidate(),
    )

    assert match is not None
    assert match.score >= DUPLICATE_FLAG_THRESHOLD


def test_a_phone_written_differently_still_matches() -> None:
    match = duplicate_score(
        first_name="Happiness",
        surname="Mollel",
        date_of_birth=None,
        guardian_phone="+255754118902",
        candidate=candidate(),
    )

    assert match is not None
    assert "same guardian phone" in match.reason


def test_an_unrelated_child_scores_nothing() -> None:
    assert (
        duplicate_score(
            first_name="Amani",
            surname="Kimaro",
            date_of_birth=date(2019, 5, 5),
            guardian_phone="0788000111",
            candidate=candidate(),
        )
        is None
    )


def test_flagged_matches_come_back_worst_first() -> None:
    weak = duplicate_score(
        first_name="Happiness",
        surname="Mollel",
        date_of_birth=date(2020, 11, 30),
        guardian_phone=None,
        candidate=candidate(guardian_phones=frozenset()),
    )
    strong = duplicate_score(
        first_name="Happiness",
        surname="Mollel",
        date_of_birth=date(2020, 11, 30),
        guardian_phone="0754118902",
        candidate=candidate(),
    )

    ranked = likely_duplicates([weak, strong])

    assert ranked[0] is strong


# --------------------------------------------------------------------------
# Acceptance is a payment
# --------------------------------------------------------------------------
def test_only_a_paid_offer_is_accepted() -> None:
    """There is no accept button. Payment is the signal, which is why an
    unpaid place cannot look accepted in the funnel."""
    assert can_accept(state=ApplicationState.OFFERED, deposit_settled=True)
    assert not can_accept(state=ApplicationState.OFFERED, deposit_settled=False)


def test_a_payment_against_anything_but_an_offer_accepts_nothing() -> None:
    for state in (
        ApplicationState.SUBMITTED,
        ApplicationState.SCREENED,
        ApplicationState.ACCEPTED,
        ApplicationState.DECLINED,
    ):
        assert not can_accept(state=state, deposit_settled=True)


# --------------------------------------------------------------------------
# The offer
# --------------------------------------------------------------------------
def test_an_offer_is_held_for_three_weeks() -> None:
    assert offer_expiry(offered_on=date(2027, 1, 8)) == date(2027, 1, 29)


def test_a_lapsed_offer_is_reported_not_withdrawn() -> None:
    """A deposit arriving a day late is a pupil, not an error. This answers a
    question for a screen, not for a scheduled job."""
    assert offer_has_lapsed(expires_on=date(2026, 11, 19), as_of=TODAY)
    assert not offer_has_lapsed(expires_on=date(2026, 11, 20), as_of=TODAY)
    assert not offer_has_lapsed(expires_on=None, as_of=TODAY)


def test_a_reference_survives_being_read_aloud() -> None:
    assert reference(year=2027, sequence=43) == "APP-2027-0043"


# --------------------------------------------------------------------------
# The last gate
# --------------------------------------------------------------------------
def test_a_ready_application_can_become_a_pupil() -> None:
    readiness = readiness_to_enrol(
        state=ApplicationState.ACCEPTED,
        has_level=True,
        has_class_group=True,
        has_guardian_details=True,
        admission_no="MPS/2027/0471",
    )

    assert readiness.ready
    assert readiness.problems == ()


def test_every_missing_piece_is_named_at_once() -> None:
    """One round trip rather than four: this is the step that creates a child,
    a guardian, an enrolment and a login together, and a registrar should see
    the whole list."""
    readiness = readiness_to_enrol(
        state=ApplicationState.SUBMITTED,
        has_level=False,
        has_class_group=False,
        has_guardian_details=False,
        admission_no=None,
    )

    assert not readiness.ready
    assert len(readiness.problems) == 5


def test_an_unpaid_application_names_the_deposit_as_the_reason() -> None:
    readiness = readiness_to_enrol(
        state=ApplicationState.OFFERED,
        has_level=True,
        has_class_group=True,
        has_guardian_details=True,
        admission_no="MPS/2027/0471",
    )

    assert "the deposit arriving" in readiness.problems[0]


# --------------------------------------------------------------------------
# What the deposit comes to
# --------------------------------------------------------------------------
def test_the_deposit_is_the_joining_charges_plus_a_share_of_the_term() -> None:
    """A school that asks for the whole term up front loses places it would
    have filled; one that asks for nothing fills places that never pay."""
    from app.domain.invoicing import DraftLine, InvoiceLineKind, deposit_due

    term = [DraftLine("Tuition, Term 1", Decimal("300000"), InvoiceLineKind.CHARGE)]
    joining = [
        DraftLine("Admission fee", Decimal("100000"), InvoiceLineKind.CHARGE),
        DraftLine("Uniform set", Decimal("75000"), InvoiceLineKind.CHARGE),
    ]

    assert deposit_due(term, joining, percent=30) == Decimal("265000.00")
    assert deposit_due(term, joining, percent=0) == Decimal("175000.00")
    assert deposit_due(term, joining, percent=100) == Decimal("475000.00")

    with pytest.raises(ValueError, match="between 0 and 100"):
        deposit_due(term, joining, percent=140)
