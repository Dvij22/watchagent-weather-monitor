"""Tests for duplicate-reading handling in ReadingRepository.

The spec requires: "mock the weather API to return the same reading twice
and assert only one row is stored."  Both paths are tested here:

1. Repository-level: insert the same RawReading object twice directly.
2. API-mock-level: simulate the WeatherClient returning an identical payload
   on two consecutive calls, then verify a single row in the DB.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.models.reading import WeatherReading
from app.repositories.reading_repo import ReadingRepository
from app.services.weather_client import WeatherClient
from tests.conftest import sample_reading


def test_first_insert_returns_row(db_session):
    """A fresh reading is persisted and the ORM instance is returned."""
    repo = ReadingRepository(db_session)
    result = repo.insert(sample_reading())
    assert result is not None
    assert isinstance(result, WeatherReading)
    assert result.city == "Ottawa"


def test_duplicate_insert_returns_none(db_session):
    """Inserting the same (city, timestamp) pair a second time returns None."""
    repo = ReadingRepository(db_session)
    reading = sample_reading()

    first = repo.insert(reading)
    second = repo.insert(reading)

    assert first is not None
    assert second is None


def test_duplicate_leaves_exactly_one_row(db_session):
    """After two identical inserts the table contains exactly one row."""
    repo = ReadingRepository(db_session)
    reading = sample_reading()

    repo.insert(reading)
    repo.insert(reading)

    count = db_session.query(WeatherReading).count()
    assert count == 1


def test_different_timestamp_is_not_duplicate(db_session):
    """Same city but different timestamp is a distinct reading, not a duplicate."""
    repo = ReadingRepository(db_session)
    t0 = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)

    r1 = repo.insert(sample_reading(timestamp=t0))
    r2 = repo.insert(sample_reading(timestamp=t1))

    assert r1 is not None
    assert r2 is not None
    assert db_session.query(WeatherReading).count() == 2


def test_different_city_same_timestamp_is_not_duplicate(db_session):
    """Same timestamp but different city is a distinct reading."""
    repo = ReadingRepository(db_session)
    ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

    r_ottawa = repo.insert(sample_reading(city="Ottawa", timestamp=ts))
    r_toronto = repo.insert(sample_reading(city="Toronto", timestamp=ts))

    assert r_ottawa is not None
    assert r_toronto is not None
    assert db_session.query(WeatherReading).count() == 2


# ---------------------------------------------------------------------------
# Weather-API-mock deduplication  (spec: "mock the weather API to return
# the same reading twice and assert only one row is stored")
# ---------------------------------------------------------------------------

_MOCK_API_RESPONSE = {
    "current": {
        "time": "2024-01-15T14:00",
        "temperature_2m": 5.0,
        "apparent_temperature": 3.0,
        "precipitation": 0.0,
        "wind_speed_10m": 10.0,
        "weather_code": 0,
    }
}


def _mock_client_returning_identical_readings() -> AsyncMock:
    """Return an AsyncMock httpx.AsyncClient that always yields the same payload."""
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status.return_value = None
    response.text = str(_MOCK_API_RESPONSE)
    response.json.return_value = _MOCK_API_RESPONSE
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.get.return_value = response
    return mock


@pytest.mark.asyncio
async def test_same_api_response_twice_stores_one_row(db_session):
    """Simulates the weather API returning an identical payload on two consecutive
    calls.  Only the first should be persisted; the second is a duplicate.
    """
    http_mock = _mock_client_returning_identical_readings()
    weather_client = WeatherClient(client=http_mock)
    repo = ReadingRepository(db_session)

    # First fetch → new reading
    reading_1 = await weather_client.fetch("Ottawa")
    assert reading_1 is not None
    first_result = repo.insert(reading_1)
    assert first_result is not None, "first insert should succeed"

    # Second fetch → same timestamp/city from Open-Meteo (API hasn't updated yet)
    reading_2 = await weather_client.fetch("Ottawa")
    assert reading_2 is not None
    assert reading_2.timestamp == reading_1.timestamp  # same payload
    second_result = repo.insert(reading_2)
    assert second_result is None, "duplicate should be silently dropped"

    # Exactly one row in the database
    assert db_session.query(WeatherReading).count() == 1
