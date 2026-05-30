# EventDetectionReviewer — Session Log

Concrete interactions with the EventDetectionReviewer agent during development
of `app/services/event_detector.py`. Each session shows the prompt given, what
the agent flagged, and the exact change that resulted.

---

## Session 1 — Reviewing `_check_sudden_temp_rise` for cold-start symmetry

**I asked:**

> Review `_check_sudden_temp_rise` and `_check_sudden_temp_drop` side by side.
> Do both have adequate cold-start guards? Are the tests symmetric?

**Agent flagged:**

> `_check_sudden_temp_drop` correctly begins with `if not history: return None`,
> preventing a crash when the poller starts and no previous reading exists.
> `_check_sudden_temp_rise` is **missing this guard entirely**. On the 2nd poll
> ever collected for a city, `history[0]` would be accessed without a bounds
> check, raising an `IndexError` that crashes the poll cycle for that city.
>
> The test suite has `test_sudden_temp_drop_no_history` to prove the guard
> works, but there is **no `test_sudden_temp_rise_no_history` counterpart**.
> Without the symmetric test, the missing guard went undetected. Add the guard
> at line 1 of `_check_sudden_temp_rise` (`if not history: return None`) and
> add a corresponding cold-start test.

**Change made:**

Added `if not history: return None` as the first line of `_check_sudden_temp_rise`
in `event_detector.py`. Added `test_sudden_temp_rise_no_history` to
`tests/test_event_detection.py`:

```python
def test_sudden_temp_rise_no_history():
    """Cold-start guard: no history → cannot compute a delta, must not fire."""
    events = _detect(sample_reading(temperature=12.0), [])
    assert "sudden_temp_rise" not in _event_types(events)
```

---

## Session 2 — Reviewing `_FEELS_LIKE_GAP_THRESHOLD` Canadian calibration

**I asked:**

> Review `_check_feels_like_gap`. The threshold is `_FEELS_LIKE_GAP_THRESHOLD = 8.0`.
> Is that defensible for all three cities? Would it fire on a typical Ottawa
> winter day or a typical Vancouver fall day?

**Agent flagged:**

> At `_FEELS_LIKE_GAP_THRESHOLD = 8.0`, the check would **never fire during the
> most common Canadian wind-chill scenario**. Ottawa in November at −2°C with
> 35 km/h winds produces a wind-chill apparent temperature of about −8.7°C — a
> gap of 6.7°C. Under the 8.0°C threshold that reading would be silently
> suppressed. That is exactly the condition you want to surface: someone dressed
> for −2°C is actually exposed to −8.7°C.
>
> Environment Canada's comfort guidelines cite 5–8°C wind-chill divergence as
> the range where clothing recommendations diverge from the thermometer. Setting
> the threshold at 8.0°C puts it at the top of that range, catching only the
> extreme end and missing the bulk of actionable conditions.
>
> Recommend lowering to **6.0°C**. This matches the midpoint of Environment
> Canada's guidance and will fire on Ottawa's observed wind-chill gaps (live
> data showed 6.8°C at 35 km/h) without triggering on calm-air days where
> apparent and actual temperature are nearly identical. The `test_feels_like_gap_no_fire`
> test value must be updated from 7.9°C to 5.9°C gap to stay below the new threshold.

**Change made:**

Updated constant in `event_detector.py`:
```python
# Before
_FEELS_LIKE_GAP_THRESHOLD = 8.0

# After
_FEELS_LIKE_GAP_THRESHOLD = 6.0  # midpoint of Environment Canada's 5–8°C guidance
```

Updated the constant's comment to cite the 6.8°C observed Ottawa gap that validates
the new value. Updated `test_feels_like_gap_no_fire` to use a 5.9°C gap (below the
new threshold) to keep the no-fire test meaningful.

---

## Session 3 — Reviewing `_DANGEROUS_WIND_KMH` advisory-window gap

**I asked:**

> Review `_check_dangerous_wind`. The threshold is `_DANGEROUS_WIND_KMH = 80.0`.
> Environment Canada issues wind advisories at 60–70 km/h. Are we calibrated
> correctly against that scale?

**Agent flagged:**

> At `_DANGEROUS_WIND_KMH = 80.0`, this detector fires **only after** the
> official warning threshold has already been passed. Environment Canada issues:
>
> - **Wind advisory**: sustained 60–70 km/h
> - **Wind warning**: sustained 70–80 km/h
>
> Setting the trigger at 80 km/h means the entire advisory window (60–80 km/h)
> is invisible to WatchAgent. A user of this system would see no event during the
> advisory period and then suddenly receive a `dangerous_wind` event at the
> point when Environment Canada would already have issued a full warning. The
> monitor is a lap behind the official system.
>
> The right trigger is **70 km/h** — the lower bound of the wind warning
> category. This fires before structural damage is likely and gives meaningful
> lead time. The `test_dangerous_wind_no_fire` test must be updated: its
> current 79.0 km/h input now crosses the new threshold, so it needs to drop
> to 69.0 km/h to remain a valid no-fire case.

**Change made:**

Updated constant in `event_detector.py`:
```python
# Before
_DANGEROUS_WIND_KMH = 80.0

# After
_DANGEROUS_WIND_KMH = 70.0  # lower bound of EC wind warning; fires during advisory window
```

Updated `test_dangerous_wind_no_fire` to use 69.0 km/h (just below the new threshold)
and updated its docstring to match. The `reason` string in `_check_dangerous_wind`
was updated to cite `70.0 km/h` and reference the Environment Canada warning level
explicitly.

---

*Log maintained in `.cursor/agents/session_log.md`. One entry per substantive
agent review session that produced a code or test change.*
