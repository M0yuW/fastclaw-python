"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from fastclaw.gateway import Gateway, GatewaySettings, create_gateway_router
from fastclaw.models import HealthResponse, ReadinessResponse
from fastclaw.runtime import Runtime, RuntimeState
from fastclaw.storage import Database


def create_app(
    runtime: Runtime | None = None,
    *,
    settings: GatewaySettings | None = None,
    database: Database | None = None,
) -> FastAPI:
    """Create a FastAPI app bound to a runtime instance."""

    app_runtime = runtime or Runtime()
    app_settings = settings or GatewaySettings.from_env()
    app_database = database or Database(app_settings.database_url)
    gateway = Gateway(app_settings, app_database, app_runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = app_runtime
        app.state.database = app_database
        app.state.gateway = gateway
        await app_database.create_schema()
        await app_runtime.start()
        try:
            yield
        finally:
            await app_runtime.stop()
            await app_database.close()

    app = FastAPI(title="FastClaw", version="0.1.0", lifespan=lifespan)
    app.state.runtime = app_runtime
    app.state.database = app_database
    app.state.gateway = gateway

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
        is_ready = app_runtime.state is RuntimeState.RUNNING and all(providers.values())
        response = ReadinessResponse(
            status="ready" if is_ready else "not_ready",
            state=app_runtime.state,
            providers=providers,
        )
        if is_ready:
            return response
        return JSONResponse(status_code=503, content=response.model_dump(mode="json"))

    app.include_router(create_gateway_router(gateway))
    if app_settings.web_root is not None:
        app.mount("/", StaticFiles(directory=app_settings.web_root, html=True), name="web")

    return app


app = create_app()
