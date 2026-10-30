# Roadmap

Measured 30 October 2026, against both repos. This file replaces the one-line
comment in `app/api/v1/router.py` that deferred four modules to "phase 2"
without saying what that meant or what it cost.

The delivery plan this follows is the spec doc, *BeyondSchools School
Management System — Architecture & Delivery Plan*. Where this file and the spec
disagree, the disagreement is noted and argued, not silently resolved.

---

## The two dates that set the order

Everything below is sequenced by two commitments from the spec, not by what is
pleasant to build.

**January 2027 — BeyondSchools Academy runs its own intake on this system,
with no parallel spreadsheet.** That is the Phase 1 gate, and the spec is
blunt about it: *"If a spreadsheet survives the intake, the phase is not
done."* It is roughly nine weeks away.

**December 2027 — the promotion engine runs the double exit cohort.** In 2027
one school holds two exit cohorts at once: pupils who were in Standard 6 in
2026 move to Standard 7 and sit the PSLE, and pupils who were in Standard 5
sit the same examination from Standard 6. The spec: *"Getting that wrong in
front of a real school is not a bug report, it is the end of the product."*
Dry run against real data in a staging copy in September 2027, not in
December.

---

## Where we are

| | Built | Stubbed or absent |
|---|---|---|
| API endpoints | 72 | — |
| API services | 6 real (4,671 lines) | 7 one-line stubs |
| API repositories | 9 real (3,482 lines) | 4 stubs |
| API tests | 349 passing (7,642 lines) | — |
| Database tables | 29 | none for attendance, marks, timetable, homework |
| Web screens | 11 | 5 designed and unbuilt, academic ops undesigned |
| Web tests | 106 passing | — |
| Design artboards | 33 | none for academic operations |

Working end to end: sign-in and the three password routes, dashboard, students
roll and profile, the public application form, the admissions funnel with one
application carried from submitted to enrolled, fees list, invoice detail,
manual payment entry, suspense matching.

### What the measurement turned up

1. **Academic operations does not exist at any layer.** No table, no model, no
   repository, no service, no route, no screen, no artboard. `report_card`
   appears only as a document kind — a scan from a previous school — and
   `examination` only as a fee category. Neither is a marks system.
2. **29 of 72 endpoints are unreachable from any screen.** Working, tested
   code with no way in.
3. **Three primary buttons lead to the not-found page.** `/students/new` is
   linked from the dashboard, the students roll and the admissions overview;
   the router has no such route.
4. **The committed API types have drifted.** `src/api/schema.d.ts` does not
   know `/auth/password/reset`, which the web calls and the API serves.
   Nothing in CI catches this.
5. **No SMS is ever sent.** The Beem provider is 193 lines and tested;
   `app/services/notifications.py` is one line and all four Celery task files
   are one line each.
6. **Staff user management has no API whatsoever.** There is no way to add a
   teacher, which means `teaching_assignment` cannot be used, which means
   marks and registers have nobody to attribute them to.

### The good news

Sections B, C, D, E and F — every designed, unbuilt screen — need **no API
work**. `POST /fees/structures`, `PUT /structures/{id}/items`,
`POST /structures/{id}/publish`, `POST /enrolments`,
`GET /guardians/me/children`, `POST /academic/levels|subjects|class-groups`
and `PUT /academic/years/{id}/terms` all exist and are tested. Closing Phase 1
is five screens against an API that is already there.

---

## Stage 1 — close Phase 1

Web only. No migrations, no new endpoints. Ordered by what blocks the January
intake, not by size.

### Step 0 — hygiene (half a day)

Small, and it stops the next five steps from drifting.

**Done.** The contract drift is closed and guarded in both repos' CI, and an
enrolled child's guardian is now sent a message.

**Corrected while doing it.** Two items in the first draft of this list were
wrong, and the measurement that produced them was too shallow:

- *"Bring up the Celery worker and beat containers, which are not running."*
  They are not runnable. `celery_app.conf.beat_schedule` names three tasks —
  `reconciliation.reconcile_previous_day`, `notifications.send_fee_reminders`,
  `invoices.audit_balances` — and all three modules are one-line stubs, so the
  worker registers **zero** tasks. Starting beat would fire `NotRegistered`
  nightly and weekly, for ever. The containers stay down until the tasks
  exist, and the tasks are not step 0: fee reminders need a reminder message,
  a query for who owes and a send loop; the balance audit and the nightly
  reconciliation need the reconciliation service, which is also a stub.
- *"Resolve Beem's unreachability from the container."* Not resolvable in this
  repo, because it is not this repo's problem. The container is handed a
  certificate for `*.beem.africa` issued by `Avast Web/Mail Shield Root`:
  the antivirus on the development machine is terminating the TLS connection
  and re-signing it, and the container does not trust that root. Production is
  unaffected — nothing intercepts there. The fix belongs on the machine
  (exclude `*.beem.africa` from Avast's HTTPS scanning), and the one thing not
  to do is relax certificate verification in code to make a local antivirus
  happy, which would ship as a real weakness to work around a local one.

**Still owed here:** the first real Beem send to the test handset, once the
machine stops intercepting it. Everything up to the gateway is exercised.

The spec files Communications under Phase 3. That is wrong for this gate: an
intake with no parallel spreadsheet means the school stops phoning parents by
hand, and the spec itself says *"SMS as the real channel… the portal is the
destination, not the notification."* A minimal send — enrolment invite, fee
reminder — comes forward.

### Step D — fee structure editor

**First, because it is the only gap that stops the school operating.** A new
class level cannot be billed at all without it.

- API: `GET`/`POST /fees/structures`, `PUT /structures/{id}/items`,
  `POST /structures/{id}/publish` — all present.
- Design: `FeeStructureEditor.dc.html`.
- Gate: a structure is drafted for a level, its items entered, published, and
  a term run bills it.

### Step B — admit a child by hand

Kills the three dead buttons the same day it lands.

- API: `POST /enrolments` — present.
- Design: `AdmitByHand.dc.html`, `AdmitByHandPhone.dc.html`.
- Gate: a child who never used the public form is enrolled, invoiced and
  appears on the roll.

### Step E — school setup

Needed before January for a plain reason: there is currently no way to create
the 2027 academic year, its terms, or the classes the intake enrols into.

- API: `POST /academic/levels|subjects|class-groups`,
  `PATCH /academic/levels/{id}`, `PUT /academic/years/{id}/terms`,
  `POST /academic/years/{id}/current`, `PATCH /schools/me` — all present.
- Design: `AcademicYears`, `Levels`, `Classes`, `Subjects`, `SchoolSettings`.
- Gate: the 2027 year, its three terms, its levels and its class groups are
  created through the UI, with no SQL.
- Hold the line on the level ladder: `level.order_index` and a per-cohort
  `exit_level_id` on the academic year, never a constant in code. The
  `academic_year_exit_level` table already exists for this. It is what makes
  the December 2027 run possible.

### Step F — parent and student

Closes a live hole: a guardian who sets a password today lands on the staff
dashboard and sees almost nothing.

- API: `GET /guardians/me/children`, `POST /auth/password`,
  `POST /auth/ai-handoff` — all present. `ai-handoff` has never been called by
  anything; this is the step that proves the single sign-on the whole project
  rests on.
- Design: `ParentHome(+Phone)`, `StudentHome(+Phone)`, `MyAccount(+Phone)`.
- Gate: a guardian signs in on a 360 px viewport, sees each child's balance,
  and crosses to the AI platform without a second login.

The spec files the parent portal under Phase 3 in its module map and under
Phase 2 in its roadmap. It is built now: the screens are designed, the API
exists, and the gate above is the only way to test the handoff.

### Step C — the profile panels

Last of the five, because nothing is blocked on it — but it unlocks 13
orphaned endpoints and is the difference between a record that can be read and
one that can be kept.

- API: documents and presigned URLs, guardians, health, emergency contacts,
  custom fields, transfer-out — all present.
- Design: `ProfileDocuments`, `ProfileGuardians`, `ProfileMedical`,
  `ProfileCustomFields`, `ChangeClassDialog`.
- Gate: a child's record is corrected, a guardian added, a document attached
  and re-read through a presigned link, and a class changed.

**Stage 1 is done when** a January 2027 intake can be run start to finish on
this system by somebody who is not a developer.

---

## Stage 2 — academic operations

Full stack, every step: migration, model, repository, domain rules, service,
routes, tests, **then** a design pass, then screens. No artboards exist for
any of this, so each step begins with design — that is new work, not a
formality, and it is why this stage is comparable in size to everything built
so far rather than a tail.

### Step 1 — staff accounts

A hard prerequisite with no API today. Attendance, marks and the timetable all
have to be attributed to a teacher, and `teaching_assignment` is unusable
until teachers can exist.

Minimal and no more: create a staff user, assign a role, bind them to subjects
and class groups, deactivate them. The spec's fuller "Staff records" — profiles,
leave register, contracted load — stays in Phase 3.

### Step 2 — attendance

Depends on class groups and staff. Earliest daily value, and the thing that
makes a teacher open the system every morning.

Daily and period registers, absence reasons, guardian alerts by SMS. Scoped to
enrolment, like everything else.

### Step 3 — assessments and marks

Per the spec's data model: `assessment` keyed on class group, subject, term,
kind and max marks — continuous assessment and terminal exams in **one table,
different kind**; `mark` keyed on assessment, enrolment, score and entered_by.
Scoped to enrolment so a repeated year keeps both sets of marks.

Who may enter a mark comes from `teaching_assignment`, which is why staff
accounts come first.

### Step 4 — report cards

Grading scales as school data, not constants: PSLE, CSEE and ACSEE band
differently, and a school with its own internal scale must not need a
deployment. Report generation, a publish step, and a parent-visible result
once published.

**This is the Phase 2 gate:** a report-card run the head of academics signs
off without correcting by hand.

### Step 5 — homework and the AI handoff

The module that justifies the project. `assignment` and `submission` as the
spec shapes them, three authoring paths (pick a lesson, pick and trim, write
your own), dispatch into the AI platform, and a completion grid — a row per
student, a column per assignment — which is the screen a teacher actually
wants.

An AI-posted score lands as a **provisional** mark the subject teacher can
override with a reason. The spec is right that teachers who cannot override an
automatic mark stop trusting the system within a term.

Design for the catalogue's real bound: it is primary-only today, so Standard 1
to 6 can be dispatched and tutored, while Form 1 upward needs the
teacher-written path marked inside this system. Build both now rather than
discovering it when a Form 2 teacher sets the first assignment.

### Step 6 — timetable

Manual builder with live clash detection first. Automatic generation behind
it, and only when it is worth building — the spec's reasoning holds: full
auto-timetabling is NP-hard, every school has unwritten rules it will not
mention until the draft is wrong, and the real pain is not "produce a
timetable" but "stop double-booking Mr. Mwakalebela".

Hard constraints as database indexes, so no code path can violate them: one
slot per teacher per period, one per class group per period, no double-booked
room. Soft constraints scored and shown as warnings a user may accept.
Publishing snapshots a version with an effective date; edits happen on a
working copy.

### Step 7 — promotion engine

A reviewed batch run, never a cron job. A date-triggered silent promotion will
move up a child who has left, one who should repeat, and one whose fees were
never settled, and nobody notices until March.

A run is a row with an id, a status and an author. The engine proposes
promote, repeat, graduate, transfer_out or withdraw for every active
enrolment, from rules the school sets. Proposals are reviewed by class with
the reason shown, and overridden where wrong. Committing writes every new
enrolment in one transaction stamped with the `run_id`.

**Build the undo on day one.** Because every row carries the `run_id` the
whole run reverses in one operation, up until the first mark of the new term is
entered — and it will be needed the first time somebody commits with the wrong
academic year selected.

Committing then cascades: new class group assignments, the first term's
invoice at the new level's structure, arrears carried forward on the student
rather than stranded on a closed enrolment, and the AI platform told each
student's new level so the content served moves up with them.

Deadline is not negotiable: finished and trusted before December 2027, dry-run
against real 2027 data in September.

---

## How we work

One step at a time. Each ends in a commit and a written report that says what
landed, what was left out and why, and what it cost. No step is reported done
on unit tests alone — each is driven in a real browser against the seeded
school first, at 360 px as well as desktop.

Four gates stay green: web unit tests, API tests, lint and typecheck, build.

## Open decisions

1. **Does academic operations start before Stage 1 finishes?** This plan says
   no — the January gate is nine weeks out and Stage 1 is five screens against
   an existing API. But the sequence is a choice, and it is the user's.
2. **The spec's own warning, now due.** *"If this is being built alongside the
   existing AI platform work and the Imforia commitments, each phase stretches
   and the December 2027 date stops being comfortable — which is worth
   deciding now rather than in October 2027."* It is October 2026. There is one
   year of slack and it should be spent deliberately.
3. **Payments.** Phase 1 settles fees by audited manual entry, which is the
   school's actual process. The aggregator adapter, a school lookup that works
   without a subdomain header, and the signed callback route are still owed;
   `app/api/v1/webhooks.py` holds the note.
