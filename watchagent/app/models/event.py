"""ORM model for a weather event detected by the EventDetector."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WeatherEvent(Base):
    """A notable weather condition detected for a city.

    Conforms to the event_schema rule: every event carries city, event_type,
    timestamp, summary, reason, and a metrics dict with the triggering values.
    """

    __tablename__ = "weather_events"

    __table_args__ = (
        Index("ix_event_city", "city"),
        Index("ix_event_event_type", "event_type"),
        Index("ix_event_timestamp", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    city: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<WeatherEvent city={self.city!r} event_type={self.event_type!r} "
            f"timestamp={self.timestamp!r}>"
        )
