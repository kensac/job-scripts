"""Everything a board lists is kept, with the text it carried, so a candidate
pattern is judged against it before it goes live and no page is scraped for
text the board already handed over."""

from __future__ import annotations

import asyncio
import dataclasses

from api import db, verdicts
from api.tasks import ingest
from core import boards
from core.pittcsc_simplify import JobPosting

TODAY = 2_000_000_000


def _posting(title: str, url: str) -> JobPosting:
    return JobPosting(
        company="Acme",
        locations=["Austin, TX"],
        title=title,
        url=url,
        terms=[],
        active=True,
        date_posted=TODAY,
        raw_url="",
    )


LISTED = [
    _posting("Software Engineer, New Grad", "https://boards.greenhouse.io/acme/jobs/1"),
    _posting("Software Engineer", "https://boards.greenhouse.io/acme/jobs/2"),
    _posting("Senior Software Engineer", "https://boards.greenhouse.io/acme/jobs/3"),
    _posting("Quantitative Researcher", "https://boards.greenhouse.io/acme/jobs/4"),
]


def _ingest(monkeypatch, f, listed):
    async def no_fetch(*a, **kw):
        return None, None

    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: listed)
    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": "acme"}, status="running")
    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "acme"}))


def test_what_the_pattern_drops_is_kept_and_ages_out_when_the_board_stops_listing_it(
    monkeypatch, f
):
    f.make_source("acme")
    db.execute("UPDATE sources SET title_pattern = 'new grad' WHERE name = 'acme'")

    _ingest(monkeypatch, f, LISTED)

    assert {r["title"] for r in db.query("SELECT title FROM jobs WHERE source = 'acme'")} == {
        "Software Engineer, New Grad"
    }
    screened = {
        r["title"]: r for r in db.query("SELECT * FROM listings WHERE source = 'acme' AND NOT kept")
    }
    assert set(screened) == {
        "Software Engineer",
        "Senior Software Engineer",
        "Quantitative Researcher",
    }
    assert screened["Software Engineer"]["pattern"] == "new grad"
    assert screened["Software Engineer"]["company"] == "Acme"

    # The board drops one; a later pull refreshes the rest and the dropped
    # one lingers only for the retention window.
    db.execute(
        "UPDATE listings SET last_seen_at = now() - interval '40 days' "
        "WHERE title = 'Quantitative Researcher'"
    )
    db.execute("UPDATE app_config SET value = '30' WHERE key = 'screened_retention_days'")
    _ingest(monkeypatch, f, LISTED[:3])
    assert {r["title"] for r in db.query("SELECT title FROM listings")} == {
        "Software Engineer, New Grad",
        "Software Engineer",
        "Senior Software Engineer",
    }

    # A wider pattern admits a screened posting on the next pull, no backfill.
    db.execute("UPDATE sources SET title_pattern = 'engineer' WHERE name = 'acme'")
    _ingest(monkeypatch, f, LISTED[:3])
    assert {r["title"] for r in db.query("SELECT title FROM jobs WHERE source = 'acme'")} == {
        "Software Engineer, New Grad",
        "Software Engineer",
        "Senior Software Engineer",
    }


def test_a_candidate_pattern_is_judged_against_everything_the_board_listed(
    monkeypatch, f, client, admin_headers
):
    f.make_source("acme")
    db.execute("UPDATE sources SET title_pattern = 'new grad' WHERE name = 'acme'")
    _ingest(monkeypatch, f, LISTED)

    r = client.post(
        "/v1/admin/sources/acme/pattern-preview",
        json={"title_pattern": r"^(?!.*\bsenior\b).*engineer", "samples": 10},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["titles"], body["admitted"], body["excluded"]) == (4, 2, 2)
    # Widening by one screened title, dropping nothing the live pattern kept.
    assert (body["would_add"], body["would_drop"]) == (1, 0)
    assert body["samples"]["admitted"] == ["Software Engineer", "Software Engineer, New Grad"]
    assert "Senior Software Engineer" in body["samples"]["excluded"]

    # A narrowing candidate says what it would cost.
    r = client.post(
        "/v1/admin/sources/acme/pattern-preview",
        json={"title_pattern": "quant"},
        headers=admin_headers,
    )
    assert (r.json()["would_add"], r.json()["would_drop"]) == (1, 1)

    r = client.post(
        "/v1/admin/sources/acme/pattern-preview", json={"title_pattern": "("}, headers=admin_headers
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "BAD_TITLE_PATTERN"
    assert (
        client.post(
            "/v1/admin/sources/nope/pattern-preview",
            json={"title_pattern": "x"},
            headers=admin_headers,
        ).status_code
        == 404
    )

    page = client.get(
        "/v1/admin/sources/acme/screened", params={"limit": 2}, headers=admin_headers
    ).json()
    assert page["total"] == 3 and len(page["rows"]) == 2 and page["has_more"] is True
    assert all(row["pattern"] == "new grad" for row in page["rows"])


def test_every_listing_is_stored_with_its_text_and_the_text_becomes_the_content(monkeypatch, f):
    """Kept or not, the listing is on record with the text the board carried
    and the raw record; ingest stores that text as the posting's content
    instead of fetching the page, which is the request that gets blocked."""
    f.make_source("acme")
    db.execute("UPDATE sources SET title_pattern = 'new grad' WHERE name = 'acme'")
    kept = dataclasses.replace(
        _posting("Software Engineer, New Grad", "https://boards.greenhouse.io/acme/jobs/1"),
        description="Software Engineer, New Grad\n\nAustin, TX\n\nYou will build things.",
        raw={"id": 1, "departments": [{"name": "Eng"}]},
    )
    dropped = dataclasses.replace(
        _posting("Senior Software Engineer", "https://boards.greenhouse.io/acme/jobs/3"),
        description="Senior text",
    )
    fetched: list[str] = []

    async def no_fetch(url, **kw):
        fetched.append(url)
        return None, None

    monkeypatch.setattr(boards, "fetch_listings", lambda url, company=None: [kept, dropped])
    monkeypatch.setattr(verdicts, "refresh_content", no_fetch)
    task_id = f.make_task("ingest_source", {"source": "acme"}, status="running")
    asyncio.run(ingest.handle_ingest_source(task_id, {"source": "acme"}))

    rows = {r["url"]: r for r in db.query("SELECT * FROM listings WHERE source = 'acme'")}
    assert rows[kept.url]["kept"] is True and rows[dropped.url]["kept"] is False
    assert rows[kept.url]["description"] == kept.description
    assert rows[kept.url]["raw"] == kept.raw
    assert rows[dropped.url]["description"] == "Senior text"
    # The kept posting's content came from the listing, not a page fetch.
    assert fetched == []
    content = db.query_one(
        "SELECT input_content, reason FROM ai_queries WHERE url = %s AND check_type = 'content'",
        (kept.url,),
    )
    assert content is not None and content["input_content"] == kept.description
    assert content["reason"] == "listing text"


def test_retention_is_admin_config(client, admin_headers):
    cfg = client.get("/v1/admin/config", headers=admin_headers).json()["config"]
    assert cfg["screened_retention_days"] == 30
    r = client.put(
        "/v1/admin/config/screened_retention_days", json={"value": 0}, headers=admin_headers
    )
    assert r.status_code == 400
