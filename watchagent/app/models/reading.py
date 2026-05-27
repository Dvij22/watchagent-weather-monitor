"""ORM model for a single weather reading polled from Open-Meteo."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WeatherReading(Base):
    """One snapshot of weather conditions for a city at a specific timestamp.

    The unique constraint on (city, timestamp) lets the repository detect
    and skip duplicate readings without raising a DB error.
    """

    __tablename__ = "weather_readings"

    __table_args__ = (
        UniqueConstraint("city", "timestamp", name="uq_reading_city_timestamp"),
        Index("ix_reading_city", "city"),
        Index("ix_reading_timestamp", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    city: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    apparent_temperature: Mapped[float] = mapped_column(Float, nullable=False)
    precipitation: Mapped[float] = mapped_column(Float, nullable=False)
    wind_speed: Mapped[float] = mapped_column(Float, nullable=False)
    weather_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<WeatherReading city={self.city!r} timestamp={self.timestamp!r} "
            f"temp={self.temperature}>"
        )
