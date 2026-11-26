"""The text a company says about itself, taken out before a posting is
embedded.

A company's postings share long stretches: the about-us blurb, benefits, the
equal-opportunity statement, the agency notice, and the company's own name
throughout. Embedded as written, a posting's nearest neighbours are its
employer's other postings, whatever the role. On 300 production postings from
15 companies with at least four role families each (2026-11-26), the share of
a posting's ten nearest from its own company (chance 0.06), and the share in
its primary role family (job_profiles):

    input                                           same company   same family
    text-embedding-3-small, as written                  0.89           0.24
    nomic-embed-text-v1.5, as written                   0.78           0.30
    name masked only                                    0.56           0.38
    company text removed and name masked (this rule)    0.42           0.44

What counts as the company's text was settled by reading what each rule
removed, not by the score alone. Repetition across titles is not enough: a
hospital's nurse titles all carry "RN - State Licensure required", ADT's
field roles "Valid driver's license", and a sales team's titles the same
duty, and a rule that stripped every paragraph repeated under three titles
removed those while scoring 0.41 and 0.44. Exact phrasing spread across
companies does not separate them either: that hospital's licensure line is
worded as no other employer words it. What does: a paragraph is the
company's when it repeats under at least two of its titles and names the
company (its full name, or the name without its legal form), or repeats
under at least half of its titles and at least three. A role's text repeats
under a subset of the titles and rarely names the company; the about-us
blurb, the EEO statement, the agency notice and the benefits list do one or
the other. Read on the same set, every named paragraph under only two or
three titles was company text (one of several versions of a company's
about-us, EEO or agency notice) or a heading; none was a role's duty, so a
named paragraph needs no share of the titles, and requiring one would keep
those versions.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

# The rule profiles are built under; a profile built under an older one is
# rebuilt. 1: repeated under three titles. 2: the module docstring's rule.
PROFILE_RULE = 2

# Titles a paragraph must repeat under to be the company's at all, and the
# share of the company's titles that makes it the company's without naming it.
SHARED_MIN_TITLES = 2
UNNAMED_MIN_SHARE = 0.5
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


def shared_paragraphs(company: str, postings: Iterable[tuple[str, str]]) -> tuple[int, set[bytes]]:
    """From one company's (title, text) postings, how many distinct titles it
    has and the paragraphs that are its own text (see the module docstring)."""
    titles_of: dict[bytes, set[str]] = {}
    text_of: dict[bytes, str] = {}
    titles: set[str] = set()
    for raw_title, text in postings:
        title = raw_title.strip().lower()
        titles.add(title)
        for paragraph in paragraphs(text):
            key = paragraph_key(paragraph)
            if key is not None:
                titles_of.setdefault(key, set()).add(title)
                text_of[key] = paragraph
    unnamed_floor = max(SHARED_MIN_TITLES + 1, UNNAMED_MIN_SHARE * len(titles))
    own = {
        key
        for key, seen in titles_of.items()
        if len(seen) >= SHARED_MIN_TITLES
        and (len(seen) >= unnamed_floor or names_company(text_of[key], company))
    }
    return len(titles), own


# Words of a company name that name nothing: legal forms and connectives.
_NAME_NOISE = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "co",
        "limited",
        "plc",
        "gmbh",
        "the",
        "and",
        "of",
        "group",
        "services",
        "company",
    }
)


def _name_phrases(company: str) -> list[str]:
    """What a posting calls its company: the full name, the name without its
    legal form, and the name's first word, which is how a posting shortens
    "Acme Robotics" to "Acme". Field words later in the name ("Robotics")
    are left alone; a one-word name is masked as that word wherever it
    appears, the cost of masking it at all."""
    words = [w for w in re.split(r"[^\w&]+", company.lower()) if w]
    meaningful = [w for w in words if w not in _NAME_NOISE]
    phrases = {" ".join(words), " ".join(meaningful)}
    if meaningful:
        phrases.add(meaningful[0])
    return sorted((p for p in phrases if len(p) > 2), key=len, reverse=True)


def _name_pattern(phrase: str) -> str:
    return rf"\b{r'[\s,.]+'.join(map(re.escape, phrase.split()))}\b"


def names_company(text: str, company: str) -> bool:
    """Whether `text` says the company's full name, or that name without its
    legal form. Not its first word alone: for Advanced Micro Devices that is
    "advanced", which a requirement says without naming anyone."""
    words = [w for w in re.split(r"[^\w&]+", company.lower()) if w]
    meaningful = [w for w in words if w not in _NAME_NOISE]
    phrases = {" ".join(words), " ".join(meaningful)}
    return any(
        re.search(_name_pattern(p), text, flags=re.IGNORECASE) for p in phrases if len(p) > 2
    )


def mask_company(text: str, company: str) -> str:
    """The company's name replaced by "the company", longest phrase first:
    the full name, the name without its legal form, and its first word. On the
    set above, masking these scored as masking every word of the name did,
    and better than the full name alone (0.44 and 0.43)."""
    for phrase in _name_phrases(company):
        text = re.sub(_name_pattern(phrase), "the company", text, flags=re.IGNORECASE)
    return text


def embedding_text(text: str, company: str, shared: set[bytes]) -> str:
    """What is embedded for a posting: its paragraphs other than the
    company's own text, with the company's name masked."""
    kept = [p for p in paragraphs(text) if paragraph_key(p) not in shared]
    stripped = "\n".join(kept)
    return mask_company(stripped if len(stripped) >= MIN_KEPT_CHARS else text, company)


def shared_by_company(rows: Iterable[Mapping[str, Any]]) -> dict[str, set[bytes]]:
    """company_shared_paragraphs rows grouped by company key."""
    out: dict[str, set[bytes]] = {}
    for row in rows:
        out.setdefault(row["company_key"], set()).add(bytes(row["paragraph_sha"]))
    return out
