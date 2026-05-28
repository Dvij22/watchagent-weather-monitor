Skills in this directory are executable Python scripts that query the
live database and return structured output. Run from the watchagent/ directory:

  python .cursor/skills/data_analysis.py --question summary
  python .cursor/skills/data_analysis.py --question trends
  python .cursor/skills/data_analysis.py --question anomalies
  python .cursor/skills/data_analysis.py --question compare

  python .cursor/skills/replay_detection.py
  python .cursor/skills/replay_detection.py --n 96
  python .cursor/skills/replay_detection.py --n 48 --city Ottawa

Both scripts load DATABASE_URL from the environment or a local .env file.
They exit 0 on success and 1 on DB connection failure.
