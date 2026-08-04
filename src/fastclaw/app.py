"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from fastclaw.models import HealthResponse, ReadinessResponse
from fastclaw.runtime import Runtime, RuntimeState


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Create a FastAPI app bound to a runtime instance."""

    app_runtime = runtime or Runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = app_runtime
        await app_runtime.start()
        try:
            yield
        finally:
            await app_runtime.stop()

    app = FastAPI(title="FastClaw", version="0.1.0", lifespan=lifespan)
    app.state.runtime = app_runtime

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

    return app


app = create_app()
