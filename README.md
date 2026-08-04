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

Start the development server:

```bash
uvicorn fastclaw.app:app --reload
```

Then inspect `http://127.0.0.1:8000/healthz` and
`http://127.0.0.1:8000/readyz`.

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

## License

See [LICENSE](LICENSE).
