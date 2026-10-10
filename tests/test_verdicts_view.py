"""`verdicts` is the one statement of which ai_queries rows are answers.

The view exists so a reader names the concept instead of restating its
predicate. These pin what it admits, and that the predicate is not restated
by a new reader, which is how the fifty copies it replaced came about.
"""

from __future__ import annotations

import pathlib
import re

from api import db
from core import verdict_reads
from core.checks import POSTING_CHECK_NAMES
from core.store import add_ai_result

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# Readers that keep the predicate on purpose, because they read calls rather
# than answers: spend prices every call in a window and counts the decided
# ones as a share of it; a review gate outcome records whether its call wrote
# an answer. The ORM's index predicates describe ai_queries itself.
_CALL_READERS = {
    "api/routers/spend.py",
    "api/review_gate_records.py",
    "api/orm/ai.py",
}
_DECIDED = re.compile(r"status IN \('passed', ?'rejected'\)")


def test_every_check_and_filter_answer_is_a_verdict_and_nothing_else_is():
    checks = (*POSTING_CHECK_NAMES, "custom")
    for check in checks:
        for status in ("passed", "rejected", "failed"):
            add_ai_result(f"https://x/{check}/{status}", status, check_type=check)
    add_ai_result("https://x/page", "passed", check_type="content", input_content="text")

    rows = db.query("SELECT url FROM verdicts ORDER BY url")
    assert [r["url"] for r in rows] == sorted(
        f"https://x/{check}/{status}" for check in checks for status in ("passed", "rejected")
    )


def test_only_call_readers_restate_what_a_decided_answer_is():
    restated = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if _DECIDED.search(path.read_text())
    )
    assert set(restated) <= _CALL_READERS, (
        "read decided answers from the verdicts view instead of restating its predicate: "
        f"{sorted(set(restated) - _CALL_READERS)}"
    )


# How an answer is read is core/verdict_reads.py's. A module that writes the
# latest-answer shape itself (an ORDER BY id DESC over the view, or an EXISTS
# on one posting's answers) is a copy free to drift from it. Each entry here
# is a read that is not that shape, or a copy not yet moved, with why.
_OWNER = "core/verdict_reads.py"
_NOT_YET_MOVED = "latest row per key, moves onto verdict_reads in the next change"
_HAND_WRITTEN_ALLOWED = {
    # The first answer to each check, not the latest: `p.id < q.id`.
    "api/health/sources.py": "earliest answer per posting and check",
    "api/board/eligibility.py": _NOT_YET_MOVED,
    "api/board/visibility.py": _NOT_YET_MOVED,
    "api/compensation_candidates.py": _NOT_YET_MOVED,
    "api/experiments.py": _NOT_YET_MOVED,
    "api/managed_board_runs.py": _NOT_YET_MOVED,
    "api/routers/analytics.py": _NOT_YET_MOVED,
    "api/routers/companies.py": _NOT_YET_MOVED,
    "api/routers/filters.py": _NOT_YET_MOVED,
    "api/routers/job_detail.py": _NOT_YET_MOVED,
    "core/store.py": _NOT_YET_MOVED,
    "tasks/verify.py": _NOT_YET_MOVED,
}
_READ = re.compile(r"\b(?:FROM|JOIN) verdicts\b")
_NEWEST_FIRST = re.compile(r"\bq?id DESC\b")
# A Python string ends where the next line does not continue it with another.
_STRING_END = re.compile(r'"[ \t]*,?[ \t]*\n[ \t]*+(?![frb]*")')
_EXISTS = re.compile(r"SELECT 1 FROM verdicts \w+\s+WHERE \w+\.url = ")


def _hand_written(text: str) -> bool:
    for read in _READ.finditer(text):
        close = text.find('"""', read.end())
        statement = text[read.end() : close if close != -1 else len(text)]
        if end := _STRING_END.search(statement):
            statement = statement[: end.start()]
        if _NEWEST_FIRST.search(statement):
            return True
    return bool(_EXISTS.search(text))


def test_the_guard_sees_each_shape_it_names():
    assert _hand_written(
        '"SELECT status FROM verdicts WHERE url = %s "\n    "ORDER BY id DESC LIMIT 1",\n'
    )
    assert _hand_written("SELECT DISTINCT ON (url) url FROM verdicts q\nORDER BY url, q.id DESC")
    assert _hand_written("WHERE EXISTS (SELECT 1 FROM verdicts c\n WHERE c.url = j.url)")
    assert not _hand_written(
        '"SELECT DISTINCT url FROM verdicts WHERE url = ANY(%s)",\n    (urls,),\n'
        '"SELECT id FROM jobs ORDER BY id DESC"'
    )


def test_only_the_owner_writes_a_latest_answer_shape():
    hand_written = {
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if _hand_written(path.read_text())
    } - {_OWNER}
    allowed = set(_HAND_WRITTEN_ALLOWED)
    assert hand_written <= allowed, (
        "read the latest answer through core.verdict_reads instead of writing it out: "
        f"{sorted(hand_written - allowed)}"
    )
    assert allowed <= hand_written, (
        f"no longer hand-written, drop from the allow-list: {sorted(allowed - hand_written)}"
    )


def test_latest_is_the_highest_id_and_has_verdict_is_any():
    add_ai_result("https://x/a", "rejected", check_type="closed")
    add_ai_result("https://x/a", "passed", check_type="closed")
    row = db.query_one(
        f"SELECT {verdict_reads.closed_verdict('%(url)s')} AS shown, "
        f"{verdict_reads.has_verdict('%(url)s', 'closed', 'rejected')} AS ever_rejected, "
        f"{verdict_reads.has_verdict('%(url)s', 'clearance')} AS any_clearance",
        {"url": "https://x/a"},
    )
    assert row == {"shown": "open", "ever_rejected": True, "any_clearance": False}
    latest = verdict_reads.read_latest("https://x/a", "closed")
    assert latest is not None and latest.status == "passed"
    assert verdict_reads.read_latest("https://x/a", "clearance") is None
