from __future__ import annotations

import asyncio
import logging
import os
import re
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


# A path segment that identifies a specific posting. Two digits minimum is what
# separates an id (999, 90297, 200681316, a uuid) from the locale and version
# fragments that share a path with it (en-us, v2, us1) - requiring length alone
# either missed short ids or matched those.
_ID_SEGMENT = re.compile(r"^(?=(?:\D*\d){2,})[A-Za-z0-9_-]{3,}$")


def _posting_ids(path: str) -> set[str]:
    return {seg for seg in path.split("/") if _ID_SEGMENT.match(seg)}


def redirected_away(requested: str, final: str | None) -> bool:
    """True when a fetch landed on something that is no longer this posting.

    Comparing host+path alone is wrong, and marked live jobs dead. Boards
    routinely rewrite a posting URL without moving the posting:

        jobs.apple.com/details/200681316
            -> /details/200681316/cellular-layer-1-control-...   (slug appended)
        careers.amd.com/jobs/90297
            -> /careers-home/jobs/90297                          (prefix changed)

    Both are canonicalisation. What actually distinguishes a dead posting is
    that the destination no longer identifies it - a bounce to /careers or a
    board index drops the id entirely. So when the requested URL carries an
    id-like segment, the test is whether that id survives the redirect; only
    when there is no id to track do we fall back to comparing host and path.
    """
    if not final:
        return False
    a, b = urlparse(requested), urlparse(final)
    if (a.netloc, a.path.rstrip("/")) == (b.netloc, b.path.rstrip("/")):
        return False
    ids = _posting_ids(a.path)
    if ids:
        return not (ids & _posting_ids(b.path))
    return True


async def fetch_page(url: str) -> tuple[str | None, bool]:
    """Returns (content, landed_elsewhere).

    The flag is a HINT, never a verdict. A redirect used to short-circuit
    straight to "this posting is closed" without the page being read, which
    marked 74 live jobs dead: boards rewrite posting URLs routinely, and no
    URL comparison can reliably tell a canonicalisation from a bounce.

    So the content comes back either way. The browser followed the redirect
    and has the page; the closed-check reads it and decides, which is the
    thing that can actually tell a job posting from a careers index.
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
        landed_elsewhere = redirected_away(url, final_url)
        if landed_elsewhere:
            # Recorded and logged, but NOT acted on: the content is returned
            # so the model can judge the page it actually landed on.
            metrics.SCRAPES.labels("redirected").inc()
            logger.info(f"fetch landed elsewhere: {url} -> {final_url}")
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
    if content:
        metrics.SCRAPES.labels("ok").inc()
        return content, landed_elsewhere
    metrics.SCRAPES.labels("ok" if content else "empty").inc()
    return content, False


# Text-length gate for a browserless fetch. Measured on 2026-09-04 over 431
# pages a browser or resolver had served: every JavaScript shell came back
# under 1,016 characters of text (median 0, p90 323) and every real page over
# 767 (p10 3,828); at 1,500 no shell passed and 2 of 111 real pages fell
# through to the browser, which is the safe direction. The number lives in
# app_config (static_fetch_min_chars); this is what the seed says.


async def fetch_static(url: str, min_chars: int) -> str | None:
    """A page fetch without a browser, with a real Chrome TLS and HTTP/2
    fingerprint (curl_cffi), accepted only when it plainly worked: an HTML
    200 whose extracted text is at least min_chars and reads as a page rather
    than a challenge. Anything else returns None and the caller goes to the
    browser, so this tier can only save a browser fetch, never lose one.

    On the pages the browser was serving this week, one in seven came back
    whole this way in 0.3s; the rest were shells, which the gate rejects.
    """
    from api import ssrf
    from core.ats import clean_html

    error = await asyncio.to_thread(ssrf.public_url_error, url)
    if error:
        metrics.SCRAPES.labels("blocked_target").inc()
        return None

    def _get():
        from curl_cffi import requests as cffi

        return cffi.get(url, impersonate="chrome", timeout=25, allow_redirects=True)

    try:
        resp = await asyncio.wait_for(asyncio.to_thread(_get), 30)
    except Exception as exc:
        metrics.SCRAPES.labels("static_error").inc()
        logger.info(f"static fetch failed for {url}: {type(exc).__name__}")
        return None
    final_url = str(getattr(resp, "url", "") or url)
    if final_url != url and await asyncio.to_thread(ssrf.public_url_error, final_url):
        metrics.SCRAPES.labels("blocked_target").inc()
        return None
    if resp.status_code != 200 or "html" not in (resp.headers.get("content-type") or ""):
        metrics.SCRAPES.labels("static_rejected").inc()
        return None
    text = clean_html(resp.text)
    if len(text) < min_chars or looks_blocked(text):
        # A shell or a challenge page: the browser will render it.
        metrics.SCRAPES.labels("static_shell" if len(text) < min_chars else "static_blocked").inc()
        return None
    metrics.SCRAPES.labels("static_ok").inc()
    return text
