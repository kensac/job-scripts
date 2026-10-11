"""Fetch listings from the Bytedance board format."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse, urlunparse

from core.fetching.ats import (
    join,
)
from core.fetching.board_common import (
    PartialPull,
    _post,
    posting_from_listing,
)
from core.fetching.posting import JobPosting

# ByteDance's careers API, which serves TikTok (lifeattiktok.com) and
# ByteDance (joinbytedance.com) alike. Either host answers for either board:
# the board is the website-path header, and without it the API answers 400
# (measured 2026-10-05). So the listings URL names the board in a website-path
# query parameter, which is sent as the header, and the public posting page is
# the one that board's own site links to.
_BYTEDANCE_HOSTS = frozenset({"api.lifeattiktok.com", "jobs.bytedance.com"})
_BYTEDANCE_SEARCH = "/api/v1/public/supplier/search/job/posts"
_BYTEDANCE_POSTING = {
    "tiktok": "https://lifeattiktok.com/search/",
    # jobs.bytedance.com/en/position/{id}/detail redirects here.
    "en": "https://joinbytedance.com/search/",
}

# The limit asked for is honoured: 5,000 returned TikTok's whole board in one
# 18 MB reply on 2026-10-05. 500 keeps a reply near 2 MB and at most about 11
# seconds (ByteDance's slowest page that day), inside client.TIMEOUT, and reads TikTok
# (4,289) in 9 requests rather than 43.
_BYTEDANCE_PAGE = 500

# The search serves only its first 10,000 rows: a request whose offset plus
# limit passes 10,000 returns no rows and a count of 10,000, whatever the
# board holds (both boards, 2026-10-05). No board was that large, so whether
# the first page's count also stops at 10,000 is unmeasured; a count at the
# window is read as "at least this many".
_BYTEDANCE_WINDOW = 10_000


def _bytedance(url: str, company: str) -> list[JobPosting]:
    """POST https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts?website-path=tiktok

    Every row carries the posting's text (description and requirement), so
    nothing downstream fetches the page to read it. Rows carry no posting date.
    """
    parsed = urlparse(url)
    board = (parse_qs(parsed.query).get("website-path") or [""])[0]
    if board not in _BYTEDANCE_POSTING:
        raise ValueError(f"unknown ByteDance board {board!r} in {url}")
    endpoint = urlunparse(parsed._replace(query="", fragment=""))
    seen: dict[str, JobPosting] = {}
    offset = 0
    total: int | None = None
    while True:
        resp = _post(
            endpoint,
            json={"limit": min(_BYTEDANCE_PAGE, _BYTEDANCE_WINDOW - offset), "offset": offset},
            headers={"website-path": board},
        )
        resp.raise_for_status()
        reply = resp.json()
        if reply.get("code") != 0:
            raise ValueError(f"{url} answered code {reply.get('code')}: {reply.get('message')}")
        data = reply.get("data") or {}
        page = data.get("job_post_list") or []
        for j in page:
            city = j.get("city_info")
            places: list[str] = []
            while city:
                places.append(city.get("en_name") or "")
                city = city.get("parent")
            # City, state, country; a city-state names itself three times.
            location = ", ".join(dict.fromkeys(p for p in places if p))
            p = posting_from_listing(
                company,
                j.get("title"),
                [location],
                _BYTEDANCE_POSTING[board] + j["id"] if j.get("id") else None,
                0,
                raw={k: v for k, v in j.items() if k != "requirement"},
                description=join(
                    j.get("title"), location, j.get("description"), j.get("requirement")
                ),
            )
            if p:
                seen[p.url] = p
        # The count rides on every page, and is the first page's to trust:
        # one past the window reads 10,000.
        if total is None:
            total = int(data.get("count") or 0)
        offset += len(page)
        if not page or offset >= min(total, _BYTEDANCE_WINDOW):
            break
    # Paging is by offset over a live board, so a posting closed mid-pull
    # shifts the rest and one goes unseen; the full pulls on 2026-10-05 saw
    # every posting once (4,289 and 1,416 distinct).
    if len(seen) < total or total >= _BYTEDANCE_WINDOW:
        raise PartialPull(list(seen.values()))
    return list(seen.values())
