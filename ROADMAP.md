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

### Step D — fee structure editor — **done**

**First, because it is the only gap that stops the school operating.** A new
class level cannot be billed at all without it.

- Design: `FeeStructureEditor.dc.html`.
- Gate: a structure is drafted for a level, its items entered, published, and
  a term run bills it. Met, and covered by the suite.

Two departures from the artboard, both deliberate and both written into the
page's own docstring. Every change to an item saves immediately instead of
behind a "Save the draft" button, because the endpoint replaces the whole item
list in one call and so there is no partial-save hazard to protect against —
only eleven typed amounts to lose. And the totals are given per term rather
than as "Term 1" and "Terms 2 and 3, each", because an item pinned to one term
makes the others unequal and one figure for both would be true of neither.

**"All present" was wrong.** Two endpoints the editor cannot work without did
not exist:

- `GET /fees/structures/{id}`. The list said a structure had five items;
  nothing said *which* five. A draft could be read back only by whoever still
  had the create response in front of them, so closing the tab lost the work.
  The service method existed and had never been routed.
- `POST /fees/structures/{id}/archive`. The artboard has "Discard the draft"
  and there was no endpoint that could. A structure started against the wrong
  level would have sat in the list for ever, in a list whose whole job is to
  say which levels are not ready to bill.

Both now carry a headcount as well — how many children a structure would bill
— because publishing is irreversible and a headcount is the cheapest check
that the right level is being priced.

**And it found a billing defect that predates the screen.** The initial schema
put a unique constraint on `(academic_year_id, level_id, stream_id)` and it did
not do what it says: PostgreSQL treats NULL as distinct in a unique index, and
a level-wide structure has a NULL stream. So the constraint covered the
stream-scoped case, the rare one, and left unconstrained every structure a
school actually writes.

What that costs: `published_items_by_level` keys the term run's prices by
`(level_id, stream_id)`, so two published level-wide structures for one level
**merge into one list of items and the whole level is billed the sum of both.**
Nothing in the run looks wrong. The invoices are simply too big, for every
child in a class, and the structure list shows two rows that each look correct
on their own. The demo database already held a duplicate pair.

Fixed in three places, because one was not enough:

- Migration `0003` replaces the constraint with two partial unique indexes
  that between them say the intended thing, and that exclude archived rows so
  a discarded draft does not block the structure written to replace it. It
  archives any existing duplicate first, keeping the published row — a
  published structure may have invoices raised from it; a draft has billed
  nobody.
- `create_structure` refuses a second one with a sentence, so the screen does
  not meet the integrity error as a 500.
- The start dialog marks levels that are already priced and will not submit
  against one, offering to open the existing structure instead.

### Step B — admit a child by hand — **done**

The three dead `/students/new` buttons work. They were always gated on
`student:manage` and always pointed at nothing.

- Design: `AdmitByHand.dc.html`, `AdmitByHandPhone.dc.html`.
- Gate: a child who never used the public form is enrolled, invoiced and
  appears on the roll. Met, and asserted end to end — the roll row carries the
  class, the guardian to ring and the balance.

Three steps at every width, as the phone artboard draws them, with the step in
the URL so the browser's back button steps back through the form. The desktop
artboard is one long page; the wizard is used for both because a registrar
reads each answer back to a parent as they go, and the running "what this will
create" summary beside it is the same information with less to scroll past.

Unlike the public application form, every step blocks. A parent at home who
abandons a half-filled form has still told the school their child exists; this
form creates a pupil, an enrolment and a bill in one irreversible go.

**"API: `POST /enrolments` — present" was the wrong endpoint.** It enrols a
child who already exists and raises nothing. The counter needs the pupil
record, the enrolment, the guardian, the link, the family's login and the term
invoice, and it needs them to be one transaction: half of it is a child the
roll cannot find or a bill nobody can sign in to pay, with a parent standing at
the desk. So `POST /admissions/by-hand` does all six, and `GET
/guardians/at-number` answers the question the form asks while the number is
being typed.

Four decisions worth keeping written down:

- **The invoice is raised at admission, not left to the term run.** The run is
  repeatable and would pick the child up — but a receipt is entered *against an
  invoice*, and money taken at a counter with nowhere to put it is how a school
  ends up back on a cash box and a notebook. It carries the term's charges and
  the one-off joining items, because this path takes no deposit and the funnel
  puts those on the deposit invoice; without them a child admitted by hand
  would never be charged an admission fee at all.
- **A staff account is never demoted.** A class teacher whose own child joins
  would otherwise be turned into a guardian by the admission, losing the
  register and the marks their job depends on. They are linked as the child's
  guardian and keep their staff login.
- **The guardian record on file wins on every field, name included.** That is
  how a family's second child joins one parent rather than making two — and it
  silently discards a name typed against a number the school already holds,
  which is why the form warns first and says whose number it is.
- **A full class is refused on a count read in the same transaction.** The
  browser's list of classes with places left goes stale the moment a second
  registrar admits somebody.

**And it closed a latent 500 in the existing offer path.** Both paths that
admit a child ask the invoicing service for a bill, and invoicing has its own
error classes which the admissions routes do not catch. The commonest refusal —
"this level has no published fee structure" — was therefore a 500 rather than a
sentence. Nothing had walked into it because the public form only lists levels
that have a structure, so only a staff offer on such a level could reach it.
Translated at the boundary now, which fixes the offer path too.

### Step E — school setup — **done**

Needed before January for a plain reason: there was no way to create the 2027
academic year, its terms, or the classes the intake enrols into.

- Design: `AcademicYears`, `Levels`, `Classes`, `Subjects`,
  `SchoolSettings` — all built.
- Gate: the year, its terms, its levels and its class groups are created
  through the UI with no SQL. **Met** for all four.
- The ladder is held where it was promised: `level.order_index` plus a
  per-year exit rule in `academic_year_exit_level`, never a constant in code.
  The Levels screen sets that rule per year, which is what makes the December
  2027 double cohort possible — Standard 6 and Standard 7 both leaving, 72
  leavers instead of 34, and the screen says the total out loud because the
  office orders certificates from it.

**Split on purpose, and here is the reason.** The five screens needed two API
additions, and both belong to Classes:

- `PATCH /academic/class-groups/{id}` — capacity and room. The counter
  admission form already refuses a full class and tells the registrar to come
  here and open it up; until now that was advice with nowhere to act on it.
- `GET /academic/teaching-staff` — who can be put in front of a class, with
  what they already hold. There was **no endpoint listing staff at all**,
  which is Stage 2's first item surfacing early. `PUT .../class-teacher`
  existed with no way to find out whom to pass it.

Subjects and School settings followed, and needed two more:

- `PATCH /academic/subjects/{id}` — rename, move, or retire. **There is no
  delete and there never will be:** a report card from a closed year names its
  subjects, and the marks that would prove it are a Stage 2 table that does
  not exist yet — so nothing would stop the deletion today and everything
  would regret it later. Retiring keeps every record and stops the subject
  appearing on new mark sheets. Renaming is safe because the code is what
  records point at.
- `GET`/`POST /schools/me/fields` and `PATCH /schools/me/fields/{id}` — the
  fields a school adds for itself. The key and the kind are fixed once values
  exist: they are what answers are stored under and typed by, so changing
  either would reinterpret what people have already said. The `SchoolField`
  table and its repository existed; nothing had ever exposed them.

The subjects screen leaves out the artboard's **periods a week** except where
a school has set it per level. A period is a slot in a timetable and there is
no timetable yet, so the column would otherwise be a guess dressed as a fact.
School settings takes a logo URL rather than an upload, because the presigned
upload that exists is scoped to a pupil's documents and a button that cannot
work is worse than a field that can.

Each fee rule says what it will do in a sentence with the school's own figures
in it. "Deposit: 30" is not a decision; "an offer asks for 30% of the first
term's fees" is. The two arrears switches get the same treatment, because they
decide whether a child is shown their own marks over money their parents owe.

**Two places the artboards were followed knowingly rather than literally.**

The years artboard draws a **fees due** date per term. There is no such field
and adding one would let two places disagree about when a bill is late — the
due date is the issue date plus the school's grace period, one rule in one
place. The screen states that rule, read-only, and says where to change it.

The classes artboard has **Close the class**. Nothing in the API closes one,
and a class that vanished while an invoice still pointed at it would leave a
bill nobody could explain. The screen says so instead of offering a button
that cannot work.

### Step F — parent and student — **done**

- Design: `ParentHome(+Phone)`, `StudentHome(+Phone)`, `MyAccount(+Phone)` —
  all built, as one responsive set rather than separate phone screens.
- Gate: a guardian signs in, sees each child's balance, and crosses to the AI
  platform without a second login. Met. `POST /auth/ai-handoff` is called by
  something for the first time, and a test asserts the destination comes from
  the server rather than the client.

**The hole was the routing, not the screens.** Every signed-in route sat under
the staff shell, so a guardian who set a password got a sidebar of screens
they may not open and tiles that 403'd. The server was right throughout — it
refused every request — which is exactly why nothing on the API side showed
it. There are now two shells chosen by role, and `/` is home for everybody.

**Three access defects came out of building it**, all of the same shape: a
permission model that was right in the abstract and untested from the outside.

- A pupil could not read their own record. `may_see_student` has always
  allowed it and never got the chance to: the `STUDENT_READ` check above it
  rejected every student before the question was asked, so the branch that
  allowed it was unreachable.
- A guardian could not read their own child's statement. It required
  `INVOICE_READ`, which a guardian does not have — so the one question the
  parent portal exists to answer was forbidden to the person asking. Opened to
  the family, scoped by the link table, with a test that a guardian still
  cannot read the school's invoice list, arrears or roll.
- `/auth/me` did not carry the phone number the account signs in with, so the
  portal could not show a parent the number their fees are tied to.

**`school` gained three columns**: bank name, account name, account number.
Payments are manual in Phase 1 — no aggregator, no button, no callback — which
makes the bank details the only payment instruction the system can give, and
the portal had nowhere to read them from. If a school has not filled them in,
the screen says "pay at the office" rather than showing a half-empty table
that looks like it might be an account number.

Two artboard details differ knowingly. The password rule is **ten**
characters, not eight, because that is what `ChangePasswordRequest` enforces
and a form that accepted eight would fail on submit with nothing to act on.
And the notices feed is built from term and fee facts rather than invented: a
notices table arrives with Communications.

### Step C — the profile panels — **done**

Last of the five, and the one that turned 13 orphaned endpoints into screens.
Every one of them was reachable and none of them was reached.

- Design: `ProfileDocuments`, `ProfileGuardians`, `ProfileMedical`,
  `ProfileCustomFields`, `ChangeClassDialog` — all built, as four tabs on the
  pupil record plus a dialog, because they are one record and a registrar
  correcting a file moves between them in one sitting.
- Gate: a guardian added, a document attached and re-read through a presigned
  link, medical notes and emergency contacts saved, the school's own fields
  filled in, and a class changed. Met.

**The document path is the part worth stating.** The bytes never touch the
API: the browser takes a presigned ticket, uploads straight to object storage,
and only then is the record written — so a failed upload records nothing, and
a birth certificate never passes through an application server's logs, memory
or crash dumps. Reading one back fetches a fresh short-lived link at the
moment of the click and never stores it. There is a test that asserts no API
call carries a `File`, because a test that only checked "a document appeared"
would pass against an implementation that posted the bytes through.

**Medical is not drawn at all without `health:read`**, rather than drawn and
then refused. Children's health data is sensitive personal data under the
Personal Data Protection Act 2022.

**A defect the tests found in a shared primitive.** `Field` generates the id
that ties a label to its control, and only the controls that read its context
get one — so the raw `<input type="file">` in the upload dialog had a label
pointing at nothing, and a screen reader could not find the input at all.
There is a `FileInput` primitive now, and the same trap is closed for anything
built on `Field` later.

Changing a class says what the move will cost before it is made: a fee
structure follows the level, so a move between levels changes what a family is
billed at the next term run while a move between streams of one level does
not. A registrar moving somebody for a timetable reason should know which of
those they are about to do.

**Stage 1 is done when** a January 2027 intake can be run start to finish on
this system by somebody who is not a developer.

**It is, and there is a test that says so.**
`tests/integration/test_january_intake.py` walks the whole thing over HTTP in
fifteen steps, with no SQL after the fixture: the school's own settings and
bank details, a field only this school needs, the 2027 year, its three terms
(an overlapping pair refused first), two levels, the exit rule that completes
the ladder, a class with a place limit and a class teacher, a subject, a fee
structure drafted and published (an empty one refused, and the published one
frozen), a family applying on their own phone and being screened, offered,
paid for and enrolled, a second family admitted at the counter, the class
filling up and being opened, the term run previewed and raised and run again
without double-billing, a receipt entered and the same slip refused twice, the
record corrected four ways, and a parent signing in by one-time code to read
their own child's balance — and only their own child's.

Two things are deliberately outside it, because they are not Stage 1's job:
the school row and the first staff login. A school is onboarded by the
platform, and somebody has to be able to sign in before they can set anything
up.

One long test on purpose. The failure it exists to catch is a seam between two
steps that each pass alone, and a seam cannot be tested in isolation.

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
