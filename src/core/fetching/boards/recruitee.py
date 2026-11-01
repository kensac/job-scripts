"""Fetch listings from the Recruitee board format."""

from __future__ import annotations

from core.fetching.ats import clean_html, join
from core.fetching.boards.common import _get, iso_posted_timestamp, posting_from_listing
from core.fetching.posting import JobPosting

# Bulk that repeats the text or belongs to the application form.
_RECRUITEE_DROP = frozenset({"requirements", "translations", "open_questions"})


def _recruitee(url: str, company: str) -> list[JobPosting]:
    """GET https://{tenant}.recruitee.com/api/offers/

    One JSON reply of every published offer with its whole text (a
    description and requirements), unpaged and with no count. It held the
    careers page's 62 offers on Hard Rock Digital on 2026-11-01. A tenant
    that does not exist, or has left Recruitee, answers 404.
    """
    resp = _get(url)
    resp.raise_for_status()
    out: list[JobPosting] = []
    for j in resp.json()["offers"]:
        places = [loc.get("name") for loc in j.get("locations") or []] or [j.get("location")]
        p = posting_from_listing(
            company,
            j.get("title"),
            places,
            j.get("careers_url"),
            iso_posted_timestamp((j.get("published_at") or "").replace(" UTC", "+00:00")),
            raw={k: v for k, v in j.items() if k not in _RECRUITEE_DROP},
            description=join(
                j.get("title"),
                "; ".join(x for x in places if x),
                clean_html(j.get("description") or ""),
                clean_html(j.get("requirements") or ""),
            ),
        )
        if p:
            out.append(p)
    return out
