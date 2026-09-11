from __future__ import annotations

import datetime
import os

from api import db
from api.routers.job_board import NOT_APPLIED
from core.store import add_ai_result

SERVICE_TOKEN = os.environ["JOBTRACKER_SERVICE_TOKEN"]


def _uid(headers: dict) -> int:
    return db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))["id"]


def _insert_job(
    source: str,
    url: str,
    active: bool = True,
    uploaded_by=None,
    locations=None,
    terms=None,
    date_posted=None,
    company: str = "Acme",
    title: str = "Engineer",
) -> int:
    row = db.query_one(
        """
        INSERT INTO jobs (url, raw_url, company, title, locations, terms, source, active, date_posted, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            url,
            url,
            company,
            title,
            locations or [],
            terms or [],
            source,
            active,
            date_posted,
            uploaded_by,
        ),
    )
    return row["id"]


def _subscribe(uid: int, source: str) -> None:
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (uid, source),
    )


def _pass_closed(url: str) -> None:
    add_ai_result(url, "passed", check_type="closed")


def _job_ids(payload: dict) -> set:
    return {r["job_id"] for r in payload["rows"]}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_service_token_is_401(client):
    resp = client.get("/v1/user/jobs")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"


def test_wrong_service_token_is_401(client):
    resp = client.get("/v1/user/jobs", headers={"X-Service-Token": "definitely-wrong"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"


def test_valid_token_missing_user_headers_is_401(client):
    resp = client.get("/v1/user/jobs", headers={"X-Service-Token": SERVICE_TOKEN})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# GET /v1/user/jobs visibility matrix
# ---------------------------------------------------------------------------


def test_subscribed_closed_passed_no_filters_is_visible(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-a", "https://x.test/a1")
    _subscribe(uid, "src-a")
    _pass_closed("https://x.test/a1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert resp.status_code == 200
    assert jid in _job_ids(resp.json())


def test_unsubscribed_source_invisible_then_user_jobs_row_overrides(client, user_headers):
    _uid(user_headers)
    jid = _insert_job("src-b", "https://x.test/b1")
    _pass_closed("https://x.test/b1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid not in _job_ids(resp.json())

    patch = client.patch(f"/v1/user/jobs/{jid}", json={"notes": "watching"}, headers=user_headers)
    assert patch.status_code == 200

    resp2 = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp2.json())


def test_enabled_filter_gates_visibility_disabled_bypasses(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-c", "https://x.test/c1")
    _subscribe(uid, "src-c")
    _pass_closed("https://x.test/c1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())

    created = client.post(
        "/v1/user/filters",
        json={"name": "must-be-remote", "prompt": "must be remote"},
        headers=user_headers,
    )
    assert created.status_code == 200
    filt = created.json()
    prompt_hash = filt["prompt_hash"]
    filter_id = filt["id"]

    # enabled filter, no verdict yet -> invisible
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid not in _job_ids(resp.json())

    add_ai_result("https://x.test/c1", "rejected", check_type="custom", prompt_hash=prompt_hash)
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid not in _job_ids(resp.json())

    add_ai_result("https://x.test/c1", "passed", check_type="custom", prompt_hash=prompt_hash)
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())

    # disabling the filter makes the job visible regardless of verdict
    patch = client.patch(
        f"/v1/user/filters/{filter_id}", json={"enabled": False}, headers=user_headers
    )
    assert patch.status_code == 200
    add_ai_result("https://x.test/c1", "rejected", check_type="custom", prompt_hash=prompt_hash)
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())


def test_criteria_excluded_locations_match_places(client, user_headers):
    from tasks import locations

    uid = _uid(user_headers)
    jid_uk = _insert_job("src-d", "https://x.test/d1", locations=["London, UK"])
    jid_wa = _insert_job("src-d", "https://x.test/d2", locations=["Tukwila, WA"])
    _subscribe(uid, "src-d")
    _pass_closed("https://x.test/d1")
    _pass_closed("https://x.test/d2")
    locations.store("London, UK", locations.LocationExtract(country="GB", city="London"), "t")
    locations.store("Tukwila, WA", locations.LocationExtract(country="US", region="WA"), "t")
    locations.store("UK", locations.LocationExtract(country="GB"), "t")

    put = client.put(
        "/v1/user/settings", json={"criteria": {"excluded_locations": ["UK"]}}, headers=user_headers
    )
    assert put.status_code == 200

    resp = client.get("/v1/user/jobs", headers=user_headers)
    ids = _job_ids(resp.json())
    assert jid_uk not in ids
    assert jid_wa in ids


def test_criteria_date_posted_after_hides_older(client, user_headers):
    # Dates inside the 30-day window a non-admin's criteria carry by default,
    # so this exercises the date criterion alone.
    uid = _uid(user_headers)
    today = datetime.date.today()
    old_id = _insert_job(
        "src-e", "https://x.test/e1", date_posted=today - datetime.timedelta(days=20)
    )
    new_id = _insert_job(
        "src-e", "https://x.test/e2", date_posted=today - datetime.timedelta(days=5)
    )
    _subscribe(uid, "src-e")
    _pass_closed("https://x.test/e1")
    _pass_closed("https://x.test/e2")

    put = client.put(
        "/v1/user/settings",
        json={"criteria": {"date_posted_after": (today - datetime.timedelta(days=10)).isoformat()}},
        headers=user_headers,
    )
    assert put.status_code == 200

    resp = client.get("/v1/user/jobs", headers=user_headers)
    ids = _job_ids(resp.json())
    assert old_id not in ids
    assert new_id in ids


def test_criteria_max_age_days_is_a_rolling_window_with_a_catalog_fallback(client, user_headers):
    """A fixed date has to be moved by hand; the window moves every day. A
    posting the board never dated ages from the day the catalog first saw
    it, so it still expires; an acted-on row does not."""
    uid = _uid(user_headers)
    today = datetime.date.today()
    fresh = _insert_job(
        "src-age", "https://x.test/a1", date_posted=today - datetime.timedelta(days=5)
    )
    stale = _insert_job(
        "src-age", "https://x.test/a2", date_posted=today - datetime.timedelta(days=40)
    )
    undated = _insert_job("src-age", "https://x.test/a3", date_posted=None)
    stale_applied = _insert_job(
        "src-age", "https://x.test/a4", date_posted=today - datetime.timedelta(days=40)
    )
    _subscribe(uid, "src-age")
    for i in range(1, 5):
        _pass_closed(f"https://x.test/a{i}")
    db.execute("UPDATE jobs SET created_at = now() - interval '45 days' WHERE id = %s", (undated,))
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, 'Applied')",
        (uid, stale_applied),
    )
    assert _job_ids(client.get("/v1/user/jobs", headers=user_headers).json()) >= {
        fresh,
        stale,
        undated,
        stale_applied,
    }

    put = client.put(
        "/v1/user/settings", json={"criteria": {"max_age_days": 30}}, headers=user_headers
    )
    assert put.status_code == 200, put.text
    ids = _job_ids(client.get("/v1/user/jobs", headers=user_headers).json())
    assert fresh in ids and stale_applied in ids
    assert stale not in ids and undated not in ids
    # Out of range is refused, not clamped.
    assert (
        client.put(
            "/v1/user/settings", json={"criteria": {"max_age_days": 0}}, headers=user_headers
        ).status_code
        == 422
    )


def test_uploaded_by_user_always_visible(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("upload", "https://x.test/f1", active=False, uploaded_by=uid)

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())


def test_statuses_csv_filter_including_not_applied_sentinel(client, user_headers):
    uid = _uid(user_headers)
    j_none = _insert_job("src-g", "https://x.test/g1")
    j_applied = _insert_job("src-g", "https://x.test/g2")
    j_interviewing = _insert_job("src-g", "https://x.test/g3")
    _subscribe(uid, "src-g")
    for url in ("https://x.test/g1", "https://x.test/g2", "https://x.test/g3"):
        _pass_closed(url)

    client.patch(f"/v1/user/jobs/{j_applied}", json={"status": "applied"}, headers=user_headers)
    client.patch(
        f"/v1/user/jobs/{j_interviewing}", json={"status": "interviewing"}, headers=user_headers
    )

    resp = client.get(
        "/v1/user/jobs", params={"statuses": "applied,not_applied"}, headers=user_headers
    )
    ids = _job_ids(resp.json())
    assert j_none in ids
    assert j_applied in ids
    assert j_interviewing not in ids


def test_bogus_sort_param_does_not_500(client, user_headers):
    resp = client.get("/v1/user/jobs", params={"sort": "not-a-real-column"}, headers=user_headers)
    assert resp.status_code == 200


def test_offset_limit_with_total(client, user_headers):
    uid = _uid(user_headers)
    for i in range(5):
        url = f"https://x.test/h{i}"
        _insert_job("src-h", url)
        _pass_closed(url)
    _subscribe(uid, "src-h")

    resp = client.get(
        "/v1/user/jobs",
        params={"limit": 2, "offset": 0, "with_total": "true"},
        headers=user_headers,
    )
    data = resp.json()
    assert data["total"] == 5
    assert len(data["rows"]) == 2
    assert data["has_more"] is True

    resp2 = client.get(
        "/v1/user/jobs",
        params={"limit": 2, "offset": 4, "with_total": "true"},
        headers=user_headers,
    )
    data2 = resp2.json()
    assert data2["total"] == 5
    assert len(data2["rows"]) == 1
    assert data2["has_more"] is False


def test_delete_user_job_removes_only_user_jobs_row(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-i", "https://x.test/i1")
    client.patch(f"/v1/user/jobs/{jid}", json={"notes": "x"}, headers=user_headers)
    assert db.query_one("SELECT 1 FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))

    resp = client.delete(f"/v1/user/jobs/{jid}", headers=user_headers)
    assert resp.status_code == 200
    assert (
        db.query_one("SELECT 1 FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))
        is None
    )
    assert db.query_one("SELECT 1 FROM jobs WHERE id = %s", (jid,)) is not None


def test_patch_status_autofills_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af1")
    resp = client.patch(f"/v1/user/jobs/{jid}", json={"status": "Applied"}, headers=user_headers)
    assert resp.status_code == 200
    # UTC, matching the server: the container's local date is an arbitrary
    # timezone to decide a user's "today" in.
    today = datetime.datetime.now(datetime.UTC).date()
    assert resp.json()["autofilled"] == {"date_applied": today.isoformat()}
    row = db.query_one(
        "SELECT date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid)
    )
    assert row["date_applied"] == today


def test_patch_explicit_date_applied_wins_over_autofill(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af2")
    resp = client.patch(
        f"/v1/user/jobs/{jid}",
        json={"status": "Applied", "date_applied": "2026-08-01"},
        headers=user_headers,
    )
    assert resp.json()["autofilled"] == {}
    row = db.query_one(
        "SELECT date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid)
    )
    assert row["date_applied"] == datetime.date(2026, 8, 1)


def test_patch_status_change_does_not_overwrite_existing_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af3")
    client.patch(
        f"/v1/user/jobs/{jid}",
        json={"status": "Applied", "date_applied": "2026-08-01"},
        headers=user_headers,
    )
    resp = client.patch(f"/v1/user/jobs/{jid}", json={"status": "Interview"}, headers=user_headers)
    assert resp.json()["autofilled"] == {}
    row = db.query_one(
        "SELECT date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid)
    )
    assert row["date_applied"] == datetime.date(2026, 8, 1)


def test_patch_notes_only_does_not_autofill_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af4")
    resp = client.patch(f"/v1/user/jobs/{jid}", json={"notes": "check later"}, headers=user_headers)
    assert resp.json()["autofilled"] == {}
    row = db.query_one(
        "SELECT date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid)
    )
    assert row["date_applied"] is None


def test_status_changes_append_history(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-h", "https://x.test/h1")
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Applied"}, headers=user_headers)
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Interview"}, headers=user_headers)
    client.patch(f"/v1/user/jobs/{jid}", json={"notes": "n"}, headers=user_headers)
    rows = db.query(
        "SELECT old_status, new_status FROM user_job_history WHERE user_id = %s AND job_id = %s ORDER BY id",
        (uid, jid),
    )
    assert [(r["old_status"], r["new_status"]) for r in rows] == [
        (None, "Applied"),
        ("Applied", "Interview"),
    ]


def test_job_detail_returns_content_verdicts_history(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-d", "https://x.test/d1", company="Acme", title="SWE")
    add_ai_result(
        "https://x.test/d1", "passed", "content cached", "content", input_content="THE JOB TEXT"
    )
    add_ai_result("https://x.test/d1", "passed", "job open", "closed")
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash) VALUES (%s, 'f1', 'p', 'hash1')",
        (uid,),
    )
    add_ai_result("https://x.test/d1", "passed", "matches profile", "custom", prompt_hash="hash1")
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Applied"}, headers=user_headers)
    resp = client.get(f"/v1/user/jobs/{jid}/detail", headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "THE JOB TEXT"
    assert body["row"]["status"] == "Applied"
    assert body["history"][0]["new_status"] == "Applied"
    assert {c["check_type"]: c["status"] for c in body["checks"]} == {"closed": "passed"}
    assert body["filter_verdicts"][0]["reason"] == "matches profile"


def test_job_options_serves_canon_plus_in_use(client, user_headers):
    jid = _insert_job("src-opt", "https://x.test/opt1")
    client.patch(
        f"/v1/user/jobs/{jid}", json={"status": "Weird Legacy State"}, headers=user_headers
    )
    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('src-opt', 'https://x') ON CONFLICT DO NOTHING"
    )
    uid = _uid(user_headers)
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, 'src-opt') ON CONFLICT DO NOTHING",
        (uid,),
    )
    resp = client.get("/v1/user/jobs/options", headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "Application Submitted" in body["statuses"]
    assert "Weird Legacy State" in body["statuses"]
    assert body["statuses"].index("Weird Legacy State") > body["statuses"].index("Rejected")
    assert body["not_applied_sentinel"] == "not_applied"
    assert "src-opt" in body["sources"]


def test_comp_amounts_are_served_with_their_currency(client, user_headers, f):
    """comp_min/comp_max are annualised numbers and 96% of extracted rows
    carry no currency. Serving the amount without the unit invites a renderer
    to supply one, which is how a CAD range becomes a dollar figure. NULL
    means we never captured a unit. It does not mean USD."""
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = f.make_ready_job(source="comped", comp_min=90000, comp_max=120000)
    f.make_board_row(user_id["id"], job_id)
    db.execute("UPDATE jobs SET comp_currency = 'CAD' WHERE id = %s", (job_id,))

    row = next(
        r
        for r in client.get("/v1/user/jobs?limit=100", headers=user_headers).json()["rows"]
        if r["job_id"] == job_id
    )
    assert row["comp_currency"] == "CAD"
    detail = client.get(f"/v1/user/jobs/{job_id}/detail", headers=user_headers).json()
    assert detail["job"]["comp_currency"] == "CAD"


def test_an_amount_with_no_captured_currency_says_so(client, user_headers, f):
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert user_id is not None
    job_id, _ = f.make_ready_job(source="uncurrenced", comp_min=90000, comp_max=120000)
    f.make_board_row(user_id["id"], job_id)

    row = next(
        r
        for r in client.get("/v1/user/jobs?limit=100", headers=user_headers).json()["rows"]
        if r["job_id"] == job_id
    )
    assert row["comp_min"] == 90000
    assert row["comp_currency"] is None, "absent currency must be visible, not implied"


def test_comp_carries_the_period_and_basis_it_was_derived_from(client, user_headers):
    """comp.py writes comp_period and comp_basis so an annualised figure can say
    what it was annualised FROM - its own comment is that a yearly number with
    no period beside it cannot be audited. Both were written and neither was
    selected, so an hourly rate and a salary rendered identically: a $45/hr
    posting and a $94k posting both showed as "$94k".
    """
    uid = _uid(user_headers)
    _subscribe(uid, "src-comp")
    job_id = _insert_job("src-comp", "https://comp.test/1")
    db.execute(
        "UPDATE jobs SET comp_min = 94000, comp_max = 94000, comp_text = '$45/hr', "
        "comp_currency = 'USD', comp_period = 'hourly', comp_basis = 'base' WHERE id = %s",
        (job_id,),
    )
    _pass_closed("https://comp.test/1")

    listed = client.get("/v1/user/jobs", headers=user_headers).json()["rows"]
    row = next(r for r in listed if r["job_id"] == job_id)
    assert row["comp_period"] == "hourly"
    assert row["comp_basis"] == "base"

    detail = client.get(f"/v1/user/jobs/{job_id}/detail", headers=user_headers).json()["job"]
    assert detail["comp_period"] == "hourly"
    assert detail["comp_basis"] == "base"


def test_total_rides_on_the_page_and_survives_an_empty_page(client, user_headers):
    uid = _uid(user_headers)
    for i in range(4):
        url = f"https://x.test/tt{i}"
        _insert_job("src-tt", url)
        _pass_closed(url)
    _subscribe(uid, "src-tt")
    page = client.get(
        "/v1/user/jobs",
        params={"limit": 3, "with_total": "true", "source": "src-tt", "sort": "company,added_at"},
        headers=user_headers,
    ).json()
    assert page["total"] == 4 and len(page["rows"]) == 3 and page["has_more"] is True
    assert "total_rows" not in page["rows"][0]
    beyond = client.get(
        "/v1/user/jobs",
        params={"limit": 3, "offset": 10, "with_total": "true", "source": "src-tt"},
        headers=user_headers,
    ).json()
    assert beyond["total"] == 4 and beyond["rows"] == [] and beyond["has_more"] is False


def test_the_board_filters_by_ats(client, user_headers):
    """ "Only the Ashby ones": the ATS is read off the url, offered with counts
    in the options, carried on every row, and filters the list."""
    uid = _uid(user_headers)
    urls = {
        "ashby": "https://jobs.ashbyhq.com/acme/1111",
        "greenhouse": "https://job-boards.greenhouse.io/acme/jobs/2222",
        "lever": "https://jobs.lever.co/acme/3333",
        "other": "https://careers.acme.test/jobs/4444",
    }
    for url in urls.values():
        _insert_job("src-ats", url)
        _pass_closed(url)
    _insert_job("src-ats", "https://jobs.ashbyhq.com/acme/5555")
    _pass_closed("https://jobs.ashbyhq.com/acme/5555")
    _subscribe(uid, "src-ats")
    options = client.get("/v1/user/jobs/options", headers=user_headers).json()
    assert options["ats"][0] == {"ats": "ashby", "count": 2}
    assert {a["ats"] for a in options["ats"]} == set(urls)
    page = client.get("/v1/user/jobs?ats=ashby&with_total=true", headers=user_headers).json()
    assert page["total"] == 2 and {r["ats"] for r in page["rows"]} == {"ashby"}
    everything = client.get("/v1/user/jobs", headers=user_headers).json()["rows"]
    assert {r["ats"] for r in everything} == set(urls)

    # The select's counts follow the lens: with both Ashby postings marked
    # applied, the to-apply lens shows no Ashby, and says so.
    for row in everything:
        if row["ats"] == "ashby":
            client.patch(
                f"/v1/user/jobs/{row['job_id']}",
                json={"status": "Application Submitted"},
                headers=user_headers,
            )
    page = client.get(
        f"/v1/user/jobs?statuses={NOT_APPLIED}&with_facets=true&ats=ashby", headers=user_headers
    ).json()
    facet = {f["ats"]: f["count"] for f in page["facets"]["ats"]}
    assert "ashby" not in facet and facet["lever"] == 1 and page["rows"] == []
    board_wide = client.get("/v1/user/jobs/options", headers=user_headers).json()["ats"]
    assert {a["ats"]: a["count"] for a in board_wide}["ashby"] == 2


def test_a_bulk_patch_publishes_one_event_not_one_per_row(client, user_headers, monkeypatch):
    """The bulk endpoint exists so a large selection is one request; the
    per-row publish is a synchronous post on the request path, so it must
    not become one post per row either. One event carries the ids."""
    from api import events

    published: list[dict] = []
    monkeypatch.setattr(events, "_publish", lambda channel, data: published.append(data))
    uid = _uid(user_headers)
    ids = [_insert_job("src-bulk", f"https://x.test/bulk{i}") for i in range(3)]
    _subscribe(uid, "src-bulk")
    for i in range(3):
        _pass_closed(f"https://x.test/bulk{i}")
    res = client.patch(
        "/v1/user/jobs",
        json={"job_ids": ids, "patch": {"status": "No Longer Interested"}},
        headers=user_headers,
    )
    assert res.status_code == 200 and res.json()["updated"] == 3
    kinds = [d["type"] for d in published]
    assert kinds.count("board_rows") == 1 and kinds.count("board_row") == 0
    event = next(d for d in published if d["type"] == "board_rows")
    assert sorted(event["job_ids"]) == sorted(ids) and event["status"] == "No Longer Interested"
