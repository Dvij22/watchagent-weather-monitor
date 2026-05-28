"""FastAPI application factory for WatchAgent."""

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request, Response

from app.api.routes import events, health, readings
from app.database import engine
from app.models import Base

_logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Create DB tables on startup (idempotent); nothing to clean up on shutdown."""
    _logger.info("db_init_start")
    Base.metadata.create_all(bind=engine)
    _logger.info("db_init_complete")
    yield


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    app = FastAPI(
        title="WatchAgent",
        version="1.0.0",
        description="Weather monitoring API for Ottawa, Toronto, and Vancouver.",
        lifespan=_lifespan,
    )

    # ------------------------------------------------------------------ #
    # Logging middleware                                                   #
    # ------------------------------------------------------------------ #

    @app.middleware("http")
    async def log_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Emit a structured INFO log for every HTTP request with timing."""
        request_id = str(uuid.uuid4())
        log = _logger.bind(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            log.error("request_unhandled_exception", exc_info=True)
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        log.info(
            "request_completed",
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    # ------------------------------------------------------------------ #
    # Routers                                                              #
    # ------------------------------------------------------------------ #

    app.include_router(health.router)
    app.include_router(readings.router)
    app.include_router(events.router)

    return app


app = create_app()
