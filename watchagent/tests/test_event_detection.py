"""Tests for all EventDetector checks (nine per-city + one cross-city).

Each per-city event type gets:
  - A "fires" test: value clearly crosses the threshold.
  - A "no-fire" test: value just below the threshold (or a cold-start guard).
  - A cooldown test: same condition on back-to-back calls — second is suppressed.

The dual-assertion (fire + no-fire) and cooldown pattern is required by the
event_detection_reviewer agent.

EventDetector and WeatherReading are instantiated directly — no DB is needed
because the detector is a pure function of its inputs.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.reading import WeatherReading
from app.services.event_detector import EventDetector
from app.services.weather_client import RawReading
from tests.conftest import sample_reading

_TS = datetime(2024, 1, 15, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _history_reading(
    temperature: float = 5.0,
    wind_speed: float = 10.0,
    precipitation: float = 0.0,
    weather_code: int = 0,
) -> WeatherReading:
    """Construct an unsaved WeatherReading for use as history."""
    return WeatherReading(
        city="Ottawa",
        timestamp=_TS - timedelta(hours=1),
        temperature=temperature,
        apparent_temperature=temperature,
        precipitation=precipitation,
        wind_speed=wind_speed,
        weather_code=weather_code,
    )


def _detect(reading, history=None):
    return EventDetector().detect_events(reading, history or [])


def _event_types(events):
    return {e["event_type"] for e in events}


# ---------------------------------------------------------------------------
# sudden_temp_drop
# ---------------------------------------------------------------------------

def test_sudden_temp_drop_fires():
    """7°C drop (prev=15, new=8) clears the 5°C threshold."""
    history = [_history_reading(temperature=15.0)]
    events = _detect(sample_reading(temperature=8.0), history)
    assert "sudden_temp_drop" in _event_types(events)


def test_sudden_temp_drop_no_fire():
    """2°C drop (prev=15, new=13) is below the 5°C threshold."""
    history = [_history_reading(temperature=15.0)]
    events = _detect(sample_reading(temperature=13.0), history)
    assert "sudden_temp_drop" not in _event_types(events)


def test_sudden_temp_drop_no_history():
    """No history → cold-start guard prevents false positive."""
    events = _detect(sample_reading(temperature=8.0), [])
    assert "sudden_temp_drop" not in _event_types(events)


# ---------------------------------------------------------------------------
# sudden_temp_rise
# ---------------------------------------------------------------------------

def test_sudden_temp_rise_fires():
    """7°C rise (prev=5, new=12) clears the 5°C threshold."""
    history = [_history_reading(temperature=5.0)]
    events = _detect(sample_reading(temperature=12.0), history)
    assert "sudden_temp_rise" in _event_types(events)


def test_sudden_temp_rise_no_fire():
    """3°C rise (prev=5, new=8) is below the 5°C threshold."""
    history = [_history_reading(temperature=5.0)]
    events = _detect(sample_reading(temperature=8.0), history)
    assert "sudden_temp_rise" not in _event_types(events)


def test_sudden_temp_rise_no_history():
    """Cold-start guard: no history → cannot compute a delta, must not fire."""
    events = _detect(sample_reading(temperature=12.0), [])
    assert "sudden_temp_rise" not in _event_types(events)


# ---------------------------------------------------------------------------
# city_anomaly
# ---------------------------------------------------------------------------

def test_city_anomaly_requires_history():
    """Fewer than 6 history readings → cold-start guard suppresses the check."""
    history = [_history_reading(temperature=20.0) for _ in range(3)]
    events = _detect(sample_reading(temperature=35.0), history)
    assert "city_anomaly" not in _event_types(events)


def test_city_anomaly_fires():
    """35°C reading against a 20°C mean (tiny stddev) is a clear anomaly (z >> 2)."""
    # Slight variation so stddev > 0; mean ≈ 20°C, stddev < 1°C.
    temps = [20.0, 19.9, 20.1, 20.0, 19.8, 20.2, 20.0, 19.9, 20.1, 20.0]
    history = [_history_reading(temperature=t) for t in temps]
    events = _detect(sample_reading(temperature=35.0), history)
    assert "city_anomaly" in _event_types(events)
    event = next(e for e in events if e["event_type"] == "city_anomaly")
    assert event["metrics"]["z_score"] > 2.0


def test_city_anomaly_no_fire_within_normal_range():
    """A reading well within 2σ of the mean does not fire.

    history stddev ≈ 0.12°C; 20.1°C gives z ≈ 0.83 — well under the 2σ threshold.
    apparent_temperature matches temperature to avoid a co-firing feels_like_gap.
    """
    temps = [20.0, 19.9, 20.1, 20.0, 19.8, 20.2, 20.0, 19.9, 20.1, 20.0]
    history = [_history_reading(temperature=t) for t in temps]
    events = _detect(
        sample_reading(temperature=20.1, apparent_temperature=20.1), history
    )
    assert "city_anomaly" not in _event_types(events)


# ---------------------------------------------------------------------------
# feels_like_gap
# ---------------------------------------------------------------------------

def test_feels_like_gap_fires():
    """11°C gap (temp=5, apparent=-6) exceeds the 8°C threshold."""
    events = _detect(sample_reading(temperature=5.0, apparent_temperature=-6.0))
    assert "feels_like_gap" in _event_types(events)
    event = next(e for e in events if e["event_type"] == "feels_like_gap")
    assert event["metrics"]["gap"] == pytest.approx(11.0)


def test_feels_like_gap_no_fire():
    """6°C gap (temp=5, apparent=-1) is exactly at the 6°C boundary — must not fire (≤ check)."""
    events = _detect(sample_reading(temperature=5.0, apparent_temperature=-1.0))
    assert "feels_like_gap" not in _event_types(events)


def test_feels_like_gap_warmer_direction():
    """Gap fires when apparent is warmer than actual (e.g. high humidity)."""
    events = _detect(sample_reading(temperature=30.0, apparent_temperature=40.0))
    assert "feels_like_gap" in _event_types(events)
    event = next(e for e in events if e["event_type"] == "feels_like_gap")
    assert "warmer" in event["summary"]


# ---------------------------------------------------------------------------
# dangerous_wind
# ---------------------------------------------------------------------------

def test_dangerous_wind_fires():
    """90 km/h exceeds the 80 km/h threshold."""
    events = _detect(sample_reading(wind_speed=90.0))
    assert "dangerous_wind" in _event_types(events)


def test_dangerous_wind_no_fire():
    """69 km/h is below the 70 km/h threshold."""
    events = _detect(sample_reading(wind_speed=69.0))
    assert "dangerous_wind" not in _event_types(events)


# ---------------------------------------------------------------------------
# wind_shift
# ---------------------------------------------------------------------------

def test_wind_shift_fires():
    """50 km/h change (prev=10, new=60) exceeds the 30 km/h threshold."""
    history = [_history_reading(wind_speed=10.0)]
    events = _detect(sample_reading(wind_speed=60.0), history)
    assert "wind_shift" in _event_types(events)


def test_wind_shift_no_fire():
    """20 km/h change (prev=10, new=30) is below the 30 km/h threshold."""
    history = [_history_reading(wind_speed=10.0)]
    events = _detect(sample_reading(wind_speed=30.0), history)
    assert "wind_shift" not in _event_types(events)


# ---------------------------------------------------------------------------
# heavy_precipitation
# ---------------------------------------------------------------------------

def test_heavy_precipitation_fires():
    """15 mm exceeds the 10 mm/h threshold."""
    events = _detect(sample_reading(precipitation=15.0))
    assert "heavy_precipitation" in _event_types(events)


def test_heavy_precipitation_no_fire():
    """7 mm is below the 7.5 mm/h threshold."""
    events = _detect(sample_reading(precipitation=7.0))
    assert "heavy_precipitation" not in _event_types(events)


# ---------------------------------------------------------------------------
# precip_streak
# ---------------------------------------------------------------------------

def test_precip_streak_fires():
    """Three consecutive readings all above 0.5 mm triggers a streak."""
    history = [
        _history_reading(precipitation=2.0),
        _history_reading(precipitation=2.0),
    ]
    events = _detect(sample_reading(precipitation=2.0), history)
    assert "precip_streak" in _event_types(events)
    event = next(e for e in events if e["event_type"] == "precip_streak")
    assert event["metrics"]["streak_length"] == 3
    assert event["metrics"]["total_precipitation"] == pytest.approx(6.0)


def test_precip_streak_no_fire_insufficient_history():
    """Only one prior reading — need two to complete a streak of 3."""
    history = [_history_reading(precipitation=2.0)]
    events = _detect(sample_reading(precipitation=2.0), history)
    assert "precip_streak" not in _event_types(events)


def test_precip_streak_no_fire_dry_reading_in_history():
    """A dry reading in the window breaks the streak."""
    history = [
        _history_reading(precipitation=0.0),  # dry — breaks streak
        _history_reading(precipitation=2.0),
    ]
    events = _detect(sample_reading(precipitation=2.0), history)
    assert "precip_streak" not in _event_types(events)


# ---------------------------------------------------------------------------
# weather_code_severity
# ---------------------------------------------------------------------------

def test_weather_code_severity_fires_on_escalation():
    """Code escalating from light (0) to heavy rain (65) fires."""
    history = [_history_reading(weather_code=0)]
    events = _detect(sample_reading(weather_code=65), history)
    assert "weather_code_severity" in _event_types(events)


def test_weather_code_severity_no_fire_same_tier():
    """Moving from 65 to 70 stays in the same heavy-rain tier — no fire."""
    history = [_history_reading(weather_code=65)]
    events = _detect(sample_reading(weather_code=70), history)
    assert "weather_code_severity" not in _event_types(events)


def test_weather_code_severity_no_fire_on_improvement():
    """Dropping from heavy rain to light is an improvement — no fire."""
    history = [_history_reading(weather_code=65)]
    events = _detect(sample_reading(weather_code=1), history)
    assert "weather_code_severity" not in _event_types(events)


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

def test_cooldown_prevents_spam_within_3_hours():
    """The same (city, event_type) pair is blocked on a second call within 3 hours."""
    detector = EventDetector()
    reading = sample_reading(temperature=5.0, apparent_temperature=-6.0)  # 11°C gap

    first = detector.detect_events(reading, history=[])
    assert "feels_like_gap" in _event_types(first)

    # Second call on the same detector instance — cooldown not yet expired.
    second = detector.detect_events(reading, history=[])
    assert "feels_like_gap" not in _event_types(second)


def test_sudden_temp_drop_adaptive_threshold_low_stddev():
    """With low-stddev history (stable city like Vancouver), the threshold adapts above 5 °C.

    6 identical readings → stddev is very low → threshold stays at the 5 °C floor.
    A 5.1 °C drop still fires.
    """
    history = [_history_reading(temperature=20.0) for _ in range(6)]
    reading = sample_reading(temperature=14.8, apparent_temperature=14.8)  # drop of 5.2 °C
    events = _detect(reading, history)
    assert "sudden_temp_drop" in _event_types(events)


def test_sudden_temp_drop_adaptive_threshold_high_stddev():
    """With high-stddev history (volatile city), the threshold rises above 5 °C so
    a marginal 5 °C drop that would fire with no history does NOT fire.

    Stddev of [5, 15, 5, 15, 5, 15] ≈ 5.48, threshold = max(5.0, 5.48 * 2) ≈ 10.96 °C.
    A 6 °C drop falls below that and should not fire.
    """
    history = [
        _history_reading(temperature=t) for t in [5, 15, 5, 15, 5, 15]
    ]
    reading = sample_reading(temperature=9.0, apparent_temperature=9.0)  # prev=15, drop=6 °C
    events = _detect(reading, history)
    assert "sudden_temp_drop" not in _event_types(events)


def test_adaptive_threshold_recorded_in_metrics():
    """The threshold used should be recorded in the event metrics."""
    history = [_history_reading(temperature=20.0) for _ in range(6)]
    reading = sample_reading(temperature=14.0, apparent_temperature=14.0)  # drop of 6 °C
    events = _detect(reading, history)
    temp_events = [e for e in events if e["event_type"] == "sudden_temp_drop"]
    assert temp_events, "expected sudden_temp_drop to fire"
    assert "threshold" in temp_events[0]["metrics"]


def test_cross_city_divergence_fires():
    """Ottawa=-15, Toronto=-5, Vancouver=10 → spread=25°C > 20°C threshold → fires."""
    detector = EventDetector()
    readings = {
        "Ottawa":    sample_reading(city="Ottawa",    temperature=-15.0, apparent_temperature=-15.0),
        "Toronto":   sample_reading(city="Toronto",   temperature=-5.0,  apparent_temperature=-5.0),
        "Vancouver": sample_reading(city="Vancouver", temperature=10.0,  apparent_temperature=10.0),
    }
    events = detector.detect_cross_city_events(readings)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "cross_city_divergence"
    assert e["city"] == "ALL"
    assert e["metrics"]["spread"] == 25.0
    assert e["metrics"]["warmest"] == "Vancouver"
    assert e["metrics"]["coldest"] == "Ottawa"
    assert e["metrics"]["ottawa"] == -15.0
    assert e["metrics"]["toronto"] == -5.0
    assert e["metrics"]["vancouver"] == 10.0


def test_cross_city_divergence_no_fire():
    """All three cities within 15°C of each other → spread=12°C < 20°C → no fire."""
    detector = EventDetector()
    readings = {
        "Ottawa":    sample_reading(city="Ottawa",    temperature=0.0,  apparent_temperature=0.0),
        "Toronto":   sample_reading(city="Toronto",   temperature=8.0,  apparent_temperature=8.0),
        "Vancouver": sample_reading(city="Vancouver", temperature=12.0, apparent_temperature=12.0),
    }
    # spread = 12 - 0 = 12°C < 20°C → should not fire
    events = detector.detect_cross_city_events(readings)
    assert not events


def test_cross_city_divergence_requires_all_three_cities():
    """If fewer than 3 cities have readings, the check must not fire."""
    detector = EventDetector()
    readings = {
        "Vancouver": sample_reading(city="Vancouver", temperature=20.0, apparent_temperature=20.0),
    }
    events = detector.detect_cross_city_events(readings)
    assert not events


def test_cross_city_divergence_cooldown():
    """A second call with the same divergent conditions within the cooldown window is suppressed."""
    detector = EventDetector()
    readings = {
        "Ottawa":    sample_reading(city="Ottawa",    temperature=-15.0, apparent_temperature=-15.0),
        "Toronto":   sample_reading(city="Toronto",   temperature=-5.0,  apparent_temperature=-5.0),
        "Vancouver": sample_reading(city="Vancouver", temperature=10.0,  apparent_temperature=10.0),
    }
    first = detector.detect_cross_city_events(readings)
    assert first, "expected first call to fire"
    assert first[0]["city"] == "ALL"
    second = detector.detect_cross_city_events(readings)
    assert not second, "expected cooldown to suppress second call"


def test_cooldown_is_per_city():
    """Cooldown for Ottawa does not suppress the same event type for Toronto."""
    detector = EventDetector()
    reading_ottawa = sample_reading(city="Ottawa", temperature=5.0, apparent_temperature=-6.0)
    reading_toronto = sample_reading(city="Toronto", temperature=5.0, apparent_temperature=-6.0)

    detector.detect_events(reading_ottawa, history=[])
    toronto_events = detector.detect_events(reading_toronto, history=[])
    assert "feels_like_gap" in _event_types(toronto_events)


def test_event_schema_has_all_required_keys():
    """Every fired event contains exactly the 6 keys required by event_schema rule."""
    required = {"city", "event_type", "timestamp", "summary", "reason", "metrics"}
    events = _detect(sample_reading(temperature=5.0, apparent_temperature=-6.0))
    assert events, "expected at least one event to fire"
    for event in events:
        assert set(event.keys()) >= required, f"Missing keys in: {event}"


# ---------------------------------------------------------------------------
# WMO tier coverage — thunderstorm and heavy_snow escalation paths
# These cover the two _wmo_tier branches that no earlier test reached.
# ---------------------------------------------------------------------------

def test_weather_code_escalation_to_heavy_snow():
    """Escalation from light (0) to heavy snow (75) fires and records new_tier='heavy_snow'."""
    history = [_history_reading(weather_code=0)]
    events = _detect(sample_reading(weather_code=75), history)
    assert "weather_code_severity" in _event_types(events)
    event = next(e for e in events if e["event_type"] == "weather_code_severity")
    assert event["metrics"]["new_tier"] == "heavy_snow"


def test_weather_code_escalation_to_thunderstorm():
    """Escalation from light (0) to thunderstorm (95) fires and records new_tier='thunderstorm'."""
    history = [_history_reading(weather_code=0)]
    events = _detect(sample_reading(weather_code=95), history)
    assert "weather_code_severity" in _event_types(events)
    event = next(e for e in events if e["event_type"] == "weather_code_severity")
    assert event["metrics"]["new_tier"] == "thunderstorm"


def test_weather_code_escalation_heavy_rain_to_heavy_snow():
    """Escalation from heavy rain (65) to heavy snow (75) is a tier upgrade."""
    history = [_history_reading(weather_code=65)]
    events = _detect(sample_reading(weather_code=75), history)
    assert "weather_code_severity" in _event_types(events)
    event = next(e for e in events if e["event_type"] == "weather_code_severity")
    assert event["metrics"]["previous_tier"] == "heavy_rain"
    assert event["metrics"]["new_tier"] == "heavy_snow"


# ---------------------------------------------------------------------------
# Per-event-type cooldown — parametrized over all 9 per-city event types
# ---------------------------------------------------------------------------

def _make_triggering_reading(event_type: str) -> tuple[RawReading, list[WeatherReading]]:
    """Return (reading, history) guaranteed to fire the named event type.

    Each case is crafted to produce a clearly above-threshold reading.
    apparent_temperature is set to match temperature unless the test specifically
    requires a gap, to avoid co-firing feels_like_gap in unrelated cases.
    """
    if event_type == "sudden_temp_drop":
        # 7°C drop: prev=15, new=8 — clears the 5°C adaptive floor
        return (
            sample_reading(temperature=8.0, apparent_temperature=8.0),
            [_history_reading(temperature=15.0)],
        )
    if event_type == "sudden_temp_rise":
        # 7°C rise: prev=5, new=12
        return (
            sample_reading(temperature=12.0, apparent_temperature=12.0),
            [_history_reading(temperature=5.0)],
        )
    if event_type == "city_anomaly":
        # 35°C against a ~20°C baseline (stddev ≈ 0.1°C) → z >> 2
        history = [
            _history_reading(temperature=t)
            for t in [20.0, 19.9, 20.1, 20.0, 19.8, 20.2, 20.0, 19.9, 20.1, 20.0]
        ]
        # apparent=temperature avoids feels_like_gap; sudden_temp_rise also fires
        # here but does not affect the city_anomaly cooldown assertion.
        return sample_reading(temperature=35.0, apparent_temperature=35.0), history
    if event_type == "feels_like_gap":
        # 11°C wind-chill gap: temp=5, apparent=-6
        return sample_reading(temperature=5.0, apparent_temperature=-6.0), []
    if event_type == "dangerous_wind":
        # 90 km/h exceeds 80 km/h threshold; default apparent=5 == temperature=5
        return sample_reading(wind_speed=90.0), []
    if event_type == "wind_shift":
        # 50 km/h jump: prev=10, new=60
        return (
            sample_reading(wind_speed=60.0),
            [_history_reading(wind_speed=10.0)],
        )
    if event_type == "heavy_precipitation":
        # 15 mm exceeds 10 mm/h threshold
        return sample_reading(precipitation=15.0), []
    if event_type == "precip_streak":
        # Three readings all above 0.5 mm
        history = [
            _history_reading(precipitation=2.0),
            _history_reading(precipitation=2.0),
        ]
        return sample_reading(precipitation=2.0), history
    if event_type == "weather_code_severity":
        # Escalation from light (0) to heavy rain (65)
        return sample_reading(weather_code=65), [_history_reading(weather_code=0)]
    raise ValueError(f"unknown event_type: {event_type!r}")


@pytest.mark.parametrize(
    "event_type",
    [
        "sudden_temp_drop",
        "sudden_temp_rise",
        "city_anomaly",
        "feels_like_gap",
        "dangerous_wind",
        "wind_shift",
        "heavy_precipitation",
        "precip_streak",
        "weather_code_severity",
    ],
)
def test_cooldown_suppresses_repeat_for_all_event_types(event_type: str) -> None:
    """The 3-hour in-memory cooldown blocks the same (city, event_type) pair from
    firing on back-to-back calls, verified for every per-city event type."""
    detector = EventDetector()
    reading, history = _make_triggering_reading(event_type)

    first = detector.detect_events(reading, history)
    assert event_type in {e["event_type"] for e in first}, (
        f"{event_type!r} did not fire on the first call — check _make_triggering_reading"
    )

    second = detector.detect_events(reading, history)
    assert event_type not in {e["event_type"] for e in second}, (
        f"{event_type!r} was not suppressed by the 3-hour cooldown on the second call"
    )
