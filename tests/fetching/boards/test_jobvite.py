"""Jobvite board behavior over Nutanix's careers page and XML feed as served on
2026-11-01, trimmed to the line read and one posting open in two places."""

from __future__ import annotations

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://jobs.jobvite.com/nutanix/jobs"
_FEED = "https://app.jobvite.com/CompanyJobs/Xml.aspx?c=qKr9VfwZ"
_PAGE = """<script>
    jvConfig = {
        companyEId: 'qKr9VfwZ',
    };
</script>"""

# The posting's own row, then the row for its second place, as the feed lists them.
_PARENT = b"""<job><id>oY4YAfwk</id><externalJobPostingId>oY4YAfwk-CKEqVfwt</externalJobPostingId><title>Head of Marketing, Canada</title><requisitionid>32901</requisitionid><category>Marketing</category><jobtype>Full-Time</jobtype><region>AMER</region><location>Ontario, Canada</location><date>10/30/2026</date><detail-url><![CDATA[http://app.jobvite.com/CompanyJobs/Job.aspx?c=qKr9VfwZ&j=oY4YAfwk]]></detail-url><apply-url><![CDATA[http://app.jobvite.com/CompanyJobs/Careers.aspx?c=qKr9VfwZ&j=oY4YAfwk&k=Apply]]></apply-url><description><![CDATA[<p><b>The Opportunity</b></p><p>Lead marketing for Canada.</p>]]></description><briefdescription></briefdescription><business_x0020_unit>S&amp;M</business_x0020_unit><hiring_x0020_team>Lopez Hernandez Adriana</hiring_x0020_team><workflow>Global</workflow><referral_x0020_bonus>Global</referral_x0020_bonus><agency_x0020_access></agency_x0020_access></job>"""
_CHILD = b"""<job><parentId>oY4YAfwk</parentId><id>oY4YAfwk-CJ6xVfw1</id><externalJobPostingId>oY4YAfwk-CJ6xVfw1</externalJobPostingId><title>Head of Marketing, Canada</title><requisitionid>32901</requisitionid><category>Marketing</category><jobtype>Full-Time</jobtype><region>AMER</region><location>Quebec, Canada</location><date>10/30/2026</date><detail-url><![CDATA[http://app.jobvite.com/CompanyJobs/Job.aspx?c=qKr9VfwZ&j=oY4YAfwk]]></detail-url><apply-url><![CDATA[http://app.jobvite.com/CompanyJobs/Careers.aspx?c=qKr9VfwZ&j=oY4YAfwk&k=Apply]]></apply-url><description><![CDATA[<p><b>The Opportunity</b></p><p>Lead marketing for Canada.</p>]]></description><briefdescription></briefdescription><business_x0020_unit>S&amp;M</business_x0020_unit><hiring_x0020_team>Lopez Hernandez Adriana</hiring_x0020_team><workflow>Global</workflow><referral_x0020_bonus>Global</referral_x0020_bonus><agency_x0020_access></agency_x0020_access></job>"""
_OTHER = b"""<job><id>oKzYAfwB</id><title>Senior Manager, Subscription Account Management</title><requisitionid>32905</requisitionid><location>Sydney, Australia</location><date>10/21/2026</date><description><![CDATA[<p>Hungry, Humble, Honest, with Heart.</p>]]></description></job>"""


def _feed(*jobs: bytes) -> bytes:
    return b'<?xml version="1.0" encoding="UTF-8"?><result>' + b"".join(jobs) + b"</result>"


def _serve(monkeypatch, page: str, feed: bytes):
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        return BoardResponse(page if url == _URL else feed)

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_jobvite_reads_the_feed_named_by_the_page_and_folds_places(monkeypatch):
    asked = _serve(monkeypatch, _PAGE, _feed(_PARENT, _CHILD, _OTHER))
    head, other = boards.fetch_listings(_URL, "Nutanix")
    assert asked == [_URL, _FEED]
    assert boards.kind(_URL) == "jobvite"
    # One posting per id, with every place its rows name.
    assert head.url == "https://jobs.jobvite.com/nutanix/job/oY4YAfwk"
    assert head.locations == ["Ontario, Canada", "Quebec, Canada"]
    assert head.company == "Nutanix"
    assert head.date_posted == 1793318400  # 2026-10-30 UTC
    assert head.description == (
        "Head of Marketing, Canada\n\nOntario, Canada; Quebec, Canada\n\n"
        "The Opportunity\nLead marketing for Canada."
    )
    # The hiring team and the referral plan are not the posting's, and not kept.
    assert head.raw == {
        "id": "oY4YAfwk",
        "requisitionid": "32901",
        "title": "Head of Marketing, Canada",
        "category": "Marketing",
        "jobtype": "Full-Time",
        "region": "AMER",
        "location": "Ontario, Canada",
        "date": "10/30/2026",
        "parentId": None,
    }
    assert other.url == "https://jobs.jobvite.com/nutanix/job/oKzYAfwB"


def test_jobvite_board_with_no_openings_is_empty(monkeypatch):
    _serve(monkeypatch, _PAGE, _feed())
    assert boards.fetch_listings(_URL, "Nutanix") == []


def test_jobvite_page_without_a_company_id_fails_the_pull(monkeypatch):
    # An unknown slug lands on Jobvite's job seeker support page.
    _serve(monkeypatch, "<html><title>Job Seeker Support</title></html>", _feed(_OTHER))
    with pytest.raises(ValueError, match="company id"):
        boards.fetch_listings(_URL, "Nutanix")


def test_jobvite_reply_that_is_not_the_feed_fails_the_pull(monkeypatch):
    _serve(monkeypatch, _PAGE, b"<html><body>Error</body></html>")
    with pytest.raises(ValueError, match="not a Jobvite feed"):
        boards.fetch_listings(_URL, "Nutanix")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://jobs.jobvite.com/nutanix/jobs", "jobvite"),
        ("https://jobs.jobvite.com/nutanix/job/oKzYAfwB", "sheet_era"),
        ("https://jobs.jobvite.com/metaphaseconsulting/apply", "sheet_era"),
    ],
)
def test_jobvite_listings_url_is_the_careers_page(url, expected):
    assert boards.kind(url) == expected
