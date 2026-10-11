from __future__ import annotations

from api import db


def _content_row(url, reason, when_days_ago):
    import datetime

    from core import page_fetches

    fetch_id = page_fetches.record(url, "passed", reason, "X" * 400)
    stamp = (datetime.datetime.now() - datetime.timedelta(days=when_days_ago)).isoformat()
    db.execute("UPDATE page_fetches SET created_at = %s WHERE id = %s", (stamp, fetch_id))


def test_health_detects_ats_collapse_and_resolves_when_it_recovers(client, admin_headers):
    from api import health

    db.execute(
        "INSERT INTO jobs (url, source, company, title) SELECT "
        "'https://h.test/' || g, 'hsrc', 'C', 'T' FROM generate_series(1,60) g"
    )
    # Baseline week: ATS text served reliably.
    for i in range(1, 31):
        _content_row(f"https://h.test/{i}", "ats text", 4)
    # Last 24h: resolver broke, everything fell back to scraping.
    for i in range(31, 61):
        _content_row(f"https://h.test/{i}", "scraped", 0)

    found = health.detect()
    collapse = [f for f in found if f["kind"] == "ats_text_collapse" and f["subject"] == "hsrc"]
    assert collapse, f"expected an ats collapse alert, got {[f['kind'] for f in found]}"
    assert collapse[0]["severity"] == "critical"

    fresh = health.record(found)
    assert any(f["subject"] == "hsrc" for f in fresh)
    # Re-detecting the same condition must not re-notify.
    assert not [f for f in health.record(health.detect()) if f["subject"] == "hsrc"]

    resp = client.get("/v1/admin/health", headers=admin_headers)
    assert any(a["subject"] == "hsrc" for a in resp.json()["open"])

    # Condition clears -> alert auto-resolves, but only after RESOLVE_GRACE of
    # silence: a detector going quiet for one cycle is not proof it is over.
    health.record([f for f in health.detect() if f["subject"] != "hsrc"])
    assert (
        db.query_one("SELECT resolved_at FROM health_alerts WHERE subject = 'hsrc'")["resolved_at"]
        is None
    )
    db.execute(
        "UPDATE health_alerts SET last_seen = now() - interval '1 day' WHERE subject = 'hsrc'"
    )
    health.record([f for f in health.detect() if f["subject"] != "hsrc"])
    row = db.query_one("SELECT resolved_at FROM health_alerts WHERE subject = 'hsrc'")
    assert row["resolved_at"] is not None


def test_health_ignores_low_volume_noise(client, admin_headers):
    from api import health

    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES "
        "('https://q.test/1','quiet','C','T'), ('https://q.test/2','quiet','C','T')"
    )
    _content_row("https://q.test/1", "ats text", 4)
    _content_row("https://q.test/2", "scraped", 0)
    assert not [f for f in health.detect() if f["subject"] == "quiet"]


def test_rate_spike_ignores_expiring_rechecks_but_catches_fresh_misclassification(
    client, admin_headers
):
    """The first live alert was a reverify backlog on a fast-expiring board:
    real closures, but coverage changing rather than anything breaking. Only
    freshly-seen jobs being written off means something upstream is wrong."""
    import datetime

    from api import health
    from core.store import add_ai_result

    def closed_row(url, status, days_ago, company):
        add_ai_result(url, status, "", "closed", company=company)
        stamp = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
        db.execute(
            "UPDATE ai_queries SET created_at = %s WHERE id = "
            "(SELECT MAX(id) FROM ai_queries WHERE url = %s)",
            (stamp, url),
        )

    # One company per job: a rate built from a single employer's bulk drop is
    # capped at MAX_PER_COMPANY, which is the point of the cap.
    db.execute(
        "INSERT INTO jobs (url, source, company, title) SELECT "
        "'https://exp.test/' || g, 'expsrc', 'co' || g, 'T' FROM generate_series(1,120) g"
    )
    # Baseline week: fresh jobs arrive open.
    for i in range(1, 61):
        closed_row(f"https://exp.test/{i}", "passed", 4, f"co{i}")
    # Last 24h: those same jobs are RE-checked and have since expired.
    for i in range(1, 61):
        closed_row(f"https://exp.test/{i}", "rejected", 0, f"co{i}")
    assert not [f for f in health.detect() if f["subject"] == "expsrc"], (
        "expiring re-checks must not read as breakage"
    )

    # Now the real signal: brand-new jobs written off on arrival.
    for i in range(61, 121):
        closed_row(f"https://exp.test/{i}", "rejected", 0, f"co{i}")
    spike = [f for f in health.detect() if f["subject"] == "expsrc"]
    assert spike and spike[0]["kind"] == "closed_rate_spike"
    assert "newly-seen" in spike[0]["message"]
