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
      "reason": "Hourly precipitation of 14.2 mm exceeds the 7.5 mm heavy precipitation threshold by 6.7 mm.",
      "metrics": {
        "precipitation": 14.2,
        "threshold": 7.5,
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

Tests use SQLite in-memory with `StaticPool` — no running Postgres required. All 75 tests complete in under one second.

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

**City-adaptive thresholds.** `sudden_temp_drop` and `sudden_temp_rise` do not use a fixed threshold for all cities. When at least 6 historical readings exist, the threshold is `max(3.0, stddev × 2)`, where stddev is computed from that city's own recent temperature history. Vancouver is a stable oceanic city (typical hourly stddev 0.5–1 °C); Ottawa is a volatile continental city (stddev 2–4 °C in transitional seasons). The same 3 °C absolute change correctly signals more urgency in stable Vancouver than in volatile Ottawa.

**Cross-city comparison.** After each poll cycle, `detect_cross_city_events` compares all three cities simultaneously. A `cross_city_divergence` event fires when the spread between the warmest and coldest city — `max(temps) − min(temps)` — exceeds 20 °C. Ottawa and Vancouver can differ by 10–15 °C as normal regional climate; 20 °C+ implies the cities are in genuinely separate synoptic weather systems simultaneously.

**Checks considered and rejected:** UV index (not in the Open-Meteo current-weather fields), humidity (not available in the free tier endpoint), and day-over-day comparison (requires 24+ hours of history before it can fire at all, making it useless for a freshly deployed instance).

### Thresholds, calibration, and failure modes

The `EventDetector` runs all ten checks against every new reading using the previous 24 readings as history context. Per-city checks return an event dict with six required keys: `city`, `event_type`, `timestamp`, `summary`, `reason`, `metrics`. The cross-city check runs once per poll cycle after all three cities are processed. A 3-hour in-memory cooldown per `(city, event_type)` pair prevents re-firing on sustained conditions.

| Event type | Threshold | Failure mode it prevents |
|---|---|---|
| `sudden_temp_drop` | > `max(3.0, stddev x 2)` from previous reading | **Adapting to city volatility.** A fixed floor would routinely miss Vancouver's smaller but equally meaningful drops (oceanic climate, s ~0.5-1 C/reading) while firing too eagerly in Ottawa (continental climate, s ~2-4 C). Using 2x the city's own recent stddev means the same relative surprise triggers the event in both cities. |
| `sudden_temp_rise` | > `max(3.0, stddev x 2)` from previous reading | **Symmetric with drop; guards against chinook false negatives.** A fixed global threshold would miss the rapid warming spikes Ottawa and Toronto see during chinook-like events (14 C in one reading in live data), while a threshold calibrated for Ottawa would fire on every mild Vancouver afternoon. |
| `city_anomaly` | \|z-score\| > 2.0, min 6 readings | **Threshold is relative to each city's own history, because Ottawa's normal range is 40 C wider than Vancouver's -- a global threshold would either spam Ottawa or miss Vancouver.** The 6-reading minimum prevents cold-start false positives when stddev is artificially low. |
| `feels_like_gap` | abs(apparent - actual) > 6 C | **Guards against the 'looks warm, is dangerous' failure.** Someone checking only the thermometer at 17 C misses the 10.9 C wind-chill reality -- a 6 C gap is the point where clothing recommendations and safety advice diverge materially from the thermometer. |
| `dangerous_wind` | wind speed > 70 km/h | **Guards against under-reporting winds that precede structural damage.** Environment Canada issues wind advisories from 60-70 km/h. Setting the threshold at 80 km/h (the full warning level) would miss the advisory window entirely. |
| `wind_shift` | single-poll change > 30 km/h | **Guards against missing frontal passage signals.** A gradual increase over hours is normal; a 30 km/h change in one 5-minute poll is the wind signature of a squall line or cold front. Live Ottawa data showed exactly this shift during a frontal passage. |
| `heavy_precipitation` | > 7.5 mm/h in one reading | **Guards against missing moderate-heavy events that cause urban flooding.** A higher threshold (20 mm/h) would only catch cloudbursts. 7.5 mm/h is the lower bound of Environment Canada's 'heavy rain' category -- the level where drainage systems begin to struggle. |
| `precip_streak` | 3 consecutive readings each > 0.5 mm | **Single-reading spikes are noise. Three consecutive readings above 0.5 mm indicates sustained precipitation worth surfacing.** A single wet reading can be a sensor artefact or a brief shower already over. Three readings (>=15 min continuous) ensures the event describes something the user is still experiencing. |
| `weather_code_severity` | WMO code escalates to a higher tier | **Guards against tier-boundary spam during sustained storms.** Without this, a thunderstorm oscillating between WMO codes 95 and 96 would fire on every poll cycle. Firing only on tier transitions (light to heavy rain to heavy snow to thunderstorm) means one event per escalation. |
| `cross_city_divergence` | max(temps) - min(temps) > 20 C | **Guards against missing region-wide weather system splits.** Ottawa-Vancouver differences of 5-15 C are normal; 20 C+ means the cities are in genuinely separate synoptic systems. The spread (max-min) is used instead of per-city deviation so no single city is falsely labelled the outlier. |

---

## Cursor Configuration

### Rules

**`logging.mdc`**
Specifies which `structlog` event key and exact field set to use at each log level in this codebase. The `poll_failed` WARNING must carry `city`, `http_status`, `attempt_count`, and `error_msg` — all four, always. This matters because an alerting rule that matches "poll_failed + city=Ottawa" only works if every poll failure actually includes the `city` field; inconsistent field presence makes field-based alerts unreliable. The rule also explicitly bans `print()` and stdlib `logging` in application code to keep all output machine-parseable.

**`event_schema.mdc`**
Locks the event dict to exactly six keys (`city`, `event_type`, `timestamp`, `summary`, `reason`, `metrics`) and the `event_type` to one of the ten allowed strings. It specifies which metric keys each event type must include — for example, every threshold-based event must carry a `"threshold"` key so downstream consumers never have to guess what value triggered it. The `reason` field must include both the actual measured value and the threshold crossed. Without this rule, detectors drift into undocumented formats over iterations, making stored events uninterpretable without reading source code.

**`repository.mdc`**
Enforces that all database queries in the codebase go through repository classes (`ReadingRepository`, `EventRepository`) rather than being written inline in routes or services. The rule lists the exact method signatures available on each repository, specifies that new queries must be added as repository methods (not inline), and explains why: keeping `db.query()` calls inside repositories prevents a new endpoint from accidentally bypassing the ordering convention, session rollback pattern, or unique-constraint logic. This rule was created after discovering a direct `db.query(WeatherReading)` call in `readings.py` that bypassed `ReadingRepository` — the violation was fixed by adding `get_all()` to the repository.

### Agent

**`EventDetectionReviewer`**
A scoped senior-engineer persona that reviews the ten event detectors (`sudden_temp_drop`, `sudden_temp_rise`, `city_anomaly`, `feels_like_gap`, `dangerous_wind`, `wind_shift`, `heavy_precipitation`, `precip_streak`, `weather_code_severity`, `cross_city_divergence`) against six criteria: cold-start guard, Canadian climate calibration, schema compliance, reason string quality, paired fire/no-fire tests, and cooldown coverage. It reviews against real climate context — Ottawa's −30 °C winters and Ottawa vs Vancouver's contrasting variability — not generic thresholds. The agent is read-only by design: it produces specific line-level critiques but does not write new detectors, because detection code without paired tests is worse than no detection code.

The agent directly influenced three concrete decisions in the final code. In the first session it flagged that `_check_sudden_temp_rise` was missing the `if not history: return None` cold-start guard that `_check_sudden_temp_drop` had — a latent `IndexError` on the second poll of a fresh deployment. In the second session it identified that `_FEELS_LIKE_GAP_THRESHOLD = 8.0` would suppress Ottawa's most common wind-chill scenario (a 6.8 °C gap at 35 km/h, documented in live data) and recommended lowering to 6.0 °C based on Environment Canada's 5–8 °C guidance range. In the third session it pointed out that `_DANGEROUS_WIND_KMH = 80.0` fired only after the official warning threshold was already exceeded, meaning the entire 60–80 km/h advisory window was invisible to the system; the threshold was lowered to 70 km/h so the monitor leads the official advisory rather than trailing it. See `.cursor/agents/session_log.md` for the full interaction records.

Scope: `app/services/event_detector.py` and `tests/test_event_detection.py` only.

### Skills

**`data_analysis.py`**
A CLI tool with five analysis modes that queries the live database and prints structured output. Use it to answer operational questions — "how many events fired this week?", "which city's temperature range was widest?", "what were the most anomalous readings?" — without opening a DB client or writing ad-hoc SQL. Each mode is designed to be parseable at a glance: `summary` gives per-city row counts and latest temperatures, `events` shows the full event log across all ten event types (count per type plus the 10 most recent), `trends` renders an ASCII scatter chart of raw temperature readings over the last 7 days, `anomalies` gives a deep-dive breakdown of `city_anomaly` events with z-scores and baseline statistics, and `compare` shows current conditions side-by-side for all three cities with 24-hour context.

```bash
# Run from inside the api container (DATABASE_URL is already configured from .env)
docker compose exec api python .cursor/skills/data_analysis.py --question summary
docker compose exec api python .cursor/skills/data_analysis.py --question events
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

| Technology | Reason | What I considered and rejected |
|---|---|---|
| **FastAPI** | Native async, automatic OpenAPI docs at `/docs`, and first-class Pydantic integration mean the API layer requires almost no boilerplate. The `response_model` parameter catches schema drift at startup rather than at runtime. | Considered Flask, but Flask's sync-by-default model would have required Gunicorn worker threads to avoid blocking the event loop while the poller's `asyncio.gather` was in flight. FastAPI's native async support matters specifically because the poller and API share the same SQLAlchemy session factory — both need to be async-safe without extra wiring. |
| **SQLAlchemy 2.x** | The `Mapped`/`mapped_column` API gives full static type-checker support on ORM models, and the same models work against both SQLite (tests) and PostgreSQL (production) without changes — the test suite runs entirely in-memory. | Considered raw `psycopg2` queries for simplicity, but the SQLite-in-memory test strategy would have been impossible — SQLite and PostgreSQL have different SQL dialects for things like `RETURNING` and `ON CONFLICT`. The ORM abstraction is worth it specifically for test isolation. |
| **structlog** | Structured key-value output is machine-parseable by default. Binding context once per component (`bind(city=city, component="poller")`) eliminates the repetitive field-passing that makes stdlib `logging` fragile at scale. | Considered stdlib `logging` with a `JSONFormatter`, but `logging`'s thread-local context mechanism (`LoggerAdapter`) does not compose cleanly with `asyncio` — binding `city` to a logger in one coroutine leaks into another coroutine on the same thread. `structlog`'s immutable `bind()` returns a new bound logger, which is safe to pass through `asyncio.gather` without shared state. |
| **tenacity** | Declarative retry policy with `stop_after_attempt` + `wait_fixed` keeps the retry logic out of the business logic. Both values are configurable via `WEATHER_API_RETRY_ATTEMPTS` and `WEATHER_API_RETRY_WAIT_SECONDS` environment variables. | Considered a manual `for attempt in range(3)` retry loop, but manual loops accumulate accidental complexity: you add a counter, then a sleep, then realise you need to distinguish transient HTTP errors from permanent 4xx errors, then add a log line per attempt. Tenacity handles all of this declaratively and the intent is visible at a glance. It also makes the retry behaviour testable by patching `asyncio.sleep` rather than timing real sleeps. |
| **PostgreSQL unique constraint** | The `(city, timestamp)` unique constraint for deduplication requires a real unique index. PostgreSQL's `pg_isready` healthcheck integrates with Docker Compose's `condition: service_healthy` so the poller never starts before the DB is ready. | Considered application-level deduplication — checking `SELECT 1 FROM readings WHERE city=? AND timestamp=?` before each insert. The problem is a TOCTOU race: two poller instances (or a restart during an insert) can both see no existing row, both proceed to insert, and one silently overwrites the other. The DB-level unique constraint makes the duplicate check atomic — the `IntegrityError` is the check — and eliminates the race entirely. |

---

## What I Would Do With More Time

These are the four highest-value improvements I did not have time to implement. I am listing them explicitly because they represent known gaps in the current design, not open questions.

**1. Persist cooldown state so poller restarts do not re-fire suppressed events.**
The 3-hour per-`(city, event_type)` cooldown lives in `EventDetector._last_fired`, a plain Python dict that is discarded when the process exits. A container restart during a sustained storm re-fires every event that was already suppressed in that window. The fix is a `weather_cooldowns` table: write `(city, event_type, fired_at)` on each fire, read all rows with `fired_at > now - 3h` on startup, and seed `_last_fired` from them. The cost is one extra DB write per event fired and one read on startup — both negligible compared to the correctness gain.

**2. Cross-city comparison using rolling 24-hour baselines, not just the current snapshot.**
`cross_city_divergence` compares the three cities' current temperatures in one point-in-time snapshot. A more useful version would compare each city's current temperature against its own 24-hour mean, then compare those normalised deviations across cities. This would catch the case where Vancouver is colder than Ottawa today but warmer than its own baseline — an anomaly that the current check misses entirely because the absolute temperatures are in normal ranges.

**3. Map WMO codes to human-readable severity descriptions.**
The `weather_code_severity` event currently reports the raw WMO code (e.g., `95`) in the summary and reason strings. The WMO publishes a complete mapping (code 95 = "Thunderstorm, slight or moderate"; code 96 = "Thunderstorm with slight hail"). Adding a `_WMO_DESCRIPTIONS: dict[int, str]` lookup table to `event_detector.py` would make the event summary readable without a separate reference, and would let the API surface the human description directly in the `metrics` dict.

**4. Alerting webhook when high-severity events fire.**
Currently all events are written to the database and exposed only through the polling `GET /events` endpoint. A production-grade monitor would POST a payload to a configurable webhook URL (Slack, PagerDuty, email gateway) when certain event types fire — at minimum `dangerous_wind`, `heavy_precipitation`, and `weather_code_severity` escalating to `thunderstorm`. The implementation is a small `WebhookNotifier` class called from `_run_event_detection` after `event_repo.insert()`, with `WEBHOOK_URL` as an optional env var (no webhook = no-op).

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
