"""Fetch listings from the IBM Kenexa BrassRing board format."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, urlparse

from core.env import comma_list
from core.fetching.boards.common import PartialPull, _get, _post, posting_from_listing
from core.fetching.posting import JobPosting

# The search page holds the tenant's display settings as JSON in this input.
_SETTINGS = re.compile(r'<input id="searchResults" type="hidden" value="([^"]+)"')

# The search answers 50 rows a page whatever is asked (2026-11-01).
_BRASSRING_PAGE = 50


def brassring_site(url: str) -> tuple[str, str]:
    """The partner and site ids a BrassRing URL names, its query keys in any case."""
    query = {k.lower(): v[0] for k, v in parse_qs(urlparse(url).query).items()}
    partner, site = query.get("partnerid", ""), query.get("siteid", "")
    if not (partner.isdecimal() and site.isdecimal()):
        raise ValueError(f"a BrassRing URL names a partnerid and a siteid: {url}")
    return partner, site


def _brassring(url: str, company: str) -> list[JobPosting]:
    """GET https://{host}/TGnewUI/Search/Home/Home?partnerid={p}&siteid={s}, then
    POST https://{host}/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs per page.

    The listings URL is the site's own search page, on sjobs.brassring.com or
    the employer's host (jobs.ubs.com). The search call is the one the page's
    "show more" button makes, and needs no cookie or token. Each row is a list
    of the tenant's questions, and which one is the title or the place is the
    tenant's setting, read off the search page: General Atomics shows formtext8
    as the title, not jobtitle, and UBS names its place fields in
    LocationCustomSolrFields. Edward Jones and Walgreens name none, so their
    rows carry no place. The row's summary field is one section of the
    posting, not its text, and is not stored; the BrassRing resolver reads the
    text from the posting page.

    Sorted by date, rows that tie trade places between pages: walks of Edward
    Jones' 668 postings saw 654 distinct, and General Atomics' 858 saw 851
    (2026-11-01). Sorted by reqid, the requisition key, five walks of four
    boards saw every posting. Every
    page states the count, and a page past the last is empty, with no window
    up to Walgreens' 22,525.
    """
    partner, site = brassring_site(url)
    home = _get(url)
    home.raise_for_status()
    found = _SETTINGS.search(home.text)
    if not found:
        raise ValueError(f"not a BrassRing search page: {url}")
    settings = json.loads(html.unescape(found.group(1)))
    shown = settings.get("JobFieldsToDisplay") or {}
    title_field = (shown.get("JobTitle") or "jobtitle").lower()
    summary_field = (shown.get("Summary") or "jobdescription").lower()
    place_fields = [f.lower() for f in comma_list(settings.get("LocationCustomSolrFields") or "")]

    parsed = urlparse(url)
    search = f"{parsed.scheme}://{parsed.netloc}/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
    seen: dict[str, JobPosting] = {}
    stated: int | None = None
    page = 0
    while stated is None or page * _BRASSRING_PAGE < stated:
        page += 1
        resp = _post(search, json=_brassring_query(partner, site, page))
        resp.raise_for_status()
        data = resp.json()
        if stated is None:
            stated = int(data["JobsCount"])
        rows = (data.get("Jobs") or {}).get("Job") or []
        if not rows:
            break
        for row in rows:
            answers = {
                str(q.get("QuestionName") or "").lower(): (q.get("Value") or "").strip()
                for q in row.get("Questions") or []
            }
            # The fields are parts of one place: state and city at General Atomics.
            place = ", ".join(dict.fromkeys(answers[f] for f in place_fields if answers.get(f)))
            p = posting_from_listing(
                company,
                answers.get(title_field),
                [place],
                row.get("Link"),
                0,
                raw={k: v for k, v in answers.items() if k != summary_field},
            )
            if p:
                seen[p.url] = p
    postings = list(seen.values())
    if len(postings) < (stated or 0):
        raise PartialPull(postings)
    return postings


def _brassring_query(partner: str, site: str, page: int) -> dict:
    return {
        "partnerId": partner,
        "siteId": site,
        "keyword": "",
        "location": "",
        "Latitude": 0,
        "Longitude": 0,
        "facetfilterfields": {"Facet": []},
        "powersearchoptions": {"PowerSearchOption": []},
        "SortType": "reqid",
        "pageNumber": page,
    }
