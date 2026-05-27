"""Pydantic response schema for WeatherReading."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ReadingOut(BaseModel):
    """Serialised representation of a stored weather reading."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    city: str
    timestamp: datetime
    temperature: float
    apparent_temperature: float
    precipitation: float
    wind_speed: float
    weather_code: int
    created_at: datetime
