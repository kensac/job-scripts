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
_FEED_STATE: dict[str, str] = {}

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


_WRITE = re.compile(
    r"UPDATE\s+jobs\s+SET\s+active\b|active\s*=\s*EXCLUDED\.active"
    r"|INSERT\s+INTO\s+jobs\s*\([^)]*\bactive\b",
    re.IGNORECASE,
)

# jobs.active is frozen (docs/agents/architecture-migration.md, the jobs.active
# contract). The dev seed fills a disposable database only.
_WRITES_ALLOWED = {"api/devseed.py"}


def test_nothing_writes_the_frozen_feed_flag():
    writers = {
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if _WRITE.search(path.read_text())
    }
    assert writers - _WRITES_ALLOWED == set()
