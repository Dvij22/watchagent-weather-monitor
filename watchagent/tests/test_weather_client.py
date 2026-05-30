"""Tests for WeatherClient — every HTTP call is replaced by a mock.

No real network request is made in this file.  The WeatherClient constructor
accepts an optional httpx.AsyncClient for exactly this purpose.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.weather_client import RawReading, WeatherClient

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MOCK_API_RESPONSE = {
    "current": {
        "time": "2024-01-15T14:00",
        "temperature_2m": -8.5,
        "apparent_temperature": -14.2,
        "precipitation": 0.2,
        "wind_speed_10m": 32.4,
        "weather_code": 61,
    }
}


def _ok_client() -> AsyncMock:
    """Return a mock AsyncClient that responds with a valid Open-Meteo payload."""
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status.return_value = None
    response.text = str(_MOCK_API_RESPONSE)
    response.json.return_value = _MOCK_API_RESPONSE
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.get.return_value = response
    return mock


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_returns_raw_reading_on_success():
    """A well-formed API response is mapped to a RawReading with correct field values."""
    client = WeatherClient(client=_ok_client())
    result = await client.fetch("Ottawa")

    assert isinstance(result, RawReading)
    assert result.city == "Ottawa"
    assert result.temperature == -8.5
    assert result.apparent_temperature == -14.2
    assert result.precipitation == 0.2
    assert result.wind_speed == 32.4
    assert result.weather_code == 61
    assert isinstance(result.timestamp, datetime)
    assert result.timestamp.tzinfo == timezone.utc  # must be UTC-aware, not naive


@pytest.mark.asyncio
async def test_fetch_passes_correct_coordinates():
    """The request uses the lat/lon coordinates registered for the requested city."""
    mock = _ok_client()
    await WeatherClient(client=mock).fetch("Vancouver")

    call_kwargs = mock.get.call_args.kwargs
    params = call_kwargs.get("params", {})
    assert params["latitude"] == pytest.approx(49.25)
    assert params["longitude"] == pytest.approx(-123.12)


# ---------------------------------------------------------------------------
# Failure / edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_unknown_city_returns_none_without_http_call():
    """Requesting a city not in CITIES returns None; no HTTP request is made."""
    mock = AsyncMock(spec=httpx.AsyncClient)
    result = await WeatherClient(client=mock).fetch("Atlantis")

    assert result is None
    mock.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_http_status_error_returns_none():
    """A 503 response (after all retries) returns None rather than raising."""
    mock_response = MagicMock()
    mock_response.status_code = 503
    error = httpx.HTTPStatusError(
        "Service Unavailable",
        request=MagicMock(),
        response=mock_response,
    )
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.get.side_effect = error

    # Patch asyncio.sleep so tenacity's 3-retry × 2s wait does not slow the test.
    with patch("asyncio.sleep"):
        result = await WeatherClient(client=mock).fetch("Ottawa")

    assert result is None
    # Tenacity should have attempted exactly 3 times before giving up.
    assert mock.get.call_count == 3


@pytest.mark.asyncio
async def test_fetch_missing_current_key_returns_none():
    """A response that omits the 'current' key is treated as a failed fetch."""
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status.return_value = None
    response.text = "{}"
    response.json.return_value = {}  # no "current" key
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.get.return_value = response

    result = await WeatherClient(client=mock).fetch("Toronto")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_malformed_current_fields_returns_none():
    """A 200 response where 'current' is present but missing data fields returns None.

    Protects against Open-Meteo returning a partial payload (e.g. only 'time'
    without the numeric fields) — must log WARNING and return None, not crash
    with KeyError.
    """
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status.return_value = None
    # 'current' exists but is missing temperature_2m, wind_speed_10m, etc.
    partial_payload = {"current": {"time": "2024-01-15T14:00"}}
    response.text = str(partial_payload)
    response.json.return_value = partial_payload
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.get.return_value = response

    result = await WeatherClient(client=mock).fetch("Ottawa")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_current_field_wrong_type_returns_none():
    """A 200 response where a numeric field is a non-castable string returns None.

    Guards against API schema drift where e.g. temperature_2m becomes "N/A".
    """
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status.return_value = None
    bad_payload = {
        "current": {
            "time": "2024-01-15T14:00",
            "temperature_2m": "N/A",       # not castable to float
            "apparent_temperature": -14.2,
            "precipitation": 0.2,
            "wind_speed_10m": 32.4,
            "weather_code": 61,
        }
    }
    response.text = str(bad_payload)
    response.json.return_value = bad_payload
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.get.return_value = response

    result = await WeatherClient(client=mock).fetch("Vancouver")

    assert result is None
