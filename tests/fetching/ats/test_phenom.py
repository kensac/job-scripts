"""core.fetching.ats: the Phenom resolver, over jobDetail replies copied from
the live endpoint on 2026-11-01 with each description cut short."""

from __future__ import annotations

import datetime

import pytest

from api import db
from api.ai import verdicts
from core.fetching import ats
from tests.fetching.ats.responses import AtsResponse

MERCK_LISTING = "https://jobs.merck.com/us/en/search-results"
MERCK_URL = "https://jobs.merck.com/us/en/job/P-101024/Director-Operational-Technology-Security"

# jobs.merck.com/widgets {"ddoKey": "jobDetail", "jobId": "P-101024"}
MERCK_DETAIL = {
    "jobDetail": {
        "status": 200,
        "hits": 1,
        "totalHits": 1,
        "data": {
            "job": {
                "jobId": "P-101024",
                "title": "Director, Operational Technology Security Strategy & Execution "
                "(Hiring Our Heroes Corporate Fellow)",
                "postedDate": "2026-06-16T00:00:00.000+0000",
                "location": "Rahway, New Jersey, United States",
                "multi_location": [
                    {
                        "city": "Rahway",
                        "state": "New Jersey",
                        "country": "United States",
                        "cityStateCountry": "Rahway, New Jersey, United States",
                        "location": "Rahway, NJ, USA",
                    },
                    {
                        "city": "West Point",
                        "state": "Pennsylvania",
                        "country": "United States",
                        "cityStateCountry": "West Point, Pennsylvania, United States",
                        "location": "West Point, PA, USA",
                    },
                ],
                "description": '<strong>Job Requirements </strong><br><p><span style="color: '
                'rgb(0, 0, 0);">We are excited to be a sponsor of Hiring our Heroes.</span></p>',
            }
        },
    }
}
# The same widget for an id the site does not have: 200, and no job.
NO_JOB = {"jobDetail": {"status": 200, "hits": 0, "totalHits": 0, "data": {}}}


def _serve(monkeypatch, body, status_code=200):
    asked: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ats.PHENOM,
        "post",
        lambda url, json: asked.append((url, json)) or AtsResponse(body, status_code),
    )
    return asked


def test_a_phenom_posting_is_read_from_its_sites_job_detail(monkeypatch):
    asked = _serve(monkeypatch, MERCK_DETAIL)
    assert ats.listing_prefixes(MERCK_URL) == (MERCK_LISTING, MERCK_LISTING)
    result = ats.resolve_listed(MERCK_URL, MERCK_LISTING)
    assert asked == [
        (
            "https://jobs.merck.com/widgets",
            {"ddoKey": "jobDetail", "jobId": "P-101024", "lang": "en_us", "country": "us"},
        )
    ]
    assert result.ok and result.source == "phenom"
    assert result.text == (
        "Director, Operational Technology Security Strategy & Execution (Hiring Our Heroes "
        "Corporate Fellow)\n\nRahway, New Jersey, United States; West Point, Pennsylvania, "
        "United States\n\nJob Requirements \nWe are excited to be a sponsor of Hiring our Heroes."
    )
    assert result.posted == datetime.date(2026, 6, 16)


@pytest.mark.parametrize("body, status", [(NO_JOB, 200), (None, 404), (None, 503)])
def test_phenom_never_reports_a_closure(monkeypatch, body, status):
    # A made-up id, and the endpoint moving or failing, all look alike.
    _serve(monkeypatch, body, status)
    assert ats.resolve_listed(MERCK_URL, MERCK_LISTING).status is ats.Status.ERROR


@pytest.mark.parametrize(
    "url, listing",
    [
        # A listing on another host says nothing about this one.
        ("https://careers.example.com/us/en/job/P-101024", MERCK_LISTING),
        # Another language of the same site is another listing.
        ("https://jobs.merck.com/ca/fr/job/P-101024", MERCK_LISTING),
    ],
)
def test_the_resolver_never_guesses_a_host_is_phenom(monkeypatch, url, listing):
    asked = _serve(monkeypatch, MERCK_DETAIL)
    assert ats.resolve_listed(url, listing) is ats.UNSUPPORTED
    # Without a source, the shape of the URL is not evidence either.
    assert ats.resolve(url) is ats.UNSUPPORTED
    assert asked == []


def test_a_posting_shaped_like_neither_needs_no_source():
    assert ats.listing_prefixes("https://jobs.merck.com/us/en/search-results") is None
    assert ats.listing_prefixes("http://jobs.merck.com/us/en/job/P-101024") is None
    assert not ats.is_ats_email_domain("jobs.merck.com")


@pytest.mark.asyncio
async def test_an_unanswered_phenom_posting_skips_the_static_shell(f, monkeypatch):
    source = f.make_source("merck_phenom")
    db.execute("UPDATE sources SET listings_url = %s WHERE name = %s", (MERCK_LISTING, source))
    db.execute("UPDATE app_config SET value = '\"static_first\"' WHERE key = 'fetch_engine'")
    f.make_job(url=MERCK_URL, source=source)
    asked = _serve(monkeypatch, NO_JOB)
    fetched = []

    async def static(url, min_chars):
        raise AssertionError("the static tier returns the site's menu, not the posting")

    async def page(url):
        fetched.append(url)
        return None, False

    monkeypatch.setattr("api.fetching.fetch_static", static)
    monkeypatch.setattr("api.fetching.fetch_page", page)
    await verdicts.refresh_content(MERCK_URL)
    assert [url for url, _ in asked] == ["https://jobs.merck.com/widgets"]
    assert fetched == [MERCK_URL]
