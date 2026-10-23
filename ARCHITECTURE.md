# How this codebase is layered

Four layers. Imports point downward only, and `make arch` fails the build when
they do not.

```
app/api/v1/        HTTP.  Parse, delegate, map the answer to a status code.
       ↓
app/services/      Orchestration.  Transactions, the order things happen in.
       ↓
app/repositories/  Persistence.  Every SQLAlchemy query in the codebase.
       ↓
app/models/        Tables.

app/domain/        Pure rules.  Imported by services; imports nothing of ours.
```

## What belongs where

**`app/api/v1/*.py` — endpoints.** An endpoint validates its input, calls one
service method, and turns the result or the exception into a response. It holds
no `if` statement that encodes a school rule, opens no transaction, and writes
no query. If you are reading an endpoint and cannot tell what the feature does,
that is correct: the endpoint is not where the feature lives.

The contract is machine-checked. `app.api.v1` may not import `sqlalchemy`,
`app.models`, `app.repositories` or `app.db` at all.

**`app/services/*.py` — the business layer.** Services own the sequence: look
things up, ask the domain what should happen, tell the repository to do it,
commit. They raise their own exceptions (`PaymentRejected`), never
`HTTPException` — a service does not know it is being called over HTTP, which is
what lets the same service back a Celery task and a CLI script unchanged.

**`app/repositories/*.py` — persistence.** The only place `select()` appears.
Each repository exposes a Protocol describing what its callers need, so a
service test hands in a fake and runs in milliseconds.

**`app/domain/*.py` — rules, pure.** No session, no clock, no network, no
config. Functions take facts and return decisions. This is where the rules that
are expensive to get wrong live — callback idempotency, promotion eligibility,
invoice arithmetic — because here they are cheap to test exhaustively.

## The worked example

Follow one request end to end before writing a new feature:

1. `app/api/v1/webhooks.py` — 30 lines, no rules
2. `app/services/payments.py` — orchestration, no SQL, no FastAPI
3. `app/domain/payments.py` — `decide()`, the actual rules, pure
4. `app/repositories/payments.py` — the queries, behind a Protocol
5. `tests/unit/test_payment_rules.py` — the rules tested with no database

## Checking it

```bash
make arch     # contracts only
make lint     # ruff + mypy + contracts
```

In CI, `make arch` runs on every pull request. When a contract fails it names
the exact import chain that broke it.

## When a contract gets in the way

That is usually the contract working. The two legitimate escapes:

- An endpoint needs data from two services: add a service method that composes
  them. Do not call two repositories from the route.
- A service genuinely needs a raw query for performance: put the query in a
  repository method with a name that says what it answers, not how.

Loosening `.importlinter` is a decision for a pull request with a reason in it,
not a local fix.
