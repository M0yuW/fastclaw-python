# Phase 9: release hardening, differential smoke, and cutover

## Release artifacts

CI now builds and verifies wheel and source distributions, builds a Linux
container, runs PostgreSQL 17 integration tests, audits installed dependencies,
scans repository history for secrets, and keeps the existing Python 3.12–3.14,
Alembic, Web build, and Playwright gates.

Distribution verification is structural, not just metadata-based. Hatch
explicitly excludes local Web build/dependency trees, and
`scripts/verify_distribution.py` enforces size limits, required bundled
Alembic/plugin/cutover files, and the absence of `node_modules`, `.next`,
`out`, or test-report caches. This was added after a local build proved that
`twine check` would accept a 105 MiB sdist containing 21,901 generated files;
the corrected artifact is about 674 KiB with 220 source files.

The container is a multi-stage Python 3.12/Node 22 build. The final image runs
as UID/GID 10001, stores mutable data only in `/data`, embeds the attributed
Web export and finance plugin in read-only application layers, and exposes only
port 18954. Operators may run the application filesystem read-only:

```bash
docker build -t fastclaw-python:local .
docker run --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -v fastclaw-python-data:/data \
  -e FASTCLAW_PROVIDER_DEEPSEEK_API_KEY \
  -e FASTCLAW_PROVIDER_OPENROUTER_API_KEY \
  -e ODDS_API_KEY \
  -p 18954:18954 fastclaw-python:local
```

## Observability

Set `FASTCLAW_LOG_FORMAT=json` for JSON service logs. Each record includes the
request correlation ID and, during Agent execution, trusted user, Agent,
Session, root execution, and call path fields. The Gateway accepts only bounded
safe correlation IDs and returns the effective value in `X-Correlation-ID`.
Structured exception records expose the exception class rather than adding a
traceback or exception message to the JSON payload. Model-visible tool and
delegation errors continue to use stable sanitized categories.

## Differential workflow

The manual `Go to Python differential smoke` workflow targets an operator-owned
self-hosted runner where Go listens on 18953 and Python listens on 18954. The
two processes must use independent databases and data roots. Runtime-specific
bearer tokens are supplied as GitHub secrets and are never written to the
fixture or report.

The harness compares status codes, recursive JSON shapes, SSE v2 event order,
event fields, monotonic sequence numbers, terminal `done`, and ToolCall/
ToolResult pairing. It deliberately ignores dynamic IDs and content unless a
locked fixture adds `equalPaths` semantic assertions. A task fixture may set
`requireTerminalTasks` to reject queued/running work before cutover. The report
is uploaded as a workflow artifact.

The comparison logic is covered by unit tests built on `httpx.MockTransport`.
Those tests do not launch either implementation, and the locked smoke fixture is
loaded only by `scripts/differential_smoke.py`. No real Go/Python dual-service
run has been performed yet. A successful run against independently persisted
Go 18953 and Python 18954 services, with its report retained and reviewed, is a
hard cutover prerequisite.

Local invocation:

```bash
FASTCLAW_DIFFERENTIAL_GO_TOKEN=... \
FASTCLAW_DIFFERENTIAL_PYTHON_TOKEN=... \
python scripts/differential_smoke.py \
  --go-base http://127.0.0.1:18953 \
  --python-base http://127.0.0.1:18954 \
  --output differential-report.json
```

## Cutover and rollback

1. Rotate every credential that appeared in a handoff document.
2. Stop Go and back up its database/WAL/SHM, workspaces, and skills.
3. Run database and asset imports first in dry-run mode, then into the separate
   Python data root.
4. Configure DeepSeek, OpenRouter, and ODDS only through environment/secrets.
5. Run finance, World Cup, and benchmark smoke on Python 18954, then the
   differential workflow.
6. Confirm no pending task, duplicate completion, cross-tenant access, or
   unmatched ToolCall history remains.
7. Move Python to 18953 while retaining the stopped Go binary and source data.

Rollback stops Python and restores Go on 18953. Python data is retained as an
audit backup and is never written back into Go.
