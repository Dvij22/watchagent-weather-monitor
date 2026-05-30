"""data_analysis.py — query the live WatchAgent database and print structured output.

Usage (run from the watchagent/ directory):
    python .cursor/skills/data_analysis.py --question summary
    python .cursor/skills/data_analysis.py --question trends
    python .cursor/skills/data_analysis.py --question anomalies
    python .cursor/skills/data_analysis.py --question compare

DATABASE_URL is read from the environment or a .env file in the current directory.
Exits 0 on success, 1 on DB connection failure.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

CITIES = ["Ottawa", "Toronto", "Vancouver"]


def _load_env() -> None:
    """Load .env from cwd if python-dotenv is available, otherwise rely on os.environ."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ImportError:
        pass


def _make_session() -> "Session":
    """Return a SQLAlchemy session using DATABASE_URL from the environment."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(database_url)
        Session = sessionmaker(bind=engine)
        session = Session()
        session.execute(text("SELECT 1"))
        return session
    except ModuleNotFoundError as exc:
        print(
            f"ERROR: missing DB driver — {exc}\n"
            "  Run inside the container: docker compose exec api python .cursor/skills/data_analysis.py",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: DB connection failed — {exc}", file=sys.stderr)
        sys.exit(1)


def _fmt_ts(ts: datetime) -> str:
    """Format a timestamp as 'YYYY-MM-DD HH:MM UTC'."""
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_date(ts: datetime) -> str:
    """Format as 'Mon D' with no leading zero on day."""
    return f"{ts:%b} {ts.day}"


# ---------------------------------------------------------------------------
# ASCII chart helper
# ---------------------------------------------------------------------------

def _ascii_chart(title: str, readings: list, max_cols: int = 38) -> None:
    """Render a temperature-over-time ASCII chart for a single city.

    Y-axis: temperature in °C, rounded to nearest 5-degree row.
    X-axis: time, oldest left to newest right.
    Points are sampled if there are more readings than columns.
    """
    if len(readings) < 2:
        print(f"\n  {title}")
        print(f"  Not enough data for a chart ({len(readings)} reading).")
        return

    sorted_r = sorted(readings, key=lambda r: r.timestamp)

    # Sample evenly if too many readings to fit
    if len(sorted_r) > max_cols:
        step = (len(sorted_r) - 1) / (max_cols - 1)
        sorted_r = [sorted_r[round(i * step)] for i in range(max_cols)]

    temps = [r.temperature for r in sorted_r]
    n = len(temps)

    t_min = min(temps)
    t_max = max(temps)
    y_min = int(math.floor(t_min / 5)) * 5
    y_max = int(math.ceil(t_max / 5)) * 5
    if y_min == y_max:
        y_min -= 5
        y_max += 5

    y_levels = list(range(y_max, y_min - 1, -5))

    # Assign each reading to the closest y level
    row_for = [min(y_levels, key=lambda lv, t=t: abs(t - lv)) for t in temps]

    first_ts = sorted(readings, key=lambda r: r.timestamp)[0].timestamp
    last_ts = sorted(readings, key=lambda r: r.timestamp)[-1].timestamp
    span_h = (last_ts - first_ts).total_seconds() / 3600

    print(f"\n  {title}  ({len(readings)} readings, {span_h:.0f}h span)")

    # Chart rows
    for level in y_levels:
        cols = ["*" if row_for[i] == level else " " for i in range(n)]
        print(f"  {level:>5} | " + " ".join(cols))

    # Axis line and time labels
    axis = "--" * n
    print(f"  {'':>5} +" + axis + "> time")
    first_label = _fmt_date(first_ts)
    last_label = _fmt_date(last_ts)
    pad = max(1, len(axis) - len(first_label) - len(last_label))
    print(f"  {'':>5}   {first_label}" + " " * pad + last_label)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

def question_summary(session: "Session") -> None:
    """Monitoring uptime, per-city snapshot, most active event type, data gaps."""
    from app.models.reading import WeatherReading
    from app.models.event import WeatherEvent

    all_readings = session.query(WeatherReading).order_by(WeatherReading.timestamp).all()
    all_events = session.query(WeatherEvent).all()
    now = datetime.now(tz=timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    print("\n=== WatchAgent Data Summary ===")

    if not all_readings:
        print("No readings in database yet.")
        return

    first_ts = min(r.timestamp for r in all_readings)
    last_ts = max(r.timestamp for r in all_readings)
    uptime_h = (last_ts - first_ts).total_seconds() / 3600

    print(f"Monitoring since: {_fmt_ts(first_ts)}")
    print(f"Total uptime:     {uptime_h:.1f} hours")

    # Per-city data
    by_city: dict[str, list] = defaultdict(list)
    for r in all_readings:
        by_city[r.city].append(r)

    events_by_city: dict[str, int] = defaultdict(int)
    for e in all_events:
        events_by_city[e.city] += 1

    events_24h_by_city: dict[str, int] = defaultdict(int)
    for e in all_events:
        if e.timestamp >= cutoff_24h:
            events_24h_by_city[e.city] += 1

    print("\nPer-city snapshot:")
    counts = {}
    for city in CITIES:
        readings = by_city.get(city, [])
        counts[city] = len(readings)
        if not readings:
            print(f"  {city:10} | no data")
            continue
        latest_r = max(readings, key=lambda r: r.timestamp)
        n_events = events_by_city.get(city, 0)
        event_word = "event" if n_events == 1 else "events"
        print(
            f"  {city:10} | {len(readings):3} readings"
            f" | latest: {latest_r.temperature:+6.1f}°C"
            f" | {n_events} {event_word} fired"
        )

    # Also show ALL-city events (cross_city_divergence)
    all_city_events = sum(1 for e in all_events if e.city == "ALL")
    if all_city_events:
        print(f"  {'ALL':10} | {all_city_events} cross-city event(s) fired")

    # Most active event type
    if all_events:
        type_counts = Counter(e.event_type for e in all_events)
        top_type, top_count = type_counts.most_common(1)[0]
        print(f"\nMost active event type: {top_type} ({top_count} occurrence{'s' if top_count != 1 else ''})")
    else:
        print("\nNo events fired yet.")

    # Quietest city in last 24h
    if uptime_h >= 1:
        window_label = "last 24h" if uptime_h >= 24 else f"last {uptime_h:.0f}h"
        quiet = [c for c in CITIES if events_24h_by_city.get(c, 0) == 0 and by_city.get(c)]
        if quiet:
            print(f"Quietest city:          {', '.join(quiet)} (0 events in {window_label})")

    # Data gap: flag if any city has ≥2 fewer readings than the max
    max_count = max(counts.values(), default=0)
    gaps = [(city, max_count - cnt) for city, cnt in counts.items() if max_count - cnt >= 2]
    if gaps:
        for city, missing in gaps:
            print(f"Data gap detected:      {city} is {missing} reading(s) behind the other cities")


def question_trends(session: "Session") -> None:
    """ASCII temperature chart for each city over available data (up to 7 days)."""
    from app.models.reading import WeatherReading

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
    readings = (
        session.query(WeatherReading)
        .filter(WeatherReading.timestamp >= cutoff)
        .order_by(WeatherReading.timestamp)
        .all()
    )

    print("\n=== Temperature Trends ===")

    if not readings:
        print("No readings in the last 7 days.")
        return

    # Check data age
    oldest = min(r.timestamp for r in readings)
    hours_available = (datetime.now(tz=timezone.utc) - oldest).total_seconds() / 3600

    if hours_available < 24:
        print(
            f"\n  Note: only {hours_available:.0f}h of data available — "
            "chart spans actual collection window, not a full 7 days."
        )

    by_city: dict[str, list] = defaultdict(list)
    for r in readings:
        by_city[r.city].append(r)

    for city in CITIES:
        city_readings = by_city.get(city, [])
        if not city_readings:
            print(f"\n  {city}: no readings in window.")
            continue
        title = f"{city} temperature"
        _ascii_chart(title, city_readings)

    print()


def question_anomalies(session: "Session") -> None:
    """All city_anomaly events with z-score, temperature, and mean."""
    from app.models.event import WeatherEvent

    events = (
        session.query(WeatherEvent)
        .filter(WeatherEvent.event_type == "city_anomaly")
        .order_by(WeatherEvent.timestamp.desc())
        .all()
    )

    print(f"\n=== City Anomaly Events ({len(events)} total) ===")

    if not events:
        print("No city_anomaly events recorded.")
        return

    for e in events:
        z = e.metrics.get("z_score", "n/a")
        temp = e.metrics.get("temperature", "n/a")
        mean = e.metrics.get("mean", "n/a")
        stddev = e.metrics.get("stddev", "n/a")
        direction = "above" if isinstance(z, (int, float)) and z > 0 else "below"
        z_str = f"{z:+.2f}σ {direction} mean" if isinstance(z, (int, float)) else str(z)
        print(
            f"\n  {_fmt_ts(e.timestamp)}  {e.city}"
            f"\n    z-score : {z_str}"
            f"\n    temp    : {temp}°C  (mean {mean}°C, σ={stddev}°C)"
            f"\n    summary : {e.summary}"
        )


def question_compare(session: "Session") -> None:
    """Side-by-side comparison: current reading, 24h avg, 24h range, 24h event count."""
    from app.models.reading import WeatherReading
    from app.models.event import WeatherEvent

    now = datetime.now(tz=timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    print("\n=== Current Conditions — WatchAgent ===")

    rows: list[tuple] = []
    has_data = False

    for city in CITIES:
        latest = (
            session.query(WeatherReading)
            .filter(WeatherReading.city == city)
            .order_by(WeatherReading.timestamp.desc())
            .first()
        )
        history = (
            session.query(WeatherReading)
            .filter(WeatherReading.city == city, WeatherReading.timestamp >= cutoff_24h)
            .all()
        )
        events_24h = (
            session.query(WeatherEvent)
            .filter(WeatherEvent.city == city, WeatherEvent.timestamp >= cutoff_24h)
            .count()
        )

        if not latest:
            rows.append((city, "n/a", "n/a", "n/a", "n/a", "n/a", 0))
            continue

        has_data = True
        now_temp = f"{latest.temperature:+.1f}°C"

        if history:
            temps = [r.temperature for r in history]
            avg = sum(temps) / len(temps)
            t_min = min(temps)
            t_max = max(temps)
            avg_str = f"{avg:+.1f}°C"
            range_str = f"{t_min:+.1f} to {t_max:+.1f}°C"
            window = f"{len(history)} readings"
        else:
            avg_str = "n/a"
            range_str = "n/a"
            window = "no 24h data"

        rows.append((city, now_temp, avg_str, range_str, window, latest.timestamp, events_24h))

    if not has_data:
        print("No readings in database yet.")
        return

    # Header
    c1, c2, c3, c4, c5 = 10, 9, 10, 20, 13
    header = (
        f"  {'City':{c1}} | {'Now':{c2}} | {'24h avg':{c3}} | {'24h range':{c4}} | {'Events (24h)':{c5}}"
    )
    divider = "  " + "-" * (c1 + 2) + "+" + "-" * (c2 + 2) + "+" + "-" * (c3 + 2) + "+" + "-" * (c4 + 2) + "+" + "-" * (c5 + 2)
    print(header)
    print(divider)

    for row in rows:
        city, now_temp, avg_str, range_str, _window, ts, events_24h = row
        print(
            f"  {city:{c1}} | {now_temp:{c2}} | {avg_str:{c3}} | {range_str:{c4}} | {events_24h!s:{c5}}"
        )

    print()
    print("  As of:")
    for row in rows:
        city, *_, ts, _ = row
        if isinstance(ts, datetime):
            print(f"    {city:12} {_fmt_ts(ts)}")

    # Note if 24h data is limited
    oldest_overall = session.query(WeatherReading).order_by(WeatherReading.timestamp).first()
    if oldest_overall:
        hours_available = (now - oldest_overall.timestamp).total_seconds() / 3600
        if hours_available < 24:
            print(
                f"\n  Note: only {hours_available:.0f}h of data collected — "
                "24h averages and ranges cover the full available window."
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_QUESTIONS = {
    "summary":   question_summary,
    "trends":    question_trends,
    "anomalies": question_anomalies,
    "compare":   question_compare,
}


def main() -> None:
    """Parse arguments, open a DB session, and run the requested analysis."""
    parser = argparse.ArgumentParser(
        description="Query the WatchAgent database and print structured output."
    )
    parser.add_argument(
        "--question",
        choices=list(_QUESTIONS),
        default="summary",
        help="Which analysis to run (default: summary)",
    )
    args = parser.parse_args()

    _load_env()

    pkg_root = Path(__file__).resolve().parents[2]
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))

    session = _make_session()
    try:
        _QUESTIONS[args.question](session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
