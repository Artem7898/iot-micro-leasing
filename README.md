# IoT Micro-Leasing 2

**English (primary)** | [Русский](README_RU.md)

A platform for tracking IoT equipment usage, with FastAPI, transactional billing,
a Django back office, and PDF generation through Celery. Python **3.14**, Django
**5.2 LTS**, exact `Decimal` amounts, and reproducible dependencies in `uv.lock`.

## Development

Use the existing checkout. Each cloud task already runs in an isolated
environment; setup does not require an additional Git worktree.

```bash
uv sync --frozen --extra dev
cp .env.example .env  # Only if you do not already have your own .env.
# Redis is required for rate limiting and background tasks:
docker compose up -d redis
uv run --frozen alembic upgrade head
uv run --frozen python admin_panel/manage.py migrate --noinput
```

SQLite stores data in `.local/state/dev.sqlite3`. FastAPI and Django share the same
`DATABASE_URL`; set an absolute `IOT_STATE_DIR` if you run commands from different
directories. PostgreSQL is supported through `postgresql+psycopg://`. The
PostgreSQL container is optional: supply `POSTGRES_PASSWORD` through a secure
environment before starting it. PostgreSQL will not initialize with an empty
password; starting Redis alone does not require a PostgreSQL password.

Run these commands in separate terminals from the project root:

```bash
make run-api
make run-admin
make run-worker
make run-beat
```

The API listens on port 8000 and Django on port 8001; both bind to loopback. API
documentation is at `/api/docs`, back office login at `/admin/`, and the
operational dashboard at `/admin/redis-metrics/`. Create your own login account
with `uv run --frozen python admin_panel/manage.py createsuperuser`. Passwords do
not belong in the repository or environment setup instructions. `runserver` is
for development.

`.env.example` explicitly disables API authentication **for local development
only**. By default, the code requires `X-API-Key`; a missing configured `API_KEY`
results in 503, and an incorrect key results in 401. Production requires
`ENVIRONMENT=production`, PostgreSQL, `API_AUTH_ENABLED=true`, a securely stored
`API_KEY` of at least 32 characters, `DJANGO_DEBUG=false`, a strong
`DJANGO_SECRET_KEY`, and explicit `DJANGO_ALLOWED_HOSTS`. Expose the application
through an HTTPS reverse proxy and a production WSGI/ASGI server; Redis and the
database must not be accessible from the Internet. The API key is intended for a
trusted gateway within one organization: user/tenant authorization and device
mTLS require separate design work. Do not share the key with untrusted clients.

## API behavior

`POST /api/v1/usage/ping` requires a unique `Idempotency-Key` (1–128 printable ASCII
characters). For retries, send **the same key and the same payload**, including
the original timestamp. The response is stored in the database: a retry does not
increase the amount owed; reusing a key for a different event returns 409. Keys
are global within a platform instance.

```json
{
  "device_id": "dev_printer01",
  "event_type": "usage_tick",
  "timestamp": "2026-10-18T12:00:00+00:00",
  "payload": {"units_consumed": "10.0000"}
}
```

First register the device through a trusted provisioning process and send
`session_start`; finish its operation with `session_end`. The dashboard
intentionally does not modify financial records. Events must include a time zone,
be no more than 300 seconds old, and must not be in the future. Events within a
session must have monotonically nondecreasing timestamps; equal timestamps are
allowed for different events. `usage_tick` requires positive consumption; other
events allow zero. Consumption is limited to 1e9 and four decimal places; JSON
bodies are limited to 16 KiB. Extra fields, negative numbers, booleans, and
NaN/Infinity are rejected. The amount owed for a session is limited to
`99999999.9999`, with half-even rounding to four decimal places. Monetary
responses are **strings**, such as `"5.0000"`, without conversion to float.

Opening a second active session, a key conflict, an out-of-order event, or an
amount overflow returns 409; an unknown device returns 404, a disabled device
403, and invalid input 422. For a temporary database error (503), retry the
request with the original key. Error responses do not expose the original
payload or internal exception messages. `X-Request-ID` links each request to
structured logs.

## Architecture and reliability

- Synchronous SQLAlchemy/SQLModel sessions run in the FastAPI thread pool and are
  always closed. Each transaction locks the device row before reading or updating
  the balance; SQLite serializes writes. A unique partial index permits only one
  active session per device. The database also enforces financial constraints.
- Closing a session, storing a replay receipt, and inserting an invoice outbox
  entry are committed together. Dispatch to Redis occurs after commit. The worker
  writes the PDF atomically, then sets `completed_at`; unfinished deliveries are
  retried after 10 minutes. Celery beat is required for recovery after failures.
  Delivery is at least once; the PDF handler supports retries and concurrent
  execution.
- Alembic owns the financial schema; Django owns user and admin tables. Financial
  Django models are unmanaged and read-only. For existing databases, follow the
  [migration instructions](migrations/README.md), first on a restored copy.
- The shared Redis-backed rate limiter does not silently fall back to in-memory
  storage when Redis fails. `/health` checks process liveness; `/ready` checks
  Redis, database connectivity, and the required tables and columns.
- The dashboard shows actual amounts, sessions, devices, and Redis metrics. It
  limits results to the 10 most recent sessions, closes Redis connections, and
  uses timeouts. Service errors are displayed without internal details. At large
  volumes, move exact aggregates to summary tables or a cache.

PDFs, logs, and the idempotency ledger need a retention policy and disk monitoring.
Automatic deletion of financial data is intentionally disabled: the operator must
define the retention period and guaranteed protection window against duplicate
requests.

## Validation

```bash
make lint
make run-tests
uv run --frozen --extra dev pytest --cov=api --cov-report=term-missing
uv run --frozen --extra dev pip-audit --local
uv run --frozen alembic check
uv run --frozen python admin_panel/manage.py makemigrations --check --dry-run
```

Unit tests isolate Redis and use temporary databases. For real PostgreSQL
concurrency and migration checks, set `TEST_POSTGRES_URL`; each test uses its own
temporary schema. Without this variable, only the PostgreSQL checks are skipped.
GitHub Actions tests both databases.

For discovered defects, fixes, and validation limits, see the
[audit report](AUDIT.md).

## Branch protection

The default branch is `master`. The policy in
[.github/branch-protection.json](.github/branch-protection.json) requires pull
requests, passing `python` and `postgres` checks on an up-to-date branch, resolved
conversations, and linear history. It blocks force pushes and deletion and also
applies to administrators. A second reviewer's approval is optional, so the
repository can be maintained by one person.

With GitHub API access and repository administration permission, run:

```bash
bash .github/protect-master.sh
```

The helper checks GitHub first, preserves existing protection or rulesets, and
verifies a newly applied policy. Committing the policy file alone does not enable
branch protection. The environment must allow `api.github.com`; do not put tokens
in tracked files or shell command arguments.
