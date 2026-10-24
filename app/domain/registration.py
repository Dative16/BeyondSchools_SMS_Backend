"""What a school needs collected, and whether it has been. Pure.

Schools differ in ways nobody can enumerate in advance. One has dormitories and
wants a bed number; one runs buses and wants a route; one asks for a baptism
certificate and one for an immunisation card. This module is the bounded
answer to that: a school declares its own fields and its own document
checklist, and a pure function says what is still missing.

The rule that keeps it from becoming a free-for-all, and it is worth stating
where somebody adding to this file will read it:

    If a rule, a charge, an access decision or a report depends on it, it
    gets a real column. If a human only reads it, it can be a custom field.

Custom fields cannot be reported on well, cannot be validated beyond their
declared kind, and tempt a school into storing medical details somewhere
nobody is guarding — which is why `is_sensitive` exists and why anything the
system starts reasoning about should be promoted out of here.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID


class FieldKind(StrEnum):
    """What a custom field holds.

    Deliberately five. Each one has an obvious input on a phone and an obvious
    validation; a sixth ("anything") would mean nothing could be checked.
    """

    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    CHOICE = "choice"


class FieldScope(StrEnum):
    """What a custom field hangs off.

    `ENROLMENT` is the interesting one: a bed number or a bus route is a fact
    about a child *this year*, not a fact about the child. Hanging it off the
    student would make last year's dormitory unanswerable.
    """

    STUDENT = "student"
    GUARDIAN = "guardian"
    ENROLMENT = "enrolment"
    APPLICATION = "application"


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    field_id: UUID
    key: str
    label: str
    kind: FieldKind
    scope: FieldScope
    options: tuple[str, ...] = ()
    #: Required before an application can be submitted. Every field marked
    #: this way costs completed applications, which is why the public form
    #: should carry almost none of them.
    required_at_registration: bool = False
    is_sensitive: bool = False
    is_active: bool = True


class FieldValueRejected(Exception):
    """A value that does not match its field's declared kind."""


def coerce_field_value(definition: FieldDefinition, raw: Any) -> Any:
    """Check a submitted value against its field, and normalise it.

    Returns something JSON can hold, so the storage layer never has to guess.
    Raises rather than silently correcting: a number field that quietly accepts
    "about 40" is a field whose column of values cannot be added up.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None

    if definition.kind is FieldKind.TEXT:
        text = str(raw).strip()
        if len(text) > 2000:
            raise FieldValueRejected(f"{definition.label} is too long")
        return text

    if definition.kind is FieldKind.NUMBER:
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise FieldValueRejected(f"{definition.label} has to be a number") from exc
        return int(number) if number.is_integer() else number

    if definition.kind is FieldKind.DATE:
        if isinstance(raw, date):
            return raw.isoformat()
        try:
            return date.fromisoformat(str(raw).strip()).isoformat()
        except ValueError as exc:
            raise FieldValueRejected(
                f"{definition.label} has to be a date, written as 2027-01-08"
            ) from exc

    if definition.kind is FieldKind.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        lowered = str(raw).strip().lower()
        if lowered in ("true", "yes", "ndiyo", "1"):
            return True
        if lowered in ("false", "no", "hapana", "0"):
            return False
        raise FieldValueRejected(f"{definition.label} has to be yes or no")

    # CHOICE
    choice = str(raw).strip()
    if choice not in definition.options:
        allowed = ", ".join(definition.options) or "nothing yet"
        raise FieldValueRejected(f"{definition.label} has to be one of: {allowed}")
    return choice


def validate_definition(*, key: str, kind: FieldKind, options: tuple[str, ...]) -> tuple[str, ...]:
    """Whether a school's own field definition makes sense."""
    problems: list[str] = []

    stripped = key.strip()
    if not stripped:
        problems.append("a field needs a key")
    elif not stripped.replace("_", "").isalnum():
        # The key ends up in an export header and in a URL, so it stays
        # boring: letters, digits and underscores.
        problems.append("a key may only hold letters, digits and underscores")

    if kind is FieldKind.CHOICE and len(options) < 2:
        problems.append("a choice field needs at least two options")
    if kind is not FieldKind.CHOICE and options:
        problems.append("only a choice field has options")
    if len(options) != len(set(options)):
        problems.append("two options are the same")

    return tuple(problems)


# --------------------------------------------------------------------------
# The checklist
# --------------------------------------------------------------------------
class Residency(StrEnum):
    DAY = "day"
    BOARDER = "boarder"


@dataclass(frozen=True, slots=True)
class DocumentRequirement:
    """One line of a school's own document checklist.

    `boarders_only` is why this is a table rather than a constant: "mahitaji
    ya ziada ya mwanafunzi wa bweni" — the extra papers a boarding pupil needs
    — must not be demanded of a day pupil.
    """

    requirement_id: UUID
    kind: str
    label: str
    is_required: bool = True
    boarders_only: bool = False
    #: Null means every level.
    level_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ChecklistSubject:
    """The child a checklist is being computed for."""

    #: What the core record holds. Keys are the column names the API uses.
    core: dict[str, Any] = dataclass_field(default_factory=dict)
    #: Document kinds already attached.
    documents: frozenset[str] = frozenset()
    #: Custom field keys that hold a value.
    field_values: frozenset[str] = frozenset()
    residency: Residency = Residency.DAY
    level_id: UUID | None = None
    has_guardian: bool = False
    has_emergency_contact: bool = False


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    key: str
    label: str
    required: bool
    done: bool


@dataclass(frozen=True, slots=True)
class Checklist:
    items: tuple[ChecklistItem, ...]

    @property
    def missing(self) -> tuple[ChecklistItem, ...]:
        return tuple(item for item in self.items if item.required and not item.done)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def done_count(self) -> int:
        return sum(1 for item in self.items if item.done)


#: The core facts a school record is not usable without. Short on purpose:
#: everything else a school wants is its own checklist's business, and a long
#: mandatory list is how a registrar ends up with half-finished records and a
#: parallel notebook.
CORE_REQUIRED = (
    ("first_name", "First name"),
    ("surname", "Surname"),
    ("date_of_birth", "Date of birth"),
    ("sex", "Sex"),
)


def build_checklist(
    *,
    subject: ChecklistSubject,
    requirements: list[DocumentRequirement],
    fields: list[FieldDefinition],
) -> Checklist:
    """What is still missing from one child's record.

    Used by the admissions queue, by the student profile and by the enrolment
    step, so that all three agree about what "complete" means. Three sources,
    one answer: the core fields, the school's document checklist, and whichever
    custom fields the school marked as required.
    """
    items: list[ChecklistItem] = []

    for key, label in CORE_REQUIRED:
        value = subject.core.get(key)
        done = value is not None and (not isinstance(value, str) or bool(value.strip()))
        items.append(ChecklistItem(key=key, label=label, required=True, done=done))

    items.append(
        ChecklistItem(
            key="guardian",
            label="At least one guardian",
            required=True,
            done=subject.has_guardian,
        )
    )
    items.append(
        ChecklistItem(
            key="emergency_contact",
            label="An emergency contact",
            required=True,
            done=subject.has_emergency_contact,
        )
    )

    for requirement in requirements:
        if requirement.boarders_only and subject.residency is not Residency.BOARDER:
            continue
        if requirement.level_id is not None and requirement.level_id != subject.level_id:
            continue
        items.append(
            ChecklistItem(
                key=f"document:{requirement.kind}",
                label=requirement.label,
                required=requirement.is_required,
                done=requirement.kind in subject.documents,
            )
        )

    for definition in fields:
        if not definition.is_active or definition.scope is FieldScope.GUARDIAN:
            continue
        items.append(
            ChecklistItem(
                key=f"field:{definition.key}",
                label=definition.label,
                required=definition.required_at_registration,
                done=definition.key in subject.field_values,
            )
        )

    return Checklist(items=tuple(items))
