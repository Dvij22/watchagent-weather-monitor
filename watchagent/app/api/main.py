"""FastAPI application factory for WatchAgent."""

import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request, Response

from app.api.routes import events, health, readings
from app.config import get_settings, mask_db_url
from app.database import engine
from app.models import Base
from app.services.weather_client import CITIES

_logger = structlog.get_logger(__name__)


def _configure_structlog() -> None:
    """Configure structlog with JSON output, ISO timestamps, and contextvars merging."""
    log_level = getattr(logging, get_settings().log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.WriteLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Configure logging, emit startup banner, create DB tables, yield, then exit."""
    _configure_structlog()
    log = _logger.bind(component="api")
    settings = get_settings()

    log.info(
        "service_started",
        service="api",
        cities=list(CITIES),
        db_url=mask_db_url(settings.database_url),
        docs_url="/docs",
    )

    Base.metadata.create_all(bind=engine)
    log.info("db_init_complete")
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
        """Emit a single structured INFO line per request with timing and result context.

        Route handlers write result counts to request.state so this middleware
        can include them without needing cross-task contextvars.
        (Starlette's call_next runs handlers in a new asyncio task — a copied
        context — so bind_contextvars() in the route would not be visible here.)
        """
        request_id = str(uuid.uuid4())
        start = time.perf_counter()

        try:
            response: Response = await call_next(request)
        except Exception:
            _logger.error("request_unhandled_exception", exc_info=True)
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 1)

        _logger.info(
            "api_request",
            method=request.method,
            path=request.url.path,
            city_filter=request.query_params.get("city") or None,
            results_returned=getattr(request.state, "results_returned", None),
            duration_ms=duration_ms,
            status_code=response.status_code,
            request_id=request_id,
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
