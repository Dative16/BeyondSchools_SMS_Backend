"""Custom fields and the registration checklist, tested without a database.

The checklist is the piece three screens depend on agreeing about — the
admissions queue, the enrolment step and the student profile — so "what does
complete mean" is settled here once.
"""

from datetime import date
from uuid import uuid4

import pytest

from app.domain.registration import (
    ChecklistSubject,
    DocumentRequirement,
    FieldDefinition,
    FieldKind,
    FieldScope,
    FieldValueRejected,
    Residency,
    build_checklist,
    coerce_field_value,
    validate_definition,
)


def definition(kind: FieldKind, **overrides) -> FieldDefinition:
    defaults = {
        "field_id": uuid4(),
        "key": "bed_number",
        "label": "Bed number",
        "kind": kind,
        "scope": FieldScope.ENROLMENT,
        "options": (),
    }
    return FieldDefinition(**{**defaults, **overrides})


def complete_subject(**overrides) -> ChecklistSubject:
    defaults = {
        "core": {
            "first_name": "Happiness",
            "surname": "Mollel",
            "date_of_birth": date(2020, 11, 30),
            "sex": "f",
        },
        "documents": frozenset({"birth_certificate"}),
        "field_values": frozenset(),
        "residency": Residency.DAY,
        "has_guardian": True,
        "has_emergency_contact": True,
    }
    return ChecklistSubject(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# Values a school types in
# --------------------------------------------------------------------------
def test_an_empty_value_is_absence_not_an_error() -> None:
    """A half-filled record is legal: the checklist reports what is missing
    rather than the write refusing it."""
    assert coerce_field_value(definition(FieldKind.TEXT), "") is None
    assert coerce_field_value(definition(FieldKind.TEXT), "   ") is None
    assert coerce_field_value(definition(FieldKind.NUMBER), None) is None


def test_text_is_trimmed_and_bounded() -> None:
    assert coerce_field_value(definition(FieldKind.TEXT), "  Bed 14  ") == "Bed 14"

    with pytest.raises(FieldValueRejected, match="too long"):
        coerce_field_value(definition(FieldKind.TEXT), "x" * 2001)


def test_a_number_field_refuses_prose() -> None:
    """A number field that quietly accepts "about 40" is a column of values
    nobody can add up."""
    assert coerce_field_value(definition(FieldKind.NUMBER), "14") == 14
    assert coerce_field_value(definition(FieldKind.NUMBER), "14.5") == 14.5

    with pytest.raises(FieldValueRejected, match="has to be a number"):
        coerce_field_value(definition(FieldKind.NUMBER), "about 40")


def test_a_date_is_normalised_to_an_iso_string() -> None:
    field = definition(FieldKind.DATE)

    assert coerce_field_value(field, date(2027, 1, 8)) == "2027-01-08"
    assert coerce_field_value(field, "2027-01-08") == "2027-01-08"

    with pytest.raises(FieldValueRejected, match="written as"):
        coerce_field_value(field, "8/1/2027")


def test_yes_and_no_are_accepted_in_english_and_kiswahili() -> None:
    """A school typing "ndiyo" into its own field should not get an error."""
    field = definition(FieldKind.BOOLEAN)

    assert coerce_field_value(field, "yes") is True
    assert coerce_field_value(field, "ndiyo") is True
    assert coerce_field_value(field, "hapana") is False
    assert coerce_field_value(field, False) is False

    with pytest.raises(FieldValueRejected, match="yes or no"):
        coerce_field_value(field, "maybe")


def test_a_choice_outside_the_options_is_refused_and_says_what_is_allowed() -> None:
    field = definition(FieldKind.CHOICE, label="Uniform size", options=("S", "M", "L"))

    assert coerce_field_value(field, "M") == "M"

    with pytest.raises(FieldValueRejected, match="S, M, L"):
        coerce_field_value(field, "XXL")


# --------------------------------------------------------------------------
# Definitions a school writes
# --------------------------------------------------------------------------
def test_a_sensible_definition_passes() -> None:
    assert validate_definition(key="bed_number", kind=FieldKind.TEXT, options=()) == ()


def test_a_key_stays_boring() -> None:
    """It ends up in an export header and in a URL."""
    problems = validate_definition(key="bed number!", kind=FieldKind.TEXT, options=())

    assert "letters, digits and underscores" in " ".join(problems)


def test_a_choice_field_needs_options_and_other_fields_must_not_have_them() -> None:
    assert "at least two options" in " ".join(
        validate_definition(key="size", kind=FieldKind.CHOICE, options=("S",))
    )
    assert "only a choice field has options" in " ".join(
        validate_definition(key="size", kind=FieldKind.TEXT, options=("S", "M"))
    )
    assert "two options are the same" in " ".join(
        validate_definition(key="size", kind=FieldKind.CHOICE, options=("S", "S"))
    )


# --------------------------------------------------------------------------
# The checklist
# --------------------------------------------------------------------------
def test_a_complete_record_is_complete() -> None:
    requirements = [
        DocumentRequirement(uuid4(), "birth_certificate", "Birth certificate"),
    ]

    checklist = build_checklist(subject=complete_subject(), requirements=requirements, fields=[])

    assert checklist.complete
    assert checklist.missing == ()


def test_a_missing_document_is_named() -> None:
    requirements = [
        DocumentRequirement(uuid4(), "birth_certificate", "Birth certificate"),
        DocumentRequirement(uuid4(), "passport_photo", "Two passport photographs"),
    ]

    checklist = build_checklist(subject=complete_subject(), requirements=requirements, fields=[])

    assert [item.label for item in checklist.missing] == ["Two passport photographs"]


def test_an_optional_document_never_blocks() -> None:
    """ "Last report card, if any" is a prompt, not a requirement."""
    requirements = [
        DocumentRequirement(uuid4(), "report_card", "Last report card", is_required=False),
    ]

    checklist = build_checklist(subject=complete_subject(), requirements=requirements, fields=[])

    assert checklist.complete
    assert any(item.label == "Last report card" for item in checklist.items)


def test_a_boarders_only_requirement_is_not_demanded_of_a_day_pupil() -> None:
    """Otherwise every day pupil's record reads as incomplete forever, and a
    checklist nobody can clear is a checklist nobody reads."""
    requirements = [
        DocumentRequirement(uuid4(), "medical", "Boarding medical form", boarders_only=True),
    ]

    day = build_checklist(subject=complete_subject(), requirements=requirements, fields=[])
    boarder = build_checklist(
        subject=complete_subject(residency=Residency.BOARDER),
        requirements=requirements,
        fields=[],
    )

    assert day.complete
    assert not boarder.complete
    assert boarder.missing[0].label == "Boarding medical form"


def test_a_requirement_scoped_to_one_level_skips_the_others() -> None:
    std1, std6 = uuid4(), uuid4()
    requirements = [
        DocumentRequirement(uuid4(), "leaving_certificate", "Leaving certificate", level_id=std6),
    ]

    joining_std1 = build_checklist(
        subject=complete_subject(level_id=std1), requirements=requirements, fields=[]
    )
    joining_std6 = build_checklist(
        subject=complete_subject(level_id=std6), requirements=requirements, fields=[]
    )

    assert joining_std1.complete
    assert not joining_std6.complete


def test_a_required_custom_field_counts_and_an_optional_one_does_not() -> None:
    required = definition(FieldKind.TEXT, key="bed_number", required_at_registration=True)
    optional = definition(FieldKind.TEXT, key="bus_route", label="Bus route")

    checklist = build_checklist(
        subject=complete_subject(), requirements=[], fields=[required, optional]
    )

    assert [item.label for item in checklist.missing] == ["Bed number"]
    assert any(item.key == "field:bus_route" for item in checklist.items)


def test_an_inactive_field_disappears_from_the_checklist() -> None:
    retired = definition(
        FieldKind.TEXT, key="old_thing", required_at_registration=True, is_active=False
    )

    checklist = build_checklist(subject=complete_subject(), requirements=[], fields=[retired])

    assert checklist.complete


def test_a_record_with_no_guardian_or_contact_is_incomplete() -> None:
    """Both are core. A child with nobody to call is the one case a school
    cannot be allowed to leave half-filled."""
    checklist = build_checklist(
        subject=complete_subject(has_guardian=False, has_emergency_contact=False),
        requirements=[],
        fields=[],
    )

    labels = [item.label for item in checklist.missing]
    assert "At least one guardian" in labels
    assert "An emergency contact" in labels


def test_a_missing_core_field_is_reported() -> None:
    subject = complete_subject(
        core={"first_name": "Happiness", "surname": "", "date_of_birth": None, "sex": "f"}
    )

    checklist = build_checklist(subject=subject, requirements=[], fields=[])

    assert {item.label for item in checklist.missing} == {"Surname", "Date of birth"}
