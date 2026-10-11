"""Move old report pages to chunks, one committed report at a time."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write verified chunks")
    args = parser.parse_args()
    url = os.environ.get("PRODUCTION_DATABASE_URL")
    if not url:
        parser.error("PRODUCTION_DATABASE_URL is required")
    os.environ["DATABASE_URL"] = url

    from api.apply.report_backfill import move_one_report, remaining_pages

    before = remaining_pages()
    print(f"old report pages: {before}")
    if not args.apply:
        return
    moved = 0
    while move_one_report() is not None:
        moved += 1
    after = remaining_pages()
    print(f"moved: {moved}; remaining: {after}")
    if after:
        raise SystemExit("old report pages remain; rerun after other writers finish")


if __name__ == "__main__":
    main()
