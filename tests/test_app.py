from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from fastclaw.app import create_app
from fastclaw.runtime import Runtime, RuntimeState


class ProbeProvider:
    name = "probe"

    def __init__(self, *, ready: bool) -> None:
        self.ready_value = ready
        self.started = False

    async def start(self, client: httpx.AsyncClient) -> None:
        self.started = not client.is_closed

    async def stop(self) -> None:
        self.started = False

    async def ready(self) -> bool:
        return self.started and self.ready_value


@asynccontextmanager
async def app_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_healthz_and_readyz_when_runtime_is_ready() -> None:
    runtime = Runtime((ProbeProvider(ready=True),))
    app = create_app(runtime)

    async with app_client(app) as client:
        health = await client.get("/healthz")
        readiness = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "state": "running"}
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ready",
        "state": "running",
        "providers": {"probe": True},
    }
    assert runtime.state is RuntimeState.STOPPED


async def test_readyz_returns_503_when_a_provider_is_not_ready() -> None:
    app = create_app(Runtime((ProbeProvider(ready=False),)))

    async with app_client(app) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "state": "running",
        "providers": {"probe": False},
    }
