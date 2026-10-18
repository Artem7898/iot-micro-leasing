# Audit and modernization — October 18, 2026

Language: **English** | [Русский](AUDIT_RU.md) · [Project guide](README.md) ·
[Database migrations](migrations/README.md)

The review covered the API, models and transactions, Celery/PDF, Django/admin,
configuration, dependencies, migrations, documentation, and tests. This report
records local validation of version 2.0.0; publication of the cloud environment
and restoration in a fresh task have not been established. It combines source
review with local functional and concurrency checks. It is not an external
penetration test or proof that every possible defect has been eliminated.

## Findings and fixes

| Priority | Original defect | Result |
|---|---|---|
| P0 | The API accepted events and exposed balances without authentication; the security module was empty | A shared `X-API-Key` dependency, constant-time comparison, and access denied when configuration is missing; authentication can be bypassed only explicitly in development/test |
| P0 | Git contained a SQLite database with an active superuser and a usable password hash; the stored session that was found had expired | The database was removed from the working tree and added to ignore rules; historical credentials are not reused |
| P1 | `get_db_session` always raised `NotImplementedError` | A working engine, bounded pools, timeouts, rollback, and guaranteed Session closure |
| P1 | Repeated requests charged the same debt again | A durable receipt with a fingerprint and a unique Idempotency-Key; identical requests replay the original response, while a different payload returns 409 |
| P1 | Read-modify-write lost charges; concurrent session starts could create multiple active sessions | The device is locked before reading its state; a single transaction, a partial unique index, and database constraints protect money values |
| P1 | Negative consumption could be committed before response serialization failed | A typed, bounded payload rejects negative, nonnumeric, and nonfinite values; the resulting debt is checked before commit |
| P1 | Invoices were sent before commit; broker failures cancelled session closure | A transactional outbox, publication after commit, and a bounded dispatcher using a lease and compare-and-swap (CAS) |
| P1 | A publication acknowledgment did not mean the PDF had been created successfully; an invoice could be lost after retries were exhausted | A separate `completed_at`, acknowledgment after an atomic write, and recovery of incomplete deliveries after 10 minutes |
| P1 | Settings contained embedded development secrets; production could inherit DEBUG | The duplicate configuration project was removed; production requires PostgreSQL, authentication, strong secrets, explicit hosts, and secure HTTPS/cookie settings |
| P1 | The admin allowed changes despite its declared read-only mode | Add/change/delete are disabled for financial models; only authorized staff can view them |
| P1 | Django and SQLModel disagreed on created_at, UUIDs, and schema ownership | A shared database, string identifiers, and aligned models; Alembic owns the financial schema and Django owns its own schema |
| P2 | Synchronous SQL/Celery calls ran in async handlers | Synchronous endpoints/services run in FastAPI's thread pool; database and broker pools are bounded |
| P2 | Naive timestamps caused exceptions; the payload had no JSON size limit | AwareDatetime, time-window/order validation, a typed payload, and a 16 KiB ASGI body limit, including chunked bodies |
| P2 | Two unrelated rate limiters existed; the endpoint used memory instead of Redis | One Redis-backed limiter and middleware, bounded connections, and no silent fallback; exceeding the limit returns 429 and Redis failure safely returns 503 |
| P2 | `/health` always claimed Redis was connected | `/health` reports liveness; `/ready` actually checks Redis, the database connection, and the current schema |
| P2 | The dashboard created Redis clients without explicit cleanup/timeouts and showed exceptions to users | A bounded pool, connect/read timeouts, close/disconnect in finally, diagnostic logs, and generic interface errors |
| P2 | Celery/ReportLab were undeclared and dependencies were outdated | Complete runtime/development dependencies, Python 3.14.8, Django 5.2 LTS, and a frozen `uv.lock` |
| P2 | Direct PDF creation allowed partial files, unsafe paths, and unbounded worker resources | A UUID-only basename, a private temporary file, fsync and atomic replacement, temporary-file cleanup, and prefetch/concurrency/recycling/time limits |
| P2 | Decimal values were converted to float | The API returns money as strings with four decimal places |
| P2 | The Makefile could not be parsed; the README referenced a missing environment example and incorrect directories | Corrected commands, `.env.example`, a migration guide, and SQLite/PostgreSQL CI configuration |

## Memory and performance

The audit did not establish a confirmed Python heap leak. It addressed specific
risks: unbounded waits for Redis/the broker, missing explicit resource cleanup,
event-loop blocking, and partial PDF writes. Tests check that connections return
to the pool on successful and failing paths, concurrent PDF generation, and
removal of temporary files when save/fsync/replace fails. These checks do not
replace a long production soak test that observes RSS under a representative
workload.

The dispatcher fetches no more than 100 records, the dashboard materializes no
more than 10 sessions, and JSON is limited to 16 KiB; money arithmetic uses
Decimal. Pools and concurrency have explicit upper bounds. Locking a single
device preserves financial ordering; different devices do not require a shared
lock on PostgreSQL. SQLite serializes writers and is intended for development.

No numerical speedup over the original version is claimed: the original API had
no working database dependency, so no comparable benchmark was available. Exact
aggregates and dashboard sorting on a large database will require summary
tables/caching. Financial PDFs, the ledger/outbox, and logs consume disk space:
retention, archiving, and monitoring remain the operator's responsibility.
Deleting ledger records reduces replay-protection guarantees, so automatic
cleanup is not enabled.

## Validation

The cloud machine used CPython 3.14.8, SQLite and a real PostgreSQL 18 instance,
Redis 7, and separate Uvicorn/Django/Celery worker and beat processes.

- `pytest`: **170 passed**, with no skips, including PostgreSQL concurrency and migrations.
- Separate Django test runner: **23 passed**.
- API coverage, including branches: **95%**. Coverage does not prove that every edge case is covered.
- Ruff lint/format, `git diff --check`, Django system checks, and migration consistency passed.
- Alembic fresh/legacy migrations, parity, reruns, and rejection of unsafe data
  passed. The original SQLite database was inspected only through a private
  temporary copy; financial and Django record counts were preserved.
- Celery does not initialize Django through the global `DJANGO_SETTINGS_MODULE`;
  a separate subprocess with an unrelated configuration verified this behavior.
- A real Redis rate-limit smoke test returned `200, 200, 429` at a limit of
  2/minute; liveness remained available.
- A real HTTP smoke test covered readiness/OpenAPI/docs, Django login/static
  assets, and start → charge → identical replay → balance → close. The worker
  completed the outbox delivery and the PDF was checked. Synthetic financial
  records were removed afterward.
- `pip-audit`: **0 known vulnerabilities** in 89 external dependencies as of the
  audit date. The project itself is not published on PyPI and was reviewed from
  source rather than through an advisory database.

Coverage includes concurrent charges/starts/replays, key conflicts, commit
failures, broker/disk failures, lease recovery, redelivery, invalid/boundary
amounts and timestamps, inactive/unknown devices, incorrect keys, safe errors,
authorization and read-only admin, unavailable Redis/databases, and legacy UUID
migration.

Upstream warnings remain: Starlette recommends `httpx2` for its TestClient;
SlowAPI uses deprecated `asyncio.iscoroutinefunction`, whose removal is expected
in Python 3.16. These checks pass on Python 3.14; the warnings are not suppressed.

## Requirements before production

1. If the superuser password from the historical SQLite database or embedded
   Django secrets were used in a real environment, rotate them and invalidate
   affected sessions. Removing files does not remove Git history. The values
   themselves were not copied into this report or the logs.
2. The API key grants access to a trusted gateway within one organization.
   Per-device/per-tenant authorization, mTLS, key management, and provisioning
   are not implemented; do not give the shared key to untrusted devices.
3. HTTPS, private Redis/database access, backups, outbox/storage monitoring,
   retention, and a production process supervisor are required. Local
   development servers are not a production deployment.
4. Migrating an existing database requires a verified backup and stopped
   writers. See the [migration guide](migrations/README.md). An unknown or unsafe
   schema causes migration to fail rather than delete or rewrite debt.

Configuring the current machine and saving a configuration draft do not establish
publication of the cloud environment or verified restoration in a fresh task.
