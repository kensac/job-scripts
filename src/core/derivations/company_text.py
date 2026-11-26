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
    company text removed and name masked (this rule)    0.42           0.43

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

Read on production, the half-the-titles test stripped duties at a company
whose few roles are all of one kind (a five-title AI startup kept 24% of its
text), as it would at a staffing agency posting one role a hundred ways. So
it skips text under a role heading ("Responsibilities", "What you'll do",
"Qualifications" and the like, until a company heading such as "Benefits"
closes the section), decided by majority over the postings that carry the
paragraph, since a posting that never closes its role section carries its
benefits inside it. That startup then kept 73%, its duties with it. Headings are matched whole (see
_ROLE_HEADINGS), so a bullet such as "Experience with cell culture" or
"Uncapped commissions" neither opens nor closes a section, and an
equal-opportunity, accommodation or agency paragraph closes one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

# The rule profiles are built under; a profile built under an older one is
# rebuilt. 1: repeated under three titles. 2: named, or under half the
# titles. 3: as 2, but the half-the-titles test skips a role section's text.
PROFILE_RULE = 3

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


# Section headings, matched whole: a heading is a line that, lowercased and
# without its bullet and trailing colon, IS one of these, or a line ending in a
# colon, of at most COLON_HEADING_WORDS words, that contains one as whole
# words. Read from the headings of the 300 postings the module docstring
# measures; a heading not listed is no heading, and its text falls to the
# general rule. A bullet ("Strong written communication skills", "Experience
# with cell culture", "Uncapped commissions") is never a heading.
_ROLE_HEADINGS = frozenset(
    {
        "responsibilities",
        "key responsibilities",
        "your responsibilities",
        "job responsibilities",
        "key job responsibilities",
        "principal duties & responsibilities",
        "essential functions",
        "essential functions/major responsibilities",
        "duties",
        "qualifications",
        "minimum qualifications",
        "preferred qualifications",
        "basic qualifications",
        "required qualifications",
        "requirements",
        "minimum requirements",
        "what you will do",
        "what you'll do",
        "what you'll be working on",
        "what you'll bring to the team",
        "what you bring",
        "what we're looking for",
        "who you are",
        "about you",
        "the role",
        "about the role",
        "about this role",
        "your role",
        "role overview",
        "job summary",
        "position summary",
        "job description",
        "summary of duties and responsibilities",
        "knowledge, skills and abilities",
        "skills",
        "experience",
        "education",
        "education and experience",
        "education & experience required",
        "licenses and certifications",
        "bonus points",
        "nice to have",
        "nice to haves",
        "must have",
        "must haves",
        "in this role",
        "how you'll make impact",
        "the difference you will make",
        "on day one we will expect you to have",
        "funciones",
        "responsabilidades",
        "requisitos",
        "vos missions",
        "votre profil",
        "profil",
        "aufgaben",
        "ihre aufgaben",
        "anforderungen",
        "ihr profil",
    }
)
_COMPANY_HEADINGS = frozenset(
    {
        "about us",
        "who we are",
        "what we do",
        "benefits",
        "our benefits",
        "benefits page",
        "what we offer",
        "why join us",
        "equal opportunity",
        "equal employment opportunity",
        "compensation",
        "compensation range",
        "salary",
        "salary range",
        "pay range",
        "perks",
        "culture",
        "our culture",
        "privacy policy",
        "applicant privacy policy",
        "our mission",
        "join our team",
        "careers",
        "contact us",
        "e-verify",
        "terms of use",
        "about the company",
        "the community you will join",
        "our commitment to inclusion & belonging",
        "avantages",
        "à propos",
        "über uns",
        "beneficios",
        "sobre nosotros",
    }
)
# Company headings that end with the company's own name ("Why join UHN?",
# "Life at Airbnb"), matched by how they start; at most COLON_HEADING_WORDS
# words, so a sentence starting so is not one.
_COMPANY_HEADING_STARTS = (
    "why join",
    "why work",
    "why you'll love",
    "life at",
    "working at",
    "about working at",
)
# Phrases (whole words) that make a colon heading a role or a company one.
_ROLE_WORDS = (
    "responsibilities",
    "qualifications",
    "requirements",
    "duties",
    "what you",
    "who you are",
    "the role",
    "this role",
    "your role",
    "job summary",
    "position summary",
    "skills",
)
_COMPANY_WORDS = ("about us", "benefits", "what we offer", "why join", "perks", "compensation")
# Colon headings in the 300 postings: 97% of those up to ten words had eight
# or fewer; longer colon lines are sentences introducing a list.
COLON_HEADING_WORDS = 8
# Paragraphs that are company boilerplate whatever section they sit in: they
# close a role section, and a template that never closes one with a heading
# still loses its equal-opportunity statement.
_BOILERPLATE = (
    "equal opportunity",
    "equal employment",
    "affirmative action",
    "reasonable accommodation",
    "e-verify",
    "third-party recruiter",
    "recruitment agenc",
    "unsolicited resume",
    "privacy policy",
    "protected veteran",
)


def _words(phrase: str) -> str:
    return rf"(?<!\w){re.escape(phrase)}(?!\w)"


def _heading(line: str, company: str) -> str | None:
    """ "role" or "company" for a heading line, None for any other line."""
    raw = line.strip()
    text = re.sub(r"^[\W\d_]+", "", raw.lower()).rstrip(" :?").strip()
    if not text:
        return None
    if text in _COMPANY_HEADINGS or any(text == f"about {p}" for p in _name_phrases(company)):
        return "company"
    if len(text.split()) <= COLON_HEADING_WORDS and text.startswith(_COMPANY_HEADING_STARTS):
        return "company"
    if text in _ROLE_HEADINGS:
        return "role"
    if raw.endswith(":") and len(text.split()) <= COLON_HEADING_WORDS:
        if any(re.search(_words(w), text) for w in _COMPANY_WORDS):
            return "company"
        if any(re.search(_words(w), text) for w in _ROLE_WORDS):
            return "role"
    return None


def sections(text: str, company: str) -> list[tuple[bytes, str, bool]]:
    """Each keyed paragraph of a posting, and whether it sits under a role
    heading: until a company heading, or a boilerplate paragraph, closes the
    section. Boilerplate is never the role's."""
    out: list[tuple[bytes, str, bool]] = []
    in_role = False
    for paragraph in paragraphs(text):
        kind = _heading(paragraph, company)
        if kind is not None:
            in_role = kind == "role"
            continue
        if any(b in paragraph.lower() for b in _BOILERPLATE):
            in_role = False
        key = paragraph_key(paragraph)
        if key is not None:
            out.append((key, paragraph, in_role))
    return out


def shared_paragraphs(company: str, postings: Iterable[tuple[str, str]]) -> tuple[int, set[bytes]]:
    """From one company's (title, text) postings, how many distinct titles it
    has and the paragraphs that are its own text (see the module docstring)."""
    titles_of: dict[bytes, set[str]] = {}
    text_of: dict[bytes, str] = {}
    titles: set[str] = set()
    # A paragraph is the role's when more of the postings carrying it put it
    # under a role heading than not: a posting whose role section is never
    # closed by a heading carries the benefits list inside it.
    votes: dict[bytes, int] = {}
    for raw_title, text in postings:
        title = raw_title.strip().lower()
        titles.add(title)
        for key, paragraph, in_role in sections(text, company):
            titles_of.setdefault(key, set()).add(title)
            text_of[key] = paragraph
            votes[key] = votes.get(key, 0) + (1 if in_role else -1)
    unnamed_floor = max(SHARED_MIN_TITLES + 1, UNNAMED_MIN_SHARE * len(titles))
    own = {
        key
        for key, seen in titles_of.items()
        if len(seen) >= SHARED_MIN_TITLES
        and (
            (len(seen) >= unnamed_floor and votes.get(key, 0) <= 0)
            or names_company(text_of[key], company)
        )
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
