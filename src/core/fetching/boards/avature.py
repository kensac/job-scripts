"""Fetch listings from the Avature board format."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from core.fetching.boards.common import (
    PartialPull,
    _get,
    posting_from_listing,
)
from core.fetching.posting import JobPosting


def _avature(url: str, company: str) -> list[JobPosting]:
    """GET https://{tenant}.avature.net/[{locale}/]{portal}/{search page}

    The listings URL is the tenant's own search page (SearchJobs at Bloomberg,
    OpenRoles at Two Sigma), on its avature.net host so the format can be read
    off it. A tenant on its own domain redirects there.

    The paged search page is the only source complete on every tenant
    (measured 2026-10-05): the RSS feed returns the same 20 items whatever
    offset or size is asked, the sitemap lists no postings at Two Sigma, and
    there is no JSON. The walk follows the page's own next link rather than
    building one: the tenant fixes its page size (6 to 25) and ignores the
    size asked for, and the offset's name is the template's (jobOffset at
    Bloomberg, folderOffset at Siemens, which answers its first page to every
    jobOffset). Some templates state the count ("1-12 of 348 results") and
    some do not (Two Sigma, Koch); where there is one, it is what proves the
    walk complete. Most templates list each result as an article; National
    Bank's lists them as rows of a table. The text is not on the list page.
    """
    seen: dict[str, JobPosting] = {}
    stated: int | None = None
    incomplete = False
    page_url: str | None = url
    while page_url:
        resp = _get(page_url)
        if resp.status_code == 406 and seen:
            # Past the window (below); the rest are unseen, not closed.
            raise PartialPull(list(seen.values()))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        if not seen:
            stated = _avature_stated(soup)
        articles = soup.select(_AVATURE_RESULT)
        fresh = [
            p for p in (_avature_posting(a, company) for a in articles) if p and p.url not in seen
        ]
        # A posting already seen means the list moved under the walk, or the
        # next link led back to a page already read; an article without a
        # title is one this parser missed. Either way absence stops being
        # evidence of a closure.
        incomplete |= len(fresh) < len(articles)
        seen.update((p.url, p) for p in fresh)
        # The class is on the anchor at most tenants and on its <li> at Siemens.
        following = soup.select_one("a.paginationNextLink[href], .paginationNextLink a[href]")
        page_url = urljoin(resp.url, str(following["href"])) if fresh and following else None
    postings = list(seen.values())
    if (
        incomplete
        or (stated is not None and len(postings) < stated)
        or (stated is None and len(postings) >= _AVATURE_WINDOW)
    ):
        raise PartialPull(postings)
    return postings


# A search serves no row past offset 2,000. Measured 2026-10-05: Koch answers
# 406 from offset 2,000 on, and Siemens, which states only "999+ results",
# answers an empty page from 2,001. A walk that long with no count to check
# it against cannot tell the end of the list from the end of the window.
_AVATURE_WINDOW = 2000

_AVATURE_RESULT = "article.article--result, table.table--jobs tbody tr"

# "999+ results" (Siemens) states no count, and the pattern does not match it.
# National Bank says "411 result(s)" in English and "411 résultats" in French.
_AVATURE_COUNT = re.compile(r"([\d,]+)\s+r[eé]sult", re.I)


def _avature_stated(soup: BeautifulSoup) -> int | None:
    """The count the search page states, or None where its template says none.
    MetLife hides the visible text and keeps the count in aria-label."""
    legend = soup.select_one(".list-controls__text__legend")
    if legend is None:
        return None
    m = _AVATURE_COUNT.search(f"{legend.get('aria-label') or ''} {legend.get_text(' ')}")
    return int(m.group(1).replace(",", "")) if m else None


def _avature_posting(article: Tag, company: str) -> JobPosting | None:
    # Macquarie links JobDetail?jobId=24902; the others JobDetail/{slug}/{id}.
    for link in article.select('a[href*="/JobDetail"]'):
        title = " ".join(link.get_text(" ").split())
        if title and "button" not in (link.get("class") or []):
            return posting_from_listing(
                company, title, _avature_locations(article), str(link["href"]), 0
            )
    return None


def _avature_locations(article: Tag) -> list[str]:
    """Where the posting is, from whichever field this tenant's template uses.

    Each tenant designs its own list markup. Measured 2026-10-05: a
    list-item-location span (Bloomberg, Harman and ManTech behind a
    "Location:" label, MetLife, Siemens as city, state and country spans
    inside it), a field labelled Location (Koch), or an unlabelled first span
    under the title (Two Sigma, CDCN). Measured 2026-11-01: a detail whose
    icon is labelled "Office Location:" (Macquarie), or the table column
    headed "Location(s)" (National Bank). Pomerleau lists no place at all,
    and returns nothing rather than a guess.
    """
    if article.name == "tr":
        table = article.find_parent("table")
        heads = (
            [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if table
            else []
        )
        cells = article.find_all(["th", "td"], recursive=False)
        return [
            cell.get_text(" ", strip=True)
            for head, cell in zip(heads, cells, strict=False)
            if head.startswith("location")
        ][:1]
    for icon in article.select(".article__details__data img[alt]"):
        value = icon.find_next_sibling("p")
        if "location" in str(icon["alt"]).lower() and value:
            return [value.get_text(" ", strip=True)]
    if el := article.select_one(".list-item-location"):
        return [re.sub(r"^location:\s*", "", " ".join(el.get_text().split()), flags=re.I)]
    for label in article.select(".article__content__field__label"):
        value = label.find_next_sibling(class_="article__content__field__value")
        if label.get_text(strip=True).rstrip(":").lower() == "location" and value:
            return [value.get_text(" ", strip=True)]
    # A classed subtitle span names its own field (Pomerleau's list-item-ref).
    if el := article.select_one(
        ".article__header__text__subtitle > span:not([class]), "
        ".article__header__content__text > span"
    ):
        return [el.get_text(" ", strip=True)]
    return []
