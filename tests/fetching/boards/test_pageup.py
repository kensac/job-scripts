"""PageUp board behavior over Mutual of Omaha's external careers feed as
served on 2026-11-01, trimmed to one item with a shortened description."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://careers.pageuppeople.com/823/cw/en-us/rss"

_HEAD = (
    b'<?xml version="1.0" encoding="utf-8"?><rss xmlns:a10="http://www.w3.org/2005/Atom" '
    b'version="2.0"><channel xmlns:job="http://pageuppeople.com/"><title /><link>'
    b"https://careers.pageuppeople.com/823/cw/en-us/listing/</link><description />"
    b'<a10:link rel="self" href="https://careers.pageuppeople.com/823/cw/en-us/latest_jobs.rss" />'
)
_ITEM = (
    b'<item><guid isPermaLink="true">https://careers.pageuppeople.com/823/cw/en-us/job/505149'
    b"</guid><link>https://careers.pageuppeople.com/823/cw/en-us/job/505149</link>"
    b"<title>Informatica System Engineer -Remote</title><description>Mutual of Omaha's "
    b"Enterprise Data Warehouse Platform and Services team is seeking an System Engineer"
    b"</description><pubDate>Fri, 30 Oct 2026 14:00:00 Z</pubDate>"
    b"<a10:updated>2026-10-30T14:00:00Z</a10:updated><job:category>Information Technology"
    b"</job:category><job:refNo>505149</job:refNo><job:description>&lt;p&gt;Mutual of Omaha's "
    b"Enterprise Data Warehouse Platform and Services team is seeking an Informatica System "
    b"Engineer.&lt;/p&gt;&lt;p&gt;You will administer enterprise data platforms.&lt;/p&gt;"
    b"</job:description><job:applyLink>https://secure.dc4.pageuppeople.com/apply/823/gateway/"
    b"default.aspx?c=apply&amp;lJobID=505149</job:applyLink><job:closingDate>Wed, 04 Nov 2026 "
    b"13:15:00 GMT</job:closingDate><job:location>Remote</job:location><job:workType>Full Time "
    b"Regular</job:workType></item>"
)
_TAIL = b"</channel></rss>"


def _serve(monkeypatch, body: bytes):
    asked: list[str] = []
    monkeypatch.setattr(
        boards._session, "get", lambda url, **kw: asked.append(url) or BoardResponse(body)
    )
    return asked


def test_pageup_reads_every_posting_with_its_whole_text_in_one_call(monkeypatch):
    asked = _serve(monkeypatch, _HEAD + _ITEM + _TAIL)
    (p,) = boards.fetch_listings(_URL, "Mutual of Omaha")
    assert asked == [_URL]
    assert boards.kind(_URL) == "pageup"
    assert p.url == "https://careers.pageuppeople.com/823/cw/en-us/job/505149"
    assert p.title == "Informatica System Engineer -Remote"
    assert p.locations == ["Remote"]
    assert p.date_posted == int(
        datetime.datetime(2026, 10, 30, 14, tzinfo=datetime.UTC).timestamp()
    )
    # job:description is the whole text; the plain description is a snippet.
    assert p.description == (
        "Informatica System Engineer -Remote\n\nRemote\n\nMutual of Omaha's Enterprise Data "
        "Warehouse Platform and Services team is seeking an Informatica System Engineer.\n"
        "You will administer enterprise data platforms."
    )
    assert p.raw is not None
    assert p.raw["refNo"] == "505149"
    assert "description" not in p.raw


def test_pageup_board_with_no_openings_is_empty(monkeypatch):
    _serve(monkeypatch, _HEAD + _TAIL)
    assert boards.fetch_listings(_URL, "Mutual of Omaha") == []


def test_pageup_page_that_is_not_the_feed_fails_the_pull(monkeypatch):
    _serve(monkeypatch, b"<html><body>Error</body></html>")
    with pytest.raises(ValueError):
        boards.fetch_listings(_URL, "Mutual of Omaha")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://careers.pageuppeople.com/823/cw/en-us/rss", "pageup"),
        # The internal careers site's feed, and the external site's listing page.
        ("https://careers.pageuppeople.com/823/ci/en-us/rss", "sheet_era"),
        ("https://careers.pageuppeople.com/823/cw/en-us/listing/", "sheet_era"),
    ],
)
def test_pageup_listings_url_is_the_external_feed(url, expected):
    assert boards.kind(url) == expected
