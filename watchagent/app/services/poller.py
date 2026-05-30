"""Background poller: fetches weather for all cities and persists readings."""

from __future__ import annotations

import asyncio
import time

import structlog
import structlog.stdlib
from sqlalchemy.orm import Session

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

    async def _poll_cycle(self, log: structlog.stdlib.BoundLogger) -> None:
        """Fetch all cities concurrently, persist each result, then run cross-city checks."""
        cycle_start = time.perf_counter()
        cities = list(CITIES)
        results: list[RawReading | None] = list(
            await asyncio.gather(
                *[self._client.fetch(city) for city in cities],
                return_exceptions=False,
            )
        )

        new_count = 0
        dup_count = 0
        events_fired = 0
        new_readings: dict[str, RawReading] = {}

        for city, reading in zip(cities, results):
            if reading is None:
                continue
            try:
                stored, n_events = await self._handle_reading(reading, log.bind(city=city))
            except Exception:
                log.error(
                    "reading_cycle_failed",
                    city=city,
                    exc_info=True,
                )
                continue
            if stored:
                new_count += 1
                events_fired += n_events
                new_readings[city] = reading
            else:
                dup_count += 1

        # Cross-city comparison — only runs when all cities produced a new reading
        if len(new_readings) == len(cities):
            events_fired += await self._run_cross_city_detection(new_readings, log)

        cycle_ms = round((time.perf_counter() - cycle_start) * 1000, 1)
        log.info(
            "poll_cycle_complete",
            cities_polled=len(cities),
            new_readings=new_count,
            duplicates=dup_count,
            events_fired=events_fired,
            cycle_duration_ms=cycle_ms,
        )

    async def _handle_reading(
        self,
        reading: RawReading,
        log: structlog.stdlib.BoundLogger,
    ) -> tuple[bool, int]:
        """Insert a reading and trigger per-city event detection.

        Returns (True, n) when the reading was new and n events fired,
        or (False, 0) when it was a duplicate.
        """
        db = SessionLocal()
        try:
            reading_repo = ReadingRepository(db)

            # Fetch history BEFORE inserting so that history[0] is the previous
            # reading, not the current one.  If fetched after insert, delta-based
            # checks (sudden_temp_drop/rise, wind_shift, weather_code_severity)
            # would compare the current reading against itself and never fire.
            history = reading_repo.get_recent(
                reading.city, limit=get_settings().history_limit
            )

            row = reading_repo.insert(reading)
            is_duplicate = row is None
            log.info(
                "reading_stored",
                city=reading.city,
                timestamp=str(reading.timestamp),
                duplicate=is_duplicate,
            )

            if is_duplicate:
                return False, 0

            n_events = await self._run_event_detection(reading, history, db, log)
            return True, n_events

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
        db: Session,
        log: structlog.stdlib.BoundLogger,
    ) -> int:
        """Run all per-city event checks, persist any that fired, return count.

        Any unexpected exception from the detector is caught, logged at ERROR,
        and swallowed so the rest of the poll cycle continues uninterrupted.
        """
        try:
            events = self._detector.detect_events(reading, history)
        except Exception:
            log.error(
                "event_detection_failed",
                city=reading.city,
                exc_info=True,
            )
            return 0

        if not events:
            return 0

        event_repo = EventRepository(db)
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

        return len(events)

    async def _run_cross_city_detection(
        self,
        readings: dict[str, RawReading],
        log: structlog.stdlib.BoundLogger,
    ) -> int:
        """Run cross-city comparison checks, persist any events that fired, return count."""
        events = self._detector.detect_cross_city_events(readings)
        if not events:
            return 0

        db = SessionLocal()
        try:
            event_repo = EventRepository(db)
            for event_data in events:
                try:
                    event_repo.insert(event_data)
                except Exception:
                    log.error(
                        "cross_city_event_persist_failed",
                        event_type=event_data.get("event_type"),
                        exc_info=True,
                    )
                    raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return len(events)
