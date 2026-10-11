"""Fetch listings from the Icims board format."""

from __future__ import annotations

import datetime
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from core.fetching.board_common import (
    PartialPull,
    _get,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.posting import JobPosting

# "Search Results Page 1 of 35" in the results heading is the portal's only
# statement of its size (Expleo writes "page 1 of 1"). It counts pages, not
# postings, and pr= is zero-based where the heading is not.
_ICIMS_PAGES = re.compile(r"\bpage\s+(\d+)\s+of\s+(\d+)", re.I)
# A tenant that moved to a hosted career site answers its portal with a script
# that sends the top window there (careers-spiritaero, careers-amd, 2026-10-05).
_ICIMS_MOVED = re.compile(r"window\.top\.location\.href\s*=\s*'([^']+)'")
# The labels a tenant gives its location field: GDMS "Job Location", Electric
# Boat "Location" (beside "Seat Location", a building), Joby "Job Locations".
_ICIMS_LOCATION = re.compile(r"^(job\s+)?locations?$", re.I)
_ICIMS_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _icims(url: str, company: str) -> list[JobPosting]:
    """GET https://careers-{tenant}.icims.com/jobs/search?pr={page}&in_iframe=1

    HTML, a page of 20 or 50 cards as the tenant configured it (GDMS 20,
    Electric Boat 50). The first page says how many pages there are; pr= past
    the last answers 200 with no cards. Measured on ten tenants, 2026-10-05.

    The pull is complete only if it read that many pages, every page but the
    last was as full as the first, and no posting appeared twice. A posting
    added or removed mid-pull shifts the sort under the pages, which shows as
    a repeat or a short page, and the pull is then partial.
    """
    seen: dict[str, JobPosting] = {}
    rows = 0
    complete = True
    pages = size = 0
    page = 0
    while page == 0 or page < pages:
        resp = _get(with_query_parameters(url, ss="1", in_iframe="1", pr=str(page)))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = [
            a.find_parent(class_="row") or a for a in soup.select(".iCIMS_JobsTable .title a[href]")
        ]
        stated = _ICIMS_PAGES.search(
            " ".join(h.get_text(" ") for h in soup.select(".iCIMS_SubHeader_Jobs"))
        )
        if page == 0:
            moved = _ICIMS_MOVED.search(resp.text)
            if not cards and moved:
                target = urlparse(moved.group(1).replace("\\/", "/"))
                raise ValueError(
                    f"{url} sends its visitors to {target.netloc}; "
                    f"list it as https://{target.netloc}/api/jobs"
                )
            if not cards:
                return []
            if not stated:
                raise ValueError(f"{url} lists postings but states no page count")
            pages, size = int(stated.group(2)), len(cards)
        elif not stated or int(stated.group(2)) != pages:
            complete = False
        last = page == pages - 1
        if not cards or (len(cards) != size and not last) or len(cards) > size:
            complete = False
        # Counted per card, so a card the parser cannot read also fails the
        # proof rather than vanishing from it.
        rows += len(cards)
        for card in cards:
            p = _icims_posting(card, company)
            if p:
                seen[p.url] = p
        page += 1
    postings = list(seen.values())
    if not complete or len(postings) != rows:
        raise PartialPull(postings)
    return postings


def _icims_posting(card: Tag, company: str) -> JobPosting | None:
    anchor = card.select_one(".title a[href]") or card
    heading = anchor.find("h3")
    title = heading.get_text(" ", strip=True) if heading else ""
    # Every field is a label and a value: a <dt>/<dd> pair in the card's
    # body, or an sr-only label beside a value span in its header, whose
    # title attribute holds the exact date where the text says "8 hours ago".
    fields: dict[str, str] = {}
    for tag in card.select(".iCIMS_JobHeaderTag"):
        dt, dd = tag.find("dt"), tag.find("dd")
        if dt and dd:
            fields[dt.get_text(" ", strip=True)] = dd.get_text(" ", strip=True)
    for label in card.select(".header .field-label"):
        value = label.find_next_sibling("span")
        if isinstance(value, Tag):
            text = value.get("title") or value.get_text(" ", strip=True)
            fields[label.get_text(" ", strip=True)] = str(text)
    locations = [
        place.strip()
        for label, value in fields.items()
        if _ICIMS_LOCATION.match(label)
        for place in value.split("|")
    ]
    posted = next((v for k, v in fields.items() if "posted" in k.lower()), "")
    # Month first: every US tenant measured writes 10/4/2026 for 4 October.
    day = _ICIMS_DATE.search(posted)
    href = anchor.get("href")
    return posting_from_listing(
        company,
        title,
        locations,
        str(href) if href else None,
        int(
            datetime.datetime(
                int(day.group(3)), int(day.group(1)), int(day.group(2)), tzinfo=datetime.UTC
            ).timestamp()
        )
        if day
        else 0,
        raw=fields,
    )
