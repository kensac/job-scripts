"""Resolve Apple posting detail and availability."""

from __future__ import annotations

import re
from dataclasses import replace

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    join_posting_sections,
    parse_posted_date,
)


class Apple(AtsResolver):
    """A jobs.apple.com posting, read from the careers site's own details API.

    The posting page builds its text from JSON embedded in the page, so the
    static fetch extracts the site's navigation alone (3,278 characters, none
    of the posting, on 2026-10-05) and that clears static_fetch_min_chars.
    jobDetails takes the id the page URL carries.

    A requisition that is no longer posted answers 404: 68 of 68 requisitions
    from an aggregator's history that the board no longer listed did, and 66
    of 66 it still listed answered 200. A 200 is not proof the posting is
    listed: one requisition missing from the board answered 200 with its
    full text, so the board pull, not this, closes that one. An unknown
    location suffix answers the requisition itself, not a 404.
    """

    name = "apple"
    # The slash keeps the marker out of the mail domains, which inherit
    # resolver markers: whether jobs.apple.com sends application mail has
    # not been measured.
    markers = ("jobs.apple.com/",)
    # A pipeline row's id is "PIPE-<n>" in the search API, and its page and
    # jobDetails both take <n>; both also accept the prefixed form.
    _JOB = re.compile(
        r"jobs\.apple\.com/[a-z]{2}-[a-z]{2}/details/(?:PIPE-)?([0-9][0-9-]*)(/[^/?#]+)?"
    )
    _SECTIONS = (
        ("jobSummary", None),
        ("description", "Description"),
        ("responsibilities", "Responsibilities"),
        ("minimumQualifications", "Minimum Qualifications"),
        ("preferredQualifications", "Preferred Qualifications"),
    )

    def canonical(self, url: str) -> str | None:
        # The listing fetcher writes /en-us/details/{id}/{slug}; any locale,
        # query or trailing page (/locationPicker) of the same posting is it.
        match = self._JOB.search(url)
        if not match:
            return None
        return f"https://jobs.apple.com/en-us/details/{match.group(1)}{match.group(2) or ''}"

    def fetch(self, url: str) -> AtsResult:
        match = self._JOB.search(url)
        if not match:
            return UNSUPPORTED
        job_id = match.group(1)
        resp = self.get(f"https://jobs.apple.com/api/v1/jobDetails/{job_id}")
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        data = resp.json().get("res") or {}
        # For an unknown location suffix jobNumber is the bare requisition and
        # selectedLocation is one of its other places, so name them all.
        place = data.get("selectedLocation") if data.get("jobNumber") == job_id else None
        places = [place] if place else data.get("locations") or []
        result = self.result(
            join_posting_sections(
                data.get("postingTitle"),
                "; ".join(
                    ", ".join(dict.fromkeys(x for x in (p.get("name"), p.get("countryName")) if x))
                    for p in places
                ),
                *(
                    join_posting_sections(label, data.get(key)) if data.get(key) else None
                    for key, label in self._SECTIONS
                ),
            )
        )
        # A managed pipeline role's date is the time of the request, here as
        # in the search API.
        if data.get("managedPipelineRole"):
            return result
        return replace(result, posted=parse_posted_date(data.get("postDateInGMT")))
