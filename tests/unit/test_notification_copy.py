"""What the school's messages say, and what they cost to say it.

These assert two things a reviewer cannot eyeball: that a message still fits
one paid segment when the school has a long name, and that the part a parent
actually needs — the admission number — is the part that survives when
something has to be dropped.
"""

from app.domain.notifications import (
    SEGMENT,
    EnrolmentFacts,
    enrolment_confirmed,
    fits_one_segment,
)


def facts(**overrides: str) -> EnrolmentFacts:
    base = {
        "child_first_name": "Hamisi",
        "admission_no": "MW-2027-0041",
        "school_short_name": "Mwenge",
        "portal": "mwenge.beyondschools.co.tz",
    }
    base.update(overrides)
    return EnrolmentFacts(**base)  # type: ignore[arg-type]


class TestSegmentCounting:
    def test_counts_an_ordinary_message_by_its_characters(self):
        assert fits_one_segment("a" * SEGMENT)
        assert not fits_one_segment("a" * (SEGMENT + 1))

    def test_charges_two_septets_for_the_escaped_characters(self):
        # A curly brace costs two. Eighty of them fill a segment exactly.
        assert fits_one_segment("{" * 80)
        assert not fits_one_segment("{" * 81)

    def test_drops_to_seventy_once_a_character_leaves_gsm7(self):
        # The apostrophe people paste out of Word is the usual culprit, and it
        # halves what a message can hold.
        assert fits_one_segment("’" + "a" * 69)
        assert not fits_one_segment("’" + "a" * 70)


class TestEnrolmentConfirmed:
    def test_says_the_child_the_school_and_the_number(self):
        body = enrolment_confirmed(facts())

        assert "Hamisi" in body
        assert "Mwenge" in body
        assert "MW-2027-0041" in body

    def test_tells_the_guardian_where_to_sign_in(self):
        assert "mwenge.beyondschools.co.tz" in enrolment_confirmed(facts())

    def test_costs_one_message(self):
        assert fits_one_segment(enrolment_confirmed(facts()))

    def test_still_costs_one_message_for_a_school_with_a_long_name(self):
        # Real example of the shape: Tanzanian schools are rarely called
        # anything as short as "Mwenge".
        body = enrolment_confirmed(
            facts(
                school_short_name="Mtakuja Lutheran Secondary",
                child_first_name="Upendo",
                admission_no="MTK-2027-00418",
                portal="mtakuja-lutheran.beyondschools.co.tz",
            )
        )

        assert fits_one_segment(body)

    def test_keeps_the_admission_number_when_something_has_to_go(self):
        # The number is what a parent is asked for at the bursar's desk. A
        # parent who cannot find the portal can still walk in with it.
        body = enrolment_confirmed(
            facts(
                school_short_name="A" * 60,
                child_first_name="B" * 40,
                portal="an-extremely-long-school-subdomain.beyondschools.co.tz",
            )
        )

        assert "MW-2027-0041" in body
        assert "beyondschools.co.tz" not in body
        assert fits_one_segment(body)

    def test_never_cuts_a_word_in_half(self):
        body = enrolment_confirmed(facts(school_short_name="C" * 70))

        assert not body.endswith(("-", " "))
        assert body.endswith(".")


class TestLocale:
    def test_writes_kiswahili_when_the_school_asked_for_it(self):
        body = enrolment_confirmed(facts(), locale="sw")

        assert "amesajiliwa" in body
        assert "enrolled" not in body

    def test_writes_english_by_default(self):
        assert "is enrolled at" in enrolment_confirmed(facts())
        assert "is enrolled at" in enrolment_confirmed(facts(), locale="en")

    def test_treats_a_missing_locale_as_english(self):
        assert "is enrolled at" in enrolment_confirmed(facts(), locale=None)

    def test_kiswahili_also_costs_one_message(self):
        # A translation is usually longer than its English, so this is the
        # case that actually breaks the budget.
        body = enrolment_confirmed(
            facts(
                school_short_name="Mtakuja Lutheran Secondary",
                admission_no="MTK-2027-00418",
                portal="mtakuja-lutheran.beyondschools.co.tz",
            ),
            locale="sw",
        )

        assert fits_one_segment(body)
