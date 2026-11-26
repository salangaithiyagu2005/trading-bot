from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from nsebot import logstore


TABLES = (
    "rules_versions",
    "app_settings",
    "sessions",
    "events",
    "orders",
    "trades",
    "reviews",
    "commands",
    "engine_status",
)
COUNTERS = {
    "rules_versions": "rules",
    "events": "events",
    "orders": "orders",
    "trades": "trades",
    "reviews": "reviews",
    "commands": "commands",
}


def migrate(source_path: Path, data_dir: Path) -> dict[str, int]:
    if os.environ.get("DATABASE_BACKEND", "").strip().lower() != "dynamodb":
        raise RuntimeError("Set DATABASE_BACKEND=dynamodb before migrating to DynamoDB.")
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    destination = None
    counts: dict[str, int] = {}
    try:
        destination = logstore.connect(data_dir)
        for table in TABLES:
            rows = source.execute(f"SELECT * FROM {table}").fetchall()
            counts[table] = len(rows)
            for row in rows:
                logstore.import_sqlite_record(
                    destination, table, dict(row), update_counter=False,
                )
            if rows and table in COUNTERS:
                destination.ensure_counter_at_least(
                    COUNTERS[table],
                    max(int(row["id"]) for row in rows),
                )
        return counts
    finally:
        source.close()
        if destination is not None:
            destination.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy local SQLite data into DynamoDB.")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(os.environ.get("DATA_DIR", "./var")) / "algo.db",
        help="SQLite database file (default: DATA_DIR/algo.db)",
    )
    arguments = parser.parse_args()
    try:
        counts = migrate(arguments.source, Path(os.environ.get("DATA_DIR", "./var")))
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    for table, count in counts.items():
        print(f"{table}: {count} rows")
    print("SQLite data migration complete.")


if __name__ == "__main__":
    main()
