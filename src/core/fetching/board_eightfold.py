"""Fetch listings from the Eightfold board format."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from core.fetching.board_common import (
    PartialPull,
    _get,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.hosts import EIGHTFOLD_PCSX
from core.fetching.posting import JobPosting


def _eightfold(url: str, company: str) -> list[JobPosting]:
    """GET https://{host}/api/pcsx/search?domain={domain}
    or  https://{host}/api/apply/v2/jobs?domain={domain}

    Both page by `start` at ten postings whatever `num` asks for, state the
    count on every page, take any offset and answer an empty page past the
    end; no result window was found up to 21,774 (Starbucks, 2026-10-05).
    Neither list carries the posting's text.

    The order is not stable from one request to the next, so a pull reads
    some postings twice and others never. A posting re-dated or removed
    mid-pull shifts every later page (Microsoft, whose count moved between
    2,297 and 2,300: 2,256 distinct), and the order moves with the count
    unchanged too (Qualcomm, 2,052 throughout: 2,009 distinct in one pull and
    2,043 in the next), all on 2026-10-05. A pull is complete only when it
    holds as many distinct postings as the largest count any page stated;
    anything short is PartialPull.
    """
    parsed = urlparse(url)
    pcsx = parsed.path.rstrip("/") == EIGHTFOLD_PCSX
    origin = f"{parsed.scheme}://{parsed.netloc}"
    seen: dict[str, JobPosting] = {}
    stated = 0
    start = 0
    while True:
        resp = _get(with_query_parameters(url, start=str(start)))
        resp.raise_for_status()
        data = resp.json()
        data = (data.get("data") or {}) if pcsx else data
        page = data.get("positions") or []
        stated = max(stated, int(data.get("count") or 0))
        for j in page:
            link = j.get("positionUrl") if pcsx else j.get("canonicalPositionUrl")
            p = posting_from_listing(
                company,
                j.get("name"),
                j.get("locations") or [j.get("location")],
                urljoin(origin, link) if link else None,
                int((j.get("postedTs") if pcsx else j.get("t_create")) or 0),
                raw=j,
            )
            if p:
                seen[p.url] = p
        if not page:
            break
        start += len(page)
    postings = list(seen.values())
    if len(postings) < stated:
        raise PartialPull(postings)
    return postings
