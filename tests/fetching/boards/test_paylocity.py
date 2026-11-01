"""Paylocity board behavior over a board page shaped like the live one on
2026-11-01 (Transcard Payments), its pageData trimmed to the keys read."""

from __future__ import annotations

import json

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://recruiting.paylocity.com/Recruiting/Jobs/All/acf634cc-bb17-43ff-950e-ad7dfb8b47de"


def _job(job_id, title, city, *, remote=False, internal=False):
    return {
        "JobId": job_id,
        "JobTitle": title,
        "LocationName": f"{city}, TN",
        "PublishedDate": "2026-09-03T08:36:27-05:00",
        "Description": "Who We Are:Payments, Orchestrated.Chattanooga-based Transcard offers",
        "IsInternal": internal,
        "JobLocation": {"City": city, "State": "TN", "Country": "USA"},
        "IsRemote": remote,
    }


def _page(jobs):
    data = {"Departments": ["All Departments"], "Jobs": jobs, "ModuleTitle": "Transcard"}
    return (
        "<html><head><script>var x = 1;</script></head><body>\n"
        f"  <script>\n    window.pageData = {json.dumps(data)};\n  </script>\n"
        "</body></html>"
    )


def _serve(monkeypatch, body):
    asked = []

    def get(url, **kw):
        asked.append(url)
        return BoardResponse(body)

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_paylocity_reads_every_public_posting_off_the_board_page(monkeypatch):
    jobs = [
        _job(4475626, "2027 - Marketing Intern", "Chattanooga"),
        _job(4476341, "2027 - Operations Intern", "Chattanooga", remote=True),
        _job(4476999, "Internal Transfer Only", "Chattanooga", internal=True),
    ]
    asked = _serve(monkeypatch, _page(jobs))
    out = boards.fetch_listings(_URL, "Transcard Payments")
    assert asked == [_URL]
    assert [p.title for p in out] == ["2027 - Marketing Intern", "2027 - Operations Intern"]
    assert out[0].url == "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4475626"
    assert out[0].locations == ["Chattanooga, TN"]
    assert out[1].locations == ["Chattanooga, TN", "Remote"]
    assert {p.company for p in out} == {"Transcard Payments"}
    assert out[0].date_posted > 0 and out[0].description == ""


def test_paylocity_empty_board_is_an_empty_list(monkeypatch):
    _serve(monkeypatch, _page([]))
    assert boards.fetch_listings(_URL, "Transcard Payments") == []


@pytest.mark.parametrize(
    "body",
    ["<html><title>Job Not Found</title></html>", '<script>window.pageData = {"Jobs": [{'],
)
def test_paylocity_page_without_its_data_fails_the_pull(monkeypatch, body):
    _serve(monkeypatch, body)
    with pytest.raises(ValueError):
        boards.fetch_listings(_URL, "Transcard Payments")
