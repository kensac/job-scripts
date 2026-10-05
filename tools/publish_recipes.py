"""Publish the extension's bundled recipe tables to the API's recipe store.

Reads extension/adapters/recipes/*.json, produced by tools/convert_ats_config.py,
validates each table the way the API does and
writes it as the current revision for its adapter. It writes to whatever
DATABASE_URL names, so publishing to production names production on purpose:

    DATABASE_URL="$PRODUCTION_DATABASE_URL" .venv/bin/python tools/publish_recipes.py
    DATABASE_URL="$PRODUCTION_DATABASE_URL" .venv/bin/python tools/publish_recipes.py workday
    .venv/bin/python tools/publish_recipes.py --dry-run  # validate only, no database

A republish of an unchanged table is the same revision and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ATS = ROOT / "extension" / "adapters" / "recipes"


def tables() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(ATS.glob("*.json")):
        cfg = json.loads(path.read_text())
        out[cfg["name"].lower()] = cfg
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Publish extension/adapters/recipes/*.json to the recipe store "
        "named by DATABASE_URL."
    )
    parser.add_argument("adapters", nargs="*", help="adapters to publish (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="validate only, no database")
    args = parser.parse_args(argv)
    wanted = {a.lower() for a in args.adapters}
    # Imported after parsing, because importing api.db opens the pool: --help
    # and a usage error must exit before any connection. A dry run never
    # touches the database, so it gets a closed port rather than the shell's.
    if args.dry_run:
        os.environ["DATABASE_URL"] = "postgresql://unused:unused@127.0.0.1:1/no_database_here"
    from api.apply import recipes as extension_recipes

    who = os.environ.get("USER", "publish_recipes")
    for adapter, cfg in tables().items():
        if wanted and adapter not in wanted:
            continue
        raw = extension_recipes.validate(adapter, cfg)
        revision = extension_recipes.revision_of(raw)
        if args.dry_run:
            print(f"{adapter}: valid, {len(raw):,} bytes, revision {revision[:12]}")
            continue
        extension_recipes.publish(adapter, cfg, who)
        print(f"{adapter}: published {len(raw):,} bytes as {revision[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
