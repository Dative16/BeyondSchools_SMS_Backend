"""Seed one demo school so a new developer has something to look at.

Mwenge Primary School, Moshi: a 2027 academic year with three terms, Standard 1
to Standard 7, two streams per level, a published fee structure, staff, children
with guardians, invoices, a few payments — including one sitting in suspense —
and three applications waiting in the review queue.

Two details are deliberate rather than decorative:

* 2027 holds **two exit cohorts**. Standard 7 is the last group through the old
  seven-year cycle, and Standard 6 is the first group to finish after six.
  Both are seeded, because a promotion engine that has never seen two is a
  promotion engine nobody has tested.
* The arithmetic is left to the database. Invoice totals are never written
  here; lines and payments are inserted and the triggers do the rest, which is
  also a check that they work.

This connects as the owning role, which bypasses row-level security. That is
right for a seeding script and wrong for anything the application serves.

    make seed        # or: python scripts/seed_demo.py
"""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password

SCHOOL_NAME = "Mwenge Primary School"
SUBDOMAIN = "mwenge"
YEAR = 2027
DEMO_PASSWORD = "Shule2027!"  # noqa: S105 - demo data, never a real credential

# A fixed seed, so two developers describing "the arrears bug on Baraka Shayo"
# are looking at the same child.
random.seed(2027)

LEVELS = [
    ("PP", "Pre-primary", "pre_primary", 0, 5),
    ("STD1", "Standard 1", "primary", 1, 6),
    ("STD2", "Standard 2", "primary", 2, 7),
    ("STD3", "Standard 3", "primary", 3, 8),
    ("STD4", "Standard 4", "primary", 4, 9),
    ("STD5", "Standard 5", "primary", 5, 10),
    ("STD6", "Standard 6", "primary", 6, 11),
    ("STD7", "Standard 7", "primary", 7, 12),
]

STREAMS = ("Blue", "Green")

SUBJECTS = [
    ("KIS", "Kiswahili", True),
    ("ENG", "English", True),
    ("MTC", "Mathematics", True),
    ("SCI", "Science and Technology", True),
    ("SST", "Social Studies", False),
    ("CIV", "Civic and Moral Education", False),
    ("VOC", "Vocational Skills", False),
    ("RE", "Religious Education", False),
]

TERMS = [
    (1, "Term 1", date(YEAR, 1, 8), date(YEAR, 4, 2)),
    (2, "Term 2", date(YEAR, 4, 19), date(YEAR, 8, 6)),
    (3, "Term 3", date(YEAR, 8, 23), date(YEAR, 12, 4)),
]

#: Tuition rises with the level; the rest is flat. Figures are plausible for a
#: fee-paying primary school in Moshi, in shillings per term.
TUITION_BY_ORDER = {
    0: 180_000,
    1: 220_000,
    2: 220_000,
    3: 240_000,
    4: 240_000,
    5: 260_000,
    6: 280_000,
    7: 300_000,
}

FIRST_NAMES_F = [
    "Neema",
    "Joyce",
    "Zawadi",
    "Upendo",
    "Glory",
    "Happiness",
    "Rehema",
    "Amina",
    "Tumaini",
    "Furaha",
    "Asha",
    "Grace",
    "Imani",
    "Sifa",
]
FIRST_NAMES_M = [
    "Baraka",
    "Amani",
    "Frank",
    "Emmanuel",
    "Juma",
    "Daniel",
    "Peter",
    "Elia",
    "Godfrey",
    "Isaya",
    "Nuru",
    "Said",
    "Hamisi",
    "Tumaini",
]
SURNAMES = [
    "Mwanjala",
    "Shayo",
    "Mushi",
    "Kimaro",
    "Mbwana",
    "Lyimo",
    "Massawe",
    "Ndossi",
    "Mollel",
    "Swai",
    "Marealle",
    "Kileo",
    "Mwakalebela",
    "Urassa",
]

STAFF = [
    ("Datius Mwanga", "admin@mwenge.ac.tz", "0754118902", "school_admin", "MPS-001"),
    ("Agnes Mkapa", "academics@mwenge.ac.tz", "0754118903", "head_of_academics", "MPS-002"),
    ("Salome Kessy", "bursar@mwenge.ac.tz", "0754118904", "bursar", "MPS-003"),
    ("Josephat Mwakalebela", "jmwakalebela@mwenge.ac.tz", "0754118905", "class_teacher", "MPS-004"),
    ("Hawa Nyerere", "hnyerere@mwenge.ac.tz", "0754118906", "class_teacher", "MPS-005"),
    ("Erasto Kweka", "ekweka@mwenge.ac.tz", "0754118907", "subject_teacher", "MPS-006"),
]


@dataclass
class Ids:
    school: UUID
    year: UUID
    terms: dict[int, UUID]
    levels: dict[str, UUID]
    streams: dict[tuple[str, str], UUID]
    class_groups: dict[tuple[str, str], UUID]
    staff: dict[str, UUID]


async def wipe(session: AsyncSession) -> None:
    """Start clean. Deleting the school cascades to everything that carries it."""
    await session.execute(
        text("DELETE FROM school WHERE subdomain = :subdomain"), {"subdomain": SUBDOMAIN}
    )
    await session.commit()


async def seed_school(session: AsyncSession) -> UUID:
    school_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO school (id, name, short_name, subdomain, registration_number, "
            " status, motto, brand_color, phone, email, region, district, "
            " admission_no_format, deposit_percent, invoice_grace_days, created_at, updated_at) "
            "VALUES (:id, :name, 'MPS', :subdomain, 'S.1234', 'active', "
            " 'Elimu ni ufunguo wa maisha', '#22C55E', '0754118900', "
            " 'habari@mwenge.ac.tz', 'Kilimanjaro', 'Moshi', "
            " 'MPS/{year}/{sequence:04d}', 30, 14, now(), now())"
        ),
        {"id": school_id, "name": SCHOOL_NAME, "subdomain": SUBDOMAIN},
    )
    return school_id


async def seed_calendar(session: AsyncSession, school_id: UUID) -> tuple[UUID, dict[int, UUID]]:
    year_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO academic_year (id, school_id, label, starts_on, ends_on, "
            " is_current, is_closed, created_at, updated_at) "
            "VALUES (:id, :school_id, :label, :starts, :ends, true, false, now(), now())"
        ),
        {
            "id": year_id,
            "school_id": school_id,
            "label": str(YEAR),
            "starts": TERMS[0][2],
            "ends": TERMS[-1][3],
        },
    )

    terms: dict[int, UUID] = {}
    for index, label, starts, ends in TERMS:
        term_id = uuid4()
        terms[index] = term_id
        await session.execute(
            text(
                "INSERT INTO term (id, school_id, academic_year_id, index, label, "
                " starts_on, ends_on, is_current, created_at, updated_at) "
                "VALUES (:id, :school_id, :year, :index, :label, :starts, :ends, "
                " :current, now(), now())"
            ),
            {
                "id": term_id,
                "school_id": school_id,
                "year": year_id,
                "index": index,
                "label": label,
                "starts": starts,
                "ends": ends,
                "current": index == 1,
            },
        )
    return year_id, terms


async def seed_structure(
    session: AsyncSession, school_id: UUID, year_id: UUID
) -> tuple[dict[str, UUID], dict[tuple[str, str], UUID], dict[tuple[str, str], UUID]]:
    levels: dict[str, UUID] = {}
    for code, name, stage, order_index, entry_age in LEVELS:
        level_id = uuid4()
        levels[code] = level_id
        await session.execute(
            text(
                "INSERT INTO level (id, school_id, code, name, stage, order_index, "
                " entry_age, is_active, created_at, updated_at) "
                "VALUES (:id, :school_id, :code, :name, :stage, :order_index, :age, "
                " true, now(), now())"
            ),
            {
                "id": level_id,
                "school_id": school_id,
                "code": code,
                "name": name,
                "stage": stage,
                "order_index": order_index,
                "age": entry_age,
            },
        )

    # The ladder, as data. Standard 7 has no next level: it is an exit.
    codes = [code for code, *_ in LEVELS]
    for current, following in zip(codes, codes[1:], strict=False):
        await session.execute(
            text("UPDATE level SET next_level_id = :next WHERE id = :id"),
            {"next": levels[following], "id": levels[current]},
        )

    # 2027's two exit cohorts, which is the reason this is a table and not a
    # constant in the promotion engine.
    for code, note in (
        ("STD7", "Last cohort through the seven-year primary cycle"),
        ("STD6", "First cohort to finish primary after six years"),
    ):
        await session.execute(
            text(
                "INSERT INTO academic_year_exit_level (id, school_id, academic_year_id, "
                " level_id, note) VALUES (gen_random_uuid(), :school_id, :year, :level, :note)"
            ),
            {"school_id": school_id, "year": year_id, "level": levels[code], "note": note},
        )

    streams: dict[tuple[str, str], UUID] = {}
    class_groups: dict[tuple[str, str], UUID] = {}
    for code, name, _stage, _order, _age in LEVELS:
        for stream_name in STREAMS:
            stream_id = uuid4()
            streams[(code, stream_name)] = stream_id
            await session.execute(
                text(
                    "INSERT INTO stream (id, school_id, level_id, name, capacity, "
                    " is_boarding, created_at, updated_at) "
                    "VALUES (:id, :school_id, :level, :name, 40, false, now(), now())"
                ),
                {
                    "id": stream_id,
                    "school_id": school_id,
                    "level": levels[code],
                    "name": stream_name,
                },
            )

            group_id = uuid4()
            class_groups[(code, stream_name)] = group_id
            await session.execute(
                text(
                    "INSERT INTO class_group (id, school_id, academic_year_id, level_id, "
                    " stream_id, name, capacity, created_at, updated_at) "
                    "VALUES (:id, :school_id, :year, :level, :stream, :name, 40, now(), now())"
                ),
                {
                    "id": group_id,
                    "school_id": school_id,
                    "year": year_id,
                    "level": levels[code],
                    "stream": stream_id,
                    "name": f"{name} {stream_name}",
                },
            )

    for code, name, is_core in SUBJECTS:
        subject_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO subject (id, school_id, code, name, is_core, is_examinable, "
                " is_active, created_at, updated_at) "
                "VALUES (:id, :school_id, :code, :name, :core, true, true, now(), now())"
            ),
            {
                "id": subject_id,
                "school_id": school_id,
                "code": code,
                "name": name,
                "core": is_core,
            },
        )
        for level_code, *_ in LEVELS:
            await session.execute(
                text(
                    "INSERT INTO subject_level (id, school_id, subject_id, level_id, "
                    " periods_per_week) "
                    "VALUES (gen_random_uuid(), :school_id, :subject, :level, :periods)"
                ),
                {
                    "school_id": school_id,
                    "subject": subject_id,
                    "level": levels[level_code],
                    "periods": 6 if is_core else 2,
                },
            )

    return levels, streams, class_groups


async def seed_staff(
    session: AsyncSession, school_id: UUID, class_groups: dict[tuple[str, str], UUID]
) -> dict[str, UUID]:
    password_hash = hash_password(DEMO_PASSWORD)
    staff: dict[str, UUID] = {}
    for full_name, email, phone, role, staff_no in STAFF:
        user_id = uuid4()
        staff[role if role not in staff else f"{role}:{staff_no}"] = user_id
        await session.execute(
            text(
                "INSERT INTO app_user (id, school_id, full_name, email, phone, role, "
                " password_hash, must_change_password, staff_no, is_active, failed_logins, "
                " created_at, updated_at) "
                "VALUES (:id, :school_id, :name, :email, :phone, :role, :hash, false, "
                " :staff_no, true, 0, now(), now())"
            ),
            {
                "id": user_id,
                "school_id": school_id,
                "name": full_name,
                "email": email,
                "phone": phone,
                "role": role,
                "hash": password_hash,
                "staff_no": staff_no,
            },
        )

    # Two class teachers, two classes, so the "their class only" permission
    # rule has something real to be tested against.
    teachers = [uid for key, uid in staff.items() if key.startswith("class_teacher")]
    for teacher_id, key in zip(teachers, (("STD4", "Blue"), ("STD6", "Green")), strict=False):
        await session.execute(
            text("UPDATE class_group SET class_teacher_id = :teacher WHERE id = :id"),
            {"teacher": teacher_id, "id": class_groups[key]},
        )
    return staff


async def seed_fees(session: AsyncSession, school_id: UUID, ids: Ids) -> dict[str, UUID]:
    """A published structure per level.

    Standard 6's is left as a draft on purpose: the dashboard's "fee structure
    is not published" warning needs something to warn about, and so does
    whoever is building that screen.
    """
    structures: dict[str, UUID] = {}
    for code, name, _stage, order_index, _age in LEVELS:
        structure_id = uuid4()
        structures[code] = structure_id
        is_draft = code == "STD6"
        await session.execute(
            text(
                "INSERT INTO fee_structure (id, school_id, academic_year_id, level_id, "
                " label, status, published_at, published_by_id, created_at, updated_at) "
                "VALUES (:id, :school_id, :year, :level, :label, :status, :published, "
                " :by, now(), now())"
            ),
            {
                "id": structure_id,
                "school_id": school_id,
                "year": ids.year,
                "level": ids.levels[code],
                "label": f"{name} {YEAR}",
                "status": "draft" if is_draft else "published",
                "published": None if is_draft else date(YEAR - 1, 11, 20),
                "by": None if is_draft else ids.staff["school_admin"],
            },
        )

        items = [
            ("Tuition", TUITION_BY_ORDER[order_index], "tuition", "per_term", None, 0),
            ("Examination", 40_000, "examination", "per_term", None, 1),
            ("Meals", 90_000, "meals", "per_term", None, 2),
            ("Development levy", 50_000, "development", "annual", 1, 3),
            ("Admission fee", 100_000, "admission", "one_off", None, 4),
            ("Uniform set", 75_000, "uniform", "one_off", None, 5),
        ]
        for item_name, amount, category, frequency, term_index, sort_order in items:
            await session.execute(
                text(
                    "INSERT INTO fee_item (id, school_id, fee_structure_id, name, amount, "
                    " category, frequency, term_index, is_mandatory, sort_order, "
                    " created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :school_id, :structure, :name, :amount, "
                    " :category, :frequency, :term_index, true, :sort_order, now(), now())"
                ),
                {
                    "school_id": school_id,
                    "structure": structure_id,
                    "name": item_name,
                    "amount": amount,
                    "category": category,
                    "frequency": frequency,
                    "term_index": term_index,
                    "sort_order": sort_order,
                },
            )
    return structures


async def seed_children(session: AsyncSession, school_id: UUID, ids: Ids) -> None:
    """Children, guardians, enrolments, and Term 1 invoices.

    Siblings are seeded on purpose — two children sharing one guardian phone —
    because that is the case duplicate screening gets wrong if nobody checks.
    """
    sequence = 0
    guardian_pool: list[tuple[UUID, str]] = []

    for code, _name, _stage, order_index, entry_age in LEVELS:
        for stream_name in STREAMS:
            class_group_id = ids.class_groups[(code, stream_name)]
            for _ in range(random.randint(26, 34)):
                sequence += 1
                sex = random.choice(("f", "m"))
                first = random.choice(FIRST_NAMES_F if sex == "f" else FIRST_NAMES_M)
                surname = random.choice(SURNAMES)
                born = date(YEAR - entry_age, random.randint(1, 12), random.randint(1, 28))

                student_id = uuid4()
                await session.execute(
                    text(
                        "INSERT INTO student (id, school_id, admission_no, first_name, "
                        " surname, date_of_birth, sex, status, nationality, "
                        " arrears_brought_forward, created_at, updated_at) "
                        "VALUES (:id, :school_id, :adm, :first, :surname, :dob, :sex, "
                        " 'active', 'Tanzanian', 0, now(), now())"
                    ),
                    {
                        "id": student_id,
                        "school_id": school_id,
                        "adm": f"MPS/{YEAR}/{sequence:04d}",
                        "first": first,
                        "surname": surname,
                        "dob": born,
                        "sex": sex,
                    },
                )

                # One child in eight is a sibling of somebody already seeded:
                # same guardian, same phone, different child.
                sibling_of = (
                    random.choice(guardian_pool)
                    if guardian_pool and random.random() < 0.125
                    else None
                )
                if sibling_of is None:
                    guardian_id = uuid4()
                    phone = f"07{random.randint(10, 89)}{random.randint(100000, 999999)}"
                    guardian_name = f"{random.choice(FIRST_NAMES_F + FIRST_NAMES_M)} {surname}"
                    await session.execute(
                        text(
                            "INSERT INTO guardian (id, school_id, full_name, phone, locale, "
                            " sms_opted_out, created_at, updated_at) "
                            "VALUES (:id, :school_id, :name, :phone, 'en', false, now(), now())"
                        ),
                        {
                            "id": guardian_id,
                            "school_id": school_id,
                            "name": guardian_name,
                            "phone": phone,
                        },
                    )
                    guardian_pool.append((guardian_id, phone))

                    # A login per guardian, with no password: they sign in with
                    # a one-time code. This is the account a fee reminder's
                    # short link lands on, and the one that carries a child
                    # across to the AI platform. Created only for a new
                    # guardian — a sibling shares the one that already exists.
                    await session.execute(
                        text(
                            "INSERT INTO app_user (id, school_id, full_name, phone, role, "
                            " must_change_password, is_active, failed_logins, "
                            " guardian_id, created_at, updated_at) "
                            "VALUES (gen_random_uuid(), :school_id, :name, :phone, "
                            " 'guardian', false, true, 0, :guardian_id, now(), now())"
                        ),
                        {
                            "school_id": school_id,
                            "name": guardian_name,
                            "phone": phone,
                            "guardian_id": guardian_id,
                        },
                    )
                else:
                    guardian_id = sibling_of[0]

                await session.execute(
                    text(
                        "INSERT INTO guardian_student (id, school_id, guardian_id, student_id, "
                        " relationship, is_primary, receives_invoices, receives_sms, "
                        " may_collect, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :school_id, :guardian, :student, "
                        " :relationship, true, true, true, true, now(), now())"
                    ),
                    {
                        "school_id": school_id,
                        "guardian": guardian_id,
                        "student": student_id,
                        "relationship": random.choice(("mother", "father", "legal_guardian")),
                    },
                )

                enrolment_id = uuid4()
                await session.execute(
                    text(
                        "INSERT INTO enrolment (id, school_id, student_id, academic_year_id, "
                        " class_group_id, outcome, enrolled_on, is_repeating, "
                        " created_at, updated_at) "
                        "VALUES (:id, :school_id, :student, :year, :group, 'active', :on, "
                        " false, now(), now())"
                    ),
                    {
                        "id": enrolment_id,
                        "school_id": school_id,
                        "student": student_id,
                        "year": ids.year,
                        "group": class_group_id,
                        "on": TERMS[0][2],
                    },
                )

                await _raise_term_invoice(
                    session,
                    school_id=school_id,
                    ids=ids,
                    student_id=student_id,
                    enrolment_id=enrolment_id,
                    sequence=sequence,
                    tuition=TUITION_BY_ORDER[order_index],
                )


async def _raise_term_invoice(
    session: AsyncSession,
    *,
    school_id: UUID,
    ids: Ids,
    student_id: UUID,
    enrolment_id: UUID,
    sequence: int,
    tuition: int,
) -> None:
    """One Term 1 invoice, then a payment for most of them.

    Nothing here writes `billed` or `settled`: the lines and the payment are
    inserted and the triggers derive both, which is also a live check that
    they do.
    """
    invoice_id = uuid4()
    issued = TERMS[0][2]
    await session.execute(
        text(
            "INSERT INTO invoice (id, school_id, reference, kind, student_id, enrolment_id, "
            " term_id, issued_on, due_date, currency, is_void, created_at, updated_at) "
            "VALUES (:id, :school_id, :ref, 'term', :student, :enrolment, :term, :issued, "
            " :due, 'TZS', false, now(), now())"
        ),
        {
            "id": invoice_id,
            "school_id": school_id,
            "ref": f"INV/{YEAR}/T1/{sequence:04d}",
            "student": student_id,
            "enrolment": enrolment_id,
            "term": ids.terms[1],
            "issued": issued,
            "due": issued + timedelta(days=14),
        },
    )

    lines = [
        ("Tuition, Term 1", tuition, "charge"),
        ("Examination", 40_000, "charge"),
        ("Meals", 90_000, "charge"),
        ("Development levy", 50_000, "charge"),
    ]
    for sort_order, (item, amount, kind) in enumerate(lines):
        await session.execute(
            text(
                "INSERT INTO invoice_line (id, school_id, invoice_id, item, amount, kind, "
                " sort_order, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :invoice, :item, :amount, :kind, "
                " :sort_order, now(), now())"
            ),
            {
                "school_id": school_id,
                "invoice": invoice_id,
                "item": item,
                "amount": amount,
                "kind": kind,
                "sort_order": sort_order,
            },
        )

    total = Decimal(tuition + 40_000 + 90_000 + 50_000)

    # Roughly the real distribution: most settled, some part paid, a minority
    # in arrears. A screen built against a seed where everybody has paid is a
    # screen whose arrears column has never been looked at.
    roll = random.random()
    if roll < 0.62:
        paid = total
    elif roll < 0.87:
        paid = (total * Decimal(random.choice((30, 45, 60, 75))) / Decimal(100)).quantize(
            Decimal("1")
        )
    else:
        paid = Decimal(0)

    if paid > 0:
        channel = random.choice(("mpesa", "airtel_money", "mixx", "halopesa", "counter"))
        await session.execute(
            text(
                "INSERT INTO payment (id, school_id, invoice_id, student_id, external_ref, "
                " amount, applied, currency, channel, state, provider, msisdn, "
                " payer_reference, received_at, settled_on, is_manual, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :invoice, :student, :ref, :amount, "
                " :applied, 'TZS', :channel, 'settled', 'mock', :msisdn, :payer_ref, "
                " :received, :settled_on, false, now(), now())"
            ),
            {
                "school_id": school_id,
                "invoice": invoice_id,
                "student": student_id,
                "ref": f"MOCK-{uuid4().hex[:12].upper()}",
                "amount": paid,
                "applied": paid,
                "channel": channel,
                "msisdn": f"07{random.randint(10, 89)}{random.randint(100000, 999999)}",
                "payer_ref": f"INV/{YEAR}/T1/{sequence:04d}",
                "received": issued + timedelta(days=random.randint(0, 25)),
                "settled_on": issued + timedelta(days=random.randint(0, 25)),
            },
        )


async def advance_admission_sequence(
    session: AsyncSession, school_id: UUID, used: int
) -> None:
    """Tell the school's counter how many admission numbers are already gone.

    This seed hands out MPS/2027/0001 upward from its own loop, and the API
    issues the next number from . Without this the first
    child admitted through the admissions funnel would be given 0001 again and
    the insert would fail on the unique index.

    The same step is needed for real: a school importing an existing roll has
    to move the counter past its highest existing number, or its first new
    admission collides with a pupil who has been there for years.
    """
    await session.execute(
        text(
            "INSERT INTO school_sequence (id, school_id, name, scope, value) "
            "VALUES (gen_random_uuid(), :school_id, 'admission_no', :scope, :used) "
            "ON CONFLICT (school_id, name, scope) "
            "DO UPDATE SET value = GREATEST(school_sequence.value, :used)"
        ),
        {"school_id": school_id, "scope": str(YEAR), "used": used},
    )


async def attach_test_handset(session: AsyncSession, school_id: UUID) -> str | None:
    """Give one guardian a real phone number, if DEMO_GUARDIAN_PHONE is set.

    Without this, every re-seed invents 420 fictional numbers and nobody can
    actually receive a one-time code. The number stays in .env rather than in
    this file: it is somebody's handset, not demo data.
    """
    phone = os.environ.get("DEMO_GUARDIAN_PHONE", "").strip()
    if not phone:
        return None

    row = (
        await session.execute(
            text(
                "SELECT g.id AS guardian_id, u.id AS user_id, s.first_name, s.surname "
                "  FROM guardian g "
                "  JOIN app_user u ON u.guardian_id = g.id "
                "  JOIN guardian_student gs ON gs.guardian_id = g.id "
                "  JOIN student s ON s.id = gs.student_id "
                " WHERE g.school_id = :school_id "
                " LIMIT 1"
            ),
            {"school_id": school_id},
        )
    ).one()

    await session.execute(
        text("UPDATE guardian SET phone = :phone WHERE id = :id"),
        {"phone": phone, "id": row.guardian_id},
    )
    await session.execute(
        text("UPDATE app_user SET phone = :phone WHERE id = :id"),
        {"phone": phone, "id": row.user_id},
    )
    return f"{phone} signs in as the guardian of {row.first_name} {row.surname}"


#: What Mwenge asks for. Taken from what a Tanzanian primary school actually
#: demands at registration: cheti cha kuzaliwa, picha za pasipoti, fomu ya
#: usajili, and the extra paperwork a boarder needs.
DOCUMENT_REQUIREMENTS = [
    ("birth_certificate", "Cheti cha kuzaliwa (birth certificate)", True, False, True, 0),
    (
        "passport_photo",
        "Picha mbili za pasipoti (two passport photographs)",
        True,
        False,
        False,
        1,
    ),
    (
        "registration_form",
        "Fomu ya usajili, imesainiwa (signed registration form)",
        True,
        False,
        False,
        2,
    ),
    ("report_card", "Ripoti ya mwisho ya shule (last report card)", False, False, True, 3),
    ("transfer_letter", "Barua ya uhamisho (transfer letter)", False, False, False, 4),
    ("immunisation_card", "Kadi ya chanjo (immunisation card)", True, True, False, 5),
    (
        "medical",
        "Fomu ya afya kwa mwanafunzi wa bweni (boarder health form)",
        True,
        True,
        False,
        6,
    ),
]

#: Two fields Mwenge needs and the system does not model: a uniform size and a
#: dormitory bed. Neither drives a rule, so neither earns a column — which is
#: the whole test for whether something belongs here.
CUSTOM_FIELDS = [
    {
        "key": "uniform_size",
        "label": "Uniform size",
        "kind": "choice",
        "scope": "student",
        "options": ["4", "6", "8", "10", "12", "14", "S", "M", "L"],
        "required": False,
        "sensitive": False,
        "help": "Measured at the uniform shop, not guessed from the age.",
    },
    {
        "key": "bed_number",
        "label": "Bed number",
        "kind": "text",
        "scope": "enrolment",
        "options": None,
        "required": False,
        "sensitive": False,
        "help": "Dormitory bed, for boarders only. Changes every year.",
    },
]

BUS_ROUTES = [
    ("Majengo", "Majengo A"),
    ("Kiboriloni", "Kiboriloni"),
    ("Shanty Town", "Shanty Town"),
]


async def seed_registration_settings(session: AsyncSession, school_id: UUID) -> None:
    """The school's own checklist and its two custom fields.

    Seeded because the admissions queue and the student profile both read
    them, and a screen built against an empty checklist is a screen whose
    checklist has never been looked at.
    """
    for kind, label, required, boarders_only, at_application, order in DOCUMENT_REQUIREMENTS:
        await session.execute(
            text(
                "INSERT INTO document_requirement (id, school_id, kind, label, is_required, "
                " boarders_only, ask_at_application, sort_order, is_active, "
                " created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :kind, :label, :required, "
                " :boarders_only, :at_application, :order, true, now(), now())"
            ),
            {
                "school_id": school_id,
                "kind": kind,
                "label": label,
                "required": required,
                "boarders_only": boarders_only,
                "at_application": at_application,
                "order": order,
            },
        )

    for index, field_def in enumerate(CUSTOM_FIELDS):
        await session.execute(
            text(
                "INSERT INTO school_field (id, school_id, key, label, help_text, kind, scope, "
                " options, required_at_registration, is_sensitive, is_active, sort_order, "
                " created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :key, :label, :help, :kind, :scope, "
                " :options, :required, :sensitive, true, :order, now(), now())"
            ),
            {
                "school_id": school_id,
                "key": field_def["key"],
                "label": field_def["label"],
                "help": field_def["help"],
                "kind": field_def["kind"],
                "scope": field_def["scope"],
                "options": field_def["options"],
                "required": field_def["required"],
                "sensitive": field_def["sensitive"],
                "order": index,
            },
        )


async def seed_boarders_and_buses(session: AsyncSession, school_id: UUID) -> tuple[int, int]:
    """Make some of the school board, and put some on a bus.

    Both are per-year facts on the enrolment, so a child who boards this year
    and goes home next year has two honest rows rather than one overwritten
    one. Only the upper classes board, which is how these schools work.
    """
    boarders = await session.execute(
        text(
            "UPDATE enrolment e SET residency = 'boarder', "
            "       dormitory = CASE WHEN s.sex = 'f' THEN 'Uhuru' ELSE 'Kibo' END "
            "  FROM student s, class_group cg, level l "
            " WHERE e.student_id = s.id AND cg.id = e.class_group_id AND l.id = cg.level_id "
            "   AND e.school_id = :school_id AND l.order_index >= 5 "
            "   AND right(s.admission_no, 1) IN ('1', '4', '7') "
            "RETURNING e.id"
        ),
        {"school_id": school_id},
    )
    boarder_count = len(boarders.all())

    riders = 0
    for index, (route, stop) in enumerate(BUS_ROUTES):
        result = await session.execute(
            text(
                "UPDATE enrolment e SET uses_transport = true, "
                "       transport_route = :route, transport_stop = :stop "
                "  FROM student s "
                " WHERE e.student_id = s.id AND e.school_id = :school_id "
                "   AND e.residency = 'day' AND e.uses_transport = false "
                "   AND right(s.admission_no, 1) = :digit "
                "RETURNING e.id"
            ),
            {
                "school_id": school_id,
                "route": route,
                "stop": stop,
                "digit": str(index + 2),
            },
        )
        riders += len(result.all())

    return boarder_count, riders


async def seed_health_and_contacts(session: AsyncSession, school_id: UUID) -> tuple[int, int]:
    """An emergency contact for every child, and health notes for a few.

    Every child gets a contact because a child with nobody to call is the one
    thing a school must not be able to leave blank. Health rows are the
    exception rather than the rule — taarifa za afya pale zinapohitajika,
    where they are needed — and they sit behind their own permission.
    """
    contacts = await session.execute(
        text(
            "INSERT INTO emergency_contact (id, school_id, student_id, full_name, "
            " relationship, phone, priority, may_collect, created_at, updated_at) "
            "SELECT gen_random_uuid(), :school_id, s.id, g.full_name, "
            "       gs.relationship::text, g.phone, 1, true, now(), now() "
            "  FROM student s "
            "  JOIN guardian_student gs ON gs.student_id = s.id AND gs.is_primary "
            "  JOIN guardian g ON g.id = gs.guardian_id "
            " WHERE s.school_id = :school_id "
            "RETURNING id"
        ),
        {"school_id": school_id},
    )
    contact_count = len(contacts.all())

    health = await session.execute(
        text(
            "INSERT INTO student_health (id, school_id, student_id, blood_group, allergies, "
            " chronic_conditions, emergency_plan, insurance_scheme, created_at, updated_at) "
            "SELECT gen_random_uuid(), :school_id, s.id, 'O+', "
            "       'Peanuts', 'Asthma', "
            "       'Inhaler in the office. Call the guardian, then the clinic.', "
            "       'NHIF', now(), now() "
            "  FROM student s "
            " WHERE s.school_id = :school_id AND right(s.admission_no, 2) = '07' "
            "RETURNING id"
        ),
        {"school_id": school_id},
    )
    return contact_count, len(health.all())


async def seed_suspense_payment(session: AsyncSession, school_id: UUID) -> None:
    """Money that arrived with nothing to match it.

    The dashboard's first "needs your attention" row, and the case a bursar
    screen has to handle before it is worth shipping.
    """
    await session.execute(
        text(
            "INSERT INTO payment (id, school_id, invoice_id, external_ref, amount, applied, "
            " currency, channel, state, provider, msisdn, received_at, settled_on, reason, "
            " is_manual, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :school_id, NULL, :ref, 150000, 0, 'TZS', 'mpesa', "
            " 'settled', 'mock', '0712445109', now() - interval '2 days', "
            " current_date - 2, 'no open invoice matched this payment', false, now(), now())"
        ),
        {"school_id": school_id, "ref": f"MOCK-{uuid4().hex[:12].upper()}"},
    )


async def seed_applications(session: AsyncSession, school_id: UUID, ids: Ids) -> None:
    """Three applications in the review queue, one of each interesting shape."""
    sibling = (
        await session.execute(
            text(
                "SELECT s.first_name, s.surname, g.full_name AS guardian, g.phone "
                "  FROM student s "
                "  JOIN guardian_student gs ON gs.student_id = s.id "
                "  JOIN guardian g ON g.id = gs.guardian_id "
                " WHERE s.school_id = :school_id "
                " LIMIT 1"
            ),
            {"school_id": school_id},
        )
    ).one()

    applications = [
        {
            "reference": f"APP-{YEAR}-0041",
            "state": "submitted",
            "first": "Glory",
            "surname": sibling.surname,
            "dob": date(YEAR - 6, 3, 14),
            "sex": "f",
            "level": "STD1",
            "previous": "Tumaini Pre-primary",
            "guardian": sibling.guardian,
            "phone": sibling.phone,
            "relationship": "father",
            "score": 35,
            "reason": "same surname, same guardian phone",
            "note": (
                f"Same guardian phone as {sibling.first_name} {sibling.surname}. "
                "Likely a sibling rather than a duplicate."
            ),
            "waiting_days": 4,
        },
        {
            "reference": f"APP-{YEAR}-0042",
            "state": "submitted",
            "first": "Emmanuel",
            "surname": "Ndossi",
            "dob": date(YEAR - 8, 8, 2),
            "sex": "m",
            "level": "STD3",
            "previous": "Mlimani Primary, Dodoma",
            "guardian": "Sarah Ndossi",
            "phone": "0788109442",
            "relationship": "mother",
            "score": None,
            "reason": None,
            "note": "Transfer in. Last report card and transfer letter attached as photos.",
            "waiting_days": 2,
        },
        {
            "reference": f"APP-{YEAR}-0043",
            "state": "screened",
            "first": "Happiness",
            "surname": "Mollel",
            "dob": date(YEAR - 7, 11, 30),
            "sex": "f",
            "level": "STD1",
            "previous": None,
            "guardian": "Neema Mollel",
            "phone": "0715663209",
            "relationship": "mother",
            "score": None,
            "reason": None,
            "note": "Birth certificate verified. No duplicate match found.",
            "waiting_days": 1,
        },
    ]

    for app in applications:
        await session.execute(
            text(
                "INSERT INTO application (id, school_id, reference, state, phone, "
                " phone_verified_at, child_first_name, child_surname, date_of_birth, sex, "
                " previous_school, level_applied_id, academic_year_id, guardian_name, "
                " guardian_phone, guardian_relationship, duplicate_score, duplicate_reason, "
                " screening_note, submitted_at, source, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :school_id, :ref, :state, :phone, "
                " now() - make_interval(days => :waiting), :first, :surname, :dob, :sex, "
                " :previous, :level, :year, :guardian, :phone, :relationship, :score, "
                " :reason, :note, now() - make_interval(days => :waiting), 'public_form', "
                " now() - make_interval(days => :waiting), now())"
            ),
            {
                "school_id": school_id,
                "ref": app["reference"],
                "state": app["state"],
                "phone": app["phone"],
                "first": app["first"],
                "surname": app["surname"],
                "dob": app["dob"],
                "sex": app["sex"],
                "previous": app["previous"],
                "level": ids.levels[app["level"]],
                "year": ids.year,
                "guardian": app["guardian"],
                "relationship": app["relationship"],
                "score": app["score"],
                "reason": app["reason"],
                "note": app["note"],
                "waiting": app["waiting_days"],
            },
        )


async def report(session: AsyncSession, school_id: UUID) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT (SELECT count(*) FROM student WHERE school_id = :s) AS students, "
                "       (SELECT count(*) FROM guardian WHERE school_id = :s) AS guardians, "
                "       (SELECT count(*) FROM class_group WHERE school_id = :s) AS classes, "
                "       (SELECT count(*) FROM invoice WHERE school_id = :s) AS invoices, "
                "       (SELECT coalesce(sum(billed), 0) FROM invoice WHERE school_id = :s) "
                "         AS billed, "
                "       (SELECT coalesce(sum(settled), 0) FROM invoice WHERE school_id = :s) "
                "         AS collected, "
                "       (SELECT count(*) FROM application WHERE school_id = :s "
                "         AND state IN ('submitted', 'screened')) AS waiting"
            ),
            {"s": school_id},
        )
    ).one()

    print(f"  {SCHOOL_NAME} ({SUBDOMAIN})")
    print(f"  {rows.students} students in {rows.classes} classes, {rows.guardians} guardians")
    print(
        f"  {rows.invoices} Term 1 invoices: {rows.billed:,.0f} billed, "
        f"{rows.collected:,.0f} collected"
    )
    print(f"  {rows.waiting} applications waiting for review")
    print(f"  sign in as admin@mwenge.ac.tz / {DEMO_PASSWORD}")


async def main() -> None:
    # The owning role: seeding writes across a whole school before any session
    # is bound to it, which is exactly what row-level security exists to stop.
    engine = create_async_engine(str(settings.MIGRATION_DATABASE_URL))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await wipe(session)

        school_id = await seed_school(session)
        year_id, terms = await seed_calendar(session, school_id)
        levels, streams, class_groups = await seed_structure(session, school_id, year_id)
        staff = await seed_staff(session, school_id, class_groups)

        ids = Ids(
            school=school_id,
            year=year_id,
            terms=terms,
            levels=levels,
            streams=streams,
            class_groups=class_groups,
            staff=staff,
        )

        await seed_fees(session, school_id, ids)
        await seed_registration_settings(session, school_id)
        await seed_children(session, school_id, ids)
        enrolled = (
            await session.execute(
                text("SELECT count(*) FROM student WHERE school_id = :s"),
                {"s": school_id},
            )
        ).scalar_one()
        await advance_admission_sequence(session, school_id, int(enrolled))
        boarders, riders = await seed_boarders_and_buses(session, school_id)
        contacts, health_rows = await seed_health_and_contacts(session, school_id)
        await seed_suspense_payment(session, school_id)
        await seed_applications(session, school_id, ids)
        handset = await attach_test_handset(session, school_id)
        await session.commit()

        await report(session, school_id)
        print(f"  {boarders} boarders, {riders} on a school bus")
        print(f"  {contacts} emergency contacts, {health_rows} health records")
        if handset:
            print(f"  {handset}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
