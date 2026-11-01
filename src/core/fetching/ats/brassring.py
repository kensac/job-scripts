"""Resolve BrassRing posting detail."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, urlparse

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    clean_html,
    join_posting_sections,
)


class BrassRing(AtsResolver):
    """A BrassRing posting, read from the JSON its page carries in a hidden input.

    The page (HomeWithPreLoad?...&PageType=JobDetails&jobid=) is an Angular
    shell of about 1 MB whose posting sits in the preLoadJSON input, which no
    page tier reads as text. Its Jobdetails list every field the posting page
    shows, each with its label ("Your skills and experience" at UBS).

    A posting the board no longer lists and a made-up id both answer 200 with
    Jobdetails null (three delisted ids at UBS and General Atomics and one
    made-up id, 2026-11-01), so null is ERROR, never GONE, and closures come
    from the board's pull.
    """

    name = "brassring"
    # No host marker: the posting sits on sjobs.brassring.com or the
    # employer's own host, and a marker would make that host a mail domain.
    markers = ()
    _PAGE = "/tgnewui/search/home/homewithpreload"
    _INPUT = re.compile(r'<input id="preLoadJSON" type="hidden" value="([^"]*)"')

    def matches(self, url: str) -> bool:
        return urlparse(url).path.lower() == self._PAGE

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        query = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items()}
        ids = [query.get(k, "") for k in ("partnerid", "siteid", "jobid")]
        if not all(i.isdecimal() for i in ids):
            return None
        partner, site, job = ids
        return (
            f"https://{parsed.netloc.lower()}/TGnewUI/Search/home/HomeWithPreLoad"
            f"?partnerid={partner}&siteid={site}&PageType=JobDetails&jobid={job}"
        )

    def fetch(self, url: str) -> AtsResult:
        page = self.canonical(url)
        if page is None:
            return UNSUPPORTED
        resp = self.get(page)
        # Never GONE (above), so a 404 is not read as a closure either.
        if resp is None or resp.status_code != 200:
            return self.result(None)
        found = self._INPUT.search(resp.text)
        data = json.loads(html.unescape(found.group(1))) if found and found.group(1) else {}
        details = data.get("Jobdetails") or {}
        # The fields in the order the page shows them, the title among them. A
        # field the page does not show has no ClassName: ids and coordinates.
        shown = [q for q in details.get("JobDetailQuestions") or [] if q.get("ClassName")]
        return self.result(
            join_posting_sections(
                *(
                    join_posting_sections(q.get("QuestionName"), clean_html(q.get("AnswerValue")))
                    for q in shown
                )
            )
        )
