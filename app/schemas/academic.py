"""Academic structure request and response models."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.academic_year import LadderOutcome
from app.domain.enrolment import LevelStage


# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------
class TermResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    academic_year_id: UUID
    index: int
    label: str
    starts_on: date
    ends_on: date
    is_current: bool


class AcademicYearResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    label: str
    starts_on: date
    ends_on: date
    is_current: bool
    is_closed: bool


class AcademicYearCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=16)
    starts_on: date
    ends_on: date
    #: A school setting up next year months in advance should not be moved into
    #: it by accident, so this is opt-in.
    make_current: bool = False


class TermDraftRequest(BaseModel):
    index: int = Field(ge=1, le=4)
    label: str = Field(default="", max_length=32)
    starts_on: date
    ends_on: date


class TermsReplaceRequest(BaseModel):
    """A year's terms, all of them.

    The rules worth enforcing — no overlaps, numbered in date order, inside the
    year — are properties of the whole set, so the whole set arrives together.
    Sending them one at a time would let a school get halfway into a calendar
    that cannot be reconciled.
    """

    terms: list[TermDraftRequest] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique_indexes(self) -> TermsReplaceRequest:
        indexes = [term.index for term in self.terms]
        if len(set(indexes)) != len(indexes):
            raise ValueError("two terms share the same number")
        return self


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------
class LevelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    stage: LevelStage
    order_index: int
    next_level_id: UUID | None
    entry_age: int | None
    is_active: bool


class LevelCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=64)
    stage: LevelStage
    order_index: int = Field(ge=0, le=100)
    entry_age: int | None = Field(default=None, ge=3, le=30)
    #: Set only where the ladder is not a straight line — a Form 1 that splits
    #: into a general and a vocational stream, for instance. Otherwise
    #: `order_index` decides what comes next.
    next_level_id: UUID | None = None


class LevelUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    stage: LevelStage | None = None
    order_index: int | None = Field(default=None, ge=0, le=100)
    entry_age: int | None = Field(default=None, ge=3, le=30)
    next_level_id: UUID | None = None
    is_active: bool | None = None


class LadderStepResponse(BaseModel):
    level_id: UUID
    outcome: LadderOutcome
    next_level_id: UUID | None
    reason: str


class ExitLevelRequest(BaseModel):
    level_id: UUID
    #: "Last cohort through the seven-year primary cycle", so that whoever
    #: reads December's promotion proposals knows why.
    note: str | None = Field(default=None, max_length=200)


class ExitLevelsReplaceRequest(BaseModel):
    """Which cohorts leave at the end of a year.

    A list, because 2027 has two: Standard 7 finishing the old seven-year cycle
    and Standard 6 finishing the new six-year one, sitting the same examination
    in the same year.
    """

    levels: list[ExitLevelRequest] = Field(default_factory=list, max_length=6)


class LadderResponse(BaseModel):
    """The ladder and everything wrong with it, in one answer.

    The problems travel with the data rather than behind a second call, because
    the academic settings screen shows both and a school that only sees the
    happy version finds out in December.
    """

    academic_year_id: UUID | None
    levels: list[LevelResponse]
    exit_level_ids: list[UUID]
    steps: list[LadderStepResponse]
    problems: list[str]
    summary: str


# --------------------------------------------------------------------------
# Classes
# --------------------------------------------------------------------------
class StreamResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    level_id: UUID
    name: str
    capacity: int | None
    is_boarding: bool


class StreamCreateRequest(BaseModel):
    level_id: UUID
    name: str = Field(min_length=1, max_length=32)
    capacity: int | None = Field(default=None, ge=1, le=200)
    is_boarding: bool = False


class ClassGroupResponse(BaseModel):
    id: UUID
    academic_year_id: UUID
    level_id: UUID
    stream_id: UUID | None
    name: str
    class_teacher_id: UUID | None
    room: str | None
    capacity: int | None
    #: Counted from active enrolments, never stored: a stored count goes stale
    #: the first time a child transfers out, and a roll that disagrees with the
    #: register costs a school's trust.
    pupils: int


class ClassGroupCreateRequest(BaseModel):
    academic_year_id: UUID
    level_id: UUID
    stream_id: UUID | None = None
    class_teacher_id: UUID | None = None
    room: str | None = Field(default=None, max_length=40)
    capacity: int | None = Field(default=None, ge=1, le=200)


class ClassGroupUpdateRequest(BaseModel):
    """What can be changed about a class after it exists.

    Not the name: it is written from the level and the stream by one function,
    so a roll, an invoice and an SMS cannot disagree about what a class is
    called. Not the level or the year either — a class that changed either
    would move every child in it without anybody saying so.
    """

    room: str | None = Field(default=None, max_length=32)
    #: Null means "nobody has said", which is not the same as nought. The
    #: admission form treats a class with no capacity as having room.
    capacity: int | None = Field(default=None, ge=1, le=300)


class TeachingStaffResponse(BaseModel):
    """Somebody who can be put in front of a class.

    What they already hold comes with them, because a head of academics
    choosing a class teacher needs to see it: a teacher who already has a
    class can take a second, but the register then falls to one person twice a
    day, and that should be a decision rather than an accident.
    """

    id: UUID
    full_name: str
    role: str
    staff_no: str | None
    #: The class they are already class teacher of, in the year asked about.
    class_group_id: UUID | None
    class_name: str | None
    #: How many subject-and-class pairs they teach. Periods a week would be
    #: better and belong to the timetable, which does not exist yet.
    subjects_taught: int


class ClassTeacherRequest(BaseModel):
    #: Null takes the current class teacher off the class.
    teacher_id: UUID | None = None


# --------------------------------------------------------------------------
# Subjects
# --------------------------------------------------------------------------
class SubjectLevelRequest(BaseModel):
    level_id: UUID
    #: What the syllabus allocates. The timetable builder scores a draft
    #: against this rather than enforcing it.
    periods_per_week: int | None = Field(default=None, ge=1, le=20)


class SubjectLevelOnSubject(BaseModel):
    """One level a subject is taught at, and how often."""

    level_id: UUID
    level_name: str
    periods_per_week: int | None


class SubjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    is_core: bool
    is_examinable: bool
    is_active: bool


class SubjectRowResponse(SubjectResponse):
    """A subject as the subjects screen lists it.

    The levels come with the row because the screen shows them, and because
    the periods per week belong to the subject-and-level pairing rather than
    to the subject: Kiswahili at Standard 2 and at Standard 6 are one subject
    against two syllabuses.
    """

    levels: list[SubjectLevelOnSubject]


class SubjectUpdateRequest(BaseModel):
    """Rename a subject, move it, or retire it.

    There is no delete. A report card from a closed year names its subjects,
    so retiring one — `is_active: false` — keeps every old record and stops it
    appearing on new mark sheets.
    """

    code: str | None = Field(default=None, min_length=1, max_length=16)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    is_core: bool | None = None
    is_examinable: bool | None = None
    #: False retires it: taught no longer, but still named by old records.
    is_active: bool | None = None
    #: Replaces the whole set when given. Left out, the levels are untouched.
    levels: list[SubjectLevelRequest] | None = None


class SubjectCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=80)
    #: Core subjects are the ones the timetable builder tries to place in
    #: morning periods: Maths, English, Kiswahili, Science.
    is_core: bool = False
    is_examinable: bool = True
    levels: list[SubjectLevelRequest] = Field(default_factory=list, max_length=30)


class TeachingAssignmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    teacher_id: UUID
    subject_id: UUID
    class_group_id: UUID


class TeachingAssignmentCreateRequest(BaseModel):
    teacher_id: UUID
    subject_id: UUID
    class_group_id: UUID
