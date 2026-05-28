# WatchAgent

A weather monitoring service that polls [Open-Meteo](https://open-meteo.com/) every 5 minutes for Ottawa, Toronto, and Vancouver, persists readings to PostgreSQL, runs nine event detectors against each new reading, and exposes the stored data through a FastAPI REST API. The system is designed for reliable unattended operation: failed polls are retried with backoff, duplicate readings are silently discarded, and event spam on sustained conditions is suppressed by a per-city, per-type cooldown.

---

## Architecture

```
                     ┌─────────────┐
                     │ Open-Meteo  │  (polled every 5 min)
                     └──────┬──────┘
                            │ HTTP (httpx + tenacity retry)
                     ┌──────▼──────┐
                     │   Poller    │
                     │             │
                     │ EventDetec- │◄── 24-reading history
                     │    tor      │
                     └──────┬──────┘
               readings &   │   events
                     ┌──────▼──────┐
                     │ PostgreSQL  │
                     └──────┬──────┘
                            │ SQLAlchemy
                     ┌──────▼──────┐
                     │   FastAPI   │
                     └──────┬──────┘
                            │ JSON
                     ┌──────▼──────┐
                     │ HTTP Client │  (curl, browser, dashboard)
                     └─────────────┘
```

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

Returns service liveness and row counts. A DB connection failure surfaces as a 500.

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

Returns stored weather readings, newest first. Optional `city` and `limit` filters.

```bash
curl "http://localhost:8000/readings?city=Ottawa&limit=10"
```
```json
{
  "readings": [
    {
      "id": "3f1b...",
      "city": "Ottawa",
      "timestamp": "2024-01-15T14:00:00+00:00",
      "temperature": -8.2,
      "apparent_temperature": -15.1,
      "precipitation": 0.0,
      "wind_speed": 32.0,
      "weather_code": 3,
      "created_at": "2024-01-15T14:01:02+00:00"
    }
  ]
}
```

### `GET /events`

Returns detected weather events, newest first. Optional `city` and `limit` filters.

```bash
curl "http://localhost:8000/events?city=Vancouver&limit=5"
```
```json
{
  "events": [
    {
      "id": "9a2c...",
      "city": "Vancouver",
      "event_type": "heavy_precipitation",
      "timestamp": "2024-01-15T09:00:00+00:00",
      "summary": "Vancouver recorded 14.2 mm of precipitation in one hour.",
      "reason": "Precipitation 14.2 mm exceeds the 10 mm/h heavy threshold.",
      "metrics": { "precipitation": 14.2 },
      "created_at": "2024-01-15T09:01:05+00:00"
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

Tests use SQLite in-memory with `StaticPool` — no running Postgres required. All 41 tests complete in under one second.

---

## Event Detection

### Design decisions

The central question is not "what can we detect?" but "what is worth surfacing?" Open-Meteo provides temperature, apparent temperature, precipitation, wind speed, and WMO weather code. From those five fields, dozens of checks are possible. We narrowed to nine using three criteria:

1. **Operationally actionable.** An event should tell a person something they can act on — dress differently, avoid driving, prepare for a power outage. A statistically unusual reading that has no practical consequence is noise, not signal.

2. **Distinguishable from normal Canadian seasonal variation.** A 2 °C temperature drop in Ottawa in November is unremarkable. The thresholds are set so that events would *not* fire daily under typical conditions for these specific cities.

3. **Detectable from available fields without external reference data.** Every check uses only what Open-Meteo provides. We deliberately avoided checks that would require historical climatology databases, forecast data, or cross-API lookups, because those introduce dependencies that break availability.

`feels_like_gap` and `city_anomaly` are the two most deliberate choices. `feels_like_gap` fires when apparent temperature diverges sharply from actual — a condition that is genuinely dangerous (hypothermia risk, heat stress) and invisible to anyone who only checks the temperature number. `city_anomaly` detects readings that are statistically unusual *relative to that city's recent baseline*, which means it self-calibrates: an Ottawa reading of −20 °C is normal in February but anomalous in October, and the detector handles both correctly without hardcoded seasonal rules.

Checks we considered and rejected: UV index (not in the Open-Meteo current-weather fields), humidity (not available in the free tier endpoint we use), and day-over-day comparison (requires 24+ hours of history before it can fire at all, making it useless for a freshly deployed instance).

### Thresholds and calibration

The `EventDetector` runs all nine checks against every new reading using the previous 24 readings as history. Each check returns `None` or an event dict with six required keys: `city`, `event_type`, `timestamp`, `summary`, `reason`, `metrics`.

A 3-hour in-memory cooldown per `(city, event_type)` pair prevents re-firing on sustained conditions.

| Event type | Threshold | Why this threshold |
|---|---|---|
| `sudden_temp_drop` | > 5 °C drop from previous reading | A 5 °C hourly drop is unusual in Canadian cities but not impossible; lower would fire on normal diurnal transitions |
| `sudden_temp_rise` | > 5 °C rise from previous reading | Symmetric with drop; chinook events in Ottawa can produce fast rises but rarely exceed 5 °C in a single poll |
| `city_anomaly` | z-score > 2, min 6 readings | 2σ catches the outer 5% of a normal distribution; 6-reading minimum avoids cold-start false positives when stddev is artificially low |
| `feels_like_gap` | `abs(apparent − actual)` > 8 °C | An 8 °C gap represents operationally dangerous wind chill or humidity; smaller gaps are common and not actionable |
| `dangerous_wind` | wind speed > 80 km/h | 80 km/h is the Environment Canada threshold for wind warnings; below this, gusts are unpleasant but not dangerous |
| `wind_shift` | change > 40 km/h from previous reading | A 40 km/h jump in one poll indicates a frontal passage or squall, not normal variability |
| `heavy_precipitation` | > 10 mm in one hourly reading | 10 mm/h is the standard meteorological definition of heavy rain; this maps cleanly to the Open-Meteo hourly field |
| `precip_streak` | 3 consecutive readings > 0.5 mm each | Three consecutive wet readings (15+ minutes of rain) distinguishes sustained precipitation from a single shower spike |
| `weather_code_severity` | WMO code escalates to a higher tier | Detects transitions between severity tiers (light → heavy rain → heavy snow → thunderstorm) rather than absolute codes, avoiding spam on sustained storms |

---

## Cursor Configuration

### Rules

**`logging.mdc`**
Enforces `structlog` throughout and bans `print()` and stdlib `logging`. Every WARNING for a failed poll must include exactly four fields: `city`, `http_status`, `attempt_count`, `error_msg`. This rule exists because inconsistent log fields make alerting impossible — a field that appears in 80% of failure logs is not a reliable alert target.

**`event_schema.mdc`**
Locks the event dict to exactly six keys and the `event_type` to one of nine allowed strings. The `reason` field must include both the threshold and the actual value that triggered it. This rule prevents detectors from drifting into undocumented formats that break the API response schema or make events uninterpretable in dashboards.

### Agent

**`EventDetectionReviewer`**
A scoped senior-engineer persona that reviews detector methods against five criteria: cold-start safety, threshold realism for Canadian weather, schema compliance, paired fire/no-fire tests, and cooldown coverage. It does not write detectors — only reviews them with specific line-level feedback. Keeping it read-only prevents the agent from introducing new code without the paired tests that make detection trustworthy.

### Skills

**`data_analysis.py`**
A CLI tool with four analysis modes (`summary`, `trends`, `anomalies`, `compare`) that queries the live database and prints structured output. Designed to answer operational questions — "how many events fired this week?", "which city has the widest temperature range?" — without opening a DB client or writing ad-hoc SQL.

**`replay_detection.py`**
Loads the last N readings per city from the database and re-runs the `EventDetector` over them in chronological order. Indispensable when adding a new event type: run it against real historical data to verify the threshold fires on actual weather conditions before shipping.

---

## Technology Choices

| Technology | Reason |
|---|---|
| **FastAPI** | Native async, automatic OpenAPI docs, and first-class Pydantic integration mean the API layer requires almost no boilerplate |
| **SQLAlchemy** | The SQLAlchemy 2.x `Mapped`/`mapped_column` API gives full type-checker support on ORM models, and the same models work against both SQLite (tests) and PostgreSQL (production) without changes |
| **structlog** | Structured key-value log output is machine-parseable by default; binding context once per component eliminates the repetitive field-passing that makes stdlib `logging` fragile at scale |
| **tenacity** | Declarative retry policy with `stop_after_attempt` + `wait_fixed` keeps the retry logic out of the business logic and makes the behaviour testable by swapping strategies |
| **PostgreSQL** | The `(city, timestamp)` unique constraint for deduplication relies on a real unique index; PostgreSQL's `pg_isready` healthcheck integrates cleanly with Docker Compose's `condition: service_healthy` |

---

## Known Tradeoffs

**In-memory cooldown resets on restart.**
The 3-hour per-`(city, event_type)` cooldown is stored in `EventDetector._last_fired`, which is discarded when the poller restarts. A container restart after a sustained storm will re-fire events that were already suppressed. Persisting the cooldown table to PostgreSQL would fix this but adds write overhead on every poll cycle.

**No cross-city comparison.**
The current detectors are per-city: each reading is compared only against that city's own history. A "Vancouver is 20 °C warmer than Ottawa" anomaly would not fire. Cross-city comparison requires a different detector architecture and a shared history buffer, which is deferred to a future phase.

**Open-Meteo hourly resolution limits sub-hour detection.**
Open-Meteo updates current conditions on an hourly cadence regardless of poll frequency. Polling every 5 minutes does not increase data resolution — it reduces the risk of missing an update window. Events that require two consecutive *different* readings (sudden drops, wind shifts, severity escalation) can only fire at most once per hour in practice.
