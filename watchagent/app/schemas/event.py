"""Pydantic response schema for WeatherEvent."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class EventOut(BaseModel):
    """Serialised representation of a stored weather event."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    city: str
    event_type: str
    timestamp: datetime
    summary: str
    reason: str
    metrics: dict[str, Any]
    created_at: datetime
