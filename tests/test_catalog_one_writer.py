"""The jobs table has one writer: core.catalog.

Writes were spread over nine files, each with its own statement and none
bound by the lock order multi-row writers keep (core.catalog._LOCK_ORDER).
"""

from __future__ import annotations

import pathlib
import re

import pytest

from core import catalog

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_OWNER = _SRC / "core" / "catalog.py"

_RAW = re.compile(
    r"\b(?:INSERT\s+INTO|DELETE\s+FROM|UPDATE|MERGE\s+INTO)\s+(?:public\.)?jobs\b",
    re.IGNORECASE,
)

# Each entry leaves when its write moves; test_allowed_writers_still_write
# fails once a file here no longer writes, so the list cannot go stale.
_ALLOWED = {
    # Seeds a disposable database only (make dev-api refuses any other).
    "api/devseed.py": "dev seed of postings in a disposable database",
}


def _writers() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        if path == _OWNER:
            continue
        text = path.read_text()
        for m in _RAW.finditer(text):
            found.setdefault(str(path.relative_to(_SRC)), []).append(
                text.count("\n", 0, m.start()) + 1
            )
    return found


def test_no_jobs_write_outside_the_catalog():
    copies = [f"{p}:{n}" for p, lines in _writers().items() if p not in _ALLOWED for n in lines]
    assert not copies, "Write jobs through core.catalog: " + ", ".join(copies)


def test_allowed_writers_still_write():
    assert not set(_ALLOWED) - set(_writers()), "Remove these from _ALLOWED"


def test_correct_posting_routes_active_and_refuses_other_columns(f):
    job_id = f.make_job(company="Old", active=True)
    row = catalog.correct_posting(job_id, {"title": "New", "active": False})
    assert row is not None
    assert (row["title"], row["company"], row["active"]) == ("New", "Old", False)
    assert catalog.correct_posting(job_id + 999, {"active": True}) is None
    assert catalog.correct_posting(job_id + 999, {"title": "x"}) is None
    with pytest.raises(ValueError, match="comp_min"):
        catalog.correct_posting(job_id, {"comp_min": 1})


def test_upload_extraction_states(f):
    uid = f.make_user()
    row = catalog.add_upload("https://x.test/up", "https://x.test/up", uid)
    assert (row["extraction_status"], row["uploaded_by"]) == ("pending", uid)
    catalog.set_extraction_status(row["id"], "failed")
    # Saving it again after a failure retries the extraction.
    assert (
        catalog.add_upload("https://x.test/up", "https://x.test/up", uid)["extraction_status"]
        == "pending"
    )
    catalog.record_extraction(row["id"], "Acme", "Engineer", ["Remote"], ["Summer 2027"])
    again = catalog.add_upload("https://x.test/up", "https://x.test/up", uid)
    assert again["extraction_status"] == "done"


# jobs.extraction_status is frozen: it keeps the sheet import's done stamp and
# one forced reparse, recorded nowhere else, and nothing may change it.
_FROZEN = re.compile(r"\bextraction_status\s*=(?!=)|\bSET\s+extraction_status\b", re.IGNORECASE)


def test_nothing_writes_the_frozen_extraction_status():
    writes = [
        f"{path.relative_to(_SRC)}:{text.count(chr(10), 0, m.start()) + 1}"
        for path in sorted(_SRC.rglob("*.py"))
        for text in [path.read_text()]
        for m in _FROZEN.finditer(text)
    ]
    assert not writes, "jobs.extraction_status is frozen: " + ", ".join(writes)
