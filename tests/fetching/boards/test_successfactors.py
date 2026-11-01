"""Successfactors board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse as _Resp

# SuccessFactors Career Site Builder, as jobs.ulalaunch.com, jobs.l3harris.com
# and jobs.deere.com answered on 2026-10-05. The first three items are verbatim
# from those feeds (descriptions trimmed); the rest are the same shape with
# their own ids.
_SF_VERBATIM = [
    (
        "Program Control Analyst 4 (Centennial, CO, US, 80112)",
        "https://jobs.ulalaunch.com/job/Centennial-Program-Control-Analyst-4-CO-80112/1407431200/"
        "?feedId=null&amp;utm_source=J2WRSS&amp;utm_medium=rss&amp;utm_campaign=J2W_RSS",
        "Sun, 04 Oct 2026 7:00:00 GMT",
        "<p><b>Requisition ID: </b>1858</p>\n\n<p><b>Location: </b>ULA - Denver<b> </b></p>",
    ),
    (
        "Sr Spec, Quality Engrg (Supplier Quality) (Rochester, NY, US, 14623)",
        "https://jobs.l3harris.com/job/Rochester-Sr-Spec%2C-Quality-Engrg-%28Supplier-Quality%29"
        "-NY-14623/1436563800/?feedId=null&amp;utm_source=J2WRSS&amp;utm_medium=rss&amp;utm_campaign=J2W_RSS",
        "Mon, 05 Oct 2026 0:00:00 GMT",
        "<p>Job Title: Sr Spec, Quality Engrg</p>",
    ),
    (
        "Stagiaire Ingénieur Test et Mesure (F/H) (Arc Les Gray Cedex, Saône (Haute), FR, 70103)",
        "https://jobs.deere.com/eightfold/job/Arc-Les-Gray-Cedex-Stagiaire-Ing%C3%A9nieur-Test-et"
        "-Mesure-%28FH%29-Sa%C3%B4n-70103/1436128200/?feedId=null&amp;utm_source=J2WRSS"
        "&amp;utm_medium=rss&amp;utm_campaign=J2W_RSS",
        "Fri, 02 Oct 2026 0:00:00 GMT",
        "<p>Stage.</p>",
    ),
]
# 25 postings, more than the 20 the feed returns when `rows` is not asked for.
_SF_ITEMS = _SF_VERBATIM + [
    (
        f"Structural Analyst {n} (Decatur, AL, US, 35601)",
        f"https://jobs.ulalaunch.com/job/Decatur-Structural-Analyst-{n}-AL-35601/14128{n:05d}/"
        "?feedId=null&amp;utm_source=J2WRSS",
        "Sat, 03 Oct 2026 7:00:00 GMT",
        "<p>Analyse structures.</p>",
    )
    for n in range(22)
]


def _sf_feed(items):
    body = "".join(
        f"<item><title><![CDATA[{t}]]></title><description><![CDATA[{d}]]></description>"
        f"<pubDate>{p}</pubDate><link>{u}</link><guid>{u}</guid></item>"
        for t, u, p, d in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?><rss version=\'2.0\' '
        "xmlns:atom='http://www.w3.org/2005/Atom'><channel><title>United Launch Alliance - "
        f"Custom Search </title><ttl>720</ttl> {body}</channel></rss>"
    ).encode()


def _sf_sitemap(items, shape):
    if shape == "urlset":
        # ULA's urlset is in Google's old namespace, not sitemaps.org's.
        locs = "".join(
            f"<url><loc>{u.split('?')[0]}</loc><lastmod>2026-10-03</lastmod></url>"
            for _, u, _, _ in items
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?><urlset '
            f'xmlns="http://www.google.com/schemas/sitemap/0.9">{locs}</urlset>'
        ).encode()
    # Deere, Halliburton, Boston Scientific and SAP serve a Google Base feed of
    # every posting there instead, its urls in <link>, and a channel <link>
    # that is not a posting.
    rows = "".join(
        f"<item><title>{t.replace('&', '&amp;')}</title><link>{u.split('?')[0]}</link>"
        f"<g:id>{u.split('?')[0].rstrip('/').rsplit('/', 1)[-1]}</g:id></item>"
        for t, u, _, _ in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0" '
        'xmlns:g="http://base.google.com/ns/1.0"><channel><title>Jobs at John Deere</title>'
        f"<link>https://jobs.deere.com/</link>{rows}</channel></rss>"
    ).encode()


class _Bytes(_Resp):
    @property
    def content(self):
        return self.body


def _sf_board(monkeypatch, feed_items, sitemap_items, feed=None, shape="urlset"):
    """The feed as it behaves live: the first `rows` items, 20 without it,
    startrow and every other paging parameter ignored, and 406 unless the
    request accepts RSS."""
    asked = []

    def get(url, headers=None, **kw):
        asked.append(url)
        parsed = urlparse(url)
        if parsed.path == "/sitemap.xml":
            return _Bytes(_sf_sitemap(sitemap_items, shape))
        assert "rss" in (headers or {}).get("Accept", ""), "the feed answers 406"
        rows = int((parse_qs(parsed.query).get("rows") or ["20"])[0])
        return _Bytes(feed if feed is not None else _sf_feed(feed_items[:rows]))

    monkeypatch.setattr(boards._session, "get", get)
    return asked


@pytest.mark.parametrize("shape", ["urlset", "rss"])
def test_successfactors_reads_every_posting_in_one_call_and_proves_it_on_the_sitemap(
    monkeypatch, shape
):
    asked = _sf_board(monkeypatch, _SF_ITEMS, _SF_ITEMS, shape=shape)
    out = boards.fetch_listings("https://jobs.ulalaunch.com/services/rss/job/", "ULA")
    # All 25: a fetcher that leaves out `rows` gets 20, and one that pages by
    # startrow gets the first page again, and the sitemap says 25.
    assert len({p.url for p in out}) == 25
    assert asked[0] == "https://jobs.ulalaunch.com/sitemap.xml"
    a, b, c = out[:3]
    assert (a.title, a.locations) == ("Program Control Analyst 4", ["Centennial, CO, US, 80112"])
    # The title's own parentheses stay in the title; the last group is the place.
    assert (b.title, b.locations) == (
        "Sr Spec, Quality Engrg (Supplier Quality)",
        ["Rochester, NY, US, 14623"],
    )
    # And the place can carry its own: the group that closes the heading.
    assert (c.title, c.locations) == (
        "Stagiaire Ingénieur Test et Mesure (F/H)",
        ["Arc Les Gray Cedex, Saône (Haute), FR, 70103"],
    )
    # The page a person opens, without the feed's tracking parameters.
    assert a.url == (
        "https://jobs.ulalaunch.com/job/Centennial-Program-Control-Analyst-4-CO-80112/1407431200"
    )
    assert a.date_posted == int(datetime.datetime(2026, 10, 4, 7, tzinfo=datetime.UTC).timestamp())
    assert a.company == "ULA"
    assert a.description == (
        "Program Control Analyst 4\n\nCentennial, CO, US, 80112\n\n"
        "Requisition ID: \n1858\n\nLocation: \nULA - Denver"
    )


@pytest.mark.parametrize("shape", ["urlset", "rss"])
def test_successfactors_is_partial_when_the_sitemap_lists_more_than_the_feed(monkeypatch, shape):
    # A feed that stops short of the board, as one capped above 2,233 would.
    _sf_board(monkeypatch, _SF_ITEMS[:24], _SF_ITEMS, shape=shape)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://jobs.ulalaunch.com/services/rss/job/", "ULA")
    assert len(raised.value.postings) == 24


def test_successfactors_query_error_is_a_failed_pull_not_an_empty_board(monkeypatch):
    error = b"<xml>Error: There is a problem with a jobs query: Query execution failed</xml>"
    _sf_board(monkeypatch, [], [], feed=error)
    with pytest.raises(ValueError):
        boards.fetch_listings("https://jobs.ulalaunch.com/services/rss/job/", "ULA")


def test_successfactors_refuses_a_document_that_declares_entities(monkeypatch):
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><rss><channel/></rss>'
    )
    _sf_board(monkeypatch, [], [], feed=bomb)
    with pytest.raises(ValueError, match="DOCTYPE"):
        boards.fetch_listings("https://jobs.ulalaunch.com/services/rss/job/", "ULA")


# jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com, first row, 2026-10-05.


# Career Portal job exports as career8.successfactors.com (MoodysProd) and
# career4.successfactors.com (Hersheys) answered on 2026-11-01, descriptions
# cut short. Moody's carries an unnamed custom field, "<>99</>", on every job.
_PORTAL_MOODYS = """<Job>
<JobTitle><![CDATA[Administrative Assistant]]></JobTitle>
<Job-Description><![CDATA[<p>At Moody&#39;s, we unite the brightest minds.</p>]]></Job-Description>
<ReqId>15174</ReqId>
<Posted-Date>09/10/2026</Posted-Date>
<>99</>
</Job>"""
_PORTAL_HERSHEY = """<Job>
<JobTitle><![CDATA[Accountant II]]></JobTitle>
<Job-Description><![CDATA[<p>Why Hershey.</p>]]></Job-Description>
<ReqId>130882</ReqId>
<Posted-Date>10/23/2026</Posted-Date>
<filter5>
<label>Location</label>
<value>Johor</value>
</filter5>
</Job>"""
_PORTAL_ESCAPED = """<Job>
<JobTitle><![CDATA[Assoc Mgr R&amp;D US CMG Reese]]></JobTitle>
<Job-Description><![CDATA[<p>Lead R&amp;D.</p>]]></Job-Description>
<ReqId>129001</ReqId>
</Job>"""
_PORTAL_URL = "https://career8.successfactors.com/career?company=MoodysProd&career_ns=job_listing_summary&resultType=XML"


def _portal_export(*jobs: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Job-Listing>\n\n'
        + "\n\n".join(jobs)
        + "\n</Job-Listing>"
    ).encode()


def _portal_board(monkeypatch, exports: dict[str, bytes]):
    """The export for each language the tenant posts in; any other language
    answers the default, as Hershey's fr_FR answered its en_US."""
    asked = []

    def get(url, **kw):
        asked.append(url)
        lang = (parse_qs(urlparse(url).query).get("lang") or [""])[0]
        return _Bytes(exports.get(lang, exports[""]))

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_a_career_portal_export_is_read_whole_in_one_call(monkeypatch):
    asked = _portal_board(
        monkeypatch, {"": _portal_export(_PORTAL_MOODYS, _PORTAL_HERSHEY, _PORTAL_ESCAPED)}
    )
    out = boards.fetch_listings(_PORTAL_URL, "Moody's")
    assert asked == [_PORTAL_URL]
    moodys, hershey, escaped = out
    assert (moodys.title, moodys.locations, moodys.company) == (
        "Administrative Assistant",
        [],
        "Moody's",
    )
    # The requisition page the employer's own site links to apply.
    assert moodys.url == (
        "https://career8.successfactors.com/sfcareer/jobreqcareer?jobId=15174&company=MoodysProd"
    )
    # Month first.
    assert moodys.date_posted == int(
        datetime.datetime(2026, 9, 10, tzinfo=datetime.UTC).timestamp()
    )
    assert (
        moodys.description
        == "Administrative Assistant\n\nAt Moody's, we unite the brightest minds."
    )
    assert (hershey.locations, hershey.raw["Location"]) == (["Johor"], "Johor")
    assert (escaped.title, escaped.date_posted) == ("Assoc Mgr R&D US CMG Reese", 0)


def test_a_career_portal_board_is_read_in_every_language_it_names(monkeypatch):
    # Allianz posts most requisitions in one language only.
    asked = _portal_board(
        monkeypatch,
        {
            "": _portal_export(_PORTAL_MOODYS),
            "en_US": _portal_export(_PORTAL_MOODYS),
            "fr_FR": _portal_export(_PORTAL_HERSHEY, _PORTAL_MOODYS),
        },
    )
    out = boards.fetch_listings(
        "https://career5.successfactors.eu/career?company=AZGROUPPROD&lang=en_US,fr_FR", "Allianz"
    )
    # A requisition posted in both is one posting.
    assert [p.title for p in out] == ["Administrative Assistant", "Accountant II"]
    assert [parse_qs(urlparse(u).query)["lang"] for u in asked] == [["en_US"], ["fr_FR"]]


def test_an_empty_career_portal_export_is_an_empty_board(monkeypatch):
    _portal_board(monkeypatch, {"": _portal_export()})
    assert boards.fetch_listings(_PORTAL_URL, "Moody's") == []


@pytest.mark.parametrize(
    "body",
    [
        # A tenant the host does not know answers its HTML login page.
        b'<html>\n<!-- verp-client(1.0.0): modules={\najaxservice:"1.0.7"} -->\n<meta id="PerfLog">',
        # Cut off mid-transfer.
        _portal_export(_PORTAL_MOODYS)[:150],
        b'<?xml version="1.0"?><error>unavailable</error>',
    ],
)
def test_a_career_portal_answer_that_is_not_an_export_fails_the_pull(monkeypatch, body):
    _portal_board(monkeypatch, {"": body})
    with pytest.raises(Exception) as raised:
        boards.fetch_listings(_PORTAL_URL, "Moody's")
    assert not isinstance(raised.value, boards.PartialPull)


def test_a_career_portal_url_without_a_company_fails(monkeypatch):
    _portal_board(monkeypatch, {"": _portal_export(_PORTAL_MOODYS)})
    with pytest.raises(ValueError, match="names no SuccessFactors company"):
        boards.fetch_listings("https://career8.successfactors.com/career?lang=en_US", "Moody's")
