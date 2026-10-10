"""The current event and the current match of a message have one owner.

`api.mail.current` replaced 36 inline copies of "newest row per message" over
`email_events` and `application_matches`, in nine files. The reference SQL
below is the copy as it was written, kept so the owner is compared against it
on rows where the newest row is not the oldest: a reclassified message, a
rematch, a refusal written over an attachment.
"""

from __future__ import annotations

import pathlib
import re

from api import db
from api.mail.current import current_event, current_match

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_OWNER = _SRC / "api" / "mail" / "current.py"

# Every column list a caller reads, as the copies spelled them.
_EVENT_COLUMNS = [
    (),
    ("kind",),
    ("kind", "detail"),
    ("id", "kind"),
    ("kind", "detail", "created_at"),
    ("kind", "confidence", "detail"),
    ("id", "kind", "detail"),
    ("kind", "confidence", "detail", "model"),
    ("kind", "id", "occurred_at", "deadline_at", "deadline_inferred"),
    ("kind", "confidence", "model", "deadline_inferred", "detail"),
]
_MATCH_COLUMNS = [
    ("id",),
    ("application_id",),
    ("application_id", "created_at"),
    ("application_id", "method"),
    ("application_id", "method", "confidence"),
    ("id", "application_id", "method", "actor_user_id"),
]


def _reference(table: str, columns: tuple[str, ...]) -> str:
    listed = "".join(f", {c}" for c in columns)
    return f"SELECT DISTINCT ON (message_id) message_id{listed}\nFROM {table} ORDER BY message_id, id DESC"


def _fill(f) -> None:
    user = f.make_user()
    apps = [
        db.query_one(
            "INSERT INTO applications (user_id, company_name, title, source_provenance) "
            "VALUES (%s, %s, 'Engineer', 'email') RETURNING id",
            (user, name),
        )["id"]
        for name in ("Acme", "Globex")
    ]
    for n, (kinds, matches) in enumerate(
        [
            # Reclassified: the rejection must retract the acknowledgement.
            (["acknowledgement", "rejection"], [(apps[0], "ats_company", None)]),
            # Rematched to another application, then refused by a person.
            (["interview_invite"], [(apps[0], "ats_company", None), (apps[1], "manual", user)]),
            (["rejection"], [(apps[1], "ats_company", None), (None, "detached", user)]),
            # Never matched, and a message with no event at all.
            (["offer", "not_job_related", "offer"], []),
            ([], [(None, "unmatched", None)]),
        ]
    ):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, sent_at) "
            "VALUES (%s, %s, 'gmail', now()) RETURNING id",
            (user, f"<m{n}@x>"),
        )["id"]
        for i, kind in enumerate(kinds):
            db.execute(
                "INSERT INTO email_events (message_id, kind, confidence, detail, model, "
                "occurred_at, deadline_inferred) "
                "VALUES (%s, %s, %s, %s, %s, now(), %s)",
                (mid, kind, f"c{i}", db.jsonb({"company": f"co{i}"}), f"m{i}", i % 2 == 1),
            )
        for app, method, actor in matches:
            db.execute(
                "INSERT INTO application_matches (message_id, application_id, method, "
                "confidence, rationale, actor_user_id) VALUES (%s, %s, %s, 'high', 'why', %s)",
                (mid, app, method, actor),
            )


def test_owner_returns_what_every_copy_returned(f):
    _fill(f)
    # The fixture can tell newest from oldest, or the comparison proves nothing.
    assert db.query_one(
        "SELECT count(*) AS n FROM (SELECT message_id FROM email_events GROUP BY 1 "
        "HAVING count(DISTINCT kind) > 1) t"
    )["n"]
    assert db.query_one(
        "SELECT count(*) AS n FROM (SELECT message_id FROM application_matches GROUP BY 1 "
        "HAVING count(*) > 1) t"
    )["n"]
    for table, fn, column_lists in (
        ("email_events", current_event, _EVENT_COLUMNS),
        ("application_matches", current_match, _MATCH_COLUMNS),
    ):
        oldest = db.query(f"SELECT DISTINCT ON (message_id) * FROM {table} ORDER BY message_id, id")
        for columns in column_lists:
            order = " ORDER BY message_id"
            old = db.query(f"SELECT * FROM ({_reference(table, columns)}) r{order}")
            new = db.query(f"SELECT * FROM ({fn(*columns)}) r{order}")
            assert new == old, (table, columns)
            assert len(new) == len(oldest)
        newest = db.query(f"SELECT * FROM ({fn('id')}) r ORDER BY message_id")
        assert [r["id"] for r in newest] != [r["id"] for r in oldest]


# A copy spelled with a table alias or extra spaces is still a copy.
_COPY = re.compile(r"DISTINCT\s+ON\s*\(\s*(?:\w+\.)?message_id\s*\)", re.IGNORECASE)


def test_no_copy_outside_the_owner():
    copies = [
        f"{path.relative_to(_SRC)}:{text.count(chr(10), 0, m.start()) + 1}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _OWNER
        for text in [path.read_text()]
        for m in _COPY.finditer(text)
    ]
    assert not copies, (
        "Read the current event or match through api.mail.current, not a new copy: "
        + ", ".join(copies)
    )
