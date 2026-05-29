"""Entrypoint for the standalone weather-polling process.

Usage:
    python -m app.poller_main
"""

import asyncio
import logging
import sys

import structlog

from app.config import get_settings
from app.database import engine
from app.models import Base
from app.services.poller import Poller


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


def _create_tables() -> None:
    """Create all DB tables if they do not already exist (idempotent)."""
    Base.metadata.create_all(bind=engine)


async def _main() -> None:
    """Configure logging, initialise the database, and run the poller forever."""
    _configure_structlog()
    log = structlog.get_logger(__name__).bind(component="poller_main")

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
