"""Move zero-field submissions recorded before #1268 onto the fills that hold
the fields (api.apply.submissions.repair), and print the counts around it.

It writes to whatever DATABASE_URL names, so repairing production names
production on purpose:

    DATABASE_URL="$PRODUCTION_DATABASE_URL" .venv/bin/python tools/repair_empty_fills.py --dry-run
    DATABASE_URL="$PRODUCTION_DATABASE_URL" .venv/bin/python tools/repair_empty_fills.py

One transaction. A dry run prints the same counts and rolls back. A second
run moves nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _DryRun(Exception):
    pass


def counts() -> dict[str, int]:
    from api import db

    row = db.query_one(
        "SELECT count(*) FILTER (WHERE submitted_at IS NOT NULL) AS submitted, "
        "count(*) FILTER (WHERE submitted_at IS NOT NULL "
        "AND jsonb_array_length(fields) = 0) AS submitted_empty, "
        "count(*) FILTER (WHERE superseded_by IS NOT NULL) AS superseded, "
        "count(*) AS fills FROM application_fills"
    )
    assert row is not None
    return row


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the counts, then roll back")
    args = parser.parse_args(argv)
    # Imported after parsing: importing api.db opens the pool.
    from api import db
    from api.apply import submissions

    try:
        with db.transaction():
            before = counts()
            moved = submissions.repair()
            after = counts()
            for empty, filled in moved:
                print(f"fill {empty} -> fill {filled}")
            print(f"moved {len(moved)}")
            for key in before:
                print(f"{key}: {before[key]} -> {after[key]}")
            if args.dry_run:
                raise _DryRun
    except _DryRun:
        print("dry run: rolled back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
