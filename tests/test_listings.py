"""Everything a board lists is kept, with the text it carried, so a candidate
pattern is judged against it before it goes live and no page is scraped for
text the board already handed over."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib

from api import db
from api.ai import verdicts
from core.fetching import boards
from core.fetching.posting import JobPosting
from core.listing_payloads import resolve
from core.payload_objects import PayloadStore, PayloadUnavailable
from tasks import ingest

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
        r["title"]: r
        for r in db.query(
            "SELECT l.*, t.pattern FROM listings l JOIN title_patterns t ON t.id = l.pattern_id "
            "WHERE l.source = 'acme' AND NOT l.kept"
        )
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


def test_the_length_limit_binds_a_new_pattern_not_the_stored_one(f, client, admin_headers):
    """2,483 sources held one 583-character pattern when the limit was 500. An
    edit that leaves it as it is, and a preview of it, are accepted; a new
    pattern that long is not."""
    stored = "|".join(f"title{i:03d}" for i in range(60))
    assert len(stored) > 500
    f.make_source("acme")
    db.execute("UPDATE sources SET title_pattern = %s WHERE name = 'acme'", (stored,))

    def patch(body: dict):
        return client.patch("/v1/admin/sources/acme", json=body, headers=admin_headers)

    assert patch({"description": "x"}).status_code == 200
    assert patch({"title_pattern": stored, "description": "y"}).status_code == 200
    r = client.post(
        "/v1/admin/sources/acme/pattern-preview",
        json={"title_pattern": stored},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    longer = stored + "|title999"
    for r in (
        patch({"title_pattern": longer}),
        client.post(
            "/v1/admin/sources/acme/pattern-preview",
            json={"title_pattern": longer},
            headers=admin_headers,
        ),
        client.post(
            "/v1/admin/sources",
            json={"name": "b", "listings_url": "https://b.test/jobs.json", "title_pattern": longer},
            headers=admin_headers,
        ),
    ):
        assert r.status_code == 400 and r.json()["detail"]["code"] == "TITLE_PATTERN_TOO_LONG"
    assert db.query_one("SELECT title_pattern FROM sources WHERE name = 'acme'") == {
        "title_pattern": stored
    }


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

    rows = {r["url"]: r for r in resolve(db.query("SELECT * FROM listings WHERE source = 'acme'"))}
    assert rows[kept.url]["kept"] is True and rows[dropped.url]["kept"] is False
    assert rows[kept.url]["description"] == kept.description
    assert rows[kept.url]["raw"] == kept.raw
    assert rows[dropped.url]["description"] == "Senior text"
    # The kept posting's content came from the listing, not a page fetch.
    assert fetched == []
    content = db.query_one(
        "SELECT content AS input_content, method AS reason FROM page_fetches WHERE url = %s",
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


# Digests do not compress, so these are stored out of line in TOAST: the case
# where rewriting an unchanged value costs the most.
LONG_TEXT = "".join(hashlib.sha256(b"text%d" % i).hexdigest() for i in range(150))
LONG_RAW = {"id": 7, "metadata": [hashlib.sha256(b"raw%d" % i).hexdigest() for i in range(150)]}


def _versions() -> dict[str, tuple[str, str]]:
    """Each row's physical version. A new xmin means the row was rewritten,
    whatever the values in it say."""
    return {
        r["url"]: (r["xmin"], r["ctid"])
        for r in db.query("SELECT url, xmin::text AS xmin, ctid::text AS ctid FROM listings")
    }


def _toast_ids(url: str) -> tuple[int | None, int | None]:
    row = db.query_one(
        "SELECT pg_column_toast_chunk_id(description) AS d, pg_column_toast_chunk_id(raw) AS r "
        "FROM listings WHERE url = %s",
        (url,),
    )
    assert row is not None
    return row["d"], row["r"]


def _refuse(self, raw, key):
    raise PayloadUnavailable("storage down")


def _long(posting: JobPosting) -> JobPosting:
    return dataclasses.replace(posting, description=LONG_TEXT, raw=LONG_RAW)


def test_a_repull_of_identical_content_writes_no_new_row_version(monkeypatch, f):
    """At 1.18M upserts a day, 99.2% of which found the row already there,
    rewriting every unchanged row was 9 GB of WAL a day. A re-pull that
    carries what the row already holds must leave it physically untouched.
    The second pull dates the postings differently, as Workday's "Posted 3
    Days Ago" does a few hours later: the stored date is the first one seen,
    so a moving date is not a change to the row."""
    f.make_source("acme")
    listed = [_long(LISTED[0]), *LISTED[1:]]
    _ingest(monkeypatch, f, listed)
    before = _versions()
    assert len(before) == 4

    redated = [dataclasses.replace(p, date_posted=TODAY + 3 * 3600) for p in listed]
    _ingest(monkeypatch, f, redated)
    # An empty description is "the listing did not carry it" and keeps the text.
    _ingest(monkeypatch, f, [dataclasses.replace(listed[0], description=""), *listed[1:]])

    assert _versions() == before


def test_a_changed_field_rewrites_its_row_and_only_its_row(monkeypatch, f):
    f.make_source("acme")
    _ingest(monkeypatch, f, LISTED)
    before = _versions()

    moved = dataclasses.replace(LISTED[1], locations=["Remote"])
    retitled = dataclasses.replace(LISTED[2], title="Staff Software Engineer")
    _ingest(monkeypatch, f, [LISTED[0], moved, retitled, LISTED[3]])

    after = _versions()
    assert {url for url in before if after[url] != before[url]} == {moved.url, retitled.url}
    rows = {r["url"]: r for r in db.query("SELECT url, title, locations FROM listings")}
    assert rows[moved.url]["locations"] == ["Remote"]
    assert rows[retitled.url]["title"] == "Staff Software Engineer"


def test_a_pattern_is_stored_once_and_a_row_without_its_pointer_is_a_change(monkeypatch, f):
    """Listings point at one stored copy of their source's pattern. A row
    written before pattern_id existed differs from what a pull would write,
    so the next pull that lists it rewrites it with the pointer, while the
    rows that already have it stay untouched."""
    f.make_source("acme")
    db.execute("UPDATE sources SET title_pattern = 'new grad' WHERE name = 'acme'")
    _ingest(monkeypatch, f, LISTED)
    _ingest(monkeypatch, f, LISTED)
    assert db.query("SELECT pattern FROM title_patterns") == [{"pattern": "new grad"}]
    stored = db.query_one("SELECT id FROM title_patterns")
    assert stored is not None
    assert {r["pattern_id"] for r in db.query("SELECT pattern_id FROM listings")} == {stored["id"]}

    db.execute("UPDATE listings SET pattern_id = NULL WHERE url = %s", (LISTED[1].url,))
    before = _versions()
    _ingest(monkeypatch, f, LISTED)
    after = _versions()
    assert {url for url in before if after[url] != before[url]} == {LISTED[1].url}
    assert db.query_one("SELECT pattern_id FROM listings WHERE url = %s", (LISTED[1].url,)) == {
        "pattern_id": stored["id"]
    }

    # A new pattern is a second row, and each listing points at the one that judged it.
    db.execute("UPDATE sources SET title_pattern = 'engineer' WHERE name = 'acme'")
    _ingest(monkeypatch, f, LISTED)
    assert {r["pattern"] for r in db.query("SELECT pattern FROM title_patterns")} == {
        "new grad",
        "engineer",
    }
    assert {
        r["pattern"]
        for r in db.query(
            "SELECT t.pattern FROM listings l JOIN title_patterns t ON t.id = l.pattern_id"
        )
    } == {"engineer"}


def test_an_update_keeps_long_text_it_did_not_change_where_it_already_is(monkeypatch, f):
    """Postgres reuses a TOASTed value only when the new row carries the old
    row's own pointer. A value arriving through EXCLUDED is a fresh copy and
    is written out again in full, chunk by chunk, even when identical. Held
    inline, which is what a pull writes while object storage is down."""
    monkeypatch.setattr(PayloadStore, "_put", _refuse)
    f.make_source("acme")
    posting = _long(LISTED[0])
    _ingest(monkeypatch, f, [posting])
    description_chunk, raw_chunk = _toast_ids(posting.url)
    assert description_chunk is not None and raw_chunk is not None

    _ingest(monkeypatch, f, [dataclasses.replace(posting, title="Software Engineer I")])

    row = db.query_one(
        "SELECT title, description, raw FROM listings WHERE url = %s", (posting.url,)
    )
    assert row is not None and row["title"] == "Software Engineer I"
    assert (row["description"], row["raw"]) == (LONG_TEXT, LONG_RAW)
    assert _toast_ids(posting.url) == (description_chunk, raw_chunk)

    # A changed text is written; the unchanged raw beside it still is not.
    _ingest(monkeypatch, f, [dataclasses.replace(posting, description=LONG_TEXT + " Apply now.")])
    new_description_chunk, new_raw_chunk = _toast_ids(posting.url)
    assert new_description_chunk != description_chunk
    assert new_raw_chunk == raw_chunk


def test_last_seen_refreshes_once_the_refresh_interval_has_passed(monkeypatch, f):
    f.make_source("acme")
    db.execute("UPDATE app_config SET value = '24' WHERE key = 'listings_seen_refresh_hours'")
    _ingest(monkeypatch, f, LISTED[:2])
    stale, fresh = LISTED[0].url, LISTED[1].url
    db.execute(
        "UPDATE listings SET last_seen_at = now() - interval '25 hours' WHERE url = %s", (stale,)
    )
    db.execute(
        "UPDATE listings SET last_seen_at = now() - interval '23 hours' WHERE url = %s", (fresh,)
    )
    before = _versions()

    _ingest(monkeypatch, f, LISTED[:2])

    age = {
        r["url"]: r["age"]
        for r in db.query("SELECT url, now() - last_seen_at AS age FROM listings")
    }
    assert age[stale] < datetime.timedelta(minutes=1)
    assert age[fresh] > datetime.timedelta(hours=22)
    assert _versions()[fresh] == before[fresh]


def test_retention_deletes_what_the_board_stopped_listing_and_never_earlier(monkeypatch, f):
    """A row's last_seen_at may lag the last pull that listed it by up to the
    refresh interval, so the age-out counts from that lag's far end: a row is
    held at least screened_retention_days after the board last listed it, as
    before, and at most the refresh interval longer. A row still listed is
    never deleted, however old its last_seen_at, because the pull refreshes
    it before the delete runs."""
    f.make_source("acme")
    db.execute("UPDATE app_config SET value = '30' WHERE key = 'screened_retention_days'")
    db.execute("UPDATE app_config SET value = '24' WHERE key = 'listings_seen_refresh_hours'")
    _ingest(monkeypatch, f, LISTED)
    still_listed, gone, lagging = LISTED[0].url, LISTED[1].url, LISTED[2].url
    for url, age in (
        (still_listed, "40 days"),
        (gone, "31 days 1 hour"),
        (lagging, "30 days 1 hour"),
    ):
        db.execute(
            "UPDATE listings SET last_seen_at = now() - %s::interval WHERE url = %s", (age, url)
        )

    _ingest(monkeypatch, f, [LISTED[0], LISTED[3]])

    assert {r["url"] for r in db.query("SELECT url FROM listings")} == {
        still_listed,
        lagging,
        LISTED[3].url,
    }


def test_the_refresh_interval_is_admin_config(client, admin_headers):
    cfg = client.get("/v1/admin/config", headers=admin_headers).json()["config"]
    assert cfg["listings_seen_refresh_hours"] == 24
    r = client.put(
        "/v1/admin/config/listings_seen_refresh_hours", json={"value": 0}, headers=admin_headers
    )
    assert r.status_code == 400
