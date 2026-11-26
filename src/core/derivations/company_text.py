"""The text a company repeats across its postings, taken out before a posting
is embedded.

A company's postings share long stretches: the about-us blurb, benefits, the
equal-opportunity statement, the apply footer, and the company's own name
throughout. Embedded as written, a posting's nearest neighbours are its
employer's other postings, whatever the role. On 300 production postings from
15 companies with at least four role families each (2026-11-26), the share of
a posting's ten nearest from its own company (chance 0.06), and the share in
its primary role family (job_profiles):

    input                                       same company   same family
    text-embedding-3-small, as written              0.89           0.24
    nomic-embed-text-v1.5, as written               0.78           0.30
    shared paragraphs removed                       0.61           0.38
    shared paragraphs removed, name masked          0.41           0.44
    looser threshold (2 titles, 10%), masked        0.38           0.43

A paragraph is shared when it appears under at least SHARED_MIN_TITLES of the
company's distinct titles and at least SHARED_MIN_SHARE of them. Titles, not
postings: a role reposted, or opened in several cities, repeats all of its
text, and counting postings would strip it to nothing. The looser threshold
removed more text for no gain, so the measured one stands.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

SHARED_MIN_TITLES = 3
SHARED_MIN_SHARE = 0.15
# Shorter lines are headings and bullets ("Benefits", "Apply now"), which say
# nothing about the company or the role either way.
MIN_PARAGRAPH_CHARS = 20
# What is left must still be a posting; when stripping leaves less than this,
# the posting is embedded as written. The same floor page_texts calls a page
# (core.store.MIN_CONTENT_CHARS).
MIN_KEPT_CHARS = 200

# Distinct titles a profile reads, one posting each: twice the twenty per
# company the comparison above measured, and at the throughput Garage served
# held pages (2026-11-26) a fraction of a second of reads per company.
PROFILE_TITLES = 40

# Words of a company name that name nothing: legal forms and connectives.
_NAME_NOISE = frozenset(
    {"inc", "llc", "ltd", "corp", "co", "the", "and", "of", "group", "services", "company"}
)


def company_key(company: str) -> str:
    """The key jobs are grouped by: lower(btrim(company)), as
    idx_jobs_company_key spells it."""
    return company.strip().lower()


def paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n", text) if p.strip()]


def paragraph_key(paragraph: str) -> bytes | None:
    """A paragraph's identity across postings: its words, case and punctuation
    ignored. None for a line too short to count."""
    words = re.sub(r"\W+", " ", paragraph.lower()).strip()
    if len(words) < MIN_PARAGRAPH_CHARS:
        return None
    return hashlib.sha256(words.encode()).digest()


def shared_paragraphs(postings: Iterable[tuple[str, str]]) -> tuple[int, set[bytes]]:
    """From one company's (title, text) postings, how many distinct titles it
    has and the paragraphs shared across them."""
    titles_of: dict[bytes, set[str]] = {}
    titles: set[str] = set()
    for raw_title, text in postings:
        title = raw_title.strip().lower()
        titles.add(title)
        for paragraph in paragraphs(text):
            key = paragraph_key(paragraph)
            if key is not None:
                titles_of.setdefault(key, set()).add(title)
    floor = max(SHARED_MIN_TITLES, SHARED_MIN_SHARE * len(titles))
    return len(titles), {key for key, seen in titles_of.items() if len(seen) >= floor}


def _name_words(company: str) -> list[str]:
    words = {w for w in re.split(r"[^\w&]+", company.lower()) if len(w) > 2}
    return sorted(words - _NAME_NOISE, key=len, reverse=True)


def mask_company(text: str, company: str) -> str:
    """The company's name, word by word, replaced by "the company"."""
    for word in _name_words(company):
        text = re.sub(rf"\b{re.escape(word)}\b", "the company", text, flags=re.IGNORECASE)
    return text


def embedding_text(text: str, company: str, shared: set[bytes]) -> str:
    """What is embedded for a posting: its paragraphs the company does not
    share across roles, with the company's name masked."""
    kept = [p for p in paragraphs(text) if paragraph_key(p) not in shared]
    stripped = "\n".join(kept)
    return mask_company(stripped if len(stripped) >= MIN_KEPT_CHARS else text, company)


def shared_by_company(rows: Iterable[Mapping[str, Any]]) -> dict[str, set[bytes]]:
    """company_shared_paragraphs rows grouped by company key."""
    out: dict[str, set[bytes]] = {}
    for row in rows:
        out.setdefault(row["company_key"], set()).add(bytes(row["paragraph_sha"]))
    return out
