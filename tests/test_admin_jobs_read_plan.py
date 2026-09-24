from api import db
from api.routers.admin import queries


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def test_recent_postings_page_does_not_aggregate_unselected_history(monkeypatch):
    db.execute(
        "INSERT INTO ai_queries(url,created_at,status,total_tokens) "
        "SELECT 'https://history.test/'||i, "
        "'2026-01-01'::timestamptz + i * interval '1 minute', 'passed', 10 "
        "FROM generate_series(1,10000) i"
    )
    db.execute("ANALYZE ai_queries")
    original = db.query
    reads = []

    def record(sql, params=None):
        if "ai_queries" in sql:
            reads.append((sql, params))
        return original(sql, params)

    monkeypatch.setattr(db, "query", record)
    page = queries.list_jobs(page_size=5, user=None)
    assert page.total == 10000
    assert [row.url for row in page.rows] == [
        f"https://history.test/{i}" for i in range(10000, 9995, -1)
    ]
    assert len(reads) == 1
    sql, params = reads[0]
    plan = db.query_one(
        "EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + sql, params
    )["QUERY PLAN"][0]["Plan"]
    examined = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in _nodes(plan)
        if node.get("Relation Name") == "ai_queries"
    )
    # Exact totals may scan the URL index. Loading five rows must not also
    # aggregate the history of all ten thousand postings.
    assert examined < 10000, plan


def test_recent_postings_keep_all_checks_ties_orphans_and_page_boundaries(client, admin_headers):
    db.execute(
        "INSERT INTO ai_queries(url,created_at,status,company,config_name,total_tokens) VALUES "
        "('https://x.test/a','2026-01-03','passed','Alpha','recent',20), "
        "('https://x.test/a','2026-01-01','rejected','Zeta','older',10), "
        "('https://x.test/b','2026-01-03','failed','Beta','recent',NULL), "
        "('https://x.test/b','2026-01-03','passed','Beta','recent',5), "
        "('https://x.test/c','2026-01-02','passed',NULL,NULL,NULL), "
        "(NULL,'2026-01-04','passed','Excluded',NULL,100)"
    )
    first = client.get("/v1/admin/jobs", params={"page_size": 1}, headers=admin_headers)
    assert first.status_code == 200
    body = first.json()
    assert body["total"] == 3
    assert body["has_more"] is True
    assert body["rows"][0] | {"last_seen": None} == {
        "url": "https://x.test/a", "company": "Zeta", "job_title": None,
        "config_name": "recent", "checks": 2, "passed": 1, "rejected": 1,
        "failed": 0, "total_tokens": 30, "last_seen": None, "verdict": "rejected",
    }
    second = client.get(
        "/v1/admin/jobs", params={"page_size": 1, "page": 2}, headers=admin_headers
    ).json()
    assert second["rows"][0]["url"] == "https://x.test/b"
    assert second["rows"][0]["checks"] == 2
    assert second["rows"][0]["total_tokens"] == 5
    empty = client.get(
        "/v1/admin/jobs", params={"page_size": 1, "page": 4}, headers=admin_headers
    ).json()
    assert empty["total"] == 3
    assert empty["rows"] == []
    assert empty["has_more"] is False
    # Filters select URLs, then the ledger reports every check on those URLs.
    filtered = client.get(
        "/v1/admin/jobs", params={"config": "older"}, headers=admin_headers
    ).json()
    assert filtered["rows"] == body["rows"]
