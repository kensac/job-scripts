"""The debug view, and the override that corrects a wrong match.

The override is the interesting one. Both logs are append-only, so a
correction is a newer row rather than an edit - which means a systematically
wrong matcher tier stays visible in the history instead of being papered over
one row at a time. That history is the only evidence the MATCHER needs fixing
rather than the row.
"""

from __future__ import annotations

import datetime

from api import db


def _msg(uid: int, mid: str, **kw) -> int:
    row = db.query_one(
        """
        INSERT INTO email_messages (user_id, provider_message_id, source, from_email,
                                    subject, sent_at, body_text, prefilter_hit)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (
            uid,
            mid,
            kw.get("source", "gmail"),
            kw.get("from_email", "no-reply@greenhouse.io"),
            kw.get("subject", "Your application"),
            kw.get("sent_at", datetime.datetime.now(datetime.UTC)),
            kw.get("body", "Thanks for applying."),
            kw.get("prefilter_hit", True),
        ),
    )
    assert row is not None
    return row["id"]


def _app(uid: int, company="Acme") -> int:
    row = db.query_one(
        "INSERT INTO applications (user_id, company_name, title) VALUES (%s, %s, %s) RETURNING id",
        (uid, company, "Engineer"),
    )
    assert row is not None
    return row["id"]


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


def test_requires_admin(client, user_headers):
    assert client.get("/v1/admin/mail", headers=user_headers).status_code == 403


def test_lists_with_the_current_verdict(client, admin_headers):
    uid = _uid(admin_headers)
    mid = _msg(uid, "<dbg1@x>")
    db.execute("INSERT INTO email_events (message_id, kind) VALUES (%s, %s)", (mid, "rejection"))
    body = client.get("/v1/admin/mail", headers=admin_headers).json()
    row = next(r for r in body["rows"] if r["id"] == mid)
    assert row["kind"] == "rejection"
    assert row["prefilter_reason"] is None or isinstance(row["prefilter_reason"], str)


def test_only_the_newest_classification_shows(client, admin_headers):
    """Append-only: a correction is a newer row, and the list must not show
    the superseded one as if it were current."""
    uid = _uid(admin_headers)
    mid = _msg(uid, "<dbg2@x>")
    db.execute("INSERT INTO email_events (message_id, kind) VALUES (%s, %s)", (mid, "rejection"))
    db.execute(
        "INSERT INTO email_events (message_id, kind) VALUES (%s, %s)", (mid, "interview_invite")
    )
    body = client.get("/v1/admin/mail", headers=admin_headers).json()
    assert next(r for r in body["rows"] if r["id"] == mid)["kind"] == "interview_invite"


def test_unmatched_is_filterable(client, admin_headers):
    """A NULL application_id is a recorded outcome - 'we looked and found
    nothing' - so it is a value to filter ON, not an absence to skip."""
    uid = _uid(admin_headers)
    mid = _msg(uid, "<dbg3@x>")
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method) VALUES (%s, %s, %s)",
        (mid, None, "unmatched"),
    )
    body = client.get("/v1/admin/mail?matched=false", headers=admin_headers).json()
    assert any(r["id"] == mid for r in body["rows"])
    body = client.get("/v1/admin/mail?matched=true", headers=admin_headers).json()
    assert not any(r["id"] == mid for r in body["rows"])


def test_detail_shows_full_history_not_just_current(client, admin_headers):
    """The history is the point: a match that changed when a posting reached
    the board is exactly what someone debugging a wrong answer needs."""
    uid = _uid(admin_headers)
    mid = _msg(uid, "<dbg4@x>")
    app = _app(uid)
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method) VALUES (%s, %s, %s)",
        (mid, None, "unmatched"),
    )
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method) VALUES (%s, %s, %s)",
        (mid, app, "ats_company"),
    )
    body = client.get(f"/v1/admin/mail/{mid}", headers=admin_headers).json()
    assert len(body["matches"]) == 2
    assert body["matches"][0]["application_id"] is None
    assert body["matches"][1]["application_id"] == app


def test_detail_exposes_what_tier_one_would_see(client, admin_headers):
    """So a missed exact-link match can be diagnosed without re-running the
    matcher and guessing at why it found nothing."""
    uid = _uid(admin_headers)
    mid = _msg(uid, "<dbg5@x>", body="apply at https://job-boards.greenhouse.io/acme/jobs/9")
    body = client.get(f"/v1/admin/mail/{mid}", headers=admin_headers).json()
    assert any("greenhouse" in u for u in body["canonical_urls"])


def test_override_appends_rather_than_edits(client, admin_headers):
    """The matcher's own attempt survives underneath. A systematically wrong
    tier stays visible instead of being corrected away one row at a time."""
    uid = _uid(admin_headers)
    mid = _msg(uid, "<dbg6@x>")
    app = _app(uid)
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method) VALUES (%s, %s, %s)",
        (mid, None, "unmatched"),
    )
    resp = client.post(
        f"/v1/admin/mail/{mid}/match", json={"application_id": app}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["current"]["application_id"] == app

    rows = db.query("SELECT * FROM application_matches WHERE message_id = %s ORDER BY id", (mid,))
    assert len(rows) == 2
    assert rows[0]["method"] == "unmatched"
    assert rows[1]["method"] == "manual"


def test_override_refuses_another_users_application(client, admin_headers, other_user_headers):
    """Would attribute one person's outcome to another's application. 404
    rather than 403: whether that application exists is not the caller's to
    learn."""
    admin_uid, other_uid = _uid(admin_headers), _uid(other_user_headers)
    mid = _msg(admin_uid, "<dbg7@x>")
    theirs = _app(other_uid, company="TheirCo")
    resp = client.post(
        f"/v1/admin/mail/{mid}/match", json={"application_id": theirs}, headers=admin_headers
    )
    assert resp.status_code == 404
    assert db.query("SELECT * FROM application_matches WHERE message_id = %s", (mid,)) == []


def test_pipeline_hides_closed_by_default(client, user_headers):
    uid = _uid(user_headers)
    live, dead = _app(uid, "LiveCo"), _app(uid, "DeadCo")
    mid = _msg(uid, "<dbg8@x>")
    db.execute("INSERT INTO email_events (message_id, kind) VALUES (%s, %s)", (mid, "rejection"))
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method) VALUES (%s, %s, %s)",
        (mid, dead, "ats_company"),
    )
    body = client.get("/v1/user/pipeline", headers=user_headers).json()
    ids = [a["id"] for a in body["applications"]]
    assert live in ids
    assert dead not in ids
    assert dead in [
        a["id"]
        for a in client.get("/v1/user/pipeline?include_closed=true", headers=user_headers).json()[
            "applications"
        ]
    ]
