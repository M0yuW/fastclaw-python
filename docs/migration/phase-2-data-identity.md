# Phase 2: Data and trusted identity

## Contract

This phase introduces SQLAlchemy 2 async repositories and an Alembic baseline
for users, agents, sessions, configurations, API-key ACLs, agent files, and cron
jobs. SQLite connections enable foreign keys, WAL, and a 5-second busy timeout.
The model is PostgreSQL-ready through the same repository interfaces and the
`asyncpg` driver.

Request identity is held in a `contextvars` context and is not accepted from
model-generated arguments. API keys retain Go's SHA-256 lookup format and
password verification retains bcrypt compatibility. `actAs` identities are
explicitly read-only.

## Import boundary

`fastclaw migrate import-go` opens the Go SQLite database with `mode=ro` and
imports into a different target URL. It records the source SHA-256 and refuses
to import into a populated target without a matching import record. A repeated
import of the same snapshot returns the stored report without duplicating rows.

The report includes source and target row counts, foreign-key results, and
SHA-256 values for agent files. It never includes credential values. Existing
web sessions are counted but skipped, and secret-looking configuration values
are blanked so credentials can be rotated and entered again.

Both the current `user_id`/`agent_id` configuration ownership schema and the
legacy `scope`/`scope_id` representation are accepted. Current routing,
chatter, public-agent, and API-key type fields are preserved.

## Validation

```bash
ruff check .
ruff format --check .
mypy
pytest
FASTCLAW_DATABASE_URL=sqlite+aiosqlite:////tmp/fastclaw-python.db \
  alembic -c alembic.ini upgrade head
```

Automated tests verify SQLite pragmas, repository transactions, bcrypt and API
key compatibility, trusted identity scoping, source immutability, secret
redaction, file hashes, row counts, foreign keys, and repeat-import behavior.

## Rollback

The Go database is never modified. Stop the Python runtime and restore the Go
runtime on its original port. Keep the Python database as an audit artifact;
do not write it back to Go.
