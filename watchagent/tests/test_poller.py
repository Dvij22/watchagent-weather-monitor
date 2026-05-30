"""Tests for poller resilience — per-city failures must not crash the poll cycle."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.event_detector import EventDetector
from app.services.poller import Poller
from app.services.weather_client import RawReading, WeatherClient
from tests.conftest import sample_reading

_BASE_TS = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_reading(city: str) -> RawReading:
    return RawReading(
        city=city,
        timestamp=_BASE_TS,
        temperature=5.0,
        apparent_temperature=5.0,
        precipitation=0.0,
        wind_speed=10.0,
        weather_code=0,
    )


# ---------------------------------------------------------------------------
# Fix 5a — detector crash is isolated to one city
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_event_detection_does_not_raise_if_detector_throws():
    """If EventDetector.detect_events raises, _run_event_detection logs at ERROR
    and returns without propagating — the poll cycle for other cities is unaffected.
    """
    import structlog

    detector = MagicMock(spec=EventDetector)
    detector.detect_events.side_effect = RuntimeError("simulated detector crash")

    poller = Poller(detector=detector)
    log = structlog.get_logger("test")

    # Must not raise
    await poller._run_event_detection(
        reading=sample_reading(city="Ottawa"),
        history=[],
        db=MagicMock(),
        log=log,
    )

    # detect_events was still called
    detector.detect_events.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 5b — per-city _handle_reading failure continues loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_cycle_continues_after_per_city_failure():
    """If _handle_reading raises for one city, the remaining cities in the same
    cycle are still processed.  One bad city must not abort the entire cycle.
    """
    import structlog

    processed: list[str] = []

    async def _patched_handle(reading: RawReading, log: object) -> bool:
        if reading.city == "Toronto":
            raise RuntimeError("simulated DB crash for Toronto")
        processed.append(reading.city)
        return True

    mock_client = MagicMock(spec=WeatherClient)

    async def _mock_fetch(city: str) -> RawReading:
        return _make_reading(city)

    mock_client.fetch.side_effect = _mock_fetch

    poller = Poller(client=mock_client)
    # Replace the real _handle_reading with our patched version
    poller._handle_reading = _patched_handle  # type: ignore[method-assign]

    # Cross-city detection is not the focus here — suppress it
    poller._run_cross_city_detection = AsyncMock(return_value=None)  # type: ignore[method-assign]

    log = structlog.get_logger("test")
    await poller._poll_cycle(log)

    # Ottawa and Vancouver must have been processed despite Toronto's failure
    assert "Ottawa" in processed
    assert "Vancouver" in processed
    assert "Toronto" not in processed
