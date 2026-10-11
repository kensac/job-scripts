"""Fetch listings from the Taleo board format."""

from __future__ import annotations

import datetime
import json
import re
from urllib.parse import parse_qs, urlparse

from core.fetching.board_common import (
    PartialPull,
    _get,
    _post,
    posting_from_listing,
)
from core.fetching.posting import JobPosting

_TALEO_SECTION = re.compile(r"^/careersection/([^/]+)/jobsearch\.ftl$")


def _taleo(url: str, company: str) -> list[JobPosting]:
    """POST https://{tenant}.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}

    The listings URL is the careersection's own search page,
    /careersection/{section}/jobsearch.ftl?lang=en&portal={portal}: the
    section names the public posting URL and the portal is what the search
    endpoint takes. A URL without the portal costs one GET of that page to
    read it (`portalNo`). No cookie, session or CSRF token is needed; the
    endpoint answers 500 unless a `tz` or `tzname` header is present, and its
    value changed nothing on Bell's board (2026-10-05).

    Pages are 25 rows. The body takes a pageSize and echoes it back, but the
    server caches a page by its query and number and not by its size, for
    some minutes and across requests that share no cookie: Textron listed
    691 postings at 25, 477 at 50, 496 at 100 and 250 at 200, and a pull at
    25 straight after saw 100-row pages and 616 postings (2026-10-05). So
    no size is sent, the page count comes from the reply, and postings are
    keyed by url so a repeated row counts once. Every page repeats the same
    totalCount, and a page past the last returns the last page again
    (Kautex, 2026-10-05: pages 5, 10, 50 and 500 all held one posting), so
    the loop is bounded by the count and never by an empty page.

    Pages come back short: the count includes requisitions the list never
    renders. On Bell they were the same 22 under twelve sort orders and every
    job-type slice. Measured on 2026-10-05: Textron 691 of 751, Bell 111 of
    133, Kautex 96 of 104, Textron Aviation 97 of 106, AAR 187 of 193. A
    public posting can also be missing from the search altogether (Bell
    338801 had a live detail page and no keyword search found it). So a pull
    short of its count raises PartialPull and retires nothing.

    Rows carry no text and no company; which column is the title, the
    locations and the date is set per careersection (BAE's carries the title
    alone).
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    lang = (query.get("lang") or ["en"])[0]
    section = _TALEO_SECTION.match(parsed.path)
    if not section:
        raise ValueError(f"{url} is not a careersection search page")
    portal = (query.get("portal") or [""])[0] or _taleo_portal(url)
    endpoint = f"https://{parsed.netloc}/careersection/rest/jobboard/searchjobs"
    detail = f"https://{parsed.netloc}/careersection/{section.group(1)}/jobdetail.ftl?job="
    seen: dict[str, JobPosting] = {}
    page_no = total = last = 1
    while True:
        resp = _post(
            f"{endpoint}?lang={lang}&portal={portal}",
            json={
                "multilineEnabled": False,
                "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {
                    "fields": {"KEYWORD": "", "LOCATION": "", "ORGANIZATION": ""},
                    "valid": True,
                },
                "filterSelectionParam": {"searchFilterSelections": []},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
                "pageNo": page_no,
            },
            headers={"tz": "GMT+00:00"},
        )
        resp.raise_for_status()
        data = resp.json()
        if page_no == 1:
            paging = data["pagingData"]
            total = int(paging["totalCount"])
            last = -(-total // int(paging["pageSize"]))
        for j in data.get("requisitionList") or []:
            columns = j.get("column") or []
            linked = j.get("linkedColumn", 0)
            located = j.get("locationsColumns") or []
            places = [p for i in located if i < len(columns) for p in _taleo_places(columns[i])]
            dates = [
                _taleo_date(c, lang)
                for i, c in enumerate(columns)
                if i != linked and i not in located
            ]
            p = posting_from_listing(
                company,
                columns[linked] if linked < len(columns) else None,
                places,
                detail + j["contestNo"] if j.get("contestNo") else None,
                next((d for d in dates if d), 0),
                raw=j,
            )
            if p:
                seen[p.url] = p
        if page_no >= last:
            break
        page_no += 1
    postings = list(seen.values())
    if len(postings) < total:
        raise PartialPull(postings)
    return postings


def _taleo_portal(url: str) -> str:
    resp = _get(url)
    resp.raise_for_status()
    match = re.search(r"portalNo: '(\d+)'", resp.text)
    if not match:
        raise ValueError(f"{url} carries no portal: not a faceted-search careersection")
    return match.group(1)


def _taleo_places(cell: str) -> list[str]:
    """A locations cell is a JSON list in a string: '["US-Kansas-Wichita"]'."""
    try:
        places = json.loads(cell)
    except ValueError:
        return [cell]
    return [str(p) for p in places] if isinstance(places, list) else [str(places)]


def _taleo_date(cell: str, lang: str) -> int:
    """The posting date as the careersection's locale writes it: 10/02/2026
    on Textron and Oct 5, 2026 on AAR, both lang=en. Slashes are read
    month first only for en, the one locale measured."""
    text = cell.strip()
    if lang == "en" and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", text):
        form = "%m/%d/%Y"
    elif re.fullmatch(r"[A-Za-z]{3} \d{1,2}, \d{4}", text):
        form = "%b %d, %Y"
    else:
        return 0
    try:
        return int(datetime.datetime.strptime(f"{text} +0000", f"{form} %z").timestamp())
    except ValueError:
        return 0
