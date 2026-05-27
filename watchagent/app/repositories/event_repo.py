"""Repository for WeatherEvent persistence and retrieval."""

from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.models.event import WeatherEvent

_logger = structlog.get_logger(__name__)


class EventRepository:
    """Data-access layer for WeatherEvent rows.

    All methods accept an explicit Session so the caller controls the
    transaction boundary.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def insert(self, event_data: dict[str, Any]) -> WeatherEvent:
        """Persist an event dict and return the new WeatherEvent row.

        *event_data* must conform to the event_schema rule: it must contain
        city, event_type, timestamp, summary, reason, and metrics.
        """
        row = WeatherEvent(
            city=event_data["city"],
            event_type=event_data["event_type"],
            timestamp=event_data["timestamp"],
            summary=event_data["summary"],
            reason=event_data["reason"],
            metrics=event_data["metrics"],
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        _logger.info(
            "event_fired",
            city=row.city,
            event_type=row.event_type,
            timestamp=str(row.timestamp),
        )
        return row

    def get_all(
        self,
        city: str | None = None,
        limit: int = 100,
    ) -> list[WeatherEvent]:
        """Return up to *limit* events, optionally filtered by *city*, newest first."""
        query = self._db.query(WeatherEvent).order_by(WeatherEvent.timestamp.desc())
        if city is not None:
            query = query.filter(WeatherEvent.city == city)
        return query.limit(limit).all()
