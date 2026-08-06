# FastClaw Python

FastClaw Python is a clean-room Python runtime for FastClaw. It is maintained
as an independent repository and does not include the Git history of the
original Go implementation.

The initial runtime provides:

- Python 3.12+ packaging with a `src/fastclaw` layout;
- a concurrency-safe runtime lifecycle;
- a typed, runtime-checkable provider protocol;
- a shared HTTPX async client for providers;
- FastAPI `/healthz` and `/readyz` endpoints;
- Pydantic v2 response models; and
- SQLAlchemy 2 async repositories with Alembic migrations;
- isolated, read-only Go SQLite import;
- a cancellable single-agent ReAct loop with policy-scoped tools and SSE v2 events;
- a bounded, tenant-safe in-process MessageBus/TaskQueue for multi-Agent delegation; and
- pytest, Ruff, mypy, and GitHub Actions checks.

## Development

Create a Python 3.12+ virtual environment and install the development extras:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,server]"
```

Run the checks used by CI:

```bash
ruff check .
ruff format --check .
mypy
pytest
```

Build the attributed Web snapshot and start the development server on the
Python migration port:

```bash
cd web
pnpm install --frozen-lockfile
pnpm build
cd ..

mkdir -p ~/.fastclaw-python
FASTCLAW_DATABASE_URL="sqlite+aiosqlite:///$HOME/.fastclaw-python/fastclaw.db" \
FASTCLAW_PORT=18954 \
uvicorn fastclaw.app:app --host 127.0.0.1 --port 18954
```

Open `http://127.0.0.1:18954/`. On a new database the Web UI redirects to
`/onboard/`, where the first administrator, Provider, default model, and Agent
are created atomically. Health and readiness probes remain available at
`/healthz` and `/readyz`.

Provider credentials are supplied centrally by environment variables. The
database stores only non-sensitive endpoint, model, and scope configuration:

```bash
FASTCLAW_PROVIDER_DEEPSEEK_API_KEY=... \
FASTCLAW_PROVIDER_OPENROUTER_API_KEY=... \
ODDS_API_KEY=... \
uvicorn fastclaw.app:app --host 127.0.0.1 --port 18954
```

Use `fastclaw providers check --database-url ...` to report missing variables
without printing their values. `/readyz` remains unavailable until every
required Agent Provider and prepared Skill environment is usable.

Cookie sessions are used by the Web UI. Programmatic clients can use a
SHA-256-backed `Bearer fc_...` API key created under `/apikeys`; agent-type keys
can access only their explicit Agent ACL. Provider create/update APIs reject
plaintext credentials and identify the required environment variable.

## Implementing a provider

A provider has a stable name, asynchronous lifecycle methods, and complete and
streaming chat operations. Provider requests and results use Pydantic models so
OpenAI-compatible and Anthropic adapters preserve the same runtime contract:

```python
import httpx

from fastclaw.providers import ChatRequest, ChatResponse, ProviderStream


class ExampleProvider:
    name = "example"

    async def start(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def ready(self) -> bool:
        return True

    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    def stream(self, request: ChatRequest) -> ProviderStream: ...

    async def stop(self) -> None:
        pass
```

Register providers before starting the runtime:

```python
from fastclaw import Runtime, create_app

runtime = Runtime((ExampleProvider(),))
app = create_app(runtime)
```

Providers start in registration order and stop in reverse order. If one fails
during startup, previously started providers are rolled back automatically.

## Database and Go import

FastClaw Python uses its own database. Never point it at the Go runtime's
SQLite file. Create or upgrade a Python database with Alembic:

```bash
FASTCLAW_DATABASE_URL=sqlite+aiosqlite:////Users/you/.fastclaw-python/fastclaw.db \
  alembic -c alembic.ini upgrade head
```

Preview a one-way import without writing a target:

```bash
fastclaw migrate import-go \
  --source ~/.fastclaw/fastclaw.db \
  --target sqlite+aiosqlite:////Users/you/.fastclaw-python/fastclaw.db \
  --dry-run
```

Remove `--dry-run` after checking the redacted report. The source is opened
read-only and verified by SHA-256 before and after import. Repeating an import
of the same source snapshot is a no-op. Go web sessions and plaintext secrets
are deliberately not imported. Password and API-key hashes remain compatible,
but browser sessions must log in again and Provider/channel credentials must be
supplied centrally.

After the database import, preview and import only assets owned by valid target
Agents:

```bash
fastclaw migrate import-assets \
  --source-root ~/.fastclaw \
  --target-root ~/.fastclaw-python \
  --database-url sqlite+aiosqlite:////Users/you/.fastclaw-python/fastclaw.db \
  --dry-run
```

Prepare declared Skill dependencies explicitly with
`fastclaw skills prepare --data-root ~/.fastclaw-python`; the Runtime never
installs packages while handling a chat request.

See [the phase 2 migration record](docs/migration/phase-2-data-identity.md) for
the schema mapping, validation commands, and rollback boundary.

## Single-agent runtime

`fastclaw.agent.AgentRunner` normalizes imported session history, streams one
provider request per round, executes registered tools under a trusted execution
context, and commits the final history only after a complete assistant reply.
Closing its `AgentStream` immediately closes the active provider stream and
does not persist partial assistant output.

Built-in tools provide workspace-confined file reads, allow-listed execution
without a shell, and bounded HTTP(S) fetches. Tool policy denials, malformed
arguments, timeouts, and exceptions are returned as visible tool results.

See [the phase 3 migration record](docs/migration/phase-3-single-agent.md).

## Multi-Agent orchestration

`fastclaw.orchestration.InProcessMessageBus` routes correlated delegation
requests through a bounded `AsyncTaskQueue`. Work is FIFO per target Agent,
parallel across targets up to the configured global limit, and deduplicated by
exact target/task within one root execution. Nested delegation inherits its
root execution slot, so `max_concurrent=1` cannot deadlock.

Tenant ownership, call-path and wait-graph cycles, backpressure, cancellation,
and shutdown completion are enforced before results reach an Agent. The
`spawn_subagent` tool accepts only target and task arguments; tenant identity is
always taken from the trusted execution context.

See [the phase 4 migration record](docs/migration/phase-4-multi-agent.md).

## Web snapshot

The Next.js application under `web/` is a source-attributed snapshot of the Go
project at the locked compatibility baseline. Its Git history was not copied.
See [web/SOURCE.md](web/SOURCE.md) for the exact source commit and license.

## License

See [LICENSE](LICENSE).
