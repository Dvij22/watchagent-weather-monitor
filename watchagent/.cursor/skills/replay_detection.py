"""replay_detection.py — re-run EventDetector over historical DB readings.

Loads the last N readings per city from the live database, replays them
through EventDetector in chronological order, and prints which events
would have fired.  Useful for evaluating whether a new or modified
detector would have caught real historical conditions.

Usage (run from the watchagent/ directory):
    python .cursor/skills/replay_detection.py
    python .cursor/skills/replay_detection.py --n 96
    python .cursor/skills/replay_detection.py --n 48 --city Ottawa

DATABASE_URL is read from the environment or a .env file in the current directory.
Exits 0 on success, 1 on DB connection failure.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ImportError:
        pass


def _make_session() -> "Session":
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
    except Exception as exc:
        print(f"ERROR: DB connection failed — {exc}", file=sys.stderr)
        sys.exit(1)


def _add_pkg_root() -> None:
    """Ensure app.* imports resolve when run from watchagent/."""
    pkg_root = Path(__file__).resolve().parents[2]
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay EventDetector over historical readings from the DB."
    )
    parser.add_argument(
        "--n",
        type=int,
        default=48,
        help="Number of most-recent readings to replay per city (default: 48)",
    )
    parser.add_argument(
        "--city",
        type=str,
        default=None,
        help="Restrict replay to a single city (default: all cities)",
    )
    args = parser.parse_args()

    _load_env()
    _add_pkg_root()

    from app.models.reading import WeatherReading
    from app.services.event_detector import EventDetector
    from app.services.weather_client import CITIES, RawReading

    session = _make_session()
    try:
        cities = [args.city] if args.city else list(CITIES)
        detector = EventDetector()
        total_events = 0

        for city in cities:
            # Fetch newest-first, then reverse so we replay oldest→newest
            rows = (
                session.query(WeatherReading)
                .filter(WeatherReading.city == city)
                .order_by(WeatherReading.timestamp.desc())
                .limit(args.n)
                .all()
            )
            rows = list(reversed(rows))

            if not rows:
                print(f"\n{city}: no readings found.")
                continue

            print(f"\n{'=' * 60}")
            print(f"REPLAY  {city}  ({len(rows)} readings)")
            first_ts = rows[0].timestamp.strftime("%Y-%m-%d %H:%M")
            last_ts  = rows[-1].timestamp.strftime("%Y-%m-%d %H:%M")
            print(f"  {first_ts}  →  {last_ts}")
            print("=" * 60)

            city_events = 0
            # Build up history as we advance through time
            history: list[WeatherReading] = []

            for row in rows:
                new = RawReading(
                    city=row.city,
                    timestamp=row.timestamp,
                    temperature=row.temperature,
                    apparent_temperature=row.apparent_temperature,
                    precipitation=row.precipitation,
                    wind_speed=row.wind_speed,
                    weather_code=row.weather_code,
                )
                # history is newest-first for the detector
                events = detector.detect_events(new, list(reversed(history)))

                for e in events:
                    ts = e["timestamp"]
                    ts_str = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else str(ts)
                    print(f"\n  [{ts_str}]  {e['event_type']}")
                    print(f"    summary : {e['summary']}")
                    print(f"    reason  : {e['reason']}")
                    print(f"    metrics : {e['metrics']}")
                    city_events += 1

                history.append(row)
                # Keep the same window size used by the live poller (settings.history_limit).
                if len(history) > args.n:
                    history.pop(0)

            if city_events == 0:
                print("  (no events would have fired)")
            else:
                print(f"\n  → {city_events} event(s) fired for {city}")
            total_events += city_events

        print(f"\n{'=' * 60}")
        print(f"REPLAY COMPLETE — {total_events} total event(s) across {len(cities)} city/cities")

    finally:
        session.close()


if __name__ == "__main__":
    main()
