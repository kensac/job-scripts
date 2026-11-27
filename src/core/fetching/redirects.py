"""Links that only redirect to a posting, resolved to the posting.

Some feeds link every posting through a link shortener: the vanshb03 lists
(internships_ouckah, fulltime_ouckah_2027) link go.vansh.nyc/<code>, which
answers 302 to the employer's posting. jobs.url is a posting's identity
(docs/agents/sources-and-boards.md, "A posting has one URL"), and a
shortener's URL cannot be reduced to the employer's without asking, so the
same posting arrived twice: 252 of 300 sampled short links (2026-11-27)
pointed at a posting the catalog already held under the employer's URL.

`resolve` asks each link once, for the redirect only (the page behind it is
never loaded), follows it while it stays on a redirector, canonicalises
where it lands (urls.normalize_url), and keeps the answer in url_redirects,
so later pulls read it instead of asking again.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urljoin, urlparse

from core.fetching import client
from core.fetching.urls import normalize_url
from core.pool import connection

logger = logging.getLogger(__name__)

# Hosts whose links only redirect to a posting elsewhere.
REDIRECTORS = frozenset({"go.vansh.nyc"})

_REDIRECT = frozenset({301, 302, 303, 307, 308})


def is_redirector(url: str) -> bool:
    return (urlparse(url).hostname or "") in REDIRECTORS


def _follow(url: str) -> str | None:
    """Where `url` lands once it leaves the redirectors, or None when a hop
    answers anything but a redirect, or the chain comes back on itself."""
    seen = {url}
    at = url
    while is_redirector(at):
        client.pace(at)
        response = client.session.get(at, allow_redirects=False)
        location = response.headers.get("location")
        if response.status_code not in _REDIRECT or not location:
            logger.info("redirector link %s answered %s", at, response.status_code)
            return None
        at = urljoin(at, location)
        if at in seen:
            logger.info("redirector link %s loops", url)
            return None
        seen.add(at)
    return normalize_url(at)


def resolve(urls: Iterable[str]) -> dict[str, str]:
    """The canonical posting URL each redirector link points to, for those
    that could be resolved: from url_redirects, else asked now and stored.
    A link that could not be resolved is absent, and is asked again next
    time."""
    wanted = sorted({u for u in urls if is_redirector(u)})
    if not wanted:
        return {}
    with connection() as conn:
        known = {
            r["url"]: r["target"]
            for r in conn.execute(
                "SELECT url, target FROM url_redirects WHERE url = ANY(%s)", (wanted,)
            ).fetchall()
        }
    asked: dict[str, str] = {}
    for url in wanted:
        if url in known:
            continue
        try:
            target = _follow(url)
        except Exception:
            logger.warning("redirector link %s could not be resolved", url, exc_info=True)
            continue
        if target:
            asked[url] = target
    if asked:
        with connection() as conn:
            conn.execute(
                "INSERT INTO url_redirects (url, target) "
                "SELECT unnest(%s::text[]), unnest(%s::text[]) ON CONFLICT (url) DO NOTHING",
                (list(asked), list(asked.values())),
            )
    return {**known, **asked}
