# Phase 2: Data and trusted identity

## Contract

This phase introduces SQLAlchemy 2 async repositories for users, agents,
sessions, configurations, API-key ACLs, agent files, and cron jobs. SQLite
connections enable foreign keys, WAL, and a 5-second busy timeout.
The model is PostgreSQL-ready through the same repository interfaces and the
`asyncpg` driver.

Request identity is held in a `contextvars` context and is not accepted from
model-generated arguments. API keys retain Go's SHA-256 lookup format and
password verification retains bcrypt compatibility. `actAs` identities are
explicitly read-only.

## Import boundary

Stop the Go runtime before running `fastclaw migrate import-go`. The importer
copies the database and any `-wal`/`-shm` sidecars into a temporary directory,
verifies that source manifest did not change, and uses SQLite's backup API on
the staged copy. It never opens the source with `immutable=1`, never writes the
source, and imports only into a different target URL.

The report includes source and target row counts, foreign-key results, and
SHA-256 values for agent files. It never includes credential values. Existing
web sessions are counted but skipped. Configuration is migrated through a
kind/namespace-specific allowlist; unknown fields are cleared rather than
guessed. Migrated channels have tokens and `credential_key` cleared, and the
report lists their IDs under `channels_require_reconfiguration`.

The target preserves Go's `(kind, scope, scope_id, name)` identity. Legacy
`user_id`/`agent_id` rows are accepted only as an inbound compatibility shape.
Current routing, chatter, public-agent, and API-key type fields are preserved.

## Validation

```bash
ruff check .
ruff format --check .
mypy
pytest
```

Automated tests verify SQLite pragmas, repository transactions, bcrypt and API
key compatibility, trusted identity scoping, source immutability, secret
redaction, file hashes, row counts, foreign keys, and repeat-import behavior.

## Rollback

The Go database is never modified. Stop the Python runtime and restore the Go
runtime on its original port. Keep the Python database as an audit artifact;
do not write it back to Go.
