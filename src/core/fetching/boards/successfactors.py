"""Fetch listings from the Successfactors board format."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse

from core.fetching.ats import (
    clean_html,
    join,
)
from core.fetching.boards.common import (
    PartialPull,
    _get,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.posting import JobPosting

# The feed returns its first `rows` postings and nothing more: it has no
# paging (startrow, start, page and offset were all ignored on 2026-10-05), and
# without `rows` it returns 20. Asked for more than a board holds it returns
# them all, measured to 2,233 of 2,233 on L3Harris, the largest board found.
# Whether it stops silently somewhere above that is unmeasured, which is why
# completeness is read off the sitemap and not off this number.
_SUCCESSFACTORS_ROWS = 100_000


def _successfactors_heading(heading: str) -> tuple[str, str]:
    """Title and primary location from a feed item's "<title> (<location>)".

    Either half can carry parentheses of its own, so the location is the
    balanced group that closes the heading: "Sr Spec, Quality Engrg (Supplier
    Quality) (Rochester, NY, US, 14623)" on L3Harris, "Stagiaire Ingénieur
    Test et Mesure (F/H) (Arc Les Gray Cedex, Saône (Haute), FR, 70103)" on
    Deere, "Senior Enterprise Account Executive FSI (Southbank (Melbourne),
    VIC, AU, 3006)" on SAP, where 69 of 877 locations nest one. A heading that does not close on
    a group is all title.
    """
    text = heading.strip()
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(text[i], 0)
        if depth == 0:
            if i and text[i] == "(" and text[:i].strip():
                return text[:i].strip(), text[i + 1 : -1].strip()
            break
    return text, ""


_ROOT_TAG = re.compile(rb"<[A-Za-z]")


def _xml(raw: bytes) -> ET.Element:
    """Parse a document a board serves, refusing one that declares a DOCTYPE.

    ElementTree expands internal entities, so a DOCTYPE is how a billion-laughs
    document hangs a worker. Neither the sitemap nor the feed declares one (six
    boards, 2026-10-05). A DOCTYPE can only stand before the root element, so
    the prolog is all that needs reading; the same rule as the mail importer's.
    """
    root = _ROOT_TAG.search(raw)
    if b"<!DOCTYPE" in raw[: root.start() if root else len(raw)].upper():
        raise ValueError("refusing an XML document with a DOCTYPE")
    return ET.fromstring(raw)  # noqa: S314 - DOCTYPE refused above


def _successfactors_id(link: str) -> str:
    """The requisition id, the last segment of /job/<slug>/<id>/."""
    return urlparse(link).path.rstrip("/").rsplit("/", 1)[-1]


def _successfactors(url: str, company: str) -> list[JobPosting]:
    """GET https://{host}/services/rss/job/ on a SuccessFactors Career Site Builder site.

    One call carries every posting with its text and date, across all of the
    site's languages (Hensoldt's search pages said 535 in English and 512 in
    German; the feed held 1,051). The feed states no total, so the site's
    /sitemap.xml, which lists the same /job/ urls, is the count: on 2026-10-05
    the two held identical requisition ids on nine boards, from 115 (ULA) to
    2,233 (L3Harris). A requisition in the sitemap and not in the feed
    makes the pull partial. The sitemap is read first, so a posting opened
    between the two calls only adds to the feed, and one closed between them
    costs a partial pull, never a wrong retirement.
    """
    parsed = urlparse(url)
    sitemap = _get(f"{parsed.scheme}://{parsed.netloc}/sitemap.xml")
    sitemap.raise_for_status()
    root = _xml(sitemap.content)
    # Two shapes, each listing every posting url. A urlset of <loc>s, whose
    # namespace is not the standard one everywhere (ULA's is
    # http://www.google.com/schemas/sitemap/0.9), so the local name is
    # matched. Or a Google Base RSS feed of every posting, read by its <link>s:
    # SAP, Deere, Halliburton and Boston Scientific serve that, and on the
    # last three its ids equalled the feed's (337, 442, 548). A sitemap index
    # has never been seen on one of these sites; its <loc>s name further
    # sitemaps, so it, or any other shape, cannot prove the pull complete.
    shape = root.tag.rsplit("}", 1)[-1]
    complete = shape in ("urlset", "rss")
    link = "loc" if shape == "urlset" else "link"
    listed = {
        _successfactors_id(e.text or "")
        for e in root.iter()
        if e.tag.rsplit("}", 1)[-1] == link and "/job/" in (e.text or "")
    }

    # The feed answers 406 to the session's JSON Accept and to application/xml.
    resp = _get(
        with_query_parameters(url, rows=str(_SUCCESSFACTORS_ROWS)),
        headers={"Accept": "application/rss+xml"},
    )
    resp.raise_for_status()
    feed = _xml(resp.content)
    # A malformed query answers 200 with <xml>Error: There is a problem with a
    # jobs query</xml> (keywords=() on ULA, 2026-10-05): a broken fetch, not
    # an empty board.
    if feed.tag != "rss":
        raise ValueError(f"not an RSS feed: {resp.content[:200]!r}")
    out: list[JobPosting] = []
    seen: set[str] = set()
    for item in feed.iterfind("./channel/item"):
        link = item.findtext("link") or ""
        # The link carries feedId and utm_ parameters; the posting is the path.
        public = urlunparse(urlparse(link)._replace(query="", fragment="")) if link else None
        heading = item.findtext("title") or ""
        title, location = _successfactors_heading(heading)
        pub = item.findtext("pubDate")
        p = posting_from_listing(
            company,
            title,
            [location],
            public,
            int(parsedate_to_datetime(pub).timestamp()) if pub else 0,
            raw={"title": heading, "link": link, "pubDate": pub},
            description=join(title, location, clean_html(item.findtext("description") or "")),
        )
        if p:
            out.append(p)
            seen.add(_successfactors_id(link))
    if not complete or listed - seen:
        raise PartialPull(out)
    return out
