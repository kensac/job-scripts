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


def test_analytics_reports_where_the_matcher_refuses(client, admin_headers, f):
    """Finding a pattern by reading a hundred messages is work a GROUP BY does
    in a second. The distribution is the point."""
    uid = f.make_user()
    for kind, method in (
        ("rejection", "ats_company"),
        ("rejection", "unmatched"),
        ("recruiter_outreach", "not_an_application"),
    ):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
            "subject, sent_at, prefilter_hit) VALUES (%s,%s,'takeout','a@greenhouse.io','s',"
            "now(),TRUE) RETURNING id",
            (uid, f"an-{kind}-{method}"),
        )["id"]
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, model, detail) "
            "VALUES (%s,%s,'high','gpt-5.6-luna',%s)",
            (mid, kind, db.jsonb({"company": "Acme"})),
        )
        db.execute(
            "INSERT INTO application_matches (message_id, application_id, method, confidence) "
            "VALUES (%s, NULL, %s, 'none')",
            (mid, method),
        )

    body = client.get("/v1/admin/mail/analytics", headers=admin_headers).json()
    methods = {r["method"]: r["messages"] for r in body["matching"]}
    assert methods["unmatched"] == 1
    assert methods["not_an_application"] == 1, (
        "deliberately unattached must not read as a matcher failure"
    )
    kinds = {r["kind"] for r in body["classification"]}
    assert {"rejection", "recruiter_outreach"} <= kinds
    assert body["classification"][0]["model"] == "gpt-5.6-luna"


def test_analytics_measures_what_a_prefilter_gate_would_have_cost(client, admin_headers, f):
    """The prefilter gates nothing on purpose: a filtered-out email is the one
    unrecoverable failure, because the posting is closed and the thread is not
    coming back. It survives to answer this question and only this question."""
    uid = f.make_user()
    for hit, kind in ((False, "rejection"), (True, "rejection"), (False, "not_job_related")):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
            "subject, sent_at, prefilter_hit) VALUES (%s,%s,'takeout','a@b.com','s',now(),%s) "
            "RETURNING id",
            (uid, f"pf-{hit}-{kind}", hit),
        )["id"]
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,%s,'high')",
            (mid, kind),
        )

    body = client.get("/v1/admin/mail/analytics", headers=admin_headers).json()
    assert body["prefilter"]["job_related_a_gate_would_have_dropped"] == 1
    assert body["prefilter"]["job_related_total"] == 2


def test_corpus_block_describes_one_population_under_any_window(client, admin_headers, f):
    """The whole block is corpus-wide, including by_source.

    by_source used to interpolate the window while the totals above it did
    not, so at days=30 the breakdown summed to LESS than the total it was
    nested under, and `oldest` reported the 2018 start of an eight-year import
    on a screen labelled "last 30 days". Not a wrong number - a number
    counting a different population than its label claims.
    """
    uid = f.make_user()
    for i, (source, days_ago) in enumerate((("takeout", 900), ("gmail", 900), ("gmail", 1))):
        db.execute(
            "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
            "subject, sent_at, prefilter_hit) VALUES (%s,%s,%s,'a@b.com','s',"
            "now() - make_interval(days => %s), TRUE)",
            (uid, f"cov-{i}", source, days_ago),
        )

    windowed = client.get("/v1/admin/mail/analytics?days=30", headers=admin_headers).json()
    corpus = windowed["corpus"]
    assert corpus["messages"] == 3, "corpus totals must ignore the window"
    assert sum(r["messages"] for r in corpus["by_source"]) == corpus["messages"], (
        "by_source must sum to the total it is nested under"
    )
    assert corpus["unclassified"] == 3

    whole = client.get("/v1/admin/mail/analytics", headers=admin_headers).json()
    assert whole["corpus"] == corpus, "the corpus block cannot depend on the window at all"
    assert whole["window_days"] is None and windowed["window_days"] == 30


def test_each_section_ships_the_population_it_counts(client, admin_headers, f):
    """classification counts every classified message; matching and
    sender_domains exclude not_job_related. Side by side those invite a
    subtraction that means nothing, so the denominators travel with the rows
    instead of being hardcoded by whoever renders them."""
    uid = f.make_user()
    for i, kind in enumerate(("rejection", "rejection", "not_job_related")):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
            "subject, sent_at, prefilter_hit) VALUES (%s,%s,'takeout',%s,'s',now(),TRUE) "
            "RETURNING id",
            (uid, f"pop-{i}", f"a@d{i}.com"),
        )["id"]
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,%s,'high')",
            (mid, kind),
        )

    pops = client.get("/v1/admin/mail/analytics", headers=admin_headers).json()["populations"]
    assert pops["classification"]["messages"] == 3
    assert pops["classification"]["excludes"] == []
    assert pops["matching"]["messages"] == 2
    assert pops["matching"]["excludes"] == ["not_job_related"]
    assert pops["sender_domains"]["messages"] == 2
    # LIMIT 40 means the rows can be a truncation; say so in numbers rather
    # than letting a caller assume it is the whole set.
    assert pops["sender_domains"]["domains_shown"] == 2
    assert pops["sender_domains"]["domains_total"] == 2


def test_analytics_needs_admin(client, user_headers):
    assert client.get("/v1/admin/mail/analytics", headers=user_headers).status_code == 403


def test_a_literal_route_is_registered_before_the_one_that_would_swallow_it():
    """/admin/mail/analytics was silently answered by /admin/mail/{message_id}
    until it was moved above it. FastAPI matches in registration order and does
    not fall through on a failed conversion, so the literal path has to come
    first - and nothing about the code reads wrong when it does not."""
    from api.routers import mail

    paths = [r.path for r in mail.router.routes]
    assert paths.index("/admin/mail/analytics") < paths.index("/admin/mail/{message_id}")


def _msg_with(f, uid, kind, method, mid_suffix):
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, subject, "
        "sent_at) VALUES (%s,%s,'takeout','a@b.com','s',now()) RETURNING id",
        (uid, f"filt-{mid_suffix}"),
    )["id"]
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,%s,'high')", (mid, kind)
    )
    if method is not None:
        db.execute(
            "INSERT INTO application_matches (message_id, application_id, method, confidence) "
            "VALUES (%s, NULL, %s, 'none')",
            (mid, method),
        )
    return mid


def test_the_list_filters_on_how_a_message_matched(client, admin_headers, f):
    """The distributions point at a tier; without this a person can only see
    which tier fired by opening every message one at a time."""
    uid = f.make_user()
    _msg_with(f, uid, "rejection", "company_title", "a")
    _msg_with(f, uid, "rejection", "unmatched", "b")

    body = client.get("/v1/admin/mail?method=company_title", headers=admin_headers).json()
    assert body["total"] == 1
    assert body["rows"][0]["method"] == "company_title"


def test_refusing_to_match_is_filterable_apart_from_failing_to(client, admin_headers, f):
    """unmatched and not_an_application look identical in any aggregate and
    mean opposite things: the matcher failing to find a home, and the matcher
    correctly refusing to give one."""
    uid = f.make_user()
    _msg_with(f, uid, "rejection", "unmatched", "c")
    _msg_with(f, uid, "recruiter_outreach", "not_an_application", "d")

    assert client.get("/v1/admin/mail?method=unmatched", headers=admin_headers).json()["total"] == 1
    refused = client.get("/v1/admin/mail?method=not_an_application", headers=admin_headers).json()
    assert refused["total"] == 1
    assert refused["rows"][0]["kind"] == "recruiter_outreach"


def test_never_attempted_has_a_name_rather_than_being_an_absence(client, admin_headers, f):
    """A third state, and the one worth filtering for when hunting failures.
    Reachable only as a gap in a list is not reachable."""
    from api.routers.mail import NEVER_ATTEMPTED

    uid = f.make_user()
    _msg_with(f, uid, "rejection", None, "e")
    _msg_with(f, uid, "rejection", "unmatched", "g")

    body = client.get(f"/v1/admin/mail?method={NEVER_ATTEMPTED}", headers=admin_headers).json()
    assert body["total"] == 1
    assert body["rows"][0]["method"] is None
