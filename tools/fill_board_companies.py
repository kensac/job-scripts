"""Name company boards that were stored without a company, then name their
postings (core.catalog.fill_source_company), and print the counts around it.

A board format that needs its source's company (boards.NEEDS_COMPANY) stores
every posting nameless when the source has none. 884 Lever and Ashby sources
were inserted that way on 2026-09-12, outside the API that refuses it.

A nameless source is named from the catalog's other postings on the same
board, which aggregators and the sheet import stored with a company: the
spelling most of them use. A tie at the top is not guessed. It is printed,
and the source is left for an administrator to name on the Sources page.
Then every company board's nameless postings take their source's company,
so a rerun also reaches sources named by hand since. Only empty values are
written: a stored company is never replaced, and a second run writes nothing.

It writes to whatever DATABASE_URL names, so filling production names
production on purpose:

    DATABASE_URL="$PRODUCTION_DATABASE_URL" .venv/bin/python tools/fill_board_companies.py --dry-run
    DATABASE_URL="$PRODUCTION_DATABASE_URL" .venv/bin/python tools/fill_board_companies.py
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The formats whose postings live under a board path the listings URL names:
# api.lever.co/v0/postings/<board> serves jobs.lever.co/<board>/<id>, and
# api.ashbyhq.com/posting-api/job-board/<board> serves
# jobs.ashbyhq.com/<board>/<id>. On 2026-11-01 every nameless company board
# was one of these two.
_POSTING_BOARD_FORMATS = frozenset({"lever", "ashby"})


def posting_board(listings_url: str) -> str:
    """The prefix every posting of this board's url starts with, lowercased."""
    parsed = urlparse(listings_url)
    board = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return f"https://jobs.{parsed.netloc.lower().removeprefix('api.')}/{board.lower()}/"


def company_boards() -> list[dict]:
    from api import db
    from core.fetching import boards

    return [
        row
        for row in db.query("SELECT name, listings_url, company FROM sources ORDER BY name")
        if boards.format_for_url(row["listings_url"]).needs_company
    ]


def counts(names: list[str]) -> dict[str, int]:
    from api import db

    row = db.query_one(
        "SELECT (SELECT count(*) FROM sources WHERE name = ANY(%(n)s) "
        "AND coalesce(btrim(company), '') = '') AS nameless_sources, "
        "(SELECT count(*) FROM jobs WHERE source = ANY(%(n)s) AND company = '') "
        "AS nameless_jobs, "
        "(SELECT count(DISTINCT source) FROM jobs WHERE source = ANY(%(n)s) AND company = '') "
        "AS sources_with_nameless_jobs",
        {"n": names},
    )
    assert row is not None
    return row


def spellings(boards_by_source: dict[str, str]) -> dict[str, list[tuple[str, int]]]:
    """Each source's company spellings on its board's other postings, most
    used first."""
    from api import db

    hosts = sorted({urlparse(board).netloc for board in boards_by_source.values()})
    rows = db.query(
        "SELECT lower(substring(url from '^(https://[^/]+/[^/?#]+/)')) AS board, "
        "btrim(company) AS company, count(*) AS n FROM jobs "
        "WHERE split_part(url, '/', 3) = ANY(%s) AND btrim(company) <> '' "
        "GROUP BY 1, 2",
        (hosts,),
    )
    by_board: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in rows:
        by_board[row["board"]].append((row["company"], row["n"]))
    return {
        source: sorted(by_board.get(board, []), key=lambda spelling: (-spelling[1], spelling[0]))
        for source, board in boards_by_source.items()
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = parser.parse_args(argv)
    # Imported after parsing: importing api.db opens the pool.
    from api import db
    from core import catalog
    from core.fetching import boards

    sources = company_boards()
    names = [s["name"] for s in sources]
    before = counts(names)

    nameless = [s for s in sources if not (s["company"] or "").strip()]
    keyed = {
        s["name"]: posting_board(s["listings_url"])
        for s in nameless
        if boards.format_for_url(s["listings_url"]).name in _POSTING_BOARD_FORMATS
    }
    for s in nameless:
        if s["name"] not in keyed:
            print(f"unnamed {s['name']}: no posting board rule for {s['listings_url']}")
    named: dict[str, str] = {}
    for source, found in spellings(keyed).items():
        if not found:
            print(f"unnamed {source}: no named posting on {keyed[source]}")
        elif len(found) > 1 and found[0][1] == found[1][1]:
            print(f"unnamed {source}: tie {found[:3]}")
        else:
            named[source] = found[0][0]
            print(f"name {source} = {found[0][0]!r} from {found}")

    if args.dry_run:
        pending = db.query(
            "SELECT source, count(*) AS n FROM jobs WHERE source = ANY(%s) AND company = '' "
            "GROUP BY source",
            (names,),
        )
        stored = {s["name"] for s in sources if (s["company"] or "").strip()}
        reached = sum(r["n"] for r in pending if r["source"] in named or r["source"] in stored)
        print(f"would name {len(named)} of {len(nameless)} nameless sources")
        print(f"would fill {reached} of {before['nameless_jobs']} nameless jobs")
        return 0

    for source, company in named.items():
        db.execute(
            "UPDATE sources SET company = %s WHERE name = %s AND coalesce(btrim(company), '') = ''",
            (company, source),
        )
    filled = 0
    # Read back rather than reuse the plan: a source named by hand wins.
    for row in db.query(
        "SELECT name, company FROM sources WHERE name = ANY(%s) "
        "AND coalesce(btrim(company), '') <> '' "
        "AND name IN (SELECT source FROM jobs WHERE company = '') ORDER BY name",
        (names,),
    ):
        filled += catalog.fill_source_company(row["name"], row["company"])
    after = counts(names)
    print(f"named {len(named)} sources, filled {filled} jobs")
    for key in before:
        print(f"{key}: {before[key]} -> {after[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
