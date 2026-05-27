"""Repository for WeatherReading persistence and retrieval."""

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.reading import WeatherReading
from app.services.weather_client import RawReading

_logger = structlog.get_logger(__name__)


class ReadingRepository:
    """Data-access layer for WeatherReading rows.

    All methods accept an explicit Session so the caller (Poller) controls
    the transaction boundary and lifetime.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def insert(self, reading: RawReading) -> WeatherReading | None:
        """Persist a RawReading as a WeatherReading row.

        Returns the new ORM instance on success, or None if a duplicate
        (city, timestamp) already exists. Duplicates are not an error — they
        are logged at INFO with duplicate=True.
        """
        row = WeatherReading(
            city=reading.city,
            timestamp=reading.timestamp,
            temperature=reading.temperature,
            apparent_temperature=reading.apparent_temperature,
            precipitation=reading.precipitation,
            wind_speed=reading.wind_speed,
            weather_code=reading.weather_code,
        )
        try:
            self._db.add(row)
            self._db.commit()
            self._db.refresh(row)
            return row
        except IntegrityError:
            self._db.rollback()
            _logger.info(
                "reading_skipped",
                city=reading.city,
                timestamp=str(reading.timestamp),
                duplicate=True,
            )
            return None

    def get_recent(self, city: str, limit: int = 24) -> list[WeatherReading]:
        """Return up to *limit* most recent readings for *city*, newest first."""
        return (
            self._db.query(WeatherReading)
            .filter(WeatherReading.city == city)
            .order_by(WeatherReading.timestamp.desc())
            .limit(limit)
            .all()
        )
