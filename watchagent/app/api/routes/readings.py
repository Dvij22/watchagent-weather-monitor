"""Readings route — query stored weather readings."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.reading import WeatherReading
from app.repositories.reading_repo import ReadingRepository
from app.schemas.reading import ReadingOut

router = APIRouter(tags=["readings"])


@router.get("/readings", response_model=dict[str, list[ReadingOut]])
async def get_readings(
    request: Request,
    city: str | None = Query(default=None, description="Filter by city name"),
    limit: int = Query(default=50, ge=1, le=500, description="Maximum number of readings to return"),
    db: Session = Depends(get_db),
) -> dict[str, list[ReadingOut]]:
    """Return stored weather readings, newest first.

    Optionally filter by city. When no city is provided, readings from all
    three monitored cities are returned interleaved by recency.
    """
    repo = ReadingRepository(db)

    if city is not None:
        rows = repo.get_recent(city, limit=limit)
    else:
        rows = (
            db.query(WeatherReading)
            .order_by(WeatherReading.timestamp.desc())
            .limit(limit)
            .all()
        )

    request.state.results_returned = len(rows)
    return {"readings": [ReadingOut.model_validate(r) for r in rows]}
