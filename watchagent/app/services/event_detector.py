"""Weather event detection for WatchAgent.

Design philosophy
-----------------
Each check is a single-responsibility private method that receives the new
reading and the ordered history (newest-first). Methods return either an event
dict matching the event_schema rule exactly, or None. They are pure functions of
their inputs — no side effects, no I/O.

Two public detection paths:
  - ``detect_events``        — nine per-city checks, called once per reading.
  - ``detect_cross_city_events`` — one cross-city check, called once per poll
                                   cycle after all three cities are processed.

All thresholds are calibrated for Canadian cities (Ottawa, Toronto, Vancouver)
where ±5 °C hourly swings, 80 km/h winds, and moderate winter precipitation
are plausible but not routine. Module-level constants collect every threshold
so the full calibration picture is visible without reading method bodies.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.models.reading import WeatherReading
from app.services.weather_client import RawReading

_logger = structlog.get_logger(__name__)

EventDict = dict[str, Any]

_COOLDOWN = timedelta(hours=3)

# Minimum absolute drop/rise before the adaptive threshold kicks in.
# When history is available, the threshold is max(_MIN_TEMP_DELTA, stddev * 2)
# so that the same 5 °C change is correctly more alarming in stable Vancouver
# (low stddev) than in volatile Ottawa (high stddev).
_MIN_TEMP_DELTA = 5.0

# Wind speed above which conditions are dangerous (km/h).
# Matches Environment Canada's wind warning criterion.
_DANGEROUS_WIND_KMH = 80.0

# Minimum wind speed change across two consecutive readings to fire wind_shift.
_WIND_SHIFT_DELTA_KMH = 40.0

# Precipitation rate (mm/h) that qualifies as heavy.
_HEAVY_PRECIP_MM = 10.0

# Minimum precipitation (mm) per reading to count toward a precip streak.
_PRECIP_STREAK_MIN_MM = 0.5

# Temperature divergence (°C) between one city and the average of the other
# two before cross_city_divergence fires.  Same-day inter-city gaps above 15 °C
# are rare in southern Canada and imply genuinely different weather systems.
_CROSS_CITY_DIVERGENCE_THRESHOLD = 15.0

# Minimum recent readings required before city_anomaly can fire.
# Fewer than 6 points yields an unreliable standard deviation estimate.
_CITY_ANOMALY_MIN_HISTORY = 6

# Z-score above which a temperature reading is classified as a city anomaly.
# |z| > 2 captures roughly the outer 5% of a normal distribution.
_CITY_ANOMALY_Z_THRESHOLD = 2.0

# Apparent temperature gap (°C) above which feels_like_gap fires.
# 8°C is a meaningful discomfort threshold: wind chill or heat index at this
# level produces a distinctly different felt experience from the thermometer.
_FEELS_LIKE_GAP_THRESHOLD = 8.0

# Number of consecutive readings (including the current one) that must all
# exceed _PRECIP_STREAK_MIN_MM before precip_streak fires.
_PRECIP_STREAK_LENGTH = 3

# WMO code severity tiers
_SEVERITY_TIER: dict[str, int] = {
    "thunderstorm": 95,
    "heavy_snow": 75,
    "heavy_rain": 65,
    "light": 0,
}

def _wmo_tier(code: int) -> str:
    """Map a WMO weather code to one of four named severity tiers."""
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

    def _temp_delta_threshold(self, history: list[WeatherReading]) -> float:
        """Return an adaptive threshold based on the city's observed temperature variability.

        When fewer than 6 readings exist, falls back to the fixed minimum.
        When history is available, uses 2× the recent stddev so that the same
        absolute change is correctly more alarming in stable Vancouver (low
        stddev, oceanic climate) than in volatile Ottawa (high stddev,
        continental climate).
        """
        if len(history) < 6:
            return _MIN_TEMP_DELTA
        temps = [r.temperature for r in history]
        stddev = statistics.stdev(temps)
        return max(_MIN_TEMP_DELTA, stddev * 2)

    def _check_sudden_temp_drop(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if not history:
            return None
        prev_temp = history[0].temperature
        delta = prev_temp - new.temperature
        threshold = self._temp_delta_threshold(history)
        if delta <= threshold:
            return None
        return _event(
            city=new.city,
            event_type="sudden_temp_drop",
            timestamp=new.timestamp,
            summary=(
                f"{new.city} dropped {delta:.1f}°C in a single poll — "
                f"from {prev_temp:.1f}°C down to {new.temperature:.1f}°C."
            ),
            reason=(
                f"Temperature fell {delta:.1f}°C in one reading "
                f"(from {prev_temp:.1f}°C to {new.temperature:.1f}°C), "
                f"exceeding the city-adaptive threshold of {threshold:.1f}°C "
                f"(2× recent temperature standard deviation, floored at {_MIN_TEMP_DELTA}°C)."
            ),
            metrics={
                "previous_temperature": prev_temp,
                "temperature": new.temperature,
                "delta": round(delta, 2),
                "threshold": round(threshold, 2),
            },
        )

    def _check_sudden_temp_rise(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if not history:
            return None
        prev_temp = history[0].temperature
        delta = new.temperature - prev_temp
        threshold = self._temp_delta_threshold(history)
        if delta <= threshold:
            return None
        return _event(
            city=new.city,
            event_type="sudden_temp_rise",
            timestamp=new.timestamp,
            summary=(
                f"{new.city} surged {delta:.1f}°C in a single poll — "
                f"from {prev_temp:.1f}°C up to {new.temperature:.1f}°C."
            ),
            reason=(
                f"Temperature rose {delta:.1f}°C in one reading "
                f"(from {prev_temp:.1f}°C to {new.temperature:.1f}°C), "
                f"exceeding the city-adaptive threshold of {threshold:.1f}°C "
                f"(2× recent temperature standard deviation, floored at {_MIN_TEMP_DELTA}°C)."
            ),
            metrics={
                "previous_temperature": prev_temp,
                "temperature": new.temperature,
                "delta": round(delta, 2),
                "threshold": round(threshold, 2),
            },
        )

    def _check_city_anomaly(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if len(history) < _CITY_ANOMALY_MIN_HISTORY:
            return None
        temps = [r.temperature for r in history]
        mean = statistics.mean(temps)
        stddev = statistics.stdev(temps)
        if stddev == 0:
            return None
        z = (new.temperature - mean) / stddev
        if abs(z) <= _CITY_ANOMALY_Z_THRESHOLD:
            return None
        direction = "above" if z > 0 else "below"
        return _event(
            city=new.city,
            event_type="city_anomaly",
            timestamp=new.timestamp,
            summary=(
                f"{new.city} temperature ({new.temperature:.1f}°C) is {abs(z):.1f}σ {direction} "
                f"its {len(history)}-reading baseline — statistically anomalous for this city."
            ),
            reason=(
                f"Current temperature ({new.temperature:.1f}°C) is {abs(z):.2f}σ {direction} "
                f"the {len(history)}-reading mean ({mean:.1f}°C, σ={stddev:.2f}°C), "
                f"exceeding the {_CITY_ANOMALY_Z_THRESHOLD:.1f}σ anomaly threshold."
            ),
            metrics={
                "temperature": new.temperature,
                "mean": round(mean, 2),
                "stddev": round(stddev, 2),
                "z_score": round(z, 2),
            },
        )

    def _check_feels_like_gap(
        self, new: RawReading, history: list[WeatherReading]  # history unused — pure single-reading check
    ) -> EventDict | None:
        gap = abs(new.apparent_temperature - new.temperature)
        if gap <= _FEELS_LIKE_GAP_THRESHOLD:
            return None
        direction = "colder" if new.apparent_temperature < new.temperature else "warmer"
        cause = "wind chill" if new.apparent_temperature < new.temperature else "heat and humidity"
        return _event(
            city=new.city,
            event_type="feels_like_gap",
            timestamp=new.timestamp,
            summary=(
                f"{new.city} feels {gap:.1f}°C {direction} than the thermometer reads: "
                f"apparent {new.apparent_temperature:.1f}°C vs actual {new.temperature:.1f}°C ({cause})."
            ),
            reason=(
                f"Apparent temperature ({new.apparent_temperature:.1f}°C) deviates {gap:.1f}°C "
                f"from actual ({new.temperature:.1f}°C), exceeding the "
                f"{_FEELS_LIKE_GAP_THRESHOLD:.1f}°C gap threshold. "
                f"The gap is driven by {cause}."
            ),
            metrics={
                "temperature": new.temperature,
                "apparent_temperature": new.apparent_temperature,
                "gap": round(gap, 2),
            },
        )

    def _check_dangerous_wind(
        self, new: RawReading, history: list[WeatherReading]  # history unused — pure single-reading check
    ) -> EventDict | None:
        if new.wind_speed <= _DANGEROUS_WIND_KMH:
            return None
        excess = round(new.wind_speed - _DANGEROUS_WIND_KMH, 1)
        return _event(
            city=new.city,
            event_type="dangerous_wind",
            timestamp=new.timestamp,
            summary=(
                f"{new.city} wind reached {new.wind_speed:.0f} km/h — "
                f"{excess:.0f} km/h above the {_DANGEROUS_WIND_KMH:.0f} km/h "
                f"Environment Canada warning threshold."
            ),
            reason=(
                f"Recorded wind speed of {new.wind_speed:.1f} km/h exceeds the "
                f"{_DANGEROUS_WIND_KMH:.0f} km/h danger threshold "
                f"(Environment Canada wind warning criterion) by {excess:.1f} km/h."
            ),
            metrics={
                "wind_speed": new.wind_speed,
                "threshold": _DANGEROUS_WIND_KMH,
                "excess_over_threshold": excess,
            },
        )

    def _check_wind_shift(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        if not history:
            return None
        prev_wind = history[0].wind_speed
        # abs() intentional: both sudden calms and sudden gusts indicate frontal passage.
        delta = abs(new.wind_speed - prev_wind)
        if delta <= _WIND_SHIFT_DELTA_KMH:
            return None
        shift_verb = "surged" if new.wind_speed > prev_wind else "dropped"
        return _event(
            city=new.city,
            event_type="wind_shift",
            timestamp=new.timestamp,
            summary=(
                f"{new.city} wind {shift_verb} {delta:.0f} km/h between readings — "
                f"from {prev_wind:.0f} to {new.wind_speed:.0f} km/h."
            ),
            reason=(
                f"Wind speed changed from {prev_wind:.1f} km/h to {new.wind_speed:.1f} km/h "
                f"in a single poll interval (change of {delta:.1f} km/h), exceeding the "
                f"{_WIND_SHIFT_DELTA_KMH:.0f} km/h rapid-shift threshold. "
                f"Both sudden gusts and sudden calms can indicate frontal passage."
            ),
            metrics={
                "previous_wind_speed": prev_wind,
                "wind_speed": new.wind_speed,
                "delta": round(delta, 1),
                "threshold": _WIND_SHIFT_DELTA_KMH,
            },
        )

    def _check_heavy_precipitation(
        self, new: RawReading, history: list[WeatherReading]  # history unused — pure single-reading check
    ) -> EventDict | None:
        if new.precipitation <= _HEAVY_PRECIP_MM:
            return None
        multiplier = round(new.precipitation / _HEAVY_PRECIP_MM, 1)
        excess = round(new.precipitation - _HEAVY_PRECIP_MM, 1)
        return _event(
            city=new.city,
            event_type="heavy_precipitation",
            timestamp=new.timestamp,
            summary=(
                f"{new.city} recorded {new.precipitation:.1f} mm in one hour — "
                f"{multiplier:.1f}× the {_HEAVY_PRECIP_MM:.0f} mm/h heavy precipitation threshold."
            ),
            reason=(
                f"Hourly precipitation of {new.precipitation:.1f} mm exceeds the "
                f"{_HEAVY_PRECIP_MM:.0f} mm/h heavy precipitation threshold by {excess:.1f} mm."
            ),
            metrics={
                "precipitation": new.precipitation,
                "threshold": _HEAVY_PRECIP_MM,
                "excess_over_threshold": excess,
                "multiplier": multiplier,
            },
        )

    def _check_precip_streak(
        self, new: RawReading, history: list[WeatherReading]
    ) -> EventDict | None:
        # Require _PRECIP_STREAK_LENGTH - 1 prior readings plus the current one.
        if len(history) < _PRECIP_STREAK_LENGTH - 1:
            return None
        streak = [new.precipitation] + [
            r.precipitation for r in history[: _PRECIP_STREAK_LENGTH - 1]
        ]
        if not all(p > _PRECIP_STREAK_MIN_MM for p in streak):
            return None
        total = round(sum(streak), 2)
        return _event(
            city=new.city,
            event_type="precip_streak",
            timestamp=new.timestamp,
            summary=(
                f"{new.city} has measured precipitation in each of the last "
                f"{_PRECIP_STREAK_LENGTH} consecutive readings — {total:.2f} mm accumulated."
            ),
            reason=(
                f"{_PRECIP_STREAK_LENGTH} consecutive readings all exceeded the "
                f"{_PRECIP_STREAK_MIN_MM} mm trace-precipitation threshold: "
                f"{streak[2]:.1f} mm \u2192 {streak[1]:.1f} mm \u2192 {streak[0]:.1f} mm "
                f"(oldest to newest). Total accumulated: {total:.2f} mm."
            ),
            metrics={
                "streak_length": _PRECIP_STREAK_LENGTH,
                "total_precipitation": total,
                "readings": streak,
            },
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
            summary=(
                f"{new.city} conditions escalated from {prev_tier.replace('_', ' ')} "
                f"to {new_tier.replace('_', ' ')} — "
                f"WMO code moved from {history[0].weather_code} to {new.weather_code}."
            ),
            reason=(
                f"WMO weather code escalated from {history[0].weather_code} "
                f"({prev_tier.replace('_', ' ')}) to {new.weather_code} "
                f"({new_tier.replace('_', ' ')}), crossing into a higher severity tier. "
                f"Tier order (ascending): light \u2192 heavy rain \u2192 heavy snow \u2192 thunderstorm."
            ),
            metrics={
                "previous_weather_code": history[0].weather_code,
                "weather_code": new.weather_code,
                "previous_tier": prev_tier,
                "new_tier": new_tier,
            },
        )

    # ------------------------------------------------------------------
    # Cross-city comparison (called from Poller after all cities fetched)
    # ------------------------------------------------------------------

    def detect_cross_city_events(
        self,
        readings: dict[str, RawReading],
    ) -> list[EventDict]:
        """Detect events that require comparing conditions across all three cities.

        Args:
            readings: Mapping of city name → the latest RawReading for that city.
                      Only fires when all three cities have a reading available.

        Returns:
            Cooldown-filtered list of cross-city event dicts.
        """
        if len(readings) < 3:
            return []

        events: list[EventDict] = []
        result = self._check_cross_city_divergence(readings)
        if result is None:
            return []

        now = datetime.now(tz=timezone.utc)
        key = (result["city"], result["event_type"])
        last = self._last_fired.get(key)
        if last is None or (now - last) >= _COOLDOWN:
            self._last_fired[key] = now
            _logger.info(
                "event_fired",
                city=result["city"],
                event_type=result["event_type"],
                timestamp=str(result["timestamp"]),
                summary=result["summary"],
            )
            events.append(result)

        return events

    def _check_cross_city_divergence(
        self,
        readings: dict[str, RawReading],
    ) -> EventDict | None:
        """Fire when one city's temperature diverges more than _CROSS_CITY_DIVERGENCE_THRESHOLD °C from the other two."""
        cities = list(readings.keys())
        temps = {c: readings[c].temperature for c in cities}

        for city in cities:
            others = [c for c in cities if c != city]
            other_avg = sum(temps[c] for c in others) / len(others)
            divergence = abs(temps[city] - other_avg)
            if divergence <= _CROSS_CITY_DIVERGENCE_THRESHOLD:
                continue
            direction = "warmer" if temps[city] > other_avg else "colder"
            # Use the outlier city as "city" in the event
            ref_str = " and ".join(f"{c} ({temps[c]:.1f}°C)" for c in others)
            return _event(
                city=city,
                event_type="cross_city_divergence",
                timestamp=readings[city].timestamp,
                summary=(
                    f"{city} ({temps[city]:.1f}°C) is {divergence:.1f}°C {direction} than the "
                    f"{other_avg:.1f}°C average of the other monitored cities — "
                    f"a distinct weather system is likely."
                ),
                reason=(
                    f"{city} temperature ({temps[city]:.1f}°C) diverges {divergence:.1f}°C from "
                    f"the mean of {ref_str} (average {other_avg:.1f}°C), exceeding the "
                    f"{_CROSS_CITY_DIVERGENCE_THRESHOLD:.0f}°C inter-city divergence threshold. "
                    f"Gaps this large across southern Canada indicate genuinely separate weather systems."
                ),
                metrics={
                    "temperature": temps[city],
                    "other_average": round(other_avg, 2),
                    "divergence": round(divergence, 2),
                    "other_cities": {c: temps[c] for c in others},
                },
            )
        return None
