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
cohorts sit the PSLE in 2027. `level.order_index` and a per-cohort
`exit_level_id` on the academic year decide what comes next, and a promotion run
is reviewable and reversible.

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
