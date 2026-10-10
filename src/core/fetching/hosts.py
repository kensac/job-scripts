"""Which upstream a URL speaks to.

`hostname` is the host a URL names, for anything that matches a URL against
a host it was configured with: fetch_host_limits counts a host's page fetches
by URL prefix, so its key is the literal host.

`pace_key` is the host a request is paced under: the host_budget row, the
ingest_host_pace_seconds entry, and the page pace inside a pull. A platform
that serves many hosts from one upstream is one key, because the upstream is
what refuses. Every caller that paces asks here; when the Workday fold lived
in two places, and the Eightfold one in a third, the budget row a pull took
and the floor its pages waited on were different keys.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Eightfold's two careers-site generations, as the path of a listings URL. A
# tenant answers one and refuses the other with 403 ("Not authorized for PCSX"
# on v2, "PCSX is not enabled" on pcsx). core.fetching.boards reads the same
# two paths to pick its fetcher, and core.fetching.ats to find a tenant.
EIGHTFOLD_PCSX = "/api/pcsx/search"
EIGHTFOLD_V2 = "/api/apply/v2/jobs"


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
