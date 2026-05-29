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
import os
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


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


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

def question_summary(session: "Session") -> None:
    """Per-city reading count, event count, latest timestamp, temperature range."""
    from sqlalchemy import func
    from app.models.reading import WeatherReading
    from app.models.event import WeatherEvent

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    cities = [r[0] for r in session.query(WeatherReading.city).distinct().all()]
    if not cities:
        print("No readings in database yet.")
        return

    for city in sorted(cities):
        readings = (
            session.query(WeatherReading)
            .filter(WeatherReading.city == city)
            .all()
        )
        event_count = (
            session.query(WeatherEvent)
            .filter(WeatherEvent.city == city)
            .count()
        )
        temps = [r.temperature for r in readings]
        latest = max(r.timestamp for r in readings)

        print(f"\n{city}")
        print(f"  Readings : {len(readings)}")
        print(f"  Events   : {event_count}")
        print(f"  Latest   : {latest.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  Temp range: {min(temps):.1f}°C – {max(temps):.1f}°C")

    total_readings = session.query(WeatherReading).count()
    total_events = session.query(WeatherEvent).count()
    print(f"\nTotal: {total_readings} readings, {total_events} events")


def question_trends(session: "Session") -> None:
    """Average temperature per day over the last 7 days, as an ASCII bar chart."""
    from app.models.reading import WeatherReading

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
    readings = (
        session.query(WeatherReading)
        .filter(WeatherReading.timestamp >= cutoff)
        .order_by(WeatherReading.timestamp)
        .all()
    )

    if not readings:
        print("No readings in the last 7 days.")
        return

    # Group by (city, date)
    buckets: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in readings:
        day = r.timestamp.strftime("%Y-%m-%d")
        buckets[r.city][day].append(r.temperature)

    print("=" * 60)
    print("7-DAY TEMPERATURE TRENDS  (°C avg per day)")
    print("=" * 60)

    for city in sorted(buckets):
        print(f"\n{city}")
        days = sorted(buckets[city])
        avgs = {d: sum(v) / len(v) for d, v in buckets[city].items()}
        min_t = min(avgs.values())
        max_t = max(avgs.values())
        scale = max_t - min_t or 1.0

        for day in days:
            avg = avgs[day]
            bar_len = int(((avg - min_t) / scale) * 30) + 1
            bar = "█" * bar_len
            print(f"  {day}  {avg:+6.1f}°C  {bar}")


def question_anomalies(session: "Session") -> None:
    """List all city_anomaly events with their z-score from the metrics field."""
    from app.models.event import WeatherEvent

    events = (
        session.query(WeatherEvent)
        .filter(WeatherEvent.event_type == "city_anomaly")
        .order_by(WeatherEvent.timestamp.desc())
        .all()
    )

    print("=" * 60)
    print(f"CITY ANOMALY EVENTS  ({len(events)} total)")
    print("=" * 60)

    if not events:
        print("No city_anomaly events recorded.")
        return

    for e in events:
        z = e.metrics.get("z_score", "n/a")
        temp = e.metrics.get("temperature", "n/a")
        mean = e.metrics.get("mean", "n/a")
        print(
            f"\n{e.timestamp.strftime('%Y-%m-%d %H:%M')}  {e.city}"
            f"\n  z-score : {z}"
            f"\n  temp    : {temp}°C  (mean {mean}°C)"
            f"\n  summary : {e.summary}"
        )


def question_compare(session: "Session") -> None:
    """Side-by-side latest conditions for all three cities."""
    from app.models.reading import WeatherReading

    cities = ["Ottawa", "Toronto", "Vancouver"]
    latest: dict[str, WeatherReading] = {}

    for city in cities:
        row = (
            session.query(WeatherReading)
            .filter(WeatherReading.city == city)
            .order_by(WeatherReading.timestamp.desc())
            .first()
        )
        if row:
            latest[city] = row

    if not latest:
        print("No readings in database yet.")
        return

    print("=" * 60)
    print("CURRENT CONDITIONS COMPARISON")
    print("=" * 60)
    header = f"{'':20} " + "  ".join(f"{c:>12}" for c in cities)
    print(f"\n{header}")
    print("-" * len(header))

    def _row(label: str, fmt: Callable, getter: Callable) -> str:
        vals = [fmt(getter(latest[c])) if c in latest else "       n/a" for c in cities]
        return f"  {label:18} " + "  ".join(f"{v:>12}" for v in vals)

    print(_row("Temperature",      lambda v: f"{v:+.1f}°C",  lambda r: r.temperature))
    print(_row("Feels like",        lambda v: f"{v:+.1f}°C",  lambda r: r.apparent_temperature))
    print(_row("Precipitation",     lambda v: f"{v:.1f} mm",  lambda r: r.precipitation))
    print(_row("Wind speed",        lambda v: f"{v:.0f} km/h", lambda r: r.wind_speed))
    print(_row("Weather code",      lambda v: str(v),          lambda r: r.weather_code))

    print("\n  As of:")
    for city in cities:
        if city in latest:
            ts = latest[city].timestamp.strftime("%Y-%m-%d %H:%M UTC")
            print(f"    {city:12} {ts}")


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

    # Add the watchagent package root to sys.path so `app.*` imports resolve
    # when the script is run from the watchagent/ directory.
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
