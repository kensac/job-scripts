from __future__ import annotations

from api import db
from api.routers.job_board import NOT_APPLIED
from core import page_fetches
from core.store import add_ai_result
from tests.job_board_support import _insert_job, _job_ids, _pass_closed, _subscribe, _uid


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


def test_job_detail_returns_content_verdicts_history(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-d", "https://x.test/d1", company="Acme", title="SWE")
    page_fetches.record("https://x.test/d1", "passed", "scraped", "THE JOB TEXT")
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
    job_id, url = f.make_ready_job(source="comped", comp_min=90000, comp_max=120000)
    f.make_board_row(user_id["id"], job_id)
    db.execute("UPDATE job_comp SET comp_currency = 'CAD' WHERE url = %s", (url,))

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
    """compensation.py writes comp_period and comp_basis so an annualised figure can say
    what it was annualised FROM - its own comment is that a yearly number with
    no period beside it cannot be audited. Both were written and neither was
    selected, so an hourly rate and a salary rendered identically: a $45/hr
    posting and a $94k posting both showed as "$94k".
    """
    uid = _uid(user_headers)
    _subscribe(uid, "src-comp")
    job_id = _insert_job("src-comp", "https://comp.test/1")
    db.execute(
        "INSERT INTO job_comp (url, comp_min, comp_max, comp_text, comp_currency, comp_period, "
        "comp_basis) VALUES (%s, 94000, 94000, '$45/hr', 'USD', 'hourly', 'base')",
        ("https://comp.test/1",),
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
