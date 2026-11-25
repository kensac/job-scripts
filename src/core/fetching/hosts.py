"""Which upstream a URL speaks to.

`hostname` is the host a URL names, for anything that matches a URL against
a host it was configured with: fetch_host_limits counts a host's page fetches
by URL prefix, so its key is the literal host.

`pace_key` is the host a request is paced under: the host_budget row and the
ingest_host_pace_seconds entry. A platform that serves many hosts from one
upstream is one key, because the upstream is what refuses. Every caller that
paces asks here; when the Workday fold lived in two places, and the Eightfold
one in a third, the budget row a pull took and the floor its pages waited on
were different keys.

`slot_key` is whose turn a page waits for inside a worker: the floor comes
from `pace_key`, the last request from `slot_key`. They differ only for a
platform that refuses per tenant.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Eightfold's two careers-site generations, as the path of a listings URL. A
# tenant answers one and refuses the other with 403 ("Not authorized for PCSX"
# on v2, "PCSX is not enabled" on pcsx). core.fetching.boards reads the same
# two paths to pick its fetcher, and core.fetching.ats to find a tenant.
EIGHTFOLD_PCSX = "/api/pcsx/search"
EIGHTFOLD_V2 = "/api/apply/v2/jobs"
# A Phenom career site's search page, on the employer's own host, with the
# site's country and language where it has them: /us/en/search-results,
# /global/en/search-results, or /search-results (career.lpl.com). Its
# postings are the same prefix and /job/<id>. core.fetching.boards reads
# the listing from it, and core.fetching.ats the posting's text.
PHENOM_SEARCH = re.compile(r"^((?:/[a-z]+/[a-z]{2})?)/search-results$")


def hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def pace_key(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    # Workday tenants have distinct subdomains but share one upstream
    # platform. A budget per tenant lets a single egress address issue one
    # simultaneous burst per company and never learn from another tenant's
    # refusal.
    if host == "myworkdayjobs.com" or host.endswith(".myworkdayjobs.com"):
        return "myworkdayjobs.com"
    # Greenhouse's form lives on boards-api.greenhouse.io while the posting
    # lives on job-boards.greenhouse.io; keyed by the posting host, form reads
    # would keep hammering an operator the listing pulls had backed off from.
    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
        return "boards-api.greenhouse.io"
    # One AWS WAF fronts every Eightfold tenant, whatever domain it serves
    # from: once it challenged an address on 2026-10-05, Lockheed, Northrop,
    # CACI, PayPal and Netflix all answered 405 with x-amzn-waf-action:
    # captcha for a few minutes (Microsoft's did not). A tenant on its own
    # domain is known by its listings path, the same mark boards.kind reads.
    if host.endswith(".eightfold.ai") or parsed.path.rstrip("/") in (
        EIGHTFOLD_PCSX,
        EIGHTFOLD_V2,
    ):
        return "eightfold.ai"
    return host


def slot_key(url: str) -> str:
    """The requests that take turns at the floor `pace_key` sets.

    Workday refuses per tenant: on 2026-11-17 it answered gdit 429 and kohls
    200 from the same address. Its all-tenant refusals (2026-11-16 to 11-21)
    were the HTTP client, which the standard-library path fixed. A tenant
    therefore waits only on its own last page. One shared slot made every
    Workday pull in a worker take turns at one page a second: from 2026-11-20
    Walmart's pull went from 114 to 182 minutes on average, Citi's from 18 to
    67 and Accenture's from 3 to 50, with up to 10 pulls queued on the slot.
    The pull budget stays shared under `pace_key`, so a refusal still holds
    every tenant on that address.

    Every other folded key keeps one slot: one WAF fronts every Eightfold
    tenant and challenged them all at once on 2026-10-05.
    """
    key = pace_key(url)
    if key == "myworkdayjobs.com":
        return hostname(url)
    return key
