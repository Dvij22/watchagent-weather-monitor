"""Background poller: fetches weather for all cities and persists readings."""

from __future__ import annotations

import asyncio

import structlog

from app.config import get_settings
from app.database import SessionLocal
from app.models.reading import WeatherReading
from app.repositories.event_repo import EventRepository
from app.repositories.reading_repo import ReadingRepository
from app.services.event_detector import EventDetector
from app.services.weather_client import CITIES, RawReading, WeatherClient

_logger = structlog.get_logger(__name__).bind(component="poller")


class Poller:
    """Continuously polls Open-Meteo for all three cities and persists results.

    Each cycle:
    1. Fetches all cities concurrently via asyncio.gather.
    2. For each successful RawReading, attempts a DB insert.
    3. If the reading is new (not a duplicate), passes it and the last 24
       readings for that city to EventDetector (wired in a later phase).
    4. Sleeps for POLL_INTERVAL_SECONDS before the next cycle.
    """

    def __init__(
        self,
        client: WeatherClient | None = None,
        detector: EventDetector | None = None,
    ) -> None:
        self._client = client or WeatherClient()
        self._detector = detector or EventDetector()

    async def run(self) -> None:
        """Poll forever. Logs and continues on per-city errors; never silently swallows."""
        log = _logger
        log.info("poller_starting", cities=list(CITIES))

        while True:
            await self._poll_cycle(log)
            interval = get_settings().poll_interval_seconds
            log.info("poller_sleeping", interval_seconds=interval)
            await asyncio.sleep(interval)

    async def _poll_cycle(self, log: structlog.BoundLogger) -> None:  # type: ignore[type-arg]
        """Fetch all cities concurrently and persist each result."""
        cities = list(CITIES)
        results: list[RawReading | None] = list(
            await asyncio.gather(
                *[self._client.fetch(city) for city in cities],
                return_exceptions=False,
            )
        )

        for city, reading in zip(cities, results):
            if reading is None:
                continue
            await self._handle_reading(reading, log.bind(city=city))

    async def _handle_reading(
        self,
        reading: RawReading,
        log: structlog.BoundLogger,  # type: ignore[type-arg]
    ) -> None:
        """Insert a reading and trigger event detection for new readings."""
        db = SessionLocal()
        try:
            reading_repo = ReadingRepository(db)
            row = reading_repo.insert(reading)

            is_duplicate = row is None
            log.info(
                "reading_stored",
                city=reading.city,
                timestamp=str(reading.timestamp),
                duplicate=is_duplicate,
            )

            if is_duplicate:
                return

            history = reading_repo.get_recent(reading.city, limit=24)

            # EventDetector wired in a later phase — placeholder call site.
            await self._run_event_detection(reading, history, db, log)

        except Exception:
            log.error(
                "reading_handle_failed",
                city=reading.city,
                timestamp=str(reading.timestamp),
                exc_info=True,
            )
            db.rollback()
            raise
        finally:
            db.close()

    async def _run_event_detection(
        self,
        reading: RawReading,
        history: list[WeatherReading],
        db: object,
        log: structlog.BoundLogger,  # type: ignore[type-arg]
    ) -> None:
        """Run all event checks and persist any that fired."""
        from sqlalchemy.orm import Session as _Session

        events = self._detector.detect_events(reading, history)
        if not events:
            return

        event_repo = EventRepository(db)  # type: ignore[arg-type]
        for event_data in events:
            try:
                event_repo.insert(event_data)
            except Exception:
                log.error(
                    "event_persist_failed",
                    city=reading.city,
                    event_type=event_data.get("event_type"),
                    exc_info=True,
                )
                raise
