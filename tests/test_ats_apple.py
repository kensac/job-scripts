"""core.fetching.ats: the jobs.apple.com resolver, over jobDetails bodies copied
from the live API on 2026-10-05 and trimmed to the keys read."""

from __future__ import annotations

import datetime

from core.fetching import ats
from core.fetching.urls import normalize_url
from core.mail.prefilter import looks_job_related

URL = "https://jobs.apple.com/en-us/details/200686523-3916/softgoods-product-design-engineer"

_SUZHOU = {"name": "Suzhou", "countryName": "China", "postingIdentifier": "3916", "active": True}
_SHANGHAI = {
    "name": "Shanghai",
    "countryName": "China",
    "postingIdentifier": "3715",
    "active": True,
}

# 200686523-3916 as jobDetails returned it, with each text field cut short.
DETAIL = {
    "res": {
        "id": "REQ-200686523",
        "jobNumber": "200686523-3916",
        "positionId": "200686523",
        "managedPipelineRole": False,
        "postingTitle": "Softgoods Product Design Engineer",
        "jobSummary": "Why Apple? Apple is where individual imaginations gather together.",
        "description": "We are looking for a highly motivated product design engineer.",
        "responsibilities": "Develop designs rooted in strong mechanical engineering fundamentals.",
        "minimumQualifications": "BS/MS/Ph.D in Mechanical or Aerospace engineering.",
        "preferredQualifications": "Skilled in working with Siemens NX or a similar CAD package.",
        "postDateInGMT": "2026-10-04T21:13:16.835+00:00",
        "selectedLocation": _SUZHOU,
        "locations": [_SHANGHAI, _SUZHOU],
    }
}

# Every id the API does not know, and every requisition no longer posted.
SERVICE_ERROR = {"error": "jobsite.general.serviceError"}


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def _apple(monkeypatch, body, status_code=200):
    asked: list[str] = []
    resolver = next(r for r in ats.RESOLVERS if r.name == "apple")
    monkeypatch.setattr(resolver, "get", lambda url: asked.append(url) or _Resp(body, status_code))
    return asked


def test_apple_reads_every_section_of_the_posting_from_job_details(monkeypatch):
    asked = _apple(monkeypatch, DETAIL)
    out = ats.resolve(URL)
    assert asked == ["https://jobs.apple.com/api/v1/jobDetails/200686523-3916"]
    assert out.ok and out.source == "apple"
    assert out.text == (
        "Softgoods Product Design Engineer\n\nSuzhou, China\n\n"
        "Why Apple? Apple is where individual imaginations gather together.\n\n"
        "Description\n\nWe are looking for a highly motivated product design engineer.\n\n"
        "Responsibilities\n\nDevelop designs rooted in strong mechanical engineering fundamentals."
        "\n\nMinimum Qualifications\n\nBS/MS/Ph.D in Mechanical or Aerospace engineering."
        "\n\nPreferred Qualifications\n\n"
        "Skilled in working with Siemens NX or a similar CAD package."
    )
    assert out.posted == datetime.date(2026, 10, 4)


def test_a_requisition_no_longer_posted_is_gone(monkeypatch):
    """68 of 68 requisitions the board no longer listed answered 404 with this
    body on 2026-10-05; 66 of 66 it still listed answered 200."""
    _apple(monkeypatch, SERVICE_ERROR, 404)
    assert ats.resolve(URL).status is ats.Status.GONE


def test_any_other_failure_is_an_error_not_a_closure(monkeypatch):
    _apple(monkeypatch, SERVICE_ERROR, 436)
    assert ats.resolve(URL).status is ats.Status.ERROR


def test_an_unknown_location_names_every_place_of_the_requisition(monkeypatch):
    """An unknown suffix answers 200 with the bare requisition number and an
    arbitrary one of its places as selectedLocation (Shanghai for -9999)."""
    body = {"res": {**DETAIL["res"], "jobNumber": "200686523", "selectedLocation": _SHANGHAI}}
    asked = _apple(monkeypatch, body)
    out = ats.resolve(URL.replace("3916", "9999"))
    assert asked == ["https://jobs.apple.com/api/v1/jobDetails/200686523-9999"]
    assert out.ok and (out.text or "").split("\n\n")[1] == "Shanghai, China; Suzhou, China"


def test_a_managed_pipeline_role_has_no_date(monkeypatch):
    """A managed role's postDateInGMT is the time of the request."""
    body = {"res": {**DETAIL["res"], "managedPipelineRole": True}}
    _apple(monkeypatch, body)
    out = ats.resolve("https://jobs.apple.com/en-us/details/PIPE-200313970/in-business-expert")
    assert out.ok and out.posted is None


def test_apple_urls_collapse_onto_the_listing_fetchers_form():
    canonical = "https://jobs.apple.com/en-us/details/200313970/in-business-expert"
    assert normalize_url(canonical) == canonical
    for variant in (
        "https://jobs.apple.com/en-us/details/200313970/in-business-expert?team=APPST",
        "https://jobs.apple.com/en-gb/details/PIPE-200313970/in-business-expert/locationPicker",
    ):
        assert normalize_url(variant) == canonical
    assert normalize_url("https://jobs.apple.com/en-us/details/200600679") == (
        "https://jobs.apple.com/en-us/details/200600679"
    )
    assert ats.resolve("https://jobs.apple.com/en-us/search").status is ats.Status.UNSUPPORTED


def test_the_resolver_does_not_make_jobs_apple_com_an_ats_mail_domain():
    assert not ats.is_ats_email_domain("jobs.apple.com")
    verdict = looks_job_related(from_email="news@jobs.apple.com", subject="Hello", body="")
    assert not verdict.hit
