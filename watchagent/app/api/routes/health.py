"""Health check route."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.event import WeatherEvent
from app.models.reading import WeatherReading

router = APIRouter(tags=["health"])


@router.get("/health", response_model=dict[str, Any])
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return service liveness and a count of stored readings and events.

    Queries the DB directly so that a broken connection surfaces as a 500
    rather than a false-positive healthy response.
    """
    readings_stored: int = db.query(WeatherReading).count()
    events_stored: int = db.query(WeatherEvent).count()
    return {
        "status": "ok",
        "readings_stored": readings_stored,
        "events_stored": events_stored,
    }
