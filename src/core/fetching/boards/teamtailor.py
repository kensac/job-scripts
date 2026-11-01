"""Fetch listings from the Teamtailor board format."""

from __future__ import annotations

from email.utils import parsedate_to_datetime

from core.fetching.ats import clean_html, join
from core.fetching.boards.common import _get, _xml, location_label, posting_from_listing
from core.fetching.posting import JobPosting

_TT = "{https://teamtailor.com/locations}"


def _teamtailor(url: str, company: str) -> list[JobPosting]:
    """GET https://{tenant}.teamtailor.com/jobs.rss

    One RSS document of every published posting with its whole text and
    date. It takes no paging (page=2 answered the first page again) and
    states no count; it held the careers page's postings one for one on
    Oatly (20), Polestar (13), Teamtailor (17) and RoadSync (2) on
    2026-11-01. A tenant that does not exist answers 404.
    """
    resp = _get(url, headers={"Accept": "application/rss+xml"})
    resp.raise_for_status()
    root = _xml(resp.content)
    if root.tag != "rss":
        raise ValueError(f"not an RSS feed: {resp.content[:200]!r}")
    out: list[JobPosting] = []
    for item in root.iterfind("./channel/item"):
        title = item.findtext("title")
        places = [
            loc.findtext(f"{_TT}name")
            or location_label(
                {"city": loc.findtext(f"{_TT}city"), "country": loc.findtext(f"{_TT}country")}
            )
            for loc in item.iterfind(f"{_TT}locations/{_TT}location")
        ]
        pub = item.findtext("pubDate")
        p = posting_from_listing(
            company,
            title,
            places,
            item.findtext("link"),
            int(parsedate_to_datetime(pub).timestamp()) if pub else 0,
            raw={
                "guid": item.findtext("guid"),
                "pubDate": pub,
                "remoteStatus": item.findtext("remoteStatus"),
                "department": item.findtext(f"{_TT}department"),
                "locations": places,
            },
            description=join(
                title, "; ".join(places), clean_html(item.findtext("description") or "")
            ),
        )
        if p:
            out.append(p)
    return out
