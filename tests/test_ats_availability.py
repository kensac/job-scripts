"""Availability signals retained by ATS APIs after a public posting disappears."""

import pytest

from api import db
from api.ai import verdicts
from core.fetching import ats


class Response:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def json(self):
        return self.body


def test_explicit_greenhouse_closure_cannot_be_overwritten_by_hostname_guess(monkeypatch):
    resolver = ats.Greenhouse()
    asked = []

    def get(url):
        asked.append(url)
        return Response({"status": 404, "error": "Job not found"}, 404)

    monkeypatch.setattr(resolver, "get", get)
    result = resolver.fetch("https://boards.greenhouse.io/nuro/jobs/8227399")
    assert result.status is ats.Status.GONE
    assert asked == ["https://boards-api.greenhouse.io/v1/boards/nuro/jobs/8227399?content=true"]


@pytest.mark.parametrize("availability", [{"active": False}, {"visibility": "INTERNAL"}])
def test_smartrecruiters_retained_description_is_not_public_availability(monkeypatch, availability):
    resolver = ats.SmartRecruiters()
    monkeypatch.setattr(
        resolver,
        "get",
        lambda url: Response(
            {
                "name": "Software Engineer",
                "jobAd": {"sections": {"jobDescription": {"text": "Build software."}}},
                **availability,
            }
        ),
    )
    assert (
        resolver.fetch("https://jobs.smartrecruiters.com/LinkedIn3/744000151447279").status
        is ats.Status.GONE
    )


@pytest.mark.asyncio
async def test_embedded_greenhouse_uses_configured_source_not_host_guess(f, monkeypatch):
    source = f.make_source("embedded")
    db.execute(
        "UPDATE sources SET listings_url = %s WHERE name = %s",
        ("https://boards-api.greenhouse.io/v1/boards/actualcompany/jobs", source),
    )
    url = "https://custom.example/careersitem?gh_jid=8227399"
    f.make_job(url=url, source=source)
    asked = []

    def resolve(target):
        asked.append(target)
        return (
            ats.AtsResult(ats.Status.GONE)
            if target == "https://boards.greenhouse.io/actualcompany/jobs/8227399"
            else ats.UNSUPPORTED
        )

    async def no_page(url):
        return None, False

    monkeypatch.setattr(ats, "resolve", resolve)
    monkeypatch.setattr("api.fetching.fetch_page", no_page)
    content, signal = await verdicts.refresh_content(url)
    assert (content, signal) == (None, "ats_gone")
    assert asked == ["https://boards.greenhouse.io/actualcompany/jobs/8227399"]
    row = db.query_one(
        "SELECT status FROM ai_queries WHERE url = %s AND check_type = 'closed'", (url,)
    )
    assert row["status"] == "rejected"
