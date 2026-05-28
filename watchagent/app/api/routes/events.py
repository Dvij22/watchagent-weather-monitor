"""Events route — query stored weather events."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.event_repo import EventRepository
from app.schemas.event import EventOut

router = APIRouter(tags=["events"])


@router.get("/events", response_model=dict[str, list[EventOut]])
def get_events(
    city: str | None = Query(default=None, description="Filter by city name"),
    limit: int = Query(default=50, ge=1, le=500, description="Maximum number of events to return"),
    db: Session = Depends(get_db),
) -> dict[str, list[EventOut]]:
    """Return detected weather events, newest first.

    Optionally filter by city. When no city is provided, events from all
    three monitored cities are returned interleaved by recency.
    """
    repo = EventRepository(db)
    rows = repo.get_all(city=city, limit=limit)
    return {"events": [EventOut.model_validate(r) for r in rows]}
