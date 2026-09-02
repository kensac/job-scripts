from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import urlparse

from api import metrics

logger = logging.getLogger("jobtracker_api")

SCRAPE_TIMEOUT_SECONDS = int(os.environ.get("JOBTRACKER_SCRAPE_TIMEOUT_SECONDS", "180"))

# Interstitial/block pages (captcha walls, geo blocks, rate limits, site-wide
# outages) must never be cached as job content or fed to closed-checks: a
# blocked fetch says nothing about the job. Markers are checked only on short
# pages; real postings are long, block pages are not.
_BLOCK_MARKERS = (
    "captcha",
    "access denied",
    "request blocked",
    "are you a robot",
    "unusual traffic",
    "too many requests",
    "rate limit",
    "just a moment",
    "checking your browser",
    "attention required",
    "service unavailable",
    "temporarily unavailable",
    "not available in your region",
    "not available in your country",
    "error 503",
    "error 502",
)


def looks_blocked(content: str | None) -> bool:
    if not content:
        return False
    text = content.strip()
    if len(text) < 300:
        return True
    if len(text) < 6000:
        lowered = text.lower()
        return any(m in lowered for m in _BLOCK_MARKERS)
    return False


def redirected_away(requested: str, final: str | None) -> bool:
    """True when a fetch landed somewhere materially different — the signature
    of an expired posting bouncing to a board index or careers page. Compared
    on host+path only, so tracking params and trailing slashes don't count."""
    if not final:
        return False
    a, b = urlparse(requested), urlparse(final)
    return (a.netloc, a.path.rstrip("/")) != (b.netloc, b.path.rstrip("/"))


async def fetch_page(url: str) -> tuple[str | None, bool]:
    """Returns (content, redirected_away).

    Both halves matter: a redirect means the posting is gone, which is a
    verdict, while a None with no redirect only means the fetch failed. Callers
    must not discard the flag — a helper that did exactly that is why redirect
    detection never reached the daily reverify sweep.
    """
    from api import ssrf
    from core.pittcsc_simplify import extract_url_content_ex

    # The browser runs with --no-sandbox --disable-web-security and will fetch
    # whatever it is pointed at, including cloud metadata and services on the
    # compose network. Validate before connecting; validate the FINAL url too,
    # because a public host can redirect into the internal one and a static
    # check cannot see that coming.
    error = await asyncio.to_thread(ssrf.public_url_error, url)
    if error:
        metrics.SCRAPES.labels("blocked_target").inc()
        logger.warning(f"refusing to fetch {url}: {error}")
        return None, False

    start = time.monotonic()
    try:
        # A wedged chromium must not hold a scrape slot forever; the thread
        # itself can't be killed, but freeing the slot keeps the chunk moving.
        content, final_url = await asyncio.wait_for(
            asyncio.to_thread(extract_url_content_ex, url), SCRAPE_TIMEOUT_SECONDS
        )
        if final_url and final_url != url:
            landed = await asyncio.to_thread(ssrf.public_url_error, final_url)
            if landed:
                metrics.SCRAPES.labels("blocked_target").inc()
                logger.warning(f"{url} redirected to a non-public target: {landed}")
                return None, False
        if redirected_away(url, final_url):
            metrics.SCRAPE_DURATION.observe(time.monotonic() - start)
            metrics.SCRAPES.labels("redirected").inc()
            logger.info(f"fetch redirected away: {url} -> {final_url}")
            return None, True
    except TimeoutError:
        metrics.SCRAPE_DURATION.observe(time.monotonic() - start)
        metrics.SCRAPES.labels("timeout").inc()
        logger.warning(f"scrape timed out after {SCRAPE_TIMEOUT_SECONDS}s: {url}")
        return None, False
    metrics.SCRAPE_DURATION.observe(time.monotonic() - start)
    if looks_blocked(content):
        metrics.SCRAPES.labels("blocked").inc()
        logger.info(f"scrape returned a block/interstitial page: {url}")
        return None, False
    metrics.SCRAPES.labels("ok" if content else "empty").inc()
    return content, False
