from __future__ import annotations

from api import db
from core.store import add_ai_result
from tests.factories import paid_answer


def test_admin_failures_breakdown_grouped_by_worker_and_host(client, admin_headers):
    add_ai_result("https://hosta.example.com/1", "failed", check_type="closed", error="boom")
    add_ai_result("https://hosta.example.com/2", "failed", check_type="closed", error="boom")
    add_ai_result("https://hostb.example.com/1", "failed", check_type="clearance", error="boom2")

    resp = client.get("/v1/admin/failures", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    by_host = {(r["host"], r["check_type"]): r["failures"] for r in rows}
    assert by_host[("hosta.example.com", "closed")] == 2
    assert by_host[("hostb.example.com", "clearance")] == 1
    assert all(r["worker"] for r in rows)


def test_query_options_serves_live_vocabularies(client, admin_headers):
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('src-a', 'https://x') ON CONFLICT DO NOTHING"
    )
    add_ai_result("https://o.test/1", "passed", "r", "closed", config_name="verify-batch")
    resp = client.get("/v1/admin/queries/options", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "src-a" in body["sources"]
    assert "closed" in body["check_types"]
    assert "verify-batch" in body["contexts"]


def test_queries_filter_by_source(client, admin_headers):
    from core.store import add_ai_result

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES "
        "('https://s.test/a', 'src-x', 'A', 'T'), ('https://s.test/b', 'src-y', 'B', 'T')"
    )
    add_ai_result("https://s.test/a", "passed", "r", "closed")
    add_ai_result("https://s.test/b", "passed", "r", "closed")
    resp = client.get("/v1/admin/queries?sources=src-x", headers=admin_headers)
    urls = [r["url"] for r in resp.json()["rows"]]
    assert "https://s.test/a" in urls and "https://s.test/b" not in urls


def test_batch_jobs_drilldown_reports_per_job_cost(client, admin_headers):

    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, purpose, model, requests, status) "
        "VALUES ('batch_x1', 'verify', 'gpt-5-nano', 1, 'completed')"
    )
    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://b.test/1','s','Acme','SWE')"
    )
    paid_answer(
        "https://b.test/1",
        "passed",
        check_type="closed",
        model=None,
        usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
        batch_id="batch_x1",
    )
    resp = client.get("/v1/admin/batches/batch_x1/jobs", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch"]["provider_batch_id"] == "batch_x1"
    row = body["rows"][0]
    assert row["url"] == "https://b.test/1" and row["prompt_tokens"] == 1000
    assert row["est_cost_usd"] is not None and row["est_cost_usd"] > 0
    assert client.get("/v1/admin/batches/nope/jobs", headers=admin_headers).status_code == 404
