"""HTTP client for fetching current weather from Open-Meteo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.config import get_settings

CITIES: dict[str, dict[str, float]] = {
    "Ottawa":    {"lat": 45.42, "lon": -75.69},
    "Toronto":   {"lat": 43.70, "lon": -79.42},
    "Vancouver": {"lat": 49.25, "lon": -123.12},
}

_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_CURRENT_FIELDS = (
    "temperature_2m,"
    "apparent_temperature,"
    "precipitation,"
    "wind_speed_10m,"
    "weather_code"
)

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RawReading:
    """Weather snapshot returned by a single Open-Meteo fetch."""

    city: str
    timestamp: datetime
    temperature: float
    apparent_temperature: float
    precipitation: float
    wind_speed: float
    weather_code: int


class WeatherClient:
    """Async client that fetches current weather for the three monitored cities.

    Each fetch is retried on httpx.HTTPError up to WEATHER_API_RETRY_ATTEMPTS
    times with WEATHER_API_RETRY_WAIT_SECONDS between attempts (both from
    settings). On final failure the method logs a WARNING and returns None
    rather than propagating the exception.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Accept an optional pre-configured AsyncClient for testing."""
        self._client = client

    async def fetch(self, city: str) -> RawReading | None:
        """Fetch current weather for *city* and return a RawReading, or None on failure.

        Args:
            city: Must be one of the keys in CITIES.

        Returns:
            A RawReading on success, or None if the API call ultimately fails.
        """
        coords = CITIES.get(city)
        if coords is None:
            _logger.warning(
                "unknown_city",
                city=city,
                known_cities=list(CITIES),
            )
            return None

        log = _logger.bind(city=city, component="weather_client")

        attempt_count = 0

        settings = get_settings()

        @retry(
            retry=retry_if_exception_type(httpx.HTTPError),
            stop=stop_after_attempt(settings.weather_api_retry_attempts),
            wait=wait_fixed(settings.weather_api_retry_wait_seconds),
            reraise=True,
        )
        async def _fetch_with_retry() -> dict[str, Any]:
            nonlocal attempt_count
            attempt_count += 1
            params = {
                "latitude": coords["lat"],
                "longitude": coords["lon"],
                "current": _CURRENT_FIELDS,
                "wind_speed_unit": "kmh",
                "timezone": "UTC",
            }
            if self._client is not None:
                response = await self._client.get(_BASE_URL, params=params)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.get(_BASE_URL, params=params)
            response.raise_for_status()
            log.debug("api_response_raw", body=response.text)
            return response.json()  # type: ignore[no-any-return]

        try:
            data = await _fetch_with_retry()
        except httpx.HTTPError as exc:
            http_status: int | None = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            log.warning(
                "poll_failed",
                city=city,
                http_status=http_status,
                attempt_count=attempt_count,
                error_msg=str(exc),
            )
            return None

        current = data.get("current")
        if current is None:
            log.warning(
                "poll_failed",
                city=city,
                http_status=None,
                attempt_count=attempt_count,
                error_msg="API response missing 'current' object",
            )
            return None

        try:
            timestamp = datetime.fromisoformat(current["time"]).replace(tzinfo=timezone.utc)
            return RawReading(
                city=city,
                timestamp=timestamp,
                temperature=float(current["temperature_2m"]),
                apparent_temperature=float(current["apparent_temperature"]),
                precipitation=float(current["precipitation"]),
                wind_speed=float(current["wind_speed_10m"]),
                weather_code=int(current["weather_code"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.warning(
                "poll_failed",
                city=city,
                http_status=None,
                attempt_count=attempt_count,
                error_msg=f"malformed API response — missing or invalid field: {exc}",
            )
            return None
