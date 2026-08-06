from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest

from fastclaw.providers import ChatRequest, ChatResponse, ProviderEvent, ProviderEventType
from fastclaw.providers.stream import ProviderStream
from fastclaw.runtime import Runtime, RuntimeStartupError, RuntimeState, RuntimeStateError


@dataclass
class FakeProvider:
    name: str
    events: list[str] = field(default_factory=list)
    ready_value: bool = True
    fail_start: bool = False

    async def start(self, client: httpx.AsyncClient) -> None:
        assert not client.is_closed
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise OSError("startup failed")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")

    async def ready(self) -> bool:
        self.events.append(f"ready:{self.name}")
        return self.ready_value

    async def chat(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse()

    def stream(self, request: ChatRequest) -> ProviderStream:
        async def events() -> AsyncIterator[ProviderEvent]:
            yield ProviderEvent(type=ProviderEventType.DONE)

        return ProviderStream(events())


def assert_runtime_state(runtime: Runtime, expected: RuntimeState) -> None:
    assert runtime.state is expected


async def test_runtime_starts_and_stops_in_dependency_order() -> None:
    events: list[str] = []
    client = httpx.AsyncClient()
    web_client = httpx.AsyncClient()
    first = FakeProvider("first", events)
    second = FakeProvider("second", events)
    runtime = Runtime(
        (first, second),
        http_client_factory=lambda: client,
        web_http_client_factory=lambda: web_client,
    )

    await runtime.start()
    await runtime.start()

    assert_runtime_state(runtime, RuntimeState.RUNNING)
    assert runtime.http_client is client
    assert runtime.web_http_client is web_client
    assert await runtime.is_ready()

    await runtime.stop()
    await runtime.stop()

    assert_runtime_state(runtime, RuntimeState.STOPPED)
    assert client.is_closed
    assert web_client.is_closed
    assert events == [
        "start:first",
        "start:second",
        "ready:first",
        "ready:second",
        "stop:second",
        "stop:first",
    ]


async def test_runtime_rolls_back_started_providers_on_failure() -> None:
    events: list[str] = []
    client = httpx.AsyncClient()
    web_client = httpx.AsyncClient()
    runtime = Runtime(
        (FakeProvider("first", events), FakeProvider("broken", events, fail_start=True)),
        http_client_factory=lambda: client,
        web_http_client_factory=lambda: web_client,
    )

    with pytest.raises(RuntimeStartupError, match="broken"):
        await runtime.start()

    assert_runtime_state(runtime, RuntimeState.FAILED)
    assert client.is_closed
    assert web_client.is_closed
    assert events == ["start:first", "start:broken", "stop:first"]


async def test_runtime_closes_provider_client_when_web_client_creation_fails() -> None:
    client = httpx.AsyncClient()

    def fail_web_client() -> httpx.AsyncClient:
        raise OSError("fixture web client startup failed")

    runtime = Runtime(
        http_client_factory=lambda: client,
        web_http_client_factory=fail_web_client,
    )

    with pytest.raises(RuntimeStartupError, match="web-http-client"):
        await runtime.start()

    assert client.is_closed
    assert_runtime_state(runtime, RuntimeState.FAILED)


async def test_provider_registration_is_unique_and_pre_start_only() -> None:
    runtime = Runtime()
    runtime.register_provider(FakeProvider("example"))

    with pytest.raises(ValueError, match="already registered"):
        runtime.register_provider(FakeProvider("example"))

    await runtime.start()
    try:
        with pytest.raises(RuntimeStateError, match="before startup"):
            runtime.register_provider(FakeProvider("late"))
    finally:
        await runtime.stop()


async def test_context_manager_controls_lifecycle() -> None:
    runtime = Runtime()

    async with runtime as running_runtime:
        assert running_runtime is runtime
        assert_runtime_state(runtime, RuntimeState.RUNNING)

    assert_runtime_state(runtime, RuntimeState.STOPPED)
