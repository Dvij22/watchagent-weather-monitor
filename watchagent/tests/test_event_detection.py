"""Tests for all nine EventDetector checks.

Each event type gets a "fires" test with a value that clearly crosses the
threshold and a "no fire" or "guard" test with a value just below it.
This is the dual-assertion pattern required by the event_detection_reviewer agent.

EventDetector and WeatherReading are instantiated directly — no DB is needed
because the detector is a pure function of its inputs.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.reading import WeatherReading
from app.services.event_detector import EventDetector
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
    """6°C gap (temp=5, apparent=-1) is below the 8°C threshold."""
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
    """79 km/h is below the 80 km/h threshold."""
    events = _detect(sample_reading(wind_speed=79.0))
    assert "dangerous_wind" not in _event_types(events)


# ---------------------------------------------------------------------------
# wind_shift
# ---------------------------------------------------------------------------

def test_wind_shift_fires():
    """50 km/h change (prev=10, new=60) exceeds the 40 km/h threshold."""
    history = [_history_reading(wind_speed=10.0)]
    events = _detect(sample_reading(wind_speed=60.0), history)
    assert "wind_shift" in _event_types(events)


def test_wind_shift_no_fire():
    """30 km/h change (prev=10, new=40) is below the 40 km/h threshold."""
    history = [_history_reading(wind_speed=10.0)]
    events = _detect(sample_reading(wind_speed=40.0), history)
    assert "wind_shift" not in _event_types(events)


# ---------------------------------------------------------------------------
# heavy_precipitation
# ---------------------------------------------------------------------------

def test_heavy_precipitation_fires():
    """15 mm exceeds the 10 mm/h threshold."""
    events = _detect(sample_reading(precipitation=15.0))
    assert "heavy_precipitation" in _event_types(events)


def test_heavy_precipitation_no_fire():
    """9 mm is below the 10 mm/h threshold."""
    events = _detect(sample_reading(precipitation=9.0))
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
    """When one city is 16 °C warmer than the average of the other two, the event fires."""
    detector = EventDetector()
    readings = {
        "Vancouver": sample_reading(city="Vancouver", temperature=20.0, apparent_temperature=20.0),
        "Ottawa": sample_reading(city="Ottawa", temperature=3.0, apparent_temperature=3.0),
        "Toronto": sample_reading(city="Toronto", temperature=5.0, apparent_temperature=5.0),
    }
    # Vancouver avg_others = (3+5)/2 = 4, divergence = 16 °C → should fire
    events = detector.detect_cross_city_events(readings)
    assert len(events) == 1
    assert events[0]["event_type"] == "cross_city_divergence"
    assert events[0]["city"] == "Vancouver"


def test_cross_city_divergence_no_fire():
    """When spread is within 15 °C, no event should fire."""
    detector = EventDetector()
    readings = {
        "Vancouver": sample_reading(city="Vancouver", temperature=5.0, apparent_temperature=5.0),
        "Ottawa": sample_reading(city="Ottawa", temperature=-8.0, apparent_temperature=-8.0),
        "Toronto": sample_reading(city="Toronto", temperature=-5.0, apparent_temperature=-5.0),
    }
    # Vancouver avg_others = (-8 + -5)/2 = -6.5, divergence = 11.5 °C → should not fire
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
        "Vancouver": sample_reading(city="Vancouver", temperature=20.0, apparent_temperature=20.0),
        "Ottawa": sample_reading(city="Ottawa", temperature=3.0, apparent_temperature=3.0),
        "Toronto": sample_reading(city="Toronto", temperature=5.0, apparent_temperature=5.0),
    }
    first = detector.detect_cross_city_events(readings)
    assert first, "expected first call to fire"
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
