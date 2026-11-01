"""JazzHR board behavior over the export feed Space Dynamics Laboratory served
on 2026-11-01, trimmed to one posting and a shortened description."""

from __future__ import annotations

import pytest
import requests

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://app.jazz.co/feeds/export/jobs/spacedynamicslaboratory"

_JOB = b"""<job>
			<id><![CDATA[job_20261014181530_PZZPSW6J1EU3GDIE]]></id>
			<internalcode><![CDATA[9802]]></internalcode>
			<status><![CDATA[Open]]></status>
			<title><![CDATA[GNC Controls Engineer]]></title>
			<department><![CDATA[Strategic Military Space]]></department>
			<url><![CDATA[https://spacedynamicslaboratory.applytojob.com/apply/RbPzFtYi1v/GNC-Controls-Engineer]]></url>
			<city><![CDATA[North Logan]]></city>
			<state><![CDATA[UT]]></state>
			<country><![CDATA[United States]]></country>
			<postalcode><![CDATA[84341]]></postalcode>
			<description><![CDATA[<p><b>Job ID: 9802</b><br>We are seeking a GNC Controls Engineer to join our Strategic &amp; Military Space Division.</p>]]></description>
			<type><![CDATA[Full Time]]></type>
			<experience><![CDATA[Mid Level]]></experience>
			<buttons><![CDATA[<script type="text/javascript" src="//app.jazz.co/widgets/buttons/create/spacedynamicslaboratory/RbPzFtYi1v"></script>]]></buttons>
		</job>"""


def _feed(*jobs: bytes) -> bytes:
    return (
        b'<?xml version="1.0" encoding="utf-8"?>\n<jobs>\n\t<publisher>JazzHR</publisher>\n'
        b"\t<publisherurl>http://app.jazz.co</publisherurl>\n"
        b"\t<lastBuildDate>Sun, 1 Nov 2026 17:05:13 GMT</lastBuildDate>\n"
        b"\t<company><![CDATA[Space Dynamics Laboratory]]></company>\n"
        + b"".join(jobs)
        + b"</jobs>"
    )


def _serve(monkeypatch, response):
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or response)
    return asked


def test_jazzhr_reads_every_posting_with_its_text_in_one_unpaged_call(monkeypatch):
    asked = _serve(monkeypatch, BoardResponse(_feed(_JOB, _JOB.replace(b"RbPzFtYi1v", b"x2"))))
    first, second = boards.fetch_listings(_URL, "Space Dynamics Laboratory")
    assert asked == [_URL]
    assert boards.kind(_URL) == "jazzhr"
    assert first.company == "Space Dynamics Laboratory"
    assert first.title == "GNC Controls Engineer"
    assert first.url == (
        "https://spacedynamicslaboratory.applytojob.com/apply/RbPzFtYi1v/GNC-Controls-Engineer"
    )
    assert second.url.endswith("/apply/x2/GNC-Controls-Engineer")
    assert first.locations == ["North Logan, UT, United States"]
    assert first.date_posted == 0
    assert first.description == (
        "GNC Controls Engineer\n\nNorth Logan, UT, United States\n\nJob ID: 9802\n"
        "We are seeking a GNC Controls Engineer to join our Strategic & Military Space Division."
    )
    assert first.raw is not None
    assert first.raw["internalcode"] == "9802"
    assert "description" not in first.raw and "buttons" not in first.raw


def test_jazzhr_board_with_no_openings_is_empty(monkeypatch):
    _serve(monkeypatch, BoardResponse(_feed()))
    assert boards.fetch_listings(_URL, "Space Dynamics Laboratory") == []


def test_jazzhr_page_that_is_not_the_feed_fails_the_pull(monkeypatch):
    _serve(monkeypatch, BoardResponse(b"<html><body>404 Page Not Found</body></html>"))
    with pytest.raises(ValueError):
        boards.fetch_listings(_URL, "Space Dynamics Laboratory")


def test_jazzhr_truncated_feed_fails_the_pull(monkeypatch):
    whole = _feed(_JOB)
    _serve(monkeypatch, BoardResponse(whole[: len(whole) // 2]))
    with pytest.raises(SyntaxError):
        boards.fetch_listings(_URL, "Space Dynamics Laboratory")


def test_jazzhr_unknown_subdomain_fails_the_pull(monkeypatch):
    class Missing(BoardResponse):
        def raise_for_status(self):
            raise requests.HTTPError("404")

    _serve(monkeypatch, Missing(b""))
    with pytest.raises(requests.HTTPError):
        boards.fetch_listings(_URL, "Space Dynamics Laboratory")
