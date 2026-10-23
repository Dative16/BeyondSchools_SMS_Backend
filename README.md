# BeyondSchools SMS — API

FastAPI service for school operations. Owns people, money and the calendar.
Lessons, tutoring and attempts stay in the AI platform; the two are joined by a
token and a webhook.

## Running it

```bash
cp .env.example .env
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

**2. A balance moves only on a verified callback.** `PaymentProvider`
(`app/integrations/payments/base.py`) is the only way money enters the ledger,
plus an explicit audited manual entry. `external_ref` is unique per school, so a
replayed webhook credits once.

**3. The level ladder is data, not code.** Tanzania is mid-reform: two primary
cohorts sit the PSLE in 2027. `level.order_index` and a per-cohort
`exit_level_id` on the academic year decide what comes next, and a promotion run
is reviewable and reversible.

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
