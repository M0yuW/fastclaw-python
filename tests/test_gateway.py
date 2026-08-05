from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI

from fastclaw.app import create_app
from fastclaw.gateway import GatewaySettings
from fastclaw.identity import hash_api_key
from fastclaw.runtime import Runtime
from fastclaw.storage import AgentRecord, APIKeyRecord, Database, UnitOfWork


@asynccontextmanager
async def gateway_client(
    path: Path, *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[tuple[httpx.AsyncClient, Database]]:
    settings = GatewaySettings(database_url=f"sqlite+aiosqlite:///{path}", port=18954)
    runtime = Runtime(
        http_client_factory=lambda: (
            httpx.AsyncClient(transport=transport) if transport is not None else httpx.AsyncClient()
        )
    )
    database = Database(settings.database_url)
    app: FastAPI = create_app(runtime, settings=settings, database=database)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, database


async def onboard(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/onboard",
        json={
            "username": "alice",
            "email": "alice@example.test",
            "password": "correct horse battery staple",
            "displayName": "Alice",
            "provider": "fixture",
            "apiBase": "https://llm.test/v1",
            "apiKey": "provider-secret",
            "model": "fixture/model-1",
            "agentName": "Analyst",
        },
    )
    assert response.status_code == 200
    data = response.json()
    return {"userId": str(data["userId"]), "agentId": str(data["agentId"])}


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/login",
        json={"login": "alice", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    assert "password_hash" not in response.json()["user"]
    assert client.cookies.get("fastclaw_session")


async def test_onboard_cookie_auth_status_agents_and_masked_provider(tmp_path: Path) -> None:
    async with gateway_client(tmp_path / "gateway.db") as (client, _):
        root = await client.get("/")
        before = await client.get("/api/status")
        created = await onboard(client)
        second = await client.post(
            "/api/onboard",
            json={
                "username": "other",
                "email": "other@example.test",
                "password": "long-enough-password",
            },
        )
        invalid_login = await client.post(
            "/api/login", json={"login": "alice", "password": "wrong-password"}
        )
        await login(client)
        me = await client.get("/api/me")
        status = await client.get("/api/status")
        agents = await client.get("/api/agents")
        providers = await client.get("/api/providers")
        config = await client.get("/api/config")
        created_key = await client.post(
            "/api/apikeys",
            json={"name": "automation", "agentIds": [created["agentId"]]},
        )
        token = created_key.json()["token"]
        listed_keys = await client.get("/api/apikeys")
        bearer_me = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})

        assert root.status_code == 200
        assert before.json()["configured"] is False
        assert second.status_code == 409
        assert invalid_login.json() == {"ok": False, "error": "invalid credentials"}
        assert me.json()["user"]["id"] == created["userId"]
        assert me.json()["authMethod"] == "cookie"
        assert status.json()["configured"] is True
        assert status.json()["running"] is True
        assert status.json()["provider"]["apiKey"] == "prov****cret"
        assert agents.json()["agents"][0]["id"] == created["agentId"]
        serialized = json.dumps([providers.json(), config.json()])
        assert "provider-secret" not in serialized
        assert created_key.status_code == 201
        assert token.startswith("fc_")
        assert token not in json.dumps(listed_keys.json())
        assert bearer_me.json()["authMethod"] == "apikey"

        logged_out = await client.post("/api/logout")
        denied = await client.get("/api/me")
        assert logged_out.status_code == 200
        assert denied.status_code == 401


async def test_bearer_api_key_enforces_agent_acl(tmp_path: Path) -> None:
    async with gateway_client(tmp_path / "acl.db") as (client, database):
        created = await onboard(client)
        now = datetime.now(UTC)
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            await store.save_agent(
                AgentRecord(
                    id="agent-denied",
                    user_id=created["userId"],
                    name="Denied",
                    config={"model": "fixture/model-1"},
                    created_at=now,
                    updated_at=now,
                )
            )
            await store.save_api_key(
                APIKeyRecord(
                    id="key-1",
                    user_id=created["userId"],
                    name="agent key",
                    key_hash=hash_api_key("fc_agent_secret"),
                    key_prefix="fc_agent",
                )
            )
            await store.set_api_key_agents("key-1", [created["agentId"]])

        headers = {"Authorization": "Bearer fc_agent_secret"}
        agents = await client.get("/api/agents", headers=headers)
        allowed = await client.get(f"/api/agents/{created['agentId']}", headers=headers)
        denied = await client.get("/api/agents/agent-denied", headers=headers)

        assert [item["id"] for item in agents.json()["agents"]] == [created["agentId"]]
        assert allowed.status_code == 200
        assert denied.status_code == 404


async def test_chat_stream_and_history_use_configured_provider(tmp_path: Path) -> None:
    async def provider_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://llm.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer provider-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "model-1"
        body = (
            'data: {"choices":[{"index":0,"delta":{"content":"hello "},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"content":"world"},'
            '"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(provider_handler)
    async with gateway_client(tmp_path / "chat.db", transport=transport) as (client, _):
        tested = await client.post(
            "/api/test-provider",
            json={
                "apiBase": "https://llm.test/v1",
                "apiKey": "provider-secret",
                "apiType": "openai-chat",
                "model": "fixture/model-1",
            },
        )
        created = await onboard(client)
        await login(client)
        provider_list = await client.get("/api/providers", params={"scope": "user"})
        provider_id = provider_list.json()["providers"][0]["id"]
        stored_test = await client.post(
            f"/api/providers/{provider_id}/test", json={"model": "fixture/model-1"}
        )
        response = await client.post(
            "/api/chat/stream",
            json={
                "agentId": created["agentId"],
                "sessionId": "session-1",
                "message": "say hello",
            },
        )
        history = await client.get(
            "/api/chat/history",
            params={"agentId": created["agentId"], "sessionId": "session-1"},
        )

        assert tested.json() == {"ok": True}
        assert stored_test.json() == {"ok": True}
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert [event["type"] for event in events] == [
            "content_delta",
            "content_delta",
            "content",
            "done",
        ]
        assert events[-1]["data"]["seq"] == 3
        messages = history.json()["history"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[-1]["content"] == "hello world"
