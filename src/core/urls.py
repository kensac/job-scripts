from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from core import ats

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "source",
    "campaign",
    "fbclid",
    "gclid",
    "_ga",
    "_gl",
    "mc_cid",
    "mc_eid",
    "hsCtaTracking",
    "hsa_",
    "mobile",
    "needsRedirect",
    "width",
    "height",
    "bga",
    "jan1offset",
    "jun1offset",
    "iis",
    "iisn",
    "in_iframe",
    "embed",
    "viewControls",
}


def normalize_url(url: str) -> str:
    if not url:
        return url

    canonical = ats.canonicalize(url)
    if canonical:
        return canonical

    parsed = urlparse(url)
    query_params = parse_qs(parsed.query, keep_blank_values=True)

    tracking_params_lower = {p.lower() for p in TRACKING_PARAMS}
    filtered_params = {
        k: v
        for k, v in query_params.items()
        if k.lower() not in tracking_params_lower
        and not any(k.lower().startswith(param) for param in tracking_params_lower)
    }

    new_query = urlencode(filtered_params, doseq=True) if filtered_params else ""

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            new_query,
            "",
        )
    )
