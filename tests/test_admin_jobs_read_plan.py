from api import db
from api.routers.admin import posting_inspection


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def test_recent_postings_page_does_not_aggregate_unselected_history(monkeypatch):
    db.execute(
        "INSERT INTO ai_queries(url,created_at,status) "
        "SELECT 'https://history.test/'||i, "
        "'2026-01-01'::timestamptz + i * interval '1 minute', 'passed' "
        "FROM generate_series(1,10000) i"
    )
    db.execute("ANALYZE ai_queries")
    original = db.query
    reads = []

    def record(sql, params=None):
        if "ledger_rows" in sql:
            reads.append((sql, params))
        return original(sql, params)

    monkeypatch.setattr(db, "query", record)
    page = posting_inspection.list_jobs(page_size=5, user=None)
    assert page.total == 10000
    assert [row.url for row in page.rows] == [
        f"https://history.test/{i}" for i in range(10000, 9995, -1)
    ]
    assert len(reads) == 1
    sql, params = reads[0]
    plan = db.query_one("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + sql, params)["QUERY PLAN"][
        0
    ]["Plan"]
    examined = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in _nodes(plan)
        if node.get("Relation Name") in ("ai_queries", "page_fetches")
    )
    # Exact totals may scan the URL index. Loading five rows must not also
    # aggregate the history of all ten thousand postings.
    assert examined < 10000, plan


def _seed_three_checked_postings() -> None:
    """Three urls, two of them checked twice (b's two checks tie on time),
    and an orphan check with no url that the page must leave out."""
    db.execute(
        "INSERT INTO ai_queries(url,created_at,status,company,config_name) VALUES "
        "('https://x.test/a','2026-01-03','passed','Alpha','recent'), "
        "('https://x.test/a','2026-01-01','rejected','Zeta','older'), "
        "('https://x.test/b','2026-01-03','failed','Beta','recent'), "
        "('https://x.test/b','2026-01-03','passed','Beta','recent'), "
        "('https://x.test/c','2026-01-02','passed',NULL,NULL), "
        "(NULL,'2026-01-04','passed','Excluded',NULL)"
    )
    # Each answer's tokens are its call's.
    db.execute(
        """
        WITH n AS (
            SELECT id, CASE WHEN url = 'https://x.test/a' AND status = 'passed' THEN 20
                            WHEN url = 'https://x.test/a' THEN 10
                            WHEN url = 'https://x.test/b' AND status = 'passed' THEN 5
                            WHEN url IS NULL THEN 100 END AS tokens
            FROM ai_queries
        ),
        calls AS (
            INSERT INTO model_calls (purpose, batched, prompt_tokens, completion_tokens,
                                     total_tokens, cached_tokens, source, source_id)
            SELECT 'verify', false, 0, 0, tokens, 0, 'verdict', id FROM n
            WHERE tokens IS NOT NULL
            RETURNING id, source_id
        )
        UPDATE ai_queries q SET model_call_id = calls.id FROM calls WHERE q.id = calls.source_id
        """
    )


def test_a_recent_postings_row_rolls_up_every_check_on_its_url(client, admin_headers):
    _seed_three_checked_postings()
    first = client.get("/v1/admin/jobs", params={"page_size": 1}, headers=admin_headers)
    assert first.status_code == 200
    body = first.json()
    assert body["total"] == 3
    assert body["has_more"] is True
    assert body["rows"][0] | {"last_seen": None} == {
        "url": "https://x.test/a",
        "company": "Zeta",
        "job_title": None,
        "config_name": "recent",
        "checks": 2,
        "passed": 1,
        "rejected": 1,
        "failed": 0,
        "total_tokens": 30,
        "last_seen": None,
        "verdict": "rejected",
        "content_failures": 0,
        "unfetchable": False,
    }


def test_the_next_recent_postings_page_keeps_both_tied_checks(client, admin_headers):
    _seed_three_checked_postings()
    second = client.get(
        "/v1/admin/jobs", params={"page_size": 1, "page": 2}, headers=admin_headers
    ).json()
    assert second["rows"][0]["url"] == "https://x.test/b"
    assert second["rows"][0]["checks"] == 2
    assert second["rows"][0]["total_tokens"] == 5


def test_a_recent_postings_page_past_the_end_is_empty_and_keeps_the_total(client, admin_headers):
    _seed_three_checked_postings()
    empty = client.get(
        "/v1/admin/jobs", params={"page_size": 1, "page": 4}, headers=admin_headers
    ).json()
    assert empty["total"] == 3
    assert empty["rows"] == []
    assert empty["has_more"] is False


def test_a_config_filter_selects_urls_and_reports_every_check_on_them(client, admin_headers):
    _seed_three_checked_postings()
    unfiltered = client.get("/v1/admin/jobs", params={"page_size": 1}, headers=admin_headers).json()
    filtered = client.get(
        "/v1/admin/jobs", params={"config": "older"}, headers=admin_headers
    ).json()
    assert filtered["rows"] == unfiltered["rows"]


def test_verdict_tabs_walk_the_window_and_match_the_grouped_read():
    """Passed and Rejected read the newest-first window and check each url's
    whole history; the answer is the one the grouped HAVING read gives."""
    db.execute(
        "INSERT INTO ai_queries(url,created_at,status) VALUES "
        "('https://v.test/rejected-late','2026-01-09','rejected'), "
        "('https://v.test/rejected-early','2026-01-01','rejected'), "
        "('https://v.test/rejected-early','2026-01-08','passed'), "
        "('https://v.test/passed','2026-01-07','passed'), "
        "('https://v.test/failed','2026-01-06','failed'), "
        "('https://v.test/tie-b','2026-01-03','passed'), "
        "('https://v.test/tie-a','2026-01-03','passed'), "
        "('https://v.test/unstatused','2026-01-02',NULL)"
    )
    db.execute(
        "INSERT INTO page_fetches(url,status,method,created_at) VALUES "
        "('https://v.test/fetched','passed','scraped','2026-01-05'), "
        "('https://v.test/verification-only','passed','verification','2026-01-04'), "
        "('https://v.test/verification-only','failed','scraped','2026-01-04'), "
        "('https://v.test/failed','passed','scraped','2026-01-02')"
    )
    for verdict in ("rejected", "passed"):
        posting_inspection._verdict_totals.clear()
        pages = [
            posting_inspection.list_jobs(verdict=verdict, page=page, page_size=2, user=None)
            for page in (1, 2, 3)
        ]
        # Two sort keys take the grouped read with the same order.
        grouped = posting_inspection.list_jobs(
            verdict=verdict, sort="last_seen,url", dir="desc,asc", page_size=50, user=None
        )
        assert [row for page in pages for row in page.rows] == grouped.rows
        assert {page.total for page in pages} == {grouped.total}
        assert grouped.total == len(grouped.rows)
        if verdict == "rejected":
            assert [row.url for row in grouped.rows] == [
                "https://v.test/rejected-late",
                "https://v.test/rejected-early",
            ]
    assert [row.url for row in grouped.rows] == [
        "https://v.test/passed",
        "https://v.test/failed",
        "https://v.test/fetched",
        "https://v.test/tie-a",
        "https://v.test/tie-b",
    ]
