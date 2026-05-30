"""Entrypoint for the standalone weather-polling process.

Usage:
    python -m app.poller_main
"""

import asyncio
import logging
import sys
import time

import structlog

from app.config import get_settings, mask_db_url
from app.database import engine
from app.models import Base
from app.services.poller import Poller
from app.services.weather_client import CITIES

_DB_READY_ATTEMPTS = 10
_DB_READY_WAIT_SECONDS = 3


def _configure_structlog() -> None:
    """Set up structlog with JSON output and stdlib integration."""
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
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def _wait_for_db(log: "structlog.stdlib.BoundLogger") -> None:
    """Block until Postgres accepts a connection, retrying with fixed back-off.

    Attempts up to _DB_READY_ATTEMPTS times, sleeping _DB_READY_WAIT_SECONDS
    between each. Calls sys.exit(1) with a clear message on final failure so
    the container restarts rather than silently hanging.
    """
    from sqlalchemy import text

    for attempt in range(1, _DB_READY_ATTEMPTS + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            log.info("db_ready", attempt=attempt)
            return
        except Exception as exc:
            if attempt == _DB_READY_ATTEMPTS:
                log.error(
                    "db_not_ready_giving_up",
                    attempts=_DB_READY_ATTEMPTS,
                    error=str(exc),
                )
                sys.exit(1)
            log.warning(
                "db_not_ready_retrying",
                attempt=attempt,
                max_attempts=_DB_READY_ATTEMPTS,
                wait_seconds=_DB_READY_WAIT_SECONDS,
                error=str(exc),
            )
            time.sleep(_DB_READY_WAIT_SECONDS)


def _create_tables() -> None:
    """Create all DB tables if they do not already exist (idempotent)."""
    Base.metadata.create_all(bind=engine)


async def _main() -> None:
    """Configure logging, wait for DB, initialise tables, and run the poller forever."""
    _configure_structlog()
    log = structlog.get_logger(__name__).bind(component="poller_main")
    settings = get_settings()

    log.info(
        "service_started",
        service="poller",
        poll_interval_seconds=settings.poll_interval_seconds,
        cities=list(CITIES),
        db_url=mask_db_url(settings.database_url),
    )

    log.info("db_ready_check_start", max_attempts=_DB_READY_ATTEMPTS, wait_seconds=_DB_READY_WAIT_SECONDS)
    _wait_for_db(log)

    log.info("db_init_start")
    try:
        _create_tables()
        log.info("db_init_complete")
    except Exception:
        log.error("db_init_failed", exc_info=True)
        raise

    await Poller().run()


if __name__ == "__main__":
    asyncio.run(_main())
