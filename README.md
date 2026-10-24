# BeyondSchools SMS — API

FastAPI service for school operations. Owns people, money and the calendar.
Lessons, tutoring and attempts stay in the AI platform; the two are joined by a
token and a webhook.

## Running it

```bash
cp .env.example .env
make keys
make up
make migrate
make seed
```

The API is on http://localhost:8000, docs at `/docs`.

`make seed` loads Mwenge Primary School: a 2027 academic year with three terms,
Standard 1 to Standard 7 in two streams each, around 470 children with
guardians and Term 1 invoices, one payment sitting in suspense, and three
applications in the review queue. Sign in as `admin@mwenge.ac.tz` /
`Shule2027!`. It also seeds **two exit cohorts for 2027**, because that is the
year the promotion engine has to get right.

If the build fails with an SSL certificate error from pip, the network is
re-signing TLS — a corporate proxy, or antivirus HTTPS scanning. Point
`EXTRA_CA_FILE` at that root CA rather than reaching for `--trusted-host`:

```bash
EXTRA_CA_FILE=./docker/local-ca.pem docker compose build api
```

## The three rules this codebase is built around

**1. No query filters by `school_id` by hand.** Routes depend on
`tenant_session` (`app/api/deps.py`), which pins `app.school_id` on the
PostgreSQL session; row-level security does the filtering. The app connects as
`bs_app` (`DB_APP_ROLE`), a non-superuser, non-owner role — RLS is bypassed by
owners, so connecting as the owner would silently disable every policy. That is
why `POSTGRES_USER` and `DB_APP_ROLE` are two different roles: the first owns
the tables and runs migrations, the second is all the application ever gets.
`tests/integration/test_tenant_isolation.py` exists to catch that, and it also
fails if a new table carries `school_id` without a policy.

Policies are `FORCE`d, so they apply to the owning role too. One consequence
worth knowing before you hit it: **a data migration that backfills tenant rows
must set `app.platform_scope` first**, or it will quietly update nothing. The
two escape hatches are `platform_scope()` and `unscoped()` in
`app/core/tenancy.py`, and both roll back on the way out.

**2. A balance is never typed in.** `invoice.billed` and `invoice.settled` are
maintained by trigger from the lines and the payments, and a direct `UPDATE` of
either is silently discarded — see `tests/integration/test_schema_invariants.py`.
Money enters the ledger one of two ways: a verified callback through
`PaymentProvider` (`app/integrations/payments/base.py`), or an explicit manual
entry that records who made it and why. A correction is a credit note or a
payment, never an edit.

**Phase 1 runs on manual entry.** `PAYMENT_PROVIDER=manual` means no aggregator
is wired up: the bursar records each payment, and the callback path stays
dormant. Two consequences worth knowing — a deposit that converts an
application into an enrolment is a bursar's audited entry rather than a
webhook, and `external_ref` is still unique per school, so the idempotency
guarantee is already in place for the day an aggregator is chosen.

**3. The level ladder is data, not code.** Tanzania is mid-reform: two primary
cohorts sit the PSLE in 2027. What comes next is decided by
`level.order_index`, an optional explicit `next_level_id` override, and the rows
in `academic_year_exit_level` — one per cohort leaving that year, so a year can
hold two. `app/domain/academic_year.py` answers it, and an exit level beats the
ladder: Standard 6 has a perfectly good next rung in 2027 and must still
graduate.

`GET /api/v1/academic/ladder` returns the ladder, the exit cohorts, and
everything wrong with both — a duplicate order index, an override pointing
downwards, a top cohort with nowhere to go. The problems travel with the data
rather than behind a second call, because the difference between "correctly
configured for the transition" and "somebody forgot a cohort" is what a head of
academics needs to see in September, not December.

## Signing in

The service is the OIDC issuer for both applications. It signs RS256 access
tokens and publishes the public half at `/.well-known/jwks.json`, which is how
aibeyondschools.co.tz validates a token without the two services sharing a
secret. `make keys` writes the local keypair into `secrets/` (gitignored);
production mounts the platform's secret at the same path.

| | Audience | Lifetime | Who holds it |
| --- | --- | --- | --- |
| Access token | `beyondschools-sms` | 15 minutes | the browser, in memory |
| Refresh token | — (opaque, hashed) | 14 days | an httpOnly cookie |
| Handoff token | `aibeyondschools` | 2 minutes | a URL, once |

The audiences are what keep the handoff safe: this API refuses a token minted
for the AI platform, and the AI platform refuses one minted for this API.
`tests/unit/test_security.py` asserts both directions.

Codes go out over Beem Africa (`SMS_PROVIDER=beem`). The sender ID has to be
one already approved on the account, or every send comes back `200` with
`successful: false` — which is why the adapter treats that as a failure rather
than trusting the status code. `SMS_PROVIDER=mock` logs the message instead of
sending it, and is the default for tests. Set `DEMO_GUARDIAN_PHONE` in `.env`
to a real handset and `make seed` will point one guardian at it, so somebody
can actually receive a code.

The wire format in `app/integrations/sms/provider.py` follows the
implementation already in production on the .NET side, including three details
that look like noise and are not: an ASCII-escaped body, `encoding` as the
string `"0"`, and Basic credentials built from ISO-8859-1 bytes.
`tests/unit/test_sms_gateway.py` pins all three.

Three behaviours worth knowing before changing this code:

- **Refresh tokens are single use.** Presenting one twice means two parties
  hold it, so every session for that user is revoked rather than just that row.
- **A guardian's handoff names the child.** A guardian with three children has
  three possible answers, so `student_id` is required and checked against
  `guardian_student` on every crossing.
- **Sign-in is scoped by the subdomain; everything after it by the token.**
  The login lookup runs with no `school_id` filter, like every other query
  here — the only thing stopping a user signing in through another school's
  subdomain is that the session was pinned first. There is a test for exactly
  that.

## Admissions

Six states, and the two that matter are the ones nobody presses a button for.

```
started -> submitted -> screened -> offered -> accepted -> enrolled
```

**Acceptance is a deposit arriving**, not a decision. There is no accept
endpoint: a bursar records the money, and the application moves itself. So an
unpaid place cannot look accepted in the funnel, and nobody can mark one
accepted by mistake.

**Enrolment is one transaction** that creates a child, a guardian, an enrolment
and a login together. Half of it is a pupil the roll cannot find or a parent
who cannot sign in to pay — and the deposit has already been taken by then.

The public form (`/api/v1/apply/*`) is reached by somebody with no account. A
verified phone number is the whole credential, and the session it buys is
scoped to one application, so none of those routes takes an application id.
The form is four steps and about four minutes: anything longer does not get
finished, and an unfinished application is a pupil the school never hears
about. So the required list is short — a name, a date of birth, a sex, a level
and a guardian who can be reached — and everything else is collected at a desk.

Two behaviours worth knowing:

- **A sibling is not a duplicate.** Screening scores name, date of birth and
  guardian phone rather than matching them, and the commonest high score in a
  real school is one phone with several children. A shared surname and phone
  is noted; only a near-exact match is flagged, and nothing is ever merged
  automatically.
- **An admission number is burnt at the offer**, from a locked per-school
  sequence, because the offer letter carries it. A school importing an
  existing roll has to move that counter past its highest existing number —
  `scripts/seed_demo.py` does exactly this, and shows what an import must do.

## Fees

Four ideas, and all four exist to stop one thing: a number on a bill that
nobody can explain.

**Nothing writes a balance.** `invoice.billed` and `invoice.settled` are
derived by database triggers from the lines and the payments. There is no
endpoint that sets them, and a guard trigger discards a direct assignment, so a
bug in a service cannot mark an invoice paid with money missing. A correction is
a credit note or a payment — never an edit.

**A published structure is frozen.** A draft can be changed; once published,
the items are fixed and next year gets its own structure. A parent who
photographed a bill must find the same figure on it three weeks later. An empty
structure cannot be published either, because a term run against one would bill
every child nothing and look as though it worked.

**The term run previews and repeats.** `dry_run` is true unless asked
otherwise, and the preview names every child it could not bill — almost always
a level whose structure is still a draft. A child who already has an invoice for
the term is skipped, so a run over 500 children that is interrupted by a
timeout, a restart or a closed laptop is recovered by running it again.

```
POST /api/v1/fees/terms/{id}/run   { "dry_run": true }   -> raised, skipped, problems
POST /api/v1/fees/terms/{id}/run   { "dry_run": false }  -> the invoices
```

**Discounts and arrears are their own lines.** A bursary is not a reduced
charge: a parent sees 240,000 tuition and 120,000 off, which is the difference
between a bill they trust and one they ring up about. Arrears carried from last
year land on the first term's bill as a line of their own, or the invoice reads
as a price rise.

Two more things that are easy to get wrong and are tested:

- **Ageing is read against a day you pass in.** `as_of` on the invoice,
  arrears and statement reads defaults to today, so a month-end report produced
  in May for April is read against April. A stored status is how an invoice ends
  up marked paid in the wrong month.
- **Arrears are one row per child**, not per invoice: a bursar chasing arrears
  rings a family, and three overdue bills for one child is one phone call.

### Money, while it is still manual

Phase 1 has no aggregator. A bursar holds an M-Pesa confirmation or a bank slip
and types its receipt, which is the school's actual process. Two things make
that safe enough to be the only path: the entry names its author, and the
receipt is unique per school, so entering the same one twice is caught rather
than doubling a credit.

```
POST /api/v1/payments/invoices/{id}/payments   a named, audited entry
GET  /api/v1/payments/suspense                 money nobody could place
POST /api/v1/payments/{id}/match               a bursar's decision about it
```

Suspense is the dashboard's first "needs your attention" row. Nothing picks
between two siblings with open balances — the wrong guess takes an afternoon to
unpick and a parent's phone call to discover.

Voiding is refused once money is against an invoice: a payment attached to a
bill that officially does not exist is worse than the mistake it was meant to
fix. Write a credit note. A voided invoice stays visible, with its reason, and
counts for nothing.

## Documents

The bytes never pass through this API. A client asks for an upload URL, PUTs
the file straight to storage, then confirms — and the confirm step asks storage
what actually arrived rather than trusting what the client said it would send,
because a presigned PUT cannot enforce a length.

```
POST /students/{id}/documents/upload-url   -> { url, object_key, headers }
PUT  <url>                                  (the client, direct to storage)
POST /students/{id}/documents               -> the record, size and type verified
GET  /students/{id}/documents/{id}/url     -> a 5-minute link to read it
```

`STORAGE_PROVIDER=local` writes to disk and serves through this service's own
`/files` routes, so the admissions flow is runnable before Cloudflare R2
exists. Production sets `STORAGE_PROVIDER=r2` and those routes are not mounted
at all.

### Pointing it at R2

Four settings, from the Cloudflare dashboard: **R2 > Manage R2 API Tokens >
Create API Token**, with **Object Read & Write** scoped to the one bucket.

```
STORAGE_PROVIDER=r2
R2_ACCOUNT_ID=...          # R2 overview, or the endpoint URL
R2_ACCESS_KEY_ID=...       # shown once, when the token is created
R2_SECRET_ACCESS_KEY=...   # shown once, when the token is created
R2_BUCKET=...
```

Then `make storage-check`, which does a real round trip — presign, upload,
HEAD, download, delete — and leaves nothing behind. Worth running once before
a deploy: the unit tests prove our signatures are correct, and only Cloudflare
can say they are accepted.

Two things to get right that are easy to miss:

**Leave the public development URL disabled.** The `pub-<hash>.r2.dev` address
is a read-only public front door, and it cannot accept an upload — presigned
PUTs go to the S3 API endpoint instead. Enabling it would also make every
object world-readable to anyone who has the key, which for children's birth
certificates is not a trade worth making. Reads go through short-lived
presigned links precisely so that opening one is an act with a permission
attached.

**Set a CORS policy on the bucket.** The browser uploads straight to R2, so
without it uploads succeed from `make storage-check` and fail in the app, which
is a confusing hour. On the bucket's settings:

```
AllowedOrigins: the web app's origin
AllowedMethods: PUT, GET
AllowedHeaders: content-type
```

Three properties worth keeping:

- **The bucket is private.** These are children's birth certificates. Reads go
  through a short-lived presigned URL generated when somebody with permission
  opens the document — a public URL is a URL that ends up in a WhatsApp group.
- **The key carries the tenant**, `school/<school_id>/...` first, so a
  lifecycle rule or a mistaken prefix delete cannot cross schools.
- **The server chooses the key.** A client that could choose its own key is a
  client that could overwrite another school's object.

## Layout

```
app/
  api/v1/        endpoints: parse, call one service, map the result to HTTP
  services/      orchestration: transactions and sequence, no SQL, no FastAPI
  repositories/  every SQLAlchemy query in the codebase, behind Protocols
  domain/        pure rules: no session, no clock, no network
  models/        SQLAlchemy tables
  schemas/       Pydantic request and response models
  core/          config, security (RS256 + JWKS), tenancy, logging
  db/            engine and session factory
  integrations/  payments/, sms/, ai_platform/ — adapters behind protocols
  workers/       Celery app and scheduled tasks
tests/
  unit/          rules, no database
  integration/   anything that needs a real PostgreSQL
```

Imports point one way only and `make arch` fails the build when they do not:
endpoints may not import `sqlalchemy`, `app.models` or `app.repositories` at
all. See `ARCHITECTURE.md` and the worked example in `app/api/v1/webhooks.py`.

## Handing types to the web repo

```bash
make openapi          # writes openapi.json
```

Then in the web repo: `npm run types`.
