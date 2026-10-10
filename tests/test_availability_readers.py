"""Readers take availability from catalog.IS_AVAILABLE, not the feed's flag.

jobs.active is feed state: whichever source upserted a url last. A reader
that wants "a source says this is open" reads catalog.IS_AVAILABLE
(docs/agents/architecture-migration.md, phase 3). This fails on a new
`j.active` outside the files that want the feed's own flag. It matches the
`j` alias only, which is how every reader spells it today.
"""

from __future__ import annotations

import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_READ = re.compile(r"\bj\.active\b")

# Files that read the feed's own flag on purpose.
_FEED_STATE = {
    "core/catalog.py": "writes jobs.active and compares it (retire, upsert, shadow)",
    # The one deliberate exception (decided 2026-10-10). The preset coverage
    # counts mean availability, but IS_AVAILABLE took each of the two from
    # 4.3 s to 7.0 s on production, on a request a person waits for, for a
    # difference of 111 of 198,319 postings.
    "api/routers/filters.py": "preset coverage counts, kept on the flag for cost",
}

# Readers whose meaning is availability and that have not moved yet. Each
# entry leaves with the change that moves it.
_NOT_YET_MOVED: set[str] = set()


def _readers() -> set[str]:
    return {
        str(path.relative_to(_SRC)) for path in _SRC.rglob("*.py") if _READ.search(path.read_text())
    }


def test_no_new_reader_of_the_feed_flag():
    assert _readers() - set(_FEED_STATE) - _NOT_YET_MOVED == set()


def test_listed_files_still_read_it():
    # A file that stopped reading j.active leaves the list, so it cannot go stale.
    assert (set(_FEED_STATE) | _NOT_YET_MOVED) - _readers() == set()
