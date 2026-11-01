"""Teamtailor board behavior over feeds served on 2026-11-01: RoadSync's
jobs.rss (one item, description shortened) and Noda's, which was empty."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://roadsync.teamtailor.com/jobs.rss"

_HEAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations">
  <channel>
    <title>RoadSync</title>
    <description/>
    <link>https://roadsync.teamtailor.com/jobs</link>
"""
_ITEM = b"""<item>
      <title>Account Executive</title>
      <description>&lt;h3&gt;&lt;strong&gt;About us and why we&#8217;re hiring...&lt;/strong&gt;&lt;/h3&gt;&lt;p&gt;RoadSync is revolutionizing the logistics industry.&lt;/p&gt;</description>
      <pubDate>Mon, 05 Oct 2026 10:01:27 -0400</pubDate>
      <link>https://roadsync.teamtailor.com/jobs/8500045-account-executive</link>
      <remoteStatus>hybrid</remoteStatus>
      <guid>9adc744f-6922-4194-b4af-c24fc36d1b02</guid>
      <tt:locations>
        <tt:location>
          <tt:name>Atlanta, GA</tt:name>
          <tt:address/>
          <tt:zip>30334</tt:zip>
          <tt:city>Atlanta</tt:city>
          <tt:country>United States</tt:country>
        </tt:location>
      </tt:locations>
      <tt:department>Sales</tt:department>
      <tt:role>Account Executive</tt:role>
    </item>
"""
_TAIL = b"  </channel>\n</rss>\n"


def _serve(monkeypatch, body: bytes):
    asked: list[str] = []
    monkeypatch.setattr(
        boards._session, "get", lambda url, **kw: asked.append(url) or BoardResponse(body)
    )
    return asked


def test_teamtailor_reads_every_posting_with_its_text_in_one_call(monkeypatch):
    asked = _serve(monkeypatch, _HEAD + _ITEM + _TAIL)
    (p,) = boards.fetch_listings(_URL, "RoadSync")
    assert asked == [_URL]
    assert boards.kind(_URL) == "teamtailor"
    assert p.url == "https://roadsync.teamtailor.com/jobs/8500045-account-executive"
    assert p.locations == ["Atlanta, GA"]
    assert p.date_posted == int(
        datetime.datetime(2026, 10, 5, 14, 1, 27, tzinfo=datetime.UTC).timestamp()
    )
    assert p.description == (
        "Account Executive\n\nAtlanta, GA\n\nAbout us and why we're hiring...\n"
        "RoadSync is revolutionizing the logistics industry."
    )
    assert p.raw == {
        "guid": "9adc744f-6922-4194-b4af-c24fc36d1b02",
        "pubDate": "Mon, 05 Oct 2026 10:01:27 -0400",
        "remoteStatus": "hybrid",
        "department": "Sales",
        "locations": ["Atlanta, GA"],
    }


def test_teamtailor_board_with_no_openings_is_empty(monkeypatch):
    _serve(
        monkeypatch,
        b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations">
  <channel>
    <title>Noda</title>
    <description>Open positions at Noda</description>
    <link>https://noda-1720000970.teamtailor.com/jobs</link>
  </channel>
</rss>
""",
    )
    assert boards.fetch_listings("https://noda-1720000970.teamtailor.com/jobs.rss", "Noda") == []


def test_teamtailor_page_that_is_not_the_feed_fails_the_pull(monkeypatch):
    _serve(monkeypatch, b"<html><head><title>Teamtailor - 404</title></head></html>")
    with pytest.raises(ValueError):
        boards.fetch_listings(_URL, "RoadSync")


def test_teamtailor_careers_page_is_not_the_feed():
    assert boards.kind("https://roadsync.teamtailor.com/jobs") == "sheet_era"
