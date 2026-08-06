from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from fastclaw.app import create_app
from fastclaw.gateway import GatewaySettings
from fastclaw.providers import ChatRequest, ChatResponse, ProviderEvent, ProviderEventType
from fastclaw.providers.stream import ProviderStream
from fastclaw.runtime import Runtime, RuntimeState
from fastclaw.storage import Database


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

    async def chat(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse()

    def stream(self, request: ChatRequest) -> ProviderStream:
        async def events() -> AsyncIterator[ProviderEvent]:
            yield ProviderEvent(type=ProviderEventType.DONE)

        return ProviderStream(events())


@asynccontextmanager
async def app_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def gateway_settings(path: Path) -> GatewaySettings:
    return GatewaySettings(
        database_url=f"sqlite+aiosqlite:///{path}",
        data_root=path.parent / "data",
    )


async def test_healthz_and_readyz_when_runtime_is_ready(tmp_path: Path) -> None:
    runtime = Runtime((ProbeProvider(ready=True),))
    settings = gateway_settings(tmp_path / "ready.db")
    app = create_app(runtime, settings=settings, database=Database(settings.database_url))

    async with app_client(app) as client:
        health = await client.get("/healthz", headers={"x-correlation-id": "probe-123"})
        readiness = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "state": "running"}
    assert health.headers["x-correlation-id"] == "probe-123"
    assert readiness.headers["x-correlation-id"].startswith("req_")
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ready",
        "state": "running",
        "providers": {"probe": True},
        "checks": {
            "database": True,
            "agent_manager": True,
            "providers": True,
            "skills": True,
            "plugins": True,
        },
    }
    assert runtime.state is RuntimeState.STOPPED


async def test_readyz_returns_503_when_a_provider_is_not_ready(tmp_path: Path) -> None:
    settings = gateway_settings(tmp_path / "not-ready.db")
    app = create_app(
        Runtime((ProbeProvider(ready=False),)),
        settings=settings,
        database=Database(settings.database_url),
    )

    async with app_client(app) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "state": "running",
        "providers": {"probe": False},
        "checks": {
            "database": True,
            "agent_manager": True,
            "providers": False,
            "skills": True,
            "plugins": True,
        },
    }


async def test_readyz_rejects_running_runtime_without_any_usable_provider(tmp_path: Path) -> None:
    settings = gateway_settings(tmp_path / "empty-provider.db")
    app = create_app(Runtime(), settings=settings, database=Database(settings.database_url))

    async with app_client(app) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["providers"] is False
