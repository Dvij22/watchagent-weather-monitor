"""Integration tests for the FastAPI routes.

All tests use the test_client fixture (TestClient wired to the test DB)
and seed data via a direct session on the same engine.  No real HTTP calls
or external services are involved.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.repositories.event_repo import EventRepository
from app.repositories.reading_repo import ReadingRepository
from tests.conftest import sample_reading

_BASE_TS = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

_SAMPLE_EVENT = {
    "city": "Ottawa",
    "event_type": "feels_like_gap",
    "timestamp": _BASE_TS,
    "summary": "Ottawa feels 11°C colder than the actual temperature.",
    "reason": "Apparent temp (-6°C) deviates 11°C from actual (5°C), exceeding 8°C threshold.",
    "metrics": {"temperature": 5.0, "apparent_temperature": -6.0, "gap": 11.0},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed(engine, readings=(), events=()):
    """Insert readings and events into the test DB and close the session."""
    _Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = _Session()
    try:
        r_repo = ReadingRepository(db)
        for r in readings:
            r_repo.insert(r)
        e_repo = EventRepository(db)
        for e in events:
            e_repo.insert(e)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_ok_empty_db(test_client):
    """Health route returns 200 with zero counts on an empty DB."""
    resp = test_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["readings_stored"] == 0
    assert data["events_stored"] == 0


def test_health_returns_accurate_counts(engine, test_client):
    """Counts reflect exactly the seeded rows — not hardcoded values."""
    _seed(
        engine,
        readings=[
            sample_reading(timestamp=_BASE_TS),
            sample_reading(timestamp=_BASE_TS + timedelta(hours=1)),
            sample_reading(timestamp=_BASE_TS + timedelta(hours=2)),
        ],
        events=[_SAMPLE_EVENT],
    )

    resp = test_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["readings_stored"] == 3
    assert data["events_stored"] == 1


# ---------------------------------------------------------------------------
# /readings
# ---------------------------------------------------------------------------

def test_readings_returns_all_cities_without_filter(engine, test_client):
    """Without a city filter, readings from all cities are returned."""
    _seed(
        engine,
        readings=[
            sample_reading(city="Ottawa", timestamp=_BASE_TS),
            sample_reading(city="Toronto", timestamp=_BASE_TS),
            sample_reading(city="Vancouver", timestamp=_BASE_TS),
        ],
    )

    resp = test_client.get("/readings")
    assert resp.status_code == 200
    data = resp.json()
    cities = {r["city"] for r in data["readings"]}
    assert cities == {"Ottawa", "Toronto", "Vancouver"}


def test_readings_city_filter_returns_only_matching(engine, test_client):
    """?city=Ottawa returns only Ottawa readings."""
    _seed(
        engine,
        readings=[
            sample_reading(city="Ottawa", timestamp=_BASE_TS),
            sample_reading(city="Ottawa", timestamp=_BASE_TS + timedelta(hours=1)),
            sample_reading(city="Toronto", timestamp=_BASE_TS),
        ],
    )

    resp = test_client.get("/readings?city=Ottawa")
    assert resp.status_code == 200
    readings = resp.json()["readings"]
    assert len(readings) == 2
    assert all(r["city"] == "Ottawa" for r in readings)


def test_readings_returned_newest_first(engine, test_client):
    """Readings are ordered newest-first."""
    t0 = _BASE_TS
    t1 = _BASE_TS + timedelta(hours=1)
    t2 = _BASE_TS + timedelta(hours=2)

    _seed(
        engine,
        readings=[
            sample_reading(timestamp=t0),
            sample_reading(timestamp=t1),
            sample_reading(timestamp=t2),
        ],
    )

    resp = test_client.get("/readings?city=Ottawa")
    timestamps = [r["timestamp"] for r in resp.json()["readings"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_readings_limit_respected(engine, test_client):
    """?limit=1 returns at most one reading."""
    _seed(
        engine,
        readings=[
            sample_reading(timestamp=_BASE_TS),
            sample_reading(timestamp=_BASE_TS + timedelta(hours=1)),
        ],
    )

    resp = test_client.get("/readings?limit=1")
    assert len(resp.json()["readings"]) == 1


def test_readings_shape_has_all_stored_fields(engine, test_client):
    """Every reading object exposes all fields persisted to the database.

    The spec says /readings must return 'all stored fields for each reading'.
    This test seeds a reading with known values and verifies every column
    is present and correct in the response body.
    """
    _seed(
        engine,
        readings=[
            sample_reading(
                city="Vancouver",
                timestamp=_BASE_TS,
                temperature=-2.5,
                apparent_temperature=-9.1,
                precipitation=3.2,
                wind_speed=45.0,
                weather_code=71,
            )
        ],
    )

    resp = test_client.get("/readings?city=Vancouver")
    assert resp.status_code == 200
    readings = resp.json()["readings"]
    assert len(readings) == 1
    r = readings[0]

    # All stored columns must be present
    assert set(r.keys()) == {"id", "city", "timestamp", "temperature",
                             "apparent_temperature", "precipitation",
                             "wind_speed", "weather_code", "created_at"}
    # Values match what was seeded
    assert r["city"] == "Vancouver"
    assert r["temperature"] == pytest.approx(-2.5)
    assert r["apparent_temperature"] == pytest.approx(-9.1)
    assert r["precipitation"] == pytest.approx(3.2)
    assert r["wind_speed"] == pytest.approx(45.0)
    assert r["weather_code"] == 71


# ---------------------------------------------------------------------------
# /events
# ---------------------------------------------------------------------------

def test_events_response_has_events_key(engine, test_client):
    """Response body always has an 'events' key even when no events exist."""
    resp = test_client.get("/events")
    assert resp.status_code == 200
    assert "events" in resp.json()


def test_events_shape_has_all_required_fields(engine, test_client):
    """Every event object contains all 6 schema-required fields plus id/created_at."""
    _seed(engine, events=[_SAMPLE_EVENT])

    resp = test_client.get("/events")
    events = resp.json()["events"]
    assert len(events) == 1

    required_fields = {"id", "city", "event_type", "timestamp", "summary", "reason", "metrics", "created_at"}
    assert required_fields.issubset(set(events[0].keys()))


def test_events_city_filter(engine, test_client):
    """?city=Ottawa returns only Ottawa events."""
    toronto_event = {**_SAMPLE_EVENT, "city": "Toronto"}
    _seed(engine, events=[_SAMPLE_EVENT, toronto_event])

    resp = test_client.get("/events?city=Ottawa")
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["city"] == "Ottawa"


def test_events_metrics_is_dict(engine, test_client):
    """metrics field is deserialised as a dict, not a string."""
    _seed(engine, events=[_SAMPLE_EVENT])

    resp = test_client.get("/events")
    metrics = resp.json()["events"][0]["metrics"]
    assert isinstance(metrics, dict)
    assert "gap" in metrics


# ---------------------------------------------------------------------------
# /readings — input validation and edge cases
# ---------------------------------------------------------------------------

def test_readings_limit_below_minimum_returns_422(test_client):
    """limit=0 is below the enforced minimum of 1 — FastAPI rejects with 422."""
    resp = test_client.get("/readings?limit=0")
    assert resp.status_code == 422


def test_readings_limit_above_maximum_returns_422(test_client):
    """limit=501 exceeds the enforced maximum of 500 — FastAPI rejects with 422."""
    resp = test_client.get("/readings?limit=501")
    assert resp.status_code == 422


def test_readings_unknown_city_returns_empty_list(test_client):
    """?city=Moscow has no readings in the DB — response must be {"readings": []} not an error.

    Unknown cities must silently return an empty list rather than a 404 or 500,
    because the client may legitimately query for a city before any readings exist.
    """
    resp = test_client.get("/readings?city=Moscow")
    assert resp.status_code == 200
    assert resp.json() == {"readings": []}
