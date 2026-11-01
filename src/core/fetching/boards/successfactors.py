"""Fetch listings from the Successfactors board format."""

from __future__ import annotations

import datetime
import html
import re
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urlparse, urlunparse

from core.fetching.ats import (
    clean_html,
    join,
)
from core.fetching.boards.common import (
    PartialPull,
    _get,
    _xml,
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


# A Career Portal host, the SuccessFactors career site that predates Career
# Site Builder: career8.successfactors.com, career012.successfactors.eu,
# career41.sapsf.com. Every tenant on a host shares it, named by `company`.
_PORTAL_HOSTS = re.compile(r"^career[0-9a-z-]*\.(?:successfactors|sapsf)\.(?:com|eu)$")
# A custom field with no XML name is written as an element with no name,
# "<>99</>" on every Moody's and A.O. Smith job, which no XML parser reads.
_UNNAMED_FIELD = re.compile(rb"<>[^<]*</>")


def is_successfactors_portal(host: str, path: str) -> bool:
    return bool(_PORTAL_HOSTS.match(host)) and path == "/career"


def _successfactors_portal(url: str, company: str) -> list[JobPosting]:
    """GET https://{host}/career?company={tenant}&career_ns=job_listing_summary&resultType=XML

    A Career Portal tenant (Moody's: careers.moodys.com is a Radancy site
    whose apply links open career8.successfactors.com, company MoodysProd)
    has no Career Site Builder feed. Its job search answers this XML export
    of every open requisition with its whole description, in one call with
    no paging and no count. Measured on 2026-11-01, it held one job per
    requisition the Career Site Builder feed of the same tenant listed:
    Halliburton 514 of 514, DTE 75 of 75, Norfolk Southern 54 of 54,
    Dominion 80 of 80, and Hershey 316 of 333, the 17 left over being its
    fr_CA, pt_BR and es_MX copies of requisitions it lists once. Moody's 324
    equalled the /job/ urls in its Radancy sitemap. So, like Gem's, a pull
    is the whole board. A tenant name the host does not know answers its
    HTML login page, which fails the parse rather than reading as empty.

    The export holds the requisitions posted in one language, the tenant's
    default unless `lang` names another. Hershey posts every requisition in
    en_US; Allianz posts most in one language only (en_US 1,098, fr_FR 305
    with 2 of them in en_US, de_DE 176 with 18; 1,727 across 15 languages),
    and a language the tenant does not use answers another one's list
    (Hershey's fr_FR its en_US). So the listings URL names every language
    the board is read in, `lang=en_US,fr_FR`, and a requisition posted in
    several is one posting.

    Rows carry a title, the description, the requisition id, and on most
    tenants a posting date; the place is in the description, or in a filter
    the tenant labelled "Location". The posting is the requisition page the
    employer's own site links to apply.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    tenant = (query.get("company") or query.get("career_company") or [""])[0]
    if not tenant:
        raise ValueError(f"{url} names no SuccessFactors company")
    langs = [lang for value in query.get("lang", []) for lang in value.split(",") if lang]
    export = (
        f"https://{parsed.netloc}/career?company={quote(tenant)}"
        "&career_ns=job_listing_summary&resultType=XML"
    )
    posting = f"https://{parsed.netloc}/sfcareer/jobreqcareer?jobId={{}}&company={quote(tenant)}"
    out: dict[str, JobPosting] = {}
    for lang in langs or [""]:
        resp = _get(export + (f"&lang={quote(lang)}" if lang else ""))
        resp.raise_for_status()
        root = _xml(_UNNAMED_FIELD.sub(b"", resp.content))
        if root.tag != "Job-Listing":
            raise ValueError(f"not a Career Portal job export: {resp.content[:200]!r}")
        for job in root.iterfind("Job"):
            req = (job.findtext("ReqId") or "").strip()
            if req in out:
                continue
            # Some tenants escape the title inside its CDATA ("R&amp;D").
            title = html.unescape(job.findtext("JobTitle") or "").strip()
            fields = {c.tag: c.text for c in job if c.tag != "Job-Description" and not len(c)}
            filters = {f.findtext("label") or f.tag: f.findtext("value") for f in job if len(f)}
            places = [(v or "").strip() for k, v in filters.items() if "location" in k.lower()]
            p = posting_from_listing(
                company,
                title,
                places,
                posting.format(req) if req else None,
                _portal_date(job.findtext("Posted-Date") or ""),
                raw=fields | filters,
                description=join(title, *places, clean_html(job.findtext("Job-Description") or "")),
            )
            if p:
                out[req] = p
    return list(out.values())


def _portal_date(text: str) -> int:
    """The posting date, month first: of 798 dates across five tenants on
    2026-11-01, 585 had a second number above 12 and none a first."""
    try:
        day = datetime.datetime.strptime(f"{text.strip()} +0000", "%m/%d/%Y %z")
    except ValueError:
        return 0
    return int(day.timestamp())
