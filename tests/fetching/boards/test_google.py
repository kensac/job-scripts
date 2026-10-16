"""Google board behavior over the careers feed, and its posting urls."""

from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET

import pytest

from core.fetching import ats, boards
from core.fetching.forms import posting_urls
from core.fetching.urls import normalize_url
from tests.fetching.boards.responses import BoardResponse

FEED_URL = "https://www.google.com/about/careers/applications/jobs/feed.xml"
CANONICAL = "https://www.google.com/about/careers/applications/jobs/results/125959927041532614"

# Two records verbatim from the feed on 2026-10-16, descriptions trimmed.
FEED = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?><jobs><updated>2026-10-16T06:24:57.744306+00:00</updated><job><published>2026-09-23T19:40:22.127Z</published><jobid>83093570004624070</jobid><title>Research Engineer, Energy Grids, Sustainability, DeepMind</title><description>&lt;h3&gt;Minimum qualifications:&lt;/h3&gt;&lt;ul&gt;
&lt;li&gt;3 years of experience in reinforcement learning or optimal control.&lt;/li&gt;
&lt;/ul&gt;&lt;div&gt;&lt;h2&gt;Responsibilities&lt;/h2&gt;&lt;ul&gt;
&lt;li&gt;Solve sequential decision-making problems under uncertainty for power grids at scale.&lt;/li&gt;
&lt;/ul&gt;&lt;/div&gt;</description><url>https://careers.google.com/jobs/results/83093570004624070-research-engineer/</url><jobtype>FULL_TIME</jobtype><employer>DeepMind</employer><languageCode>en-US</languageCode><categories><category>SOFTWARE_ENGINEERING</category></categories><remote>onsite</remote><isRemote>No</isRemote><isHybrid>No</isHybrid><locations><location><city>London</city><state/><country>UK</country></location></locations></job><job><published>2026-10-08T08:39:07.629Z</published><jobid>125959927041532614</jobid><title>Group Product Manager, Frontier Model Protection, DeepMind</title><description>&lt;h3&gt;Minimum qualifications:&lt;/h3&gt;&lt;ul&gt;
&lt;li&gt;10 years of experience in product management or a related technical role.&lt;/li&gt;
&lt;/ul&gt;</description><url>https://careers.google.com/jobs/results/125959927041532614-group-product-manager/</url><jobtype>FULL_TIME</jobtype><employer>DeepMind</employer><languageCode>en-US</languageCode><categories><category>PRODUCT_MANAGEMENT</category></categories><remote>onsite</remote><isRemote>No</isRemote><isHybrid>No</isHybrid><locations><location><city>Mountain View</city><state>CA</state><country>USA</country></location><location><city>New York</city><state>NY</state><country>USA</country></location><location><city>San Francisco</city><state>CA</state><country>USA</country></location></locations></job></jobs>"""


class _Feed(BoardResponse):
    @property
    def content(self):
        return self.body


def _serve(monkeypatch, body):
    asked = []

    def get(url, **kw):
        asked.append(url)
        return _Feed(body)

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_google_reads_every_posting_with_its_text_from_one_feed(monkeypatch):
    asked = _serve(monkeypatch, FEED)
    engineer, manager = boards.fetch_listings(FEED_URL, "Google")
    assert asked == [FEED_URL]
    assert engineer.company == manager.company == "Google"
    assert engineer.url == (
        "https://www.google.com/about/careers/applications/jobs/results/83093570004624070"
    )
    assert engineer.raw_url == (
        "https://careers.google.com/jobs/results/83093570004624070-research-engineer/"
    )
    assert engineer.locations == ["London, UK"]
    assert manager.locations == [
        "Mountain View, CA, USA",
        "New York, NY, USA",
        "San Francisco, CA, USA",
    ]
    assert manager.url == CANONICAL
    assert engineer.date_posted == int(
        datetime.datetime(2026, 9, 23, 19, 40, 22, 127000, datetime.UTC).timestamp()
    )
    assert engineer.description.startswith(
        "Research Engineer, Energy Grids, Sustainability, DeepMind\n\nLondon, UK\n\n"
        "Minimum qualifications:"
    )
    assert "Solve sequential decision-making problems" in engineer.description
    # The record minus its text: the employer it named is kept.
    assert engineer.raw["employer"] == "DeepMind"
    assert engineer.raw["categories"] == ["SOFTWARE_ENGINEERING"]
    assert engineer.raw["locations"] == [{"city": "London", "state": None, "country": "UK"}]
    assert "description" not in engineer.raw


def test_a_truncated_feed_fails_the_pull(monkeypatch):
    _serve(monkeypatch, FEED[: len(FEED) // 2])
    with pytest.raises(ET.ParseError):
        boards.fetch_listings(FEED_URL, "Google")


def test_a_document_that_is_not_the_feed_fails_the_pull(monkeypatch):
    _serve(monkeypatch, b"<html><body>Jobs search</body></html>")
    with pytest.raises(ValueError, match="not Google's jobs feed"):
        boards.fetch_listings(FEED_URL, "Google")


@pytest.mark.parametrize(
    "url",
    [
        # The feed's own link, and where it redirects.
        "https://careers.google.com/jobs/results/125959927041532614-group-product-manager/",
        "https://www.google.com/about/careers/applications/jobs/results/"
        "125959927041532614-group-product-manager/",
        # The aggregators' spelling, with and without a query.
        CANONICAL,
        CANONICAL + "?utm_source=simplify&hl=en",
        CANONICAL + "/",
        "https://google.com/about/careers/applications/jobs/results/125959927041532614",
    ],
)
def test_every_spelling_of_a_google_posting_is_one_url(url):
    assert normalize_url(url) == CANONICAL
    assert posting_urls(url) == [CANONICAL]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/about/careers/applications/jobs/results",
        FEED_URL,
        "https://www.google.com/about/careers/applications/jobs/results/x-y",
        # Not Google's host, whatever the path says.
        "https://notgoogle.com/about/careers/applications/jobs/results/125959927041532614",
    ],
)
def test_what_is_not_a_google_posting_is_left_alone(url):
    assert ats.canonicalize(url) is None


def test_google_is_not_an_ats_mail_domain():
    assert not ats.is_ats_email_domain("google.com")
