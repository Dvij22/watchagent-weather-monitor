"""Shared pytest fixtures for WatchAgent tests.

Why each fixture exists
-----------------------
engine          — Owns the SQLite in-memory database for one test.
                  Function-scoped so every test starts with clean tables.
                  StaticPool forces all sessions to reuse the same underlying
                  connection, which is required for in-memory SQLite to share
                  data between multiple sessionmaker instances in the same test.

db_session      — A real SQLAlchemy Session against the test engine.
                  Used by repository and deduplication tests that need to
                  exercise actual DB behaviour (IntegrityError, row counts, etc.).

sample_reading  — Module-level helper (not a fixture) so test files can call it
                  with keyword overrides without boilerplate.  Lives here so it
                  is importable from a single location.

test_client     — FastAPI TestClient with get_db overridden to use the test
                  engine.  Tables already exist from the engine fixture so the
                  startup event's create_all is a harmless no-op against a
                  separate connection; routes use the overridden session.
"""

import os

# Must be set before any app module is imported because app.database builds the
# engine at import time and pydantic-settings will raise if DATABASE_URL is absent.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.database import get_db
from app.models import Base
from app.services.weather_client import RawReading


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    """Fresh in-memory SQLite engine per test, shared via StaticPool."""
    _engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(_engine)
    yield _engine
    Base.metadata.drop_all(_engine)
    _engine.dispose()


# ---------------------------------------------------------------------------
# DB session
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session(engine) -> Session:
    """Real session against the test engine for repository-level tests."""
    _Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = _Session()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# TestClient
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_client(engine) -> TestClient:
    """FastAPI TestClient wired to the test engine via dependency override."""
    _Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = _Session()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


# ---------------------------------------------------------------------------
# Sample data helper
# ---------------------------------------------------------------------------

def sample_reading(
    city: str = "Ottawa",
    timestamp: datetime = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    temperature: float = 5.0,
    apparent_temperature: float = 5.0,
    precipitation: float = 0.0,
    wind_speed: float = 10.0,
    weather_code: int = 0,
) -> RawReading:
    """Return a RawReading with fully-controlled, predictable values.

    All fields have sensible defaults so callers only override what the test
    actually cares about.
    """
    return RawReading(
        city=city,
        timestamp=timestamp,
        temperature=temperature,
        apparent_temperature=apparent_temperature,
        precipitation=precipitation,
        wind_speed=wind_speed,
        weather_code=weather_code,
    )
