"""Weather event detection for WatchAgent.

Design philosophy
-----------------
Each check is a single-responsibility private method that receives the new
reading and the ordered history (newest-first). Methods return either an event
dict matching the event_schema rule exactly, or None. They are pure functions of
their inputs — no side effects, no I/O.

The public ``detect_events`` method orchestrates all nine checks, applies a
per-(city, event_type) 3-hour in-memory cooldown to suppress spam on sustained
conditions, logs every fired event at INFO, and returns the filtered list.

Thresholds are calibrated for Canadian cities (Ottawa, Toronto, Vancouver)
where ±5 °C hourly swings, 80 km/h winds, and moderate winter precipitation
are plausible but not routine.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.models.reading import WeatherReading
from app.services.weather_client import RawReading

_logger = structlog.get_logger(__name__)

EventDict = dict[str, Any]

_COOLDOWN = timedelta(hours=3)

# WMO code severity tiers
_SEVERITY_TIER: dict[str, int] = {
    "thunderstorm": 95,
    "heavy_snow": 75,
    "heavy_rain": 65,
    "light": 0,
}

def _wmo_tier(code: int) -> str:
    if code >= 95:
        return "thunderstorm"
    if code >= 75:
        return "heavy_snow"
    if code >= 65:
        return "heavy_rain"
    return "light"


def _event(
    city: str,
    event_type: str,
    timestamp: datetime,
    summary: str,
    reason: str,
    metrics: dict[str, Any],
) -> EventDict:
    """Construct a validated event dict."""
    return {
        "city": city,
        "event_type": event_type,
        "timestamp": timestamp,
        "summary": summary,
        "reason": reason,
        "metrics": metrics,
    }


class EventDetector:
    """Detects notable weather events by comparing a new reading against history.

    An instance is shared across poll cycles so the cooldown state persists
    for the lifetime of the process. Instantiate once per city, or once for
    all cities — the cooldown key includes the city name.
    """

    def __init__(self) -> None:
        self._last_fired: dict[tuple[str, str], datetime] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_events(
        self,
        new: RawReading,
        history: list[WeatherReading],
    ) -> list[EventDict]:
        """Run all nine checks and return cooldown-filtered events.

        Args:
            new:     The freshly-inserted reading.
            history: Recent readings for the same city, newest first.
                     Should contain up to 24 entries.
        """
        checks = [
            self._check_sudden_temp_drop,
            self._check_sudden_temp_rise,
            self._check_city_anomaly,
            self._check_feels_like_gap,
            self._check_dangerous_wind,
            self._check_wind_shift,
            self._check_heavy_precipitation,
            self._check_precip_streak,
            self._check_weather_code_severity,
        ]

        now = datetime.now(tz=timezone.utc)
        fired: list[EventDict] = []

        for check in checks:
            result = check(new, history)
            if result is None:
                continue
            key = (result["city"], result["event_type"])
            last = self._last_fired.get(key)
            if last is not None and (now - last) < _COOLDOWN:
                continue
            self._last_fired[key] = now
            _logger.info(
                "event_fired",
                city=result["city"],
                event_type=result["event_type"],
                timestamp=str(result["timestamp"]),
                summary=result["summary"],
            )
            fired.append(result)

        return fired

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def _check_sudden_temp_drop(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if not history:
            return None
        prev_temp = history[0].temperature
        delta = prev_temp - new.temperature
        if delta <= 5.0:
            return None
        return _event(
            city=new.city,
            event_type="sudden_temp_drop",
            timestamp=new.timestamp,
            summary=f"{new.city} temperature dropped {delta:.1f}°C in one reading.",
            reason=(
                f"Temperature fell from {prev_temp:.1f}°C to {new.temperature:.1f}°C "
                f"(drop of {delta:.1f}°C), exceeding the 5°C threshold."
            ),
            metrics={"previous_temperature": prev_temp, "temperature": new.temperature, "delta": delta},
        )

    def _check_sudden_temp_rise(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if not history:
            return None
        prev_temp = history[0].temperature
        delta = new.temperature - prev_temp
        if delta <= 5.0:
            return None
        return _event(
            city=new.city,
            event_type="sudden_temp_rise",
            timestamp=new.timestamp,
            summary=f"{new.city} temperature rose {delta:.1f}°C in one reading.",
            reason=(
                f"Temperature rose from {prev_temp:.1f}°C to {new.temperature:.1f}°C "
                f"(rise of {delta:.1f}°C), exceeding the 5°C threshold."
            ),
            metrics={"previous_temperature": prev_temp, "temperature": new.temperature, "delta": delta},
        )

    def _check_city_anomaly(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if len(history) < 6:
            return None
        temps = [r.temperature for r in history]
        mean = statistics.mean(temps)
        stddev = statistics.stdev(temps)
        if stddev == 0:
            return None
        z = (new.temperature - mean) / stddev
        if abs(z) <= 2.0:
            return None
        direction = "above" if z > 0 else "below"
        return _event(
            city=new.city,
            event_type="city_anomaly",
            timestamp=new.timestamp,
            summary=f"{new.city} temperature is unusually {direction} its recent average.",
            reason=(
                f"Temperature {new.temperature:.1f}°C is {abs(z):.2f} standard deviations "
                f"{direction} the {len(history)}-reading mean of {mean:.1f}°C "
                f"(stddev {stddev:.2f}°C), exceeding the 2σ threshold."
            ),
            metrics={"temperature": new.temperature, "mean": round(mean, 2), "stddev": round(stddev, 2), "z_score": round(z, 2)},
        )

    def _check_feels_like_gap(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        gap = abs(new.apparent_temperature - new.temperature)
        if gap <= 8.0:
            return None
        direction = "colder" if new.apparent_temperature < new.temperature else "warmer"
        return _event(
            city=new.city,
            event_type="feels_like_gap",
            timestamp=new.timestamp,
            summary=f"{new.city} feels {gap:.0f}°C {direction} than the actual temperature.",
            reason=(
                f"Apparent temp ({new.apparent_temperature:.1f}°C) deviates {gap:.1f}°C "
                f"from actual ({new.temperature:.1f}°C), exceeding the 8°C threshold."
            ),
            metrics={"temperature": new.temperature, "apparent_temperature": new.apparent_temperature, "gap": round(gap, 2)},
        )

    def _check_dangerous_wind(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if new.wind_speed <= 80.0:
            return None
        return _event(
            city=new.city,
            event_type="dangerous_wind",
            timestamp=new.timestamp,
            summary=f"{new.city} is experiencing dangerous wind speeds of {new.wind_speed:.0f} km/h.",
            reason=(
                f"Wind speed {new.wind_speed:.1f} km/h exceeds the 80 km/h danger threshold."
            ),
            metrics={"wind_speed": new.wind_speed},
        )

    def _check_wind_shift(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if not history:
            return None
        prev_wind = history[0].wind_speed
        delta = abs(new.wind_speed - prev_wind)
        if delta <= 40.0:
            return None
        return _event(
            city=new.city,
            event_type="wind_shift",
            timestamp=new.timestamp,
            summary=f"{new.city} wind speed changed sharply by {delta:.0f} km/h.",
            reason=(
                f"Wind shifted from {prev_wind:.1f} km/h to {new.wind_speed:.1f} km/h "
                f"(change of {delta:.1f} km/h), exceeding the 40 km/h threshold."
            ),
            metrics={"previous_wind_speed": prev_wind, "wind_speed": new.wind_speed, "delta": round(delta, 1)},
        )

    def _check_heavy_precipitation(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if new.precipitation <= 10.0:
            return None
        return _event(
            city=new.city,
            event_type="heavy_precipitation",
            timestamp=new.timestamp,
            summary=f"{new.city} recorded {new.precipitation:.1f} mm of precipitation in one hour.",
            reason=(
                f"Precipitation {new.precipitation:.1f} mm exceeds the 10 mm/h heavy threshold."
            ),
            metrics={"precipitation": new.precipitation},
        )

    def _check_precip_streak(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        # Need the two most recent prior readings plus the new one to form a streak of 3.
        if len(history) < 2:
            return None
        streak = [new.precipitation] + [r.precipitation for r in history[:2]]
        if not all(p > 0.5 for p in streak):
            return None
        total = round(sum(streak), 2)
        return _event(
            city=new.city,
            event_type="precip_streak",
            timestamp=new.timestamp,
            summary=f"{new.city} has had continuous precipitation across the last 3 readings.",
            reason=(
                f"All 3 consecutive readings exceeded 0.5 mm (values: "
                f"{streak[2]:.1f}, {streak[1]:.1f}, {streak[0]:.1f} mm). "
                f"Total accumulated: {total:.2f} mm."
            ),
            metrics={"streak_length": 3, "total_precipitation": total, "readings": streak},
        )

    def _check_weather_code_severity(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if not history:
            return None
        prev_tier = _wmo_tier(history[0].weather_code)
        new_tier = _wmo_tier(new.weather_code)
        if new_tier == prev_tier or _SEVERITY_TIER[new_tier] <= _SEVERITY_TIER[prev_tier]:
            return None
        return _event(
            city=new.city,
            event_type="weather_code_severity",
            timestamp=new.timestamp,
            summary=f"{new.city} weather has escalated to {new_tier.replace('_', ' ')} conditions.",
            reason=(
                f"WMO code changed from {history[0].weather_code} ({prev_tier}) "
                f"to {new.weather_code} ({new_tier}), crossing into a higher severity tier."
            ),
            metrics={
                "previous_weather_code": history[0].weather_code,
                "weather_code": new.weather_code,
                "previous_tier": prev_tier,
                "new_tier": new_tier,
            },
        )
