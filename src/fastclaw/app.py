"""FastAPI application factory."""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from fastclaw.agent.manager import AgentRuntimeConfig, AgentRuntimeManager
from fastclaw.gateway import Gateway, GatewaySettings, create_gateway_router
from fastclaw.models import HealthResponse, ReadinessResponse
from fastclaw.observability import configure_json_logging, use_correlation_id
from fastclaw.runtime import Runtime, RuntimeState
from fastclaw.storage import Database

_EXPORTED_AGENT_ROUTE = re.compile(
    r"^agents/[^/]+/(?P<section>chat|chats|customize|models|sessions|skills)/?$"
)


class ExportedNextStaticFiles(StaticFiles):
    """Serve Next's exported `default` route for arbitrary Agent IDs."""

    async def get_response(self, path: str, scope: MutableMapping[str, Any]) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code != 404:
            return response
        match = _EXPORTED_AGENT_ROUTE.fullmatch(path)
        if match is None:
            return response
        fallback = f"agents/default/{match.group('section')}/"
        return await super().get_response(fallback, scope)


def create_app(
    runtime: Runtime | None = None,
    *,
    settings: GatewaySettings | None = None,
    database: Database | None = None,
) -> FastAPI:
    """Create a FastAPI app bound to a runtime instance."""

    if os.environ.get("FASTCLAW_LOG_FORMAT", "").lower() == "json":
        configure_json_logging()

    app_runtime = runtime or Runtime()
    app_settings = settings or GatewaySettings.from_env()
    app_database = database or Database(app_settings.database_url)
    agent_manager = AgentRuntimeManager(
        app_database,
        app_runtime,
        AgentRuntimeConfig(
            data_root=app_settings.data_root,
            legacy_data_root=app_settings.legacy_data_root,
            default_provider_name=app_settings.provider_name,
            default_provider_api_key=app_settings.provider_api_key,
            default_provider_api_base=app_settings.provider_api_base,
            default_provider_api_type=app_settings.provider_api_type,
            default_model=app_settings.default_model,
        ),
    )
    gateway = Gateway(app_settings, app_database, app_runtime, agent_manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = app_runtime
        app.state.database = app_database
        app.state.gateway = gateway
        app.state.agent_manager = agent_manager
        await app_database.create_schema()
        await app_runtime.start()
        await agent_manager.start()
        try:
            yield
        finally:
            try:
                await agent_manager.stop()
            finally:
                try:
                    await app_runtime.stop()
                finally:
                    await app_database.close()

    app = FastAPI(title="FastClaw", version="0.1.0", lifespan=lifespan)
    app.state.runtime = app_runtime
    app.state.database = app_database
    app.state.gateway = gateway
    app.state.agent_manager = agent_manager

    @app.middleware("http")
    async def correlation_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        requested = request.headers.get("x-correlation-id", "")
        trusted = requested if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", requested) else ""
        with use_correlation_id(trusted) as correlation_id:
            response = await call_next(request)
            response.headers["x-correlation-id"] = correlation_id
            return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        if request.url.path.startswith("/v1/"):
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": "authentication_error"
                        if exc.status_code == 401
                        else "invalid_request_error",
                    }
                },
            )
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=exc.status_code,
                content={"ok": False, "error": str(exc.detail)},
                headers=exc.headers,
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=422,
                content={"ok": False, "error": "invalid request", "issues": exc.errors()},
            )
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    async def healthz() -> HealthResponse:
        return HealthResponse(state=app_runtime.state)

    @app.get(
        "/readyz",
        response_model=ReadinessResponse,
        responses={503: {"model": ReadinessResponse}},
        tags=["health"],
    )
    async def readyz() -> ReadinessResponse | JSONResponse:
        providers = await app_runtime.readiness()
        checks = await agent_manager.readiness()
        is_ready = (
            app_runtime.state is RuntimeState.RUNNING
            and all(providers.values())
            and all(checks.values())
        )
        response = ReadinessResponse(
            status="ready" if is_ready else "not_ready",
            state=app_runtime.state,
            providers=providers,
            checks=checks,
        )
        if is_ready:
            return response
        return JSONResponse(status_code=503, content=response.model_dump(mode="json"))

    app.include_router(create_gateway_router(gateway))
    if app_settings.web_root is not None:
        app.mount(
            "/",
            ExportedNextStaticFiles(directory=app_settings.web_root, html=True),
            name="web",
        )

    return app


app = create_app()
