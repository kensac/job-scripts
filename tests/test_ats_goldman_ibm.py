"""core.fetching.ats: the higher.gs.com and careers.ibm.com resolvers, over
replies copied from the live services on 2026-10-05 and trimmed to the keys
read."""

from __future__ import annotations

import datetime

from core.fetching import ats
from core.mail.prefilter import looks_job_related

GS_URL = "https://higher.gs.com/roles/182810"

# role(externalSourceId: "182810") as it answered, the description cut short.
GS_ROLE = {
    "data": {
        "role": {
            "jobTitle": "2027 | Americas | New York City Area | Executive Office, "
            "Sustainable Finance Group | Summer Analyst",
            "corporateTitle": "Summer Analyst",
            "status": "POSTED",
            "externalJobStatus": "POSTED",
            "lastPostedDate": "2026-10-02T18:48:53.034Z",
            "locations": [{"city": "New York", "state": "NY", "country": "United States"}],
            "compensation": {"minSalary": 80000.0, "maxSalary": 110000.0, "currency": "USD"},
            "descriptionHtml": "\n<p><b><u>About the program</u></b></p>\n<p>Our Summer "
            "Analyst Program is a nine to ten week summer internship.</p>",
        }
    }
}

# What 44 of 44 roles the board no longer listed answered, and a made-up id
# answers the same.
GS_ERROR = {
    "errors": [
        {
            "message": "An exception occurred when making a request to an external service.",
            "locations": [{"line": 1, "column": 36}],
            "path": ["role"],
            "extensions": {"classification": "INTERNAL_ERROR"},
        }
    ],
    "data": {"role": None},
}


class _Resp:
    def __init__(self, body=None, status_code=200, headers=None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._body


def _goldman(monkeypatch, body):
    asked: list[dict] = []

    def post(url, json, **kw):
        asked.append({"url": url, **json["variables"]})
        return _Resp(body)

    monkeypatch.setattr(ats._session, "post", post)
    return asked


def test_goldman_reads_the_role_as_the_listing_stores_it(monkeypatch):
    asked = _goldman(monkeypatch, GS_ROLE)
    out = ats.resolve(GS_URL + "?source=simplify")
    assert asked == [{"url": "https://api-higher.gs.com/gateway/api/v1/graphql", "id": "182810"}]
    assert out.ok and out.source == "goldman"
    # The same assembly the listing fetcher stores, so a re-check compares
    # like with like.
    assert out.text == ats.goldman_text(GS_ROLE["data"]["role"])
    assert (out.text or "").startswith(
        "2027 | Americas | New York City Area | Executive Office, Sustainable Finance Group | "
        "Summer Analyst\n\nNew York, NY, United States\n\nSummer Analyst\n\nUSD 80,000 - 110,000"
    )
    assert out.posted == datetime.date(2026, 10, 2)


def test_a_role_goldman_cannot_find_is_an_error_never_a_closure(monkeypatch):
    """The board gives a delisted role, a made-up id and its own outage one
    answer, so the page tiers and the board pull decide, not this."""
    _goldman(monkeypatch, GS_ERROR)
    assert ats.resolve(GS_URL).status is ats.Status.ERROR


def test_a_goldman_role_without_text_or_not_posted_offers_none(monkeypatch):
    role = GS_ROLE["data"]["role"]
    _goldman(monkeypatch, {"data": {"role": {**role, "descriptionHtml": None}}})
    assert ats.resolve(GS_URL).status is ats.Status.ERROR
    _goldman(monkeypatch, {"data": {"role": {**role, "externalJobStatus": "EXPIRED"}}})
    assert ats.resolve(GS_URL).status is ats.Status.ERROR


def _ibm(monkeypatch, status_code, location):
    asked: list[tuple[str, bool]] = []

    def get(url, **kw):
        asked.append((url, kw.get("allow_redirects", True)))
        return _Resp(status_code=status_code, headers={"location": location})

    monkeypatch.setattr(ats._session, "get", get)
    return asked


def test_an_ibm_posting_ibm_has_closed_is_gone(monkeypatch):
    """19 of 19 delisted ids, and the one the search index still held, went
    to /careers/Error; each opened in a browser read "this job is closed"."""
    asked = _ibm(monkeypatch, 301, "https://careers.ibm.com/en_US/careers/Error")
    out = ats.resolve("https://careers.ibm.com/careers/JobDetail?jobId=87273")
    assert out.status is ats.Status.GONE and out.source == "ibm"
    # The Avature host answers before the WAF challenge, and only if the
    # redirect is not followed into it.
    assert asked == [("https://ibmglobal.avature.net/en_US/careers/JobDetail?jobId=87273", False)]
    # The aggregators' form of the same posting.
    assert (
        ats.resolve("https://ibmglobal.avature.net/en_US/careers/JobDetail?jobId=87273").status
        is ats.Status.GONE
    )


def test_an_open_ibm_posting_leaves_its_text_to_the_browser(monkeypatch):
    asked = _ibm(monkeypatch, 301, "https://careers.ibm.com/en_US/careers/JobDetail?jobId=125179")
    out = ats.resolve("https://careers.ibm.com/careers/JobDetail?jobId=125179")
    assert out.status is ats.Status.UNSUPPORTED
    assert len(asked) == 1, "the redirect is asked for before the page tiers run"
    # A challenge or anything else is not a closure either.
    asked = _ibm(monkeypatch, 202, "")
    assert ats.resolve("https://careers.ibm.com/careers/JobDetail?jobId=125179").status is (
        ats.Status.UNSUPPORTED
    )
    assert len(asked) == 1
    # A careers page that names no posting asks nothing.
    asked = _ibm(monkeypatch, 301, "https://careers.ibm.com/en_US/careers/Error")
    assert ats.resolve("https://careers.ibm.com/careers/SearchJobs").status is (
        ats.Status.UNSUPPORTED
    )
    assert asked == []


def test_the_resolvers_add_no_ats_mail_domain():
    for domain in ("higher.gs.com", "gs.com", "careers.ibm.com", "ibm.com"):
        assert not ats.is_ats_email_domain(domain)
        assert not looks_job_related(from_email=f"news@{domain}", subject="Hello", body="").hit
