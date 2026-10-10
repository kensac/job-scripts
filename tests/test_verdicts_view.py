"""`verdicts` is the one statement of which ai_queries rows are answers.

The view exists so a reader names the concept instead of restating its
predicate. These pin what it admits, and that the predicate is not restated
by a new reader, which is how the fifty copies it replaced came about.
"""

from __future__ import annotations

import pathlib
import re

from api import db
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
