"""Tests for duplicate-reading handling in ReadingRepository."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.reading import WeatherReading
from app.repositories.reading_repo import ReadingRepository
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
