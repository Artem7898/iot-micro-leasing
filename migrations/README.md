# Application database migrations

Language: **English** | [Русский](README_RU.md) ·
[Project guide](../README.md) · [Audit report](../AUDIT.md)

Alembic owns `device`, `usagesession`, `usageevent`, and `invoiceoutbox`.
Django owns its authentication, sessions, and admin tables; its billing models
are unmanaged, read-only projections. Run commands from the repository root in
the existing checkout. Cloud tasks already have isolated workspaces; do not
create another Git worktree for setup.

## New development database

Use Python 3.14 and the committed lockfile:

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev alembic upgrade head
uv run --frozen --extra dev python admin_panel/manage.py migrate --noinput
uv run --frozen --extra dev alembic check
```

The application and Django read `DATABASE_URL` from the same environment/root
`.env`. The default database is the local state directory, outside tracked
application files. PostgreSQL URLs use `postgresql+psycopg://`; SQLite uses
`sqlite:///relative/path.sqlite3` or `sqlite:////absolute/path.sqlite3`.
Supply credentials through environment settings; never commit them or put them
in `alembic.ini`. Production requires the other application configuration
requirements too, including an API key. `AUTO_CREATE_SCHEMA` must remain false
in production: `create_all()` cannot evolve existing tables.

The initial revision contains explicit DDL. It does not call application
`metadata.create_all()`. `alembic upgrade head --sql` produces fresh-database SQL;
legacy inspection/adoption requires a live connection.

## Existing deployment: back up, inspect, then migrate

1. Back up the **actual application database** and verify that the backup can be
   restored. The previously committed `admin_panel/db.sqlite3` is an exposed
   development artifact, not a deployment database or safe credential source.
   Rotate any credentials that originated in it. Never run migrations on the
   tracked file as part of setup; use a separate restored copy for rehearsal.
2. Stop API processes, invoice workers, schedulers, and any other database
   writers. Point `DATABASE_URL` at a restored rehearsal copy first. Confirm that
   FastAPI and Django will use the same target database after deployment.
3. Run `alembic current`, then `alembic upgrade head`. An unversioned database
   containing exactly the recognized old `device` and `usagesession` application
   tables is adopted automatically. Unrelated Django auth/admin tables remain
   untouched. No `alembic stamp` is needed.
4. Check session counts, the sum of existing `total_cost`, and representative
   device/session IDs against the backup. Run `alembic check`, then Django's
   `migrate --noinput` and `check`. Rehearse billing and dashboard reads before
   repeating the procedure on the backed-up deployment database.

The migration preserves every session, debt amount, activity flag, and existing
creation timestamp. It adds missing `created_at` values using the single UTC
instant at which migration starts; these values describe migration time, not
reconstructed historical event time. Missing `last_ping_at` remains null. Legacy
naive PostgreSQL timestamps are interpreted as UTC, consistent with the old
`USE_TZ=True` configuration. Verify this assumption against deployment history.

Session IDs become strings of up to 36 characters. PostgreSQL native UUID values
are cast to their canonical textual representation. SQLite's existing 32-digit
UUID strings are preserved exactly, as are existing hyphenated strings. Django's
new `CharField` projection supports both. Review integrations that assumed the
old database-native UUID type. The old Django-generated single-column session
device index is replaced by the new `(device_id, created_at)` index, which retains
that lookup prefix. New ledger and invoice-outbox tables begin empty:
the migration cannot reconstruct historical idempotency keys and does not
generate or resend historical invoices.

Migration refuses to proceed if there are duplicate active sessions for a
device, negative/oversized/overprecision money, missing required values, invalid
session UUIDs, orphan sessions, custom check constraints, unexpected foreign-key
dependencies, partial application tables, or an unknown schema. Preserve the
backup and reconcile the specific data/schema issue with a separate reviewed
change; retry afterward. Do not resolve duplicate sessions by deleting rows or
dropping their debt. Do not `stamp head` to bypass validation. A database created
earlier through modern `create_all()` also requires an explicit reviewed
baseline procedure; it is deliberately not guessed to match this revision.

SQLite table rebuilds run with a real transaction and foreign-key consistency
checks before/after. PostgreSQL uses transactional DDL. Invalid legacy data is
checked before application tables are changed. Migrate with writers stopped;
validation is not a substitute for a maintenance window.

## Rollback and future changes

The initial revision intentionally refuses automatic downgrade: deleting the
ledger/outbox would destroy billing and replay-protection records. Restore the
tested backup or prepare a reviewed forward/data-preserving migration. Keep
application code and database schema deployment versions together.

For future schema changes, create and review an explicit revision:

```bash
uv run --frozen --extra dev alembic revision --autogenerate -m "describe change"
uv run --frozen --extra dev alembic upgrade head
uv run --frozen --extra dev alembic check
```

Autogeneration is a draft: review data preservation, locking, backfills, SQLite
compatibility, and both upgrade/rollback behavior. Existing Django-owned tables
must not be removed from a generated migration.
