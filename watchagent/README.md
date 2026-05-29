# WatchAgent

A weather monitoring service that polls [Open-Meteo](https://open-meteo.com/) every 5 minutes for Ottawa, Toronto, and Vancouver, persists readings to PostgreSQL, runs ten event detectors against each new reading, and exposes the stored data through a FastAPI REST API. The system is designed for reliable unattended operation: failed polls are retried with backoff, duplicate readings are silently discarded, and event spam on sustained conditions is suppressed by a per-city, per-type cooldown.

---

## Architecture

```
                  ┌─────────────────┐
                  │   Open-Meteo    │  (polled every 5 min)
                  └────────┬────────┘
                           │ HTTP  (httpx + tenacity retry)
                  ┌────────▼────────┐
                  │     Poller      │  app/services/poller.py
                  └────────┬────────┘
                           │ RawReading dataclass
                  ┌────────▼────────┐
                  │  EventDetector  │◄── last 24 readings / city
                  │  (10 checks)    │    (history from DB)
                  └────┬───────┬────┘
             readings  │       │ events
                  ┌────▼───────▼────┐
                  │   PostgreSQL    │
                  └────────┬────────┘
                           │ SQLAlchemy ORM
                  ┌────────▼────────┐
                  │    FastAPI      │  app/api/
                  └────────┬────────┘
                           │ JSON REST
                  ┌────────▼────────┐
                  │   HTTP Client   │  curl / browser / dashboard
                  └─────────────────┘
```

The Poller and FastAPI run as separate containers. EventDetector sits between the Poller and the database: it reads the last 24 readings for a city (history), runs all ten checks against the new reading, and the Poller writes the results (both the reading and any fired events) to PostgreSQL. The API is read-only against the same database.

---

## Quick Start

```bash
git clone https://github.com/Dvij22/watchagent-weather-monitor.git
cd watchagent-weather-monitor/watchagent
cp .env.example .env
docker compose up --build
```

The API is available at `http://localhost:8000` once the postgres healthcheck passes and both services start.

---

## API Reference

### `GET /health`

Returns service liveness and row counts. Queries the DB directly so a broken connection surfaces as a 500 rather than a false-positive healthy response.

```bash
curl http://localhost:8000/health
```
```json
{
  "status": "ok",
  "readings_stored": 142,
  "events_stored": 7
}
```

### `GET /readings`

Returns stored weather readings, newest first. Optional `city` and `limit` query parameters.

```bash
curl "http://localhost:8000/readings?city=Ottawa&limit=2"
```
```json
{
  "readings": [
    {
      "id": "479386a5-436e-4965-93f5-4619483eaea5",
      "city": "Ottawa",
      "timestamp": "2024-01-15T14:00:00Z",
      "temperature": -8.2,
      "apparent_temperature": -15.1,
      "precipitation": 0.0,
      "wind_speed": 32.0,
      "weather_code": 3,
      "created_at": "2024-01-15T14:01:02Z"
    }
  ]
}
```

### `GET /events`

Returns detected weather events, newest first. Optional `city` and `limit` query parameters.

```bash
curl "http://localhost:8000/events?city=Vancouver&limit=5"
```
```json
{
  "events": [
    {
      "id": "9a2c7f31-...",
      "city": "Vancouver",
      "event_type": "heavy_precipitation",
      "timestamp": "2024-01-15T09:00:00Z",
      "summary": "Vancouver recorded 14.2 mm in one hour — 1.4× the 10 mm/h heavy precipitation threshold.",
      "reason": "Hourly precipitation of 14.2 mm exceeds the 10.0 mm/h heavy precipitation threshold by 4.2 mm.",
      "metrics": {
        "precipitation": 14.2,
        "threshold": 10.0,
        "excess_over_threshold": 4.2,
        "multiplier": 1.4
      },
      "created_at": "2024-01-15T09:01:05Z"
    }
  ]
}
```

---

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v --cov=app --cov-report=term-missing
```

Tests use SQLite in-memory with `StaticPool` — no running Postgres required. All 67 tests complete in under one second.

The suite covers:
- **Deduplication** — repository-level and via mocked WeatherClient returning the same payload twice; both assert `db.query(WeatherReading).count() == 1`
- **Event detection** — every event type has a fire test, a no-fire test (just below threshold), and a cooldown test (same reading on back-to-back calls, second suppressed)
- **API contracts** — response shape, city filter, ordering, and field completeness
- **WeatherClient** — mocked httpx with success, HTTP errors, and malformed responses

---

## Event Detection

### Design decisions

The central question is not "what can we detect?" but "what is worth surfacing?" Open-Meteo provides temperature, apparent temperature, precipitation, wind speed, and WMO weather code. From those five fields, dozens of checks are possible. We narrowed to ten using three criteria:

1. **Operationally actionable.** An event should tell a person something they can act on — dress differently, avoid driving, prepare for a power outage. A statistically unusual reading that has no practical consequence is noise, not signal.

2. **Distinguishable from normal Canadian seasonal variation.** A 2 °C temperature drop in Ottawa in November is unremarkable. The thresholds are set so that events would *not* fire daily under typical conditions for these specific cities. Ottawa hits −30 °C in winter and +35 °C in summer; Vancouver rarely goes below −5 °C or above +30 °C. A single threshold calibrated for Ottawa is too insensitive for Vancouver, and vice versa.

3. **Detectable from available fields without external reference data.** Every check uses only what Open-Meteo provides in the current-conditions endpoint. We deliberately avoided checks that would require historical climatology databases, forecast data, or cross-API lookups, because those introduce external dependencies that degrade availability.

**`feels_like_gap`** fires when apparent temperature diverges sharply from actual — a condition that is genuinely dangerous (hypothermia risk, heat stress) and invisible to anyone who only checks the thermometer. **`city_anomaly`** detects readings that are statistically unusual *relative to that city's own recent baseline*: an Ottawa reading of −20 °C is normal in February but anomalous in October, and the detector handles both correctly without hardcoded seasonal rules.

**City-adaptive thresholds.** `sudden_temp_drop` and `sudden_temp_rise` do not use a fixed threshold for all cities. When at least 6 historical readings exist, the threshold is `max(5.0, stddev × 2)`, where stddev is computed from that city's own recent temperature history. Vancouver is a stable oceanic city (typical hourly stddev 0.5–1 °C); Ottawa is a volatile continental city (stddev 2–4 °C in transitional seasons). The same 5 °C absolute change correctly signals more urgency in stable Vancouver than in volatile Ottawa.

**Cross-city comparison.** After each poll cycle, `detect_cross_city_events` compares all three cities simultaneously. A `cross_city_divergence` event fires when one city's temperature is more than 15 °C away from the average of the other two. Ottawa and Vancouver can differ by 10–12 °C as normal regional climate; 15 °C+ implies genuinely different synoptic weather systems are active simultaneously.

**Checks considered and rejected:** UV index (not in the Open-Meteo current-weather fields), humidity (not available in the free tier endpoint), and day-over-day comparison (requires 24+ hours of history before it can fire at all, making it useless for a freshly deployed instance).

### Thresholds and calibration

The `EventDetector` runs all ten checks against every new reading using the previous 24 readings as history context. Per-city checks return an event dict with six required keys: `city`, `event_type`, `timestamp`, `summary`, `reason`, `metrics`. The cross-city check runs once per poll cycle after all three cities are processed. A 3-hour in-memory cooldown per `(city, event_type)` pair prevents re-firing on sustained conditions.

| Event type | Threshold | Why this threshold |
|---|---|---|
| `sudden_temp_drop` | > `max(5.0, stddev × 2)` from previous reading | Adapts to each city's observed variability; the 5 °C floor ensures cold-start sensitivity while Vancouver's low stddev and Ottawa's high stddev both get calibrated responses |
| `sudden_temp_rise` | > `max(5.0, stddev × 2)` from previous reading | Symmetric with drop; chinook and föhn events can produce fast rises in Ottawa/Toronto that need less suppression in stable Vancouver |
| `city_anomaly` | \|z-score\| > 2.0, min 6 readings | 2σ captures the outer 5% of a normal distribution; the 6-reading minimum avoids cold-start false positives when stddev is artificially low from insufficient data |
| `feels_like_gap` | `abs(apparent − actual)` > 8 °C | An 8 °C wind-chill or humidity gap is operationally dangerous; below this, the difference is noticeable but not a safety concern |
| `dangerous_wind` | wind speed > 80 km/h | 80 km/h is the Environment Canada threshold for official wind warnings; below this, gusts are unpleasant but not structurally dangerous |
| `wind_shift` | single-poll change > 40 km/h | A 40 km/h jump in one 5-minute poll indicates a frontal passage or squall line, not ordinary wind variability |
| `heavy_precipitation` | > 10 mm/h in one reading | 10 mm/h is the standard meteorological definition of heavy rain; this maps directly to the Open-Meteo hourly precipitation field |
| `precip_streak` | 3 consecutive readings each > 0.5 mm | Three consecutive wet readings (at least 15 minutes of continuous precipitation) distinguishes a sustained event from a single shower spike |
| `weather_code_severity` | WMO code escalates to a higher tier | Fires only on tier transitions (light → heavy rain → heavy snow → thunderstorm), not on code-within-tier changes, which eliminates spam during sustained storms |
| `cross_city_divergence` | one city > 15 °C from the other two's average | Ottawa–Vancouver normal difference is 5–12 °C; 15 °C+ indicates genuinely separate synoptic systems and is rare enough to be worth surfacing |

---

## Cursor Configuration

### Rules

**`logging.mdc`**
Specifies which `structlog` event key and exact field set to use at each log level in this codebase. The `poll_failed` WARNING must carry `city`, `http_status`, `attempt_count`, and `error_msg` — all four, always. This matters because an alerting rule that matches "poll_failed + city=Ottawa" only works if every poll failure actually includes the `city` field; inconsistent field presence makes field-based alerts unreliable. The rule also explicitly bans `print()` and stdlib `logging` in application code to keep all output machine-parseable.

**`event_schema.mdc`**
Locks the event dict to exactly six keys (`city`, `event_type`, `timestamp`, `summary`, `reason`, `metrics`) and the `event_type` to one of the ten allowed strings. It specifies which metric keys each event type must include — for example, every threshold-based event must carry a `"threshold"` key so downstream consumers never have to guess what value triggered it. The `reason` field must include both the actual measured value and the threshold crossed. Without this rule, detectors drift into undocumented formats over iterations, making stored events uninterpretable without reading source code.

### Agent

**`EventDetectionReviewer`**
A scoped senior-engineer persona that reviews the ten event detectors (`sudden_temp_drop`, `sudden_temp_rise`, `city_anomaly`, `feels_like_gap`, `dangerous_wind`, `wind_shift`, `heavy_precipitation`, `precip_streak`, `weather_code_severity`, `cross_city_divergence`) against six criteria: cold-start guard, Canadian climate calibration, schema compliance, reason string quality, paired fire/no-fire tests, and cooldown coverage. It reviews against real climate context — Ottawa's −30 °C winters and Ottawa vs Vancouver's contrasting variability — not generic thresholds. The agent is read-only by design: it produces specific line-level critiques but does not write new detectors, because detection code without paired tests is worse than no detection code.

Scope: `app/services/event_detector.py` and `tests/test_event_detection.py` only.

### Skills

**`data_analysis.py`**
A CLI tool with four analysis modes that queries the live database and prints structured output. Use it to answer operational questions — "how many events fired this week?", "which city's temperature range was widest?", "what were the most anomalous readings?" — without opening a DB client or writing ad-hoc SQL. Each mode is designed to be parseable at a glance: `summary` gives per-city row counts and temperature ranges, `trends` renders an ASCII bar chart of 7-day average temperatures, `anomalies` lists every `city_anomaly` event with its z-score, and `compare` shows current conditions side-by-side for all three cities.

```bash
# Run from inside the api container (DATABASE_URL is already configured from .env)
docker compose exec api python .cursor/skills/data_analysis.py --question summary
docker compose exec api python .cursor/skills/data_analysis.py --question trends
docker compose exec api python .cursor/skills/data_analysis.py --question anomalies
docker compose exec api python .cursor/skills/data_analysis.py --question compare
```

**`replay_detection.py`**
Loads the last N readings per city from the database and re-runs the `EventDetector` over them in strict chronological order, printing every event that fires with its timestamp, summary, reason, and metrics. This is the primary tool for threshold calibration: when adjusting a threshold constant in `event_detector.py`, run the replay against real historical data to verify the new value fires on genuine conditions and not on quiet days. The `--city` flag restricts replay to a single city when investigating a specific detector.

```bash
docker compose exec api python .cursor/skills/replay_detection.py
docker compose exec api python .cursor/skills/replay_detection.py --n 96
docker compose exec api python .cursor/skills/replay_detection.py --n 48 --city Ottawa
```

---

## Technology Choices

| Technology | Reason |
|---|---|
| **FastAPI** | Native async, automatic OpenAPI docs at `/docs`, and first-class Pydantic integration mean the API layer requires almost no boilerplate. The `response_model` parameter catches schema drift at startup rather than at runtime. |
| **SQLAlchemy 2.x** | The `Mapped`/`mapped_column` API gives full static type-checker support on ORM models, and the same models work against both SQLite (tests) and PostgreSQL (production) without changes — the test suite runs entirely in-memory. |
| **structlog** | Structured key-value output is machine-parseable by default. Binding context once per component (`bind(city=city, component="poller")`) eliminates the repetitive field-passing that makes stdlib `logging` fragile at scale. |
| **tenacity** | Declarative retry policy with `stop_after_attempt` + `wait_fixed` keeps the retry logic out of the business logic. Both values are configurable via `WEATHER_API_RETRY_ATTEMPTS` and `WEATHER_API_RETRY_WAIT_SECONDS` environment variables. |
| **PostgreSQL** | The `(city, timestamp)` unique constraint for deduplication requires a real unique index. PostgreSQL's `pg_isready` healthcheck integrates with Docker Compose's `condition: service_healthy` so the poller never starts before the DB is ready. |

---

## Known Tradeoffs

**In-memory cooldown resets on poller restart.**
The 3-hour per-`(city, event_type)` cooldown lives in `EventDetector._last_fired` — a plain Python dict that is discarded when the process exits. A container restart during a sustained storm will re-fire events that were already suppressed within that cooldown window. The fix would be to write cooldown state to a `weather_cooldowns` table on each fire and read it back on startup, but this adds a DB write on every poll cycle and a read on startup. For the current use case (informational monitoring, not alerting), duplicate events across restarts are a minor inconvenience rather than a correctness failure.

**Open-Meteo hourly resolution limits sub-hour detection.**
Open-Meteo updates its current-conditions endpoint on an approximately hourly cadence regardless of how frequently we poll. Polling every 5 minutes does not increase data resolution — it reduces the risk of missing an update window. As a practical consequence, event types that require two *different* consecutive readings (sudden temperature drop/rise, wind shift, weather code escalation) can fire at most once per hour. The 5-minute interval is a reliability choice, not a resolution choice.

**Cross-city comparison is temperature-only.**
`cross_city_divergence` compares temperatures across all three cities simultaneously, but does not extend to wind speed or precipitation. A scenario where Ottawa has a blizzard while Vancouver is clear would not fire a cross-city event — it would fire independent per-city events (`weather_code_severity`, `heavy_precipitation`) for Ottawa. Extending cross-city comparison to other fields is straightforward but multiplies the number of potential event types non-linearly.

**No timezone awareness in event timestamps.**
Readings are stored with UTC timestamps from Open-Meteo. The API returns UTC timestamps. There is no per-city local time conversion. A 14:00 UTC timestamp is 09:00 EST for Ottawa and 06:00 PST for Vancouver — users querying the API for "morning events" would need to apply timezone offsets client-side.
