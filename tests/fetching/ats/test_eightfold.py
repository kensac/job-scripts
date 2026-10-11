"""core.fetching.ats: the Eightfold resolver, over detail bodies copied from the
live endpoints on 2026-10-05 with each description cut short."""

from __future__ import annotations

import datetime

import pytest

from api import db
from api.ai import verdicts
from core.fetching import ats

NGC_LISTING = "https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com"
NGC_URL = "https://jobs.northropgrumman.com/careers/job/1340074245409"

# jobs.northropgrumman.com/api/pcsx/position_details?position_id=1340074245409
NGC_DETAIL = {
    "status": 200,
    "error": {"message": "", "body": ""},
    "data": {
        "id": 1340074245409,
        "displayJobId": "R10250452",
        "name": "Thermal Engineer Level 3 /4 (AHT)",
        "locations": ["United States-California-Woodland Hills"],
        "standardizedLocations": ["Los Angeles, CA, US"],
        "postedTs": 1790899200,
        "creationTs": 1788998400,
        "location": "United States-California-Woodland Hills",
        "positionUrl": "/careers/job/1340074245409",
        "publicUrl": "https://jobs.northropgrumman.com/careers/job/1340074245409",
        "jobDescription": 'CLEARANCE REQUIRED FOR START:  No<p style="text-align:inherit"></p>'
        'CLEARANCE TYPE:  Secret<p style="text-align:inherit"></p>'
        "<h2><b>Description</b></h2><p>Design thermal systems.</p>",
    },
}
# The same endpoint for a position it does not have, HTTP 404.
NGC_GONE = {"status": 404, "error": {"message": "Position not found"}, "data": {}, "metadata": None}

NFLX_LISTING = "https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com"
NFLX_URL = "https://explore.jobs.netflix.net/careers/job/790298014263"
# explore.jobs.netflix.net/api/apply/v2/jobs/790298014263, the older generation.
NFLX_DETAIL = {
    "id": 790298014263,
    "name": "AI Engineer 6 - AI Foundation & Tooling, Ads Platform",
    "location": "Remote, United States",
    "locations": ["Remote, United States"],
    "t_create": 1721692800,
    "t_update": 1779148800,
    "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790298014263?microsite=netflix.com",
    "job_description": "<p>At Netflix, our mission is to entertain the world.</p>",
}


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def _serve(monkeypatch, body, status_code=200):
    asked: list[str] = []
    monkeypatch.setattr(
        ats.EIGHTFOLD, "get", lambda url: asked.append(url) or _Resp(body, status_code)
    )
    return asked


def test_a_pcsx_posting_is_read_from_its_detail_endpoint(monkeypatch):
    asked = _serve(monkeypatch, NGC_DETAIL)
    result = ats.resolve_listed(NGC_URL, NGC_LISTING)
    assert asked == [
        "https://jobs.northropgrumman.com/api/pcsx/position_details?position_id=1340074245409"
    ]
    assert result.ok and result.source == "eightfold"
    assert result.text is not None
    assert result.text.startswith(
        "Thermal Engineer Level 3 /4 (AHT)\n\nUnited States-California-Woodland Hills\n\n"
    )
    # The clearance line the checks need, which the page shell does not carry.
    assert "CLEARANCE TYPE:  Secret" in result.text
    assert "Design thermal systems." in result.text
    assert result.posted == datetime.date(2026, 10, 2)


def test_a_v2_posting_is_read_from_the_older_detail_endpoint(monkeypatch):
    asked = _serve(monkeypatch, NFLX_DETAIL)
    result = ats.resolve_listed(NFLX_URL, NFLX_LISTING)
    assert asked == ["https://explore.jobs.netflix.net/api/apply/v2/jobs/790298014263"]
    assert result.text == (
        "AI Engineer 6 - AI Foundation & Tooling, Ads Platform\n\nRemote, United States\n\n"
        "At Netflix, our mission is to entertain the world."
    )
    assert result.posted == datetime.date(2024, 7, 23)


@pytest.mark.parametrize(
    "url, listing, body",
    [
        (NGC_URL, NGC_LISTING, NGC_GONE),
        (
            "https://explore.jobs.netflix.net/careers/job/790000000001",
            NFLX_LISTING,
            {"message": "Job with ID 790000000001 not found"},
        ),
    ],
)
def test_a_position_the_tenant_no_longer_has_is_gone(monkeypatch, url, listing, body):
    _serve(monkeypatch, body, 404)
    assert ats.resolve_listed(url, listing).status is ats.Status.GONE


def test_a_waf_challenge_is_not_a_closure(monkeypatch):
    _serve(monkeypatch, None, 405)
    assert ats.resolve_listed(NGC_URL, NGC_LISTING).status is ats.Status.ERROR


@pytest.mark.parametrize(
    "url, listing",
    [
        # A listing on another host says nothing about this one.
        ("https://careers.example.com/careers/job/1340074245409", NGC_LISTING),
        # Bayer lists on bayer.eightfold.ai and posts on talent.bayer.com.
        (
            "https://talent.bayer.com/careers/job/562949978488244",
            "https://bayer.eightfold.ai/api/apply/v2/jobs?domain=bayer.com",
        ),
        # A listing on the same host in another format.
        (NGC_URL, "https://jobs.northropgrumman.com/api/jobs"),
    ],
)
def test_the_resolver_never_guesses_a_host_is_eightfold(monkeypatch, url, listing):
    asked = _serve(monkeypatch, NGC_GONE, 404)
    assert ats.resolve_listed(url, listing) is ats.UNSUPPORTED
    # Without a source, the shape of the URL is not evidence either.
    assert ats.resolve(url) is ats.UNSUPPORTED
    assert asked == []


def test_eightfold_adds_no_mail_domain():
    assert not ats.is_ats_email_domain("jobs.northropgrumman.com")
    assert all(r.markers for r in ats.RESOLVERS)


@pytest.mark.asyncio
async def test_refresh_finds_the_tenant_by_a_source_on_the_posting_host(f, monkeypatch):
    source = f.make_source("ngc_eightfold")
    db.execute("UPDATE sources SET listings_url = %s WHERE name = %s", (NGC_LISTING, source))
    f.make_job(url=NGC_URL, source=source)
    _serve(monkeypatch, NGC_GONE, 404)

    async def no_page(url):
        raise AssertionError("a closure the tenant reported needs no page")

    monkeypatch.setattr("api.fetching.fetch_page", no_page)
    monkeypatch.setattr("api.fetching.fetch_static", no_page)
    assert await verdicts.refresh_content(NGC_URL) == (None, "ats_gone")


@pytest.mark.asyncio
async def test_an_unanswered_eightfold_posting_skips_the_static_shell(f, monkeypatch):
    source = f.make_source("ngc_eightfold")
    db.execute("UPDATE sources SET listings_url = %s WHERE name = %s", (NGC_LISTING, source))
    db.execute("UPDATE app_config SET value = '\"static_first\"' WHERE key = 'fetch_engine'")
    f.make_job(url=NGC_URL, source=source)
    _serve(monkeypatch, None, 405)
    fetched = []

    async def static(url, min_chars):
        raise AssertionError("the static tier returns the theme JSON, not the posting")

    async def page(url):
        fetched.append(url)
        return None, False

    monkeypatch.setattr("api.fetching.fetch_static", static)
    monkeypatch.setattr("api.fetching.fetch_page", page)
    await verdicts.refresh_content(NGC_URL)
    assert fetched == [NGC_URL]
