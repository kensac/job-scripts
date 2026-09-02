"""Decide cheaply whether a message could possibly be about a job application.

38,685 messages in the Takeout export, and the .olm archives are several times
larger again. Classifying all of them with a model is affordable but wasteful,
so a deterministic pass runs first.

The bias is RECALL, deliberately and lopsidedly. A missed email is an outcome
lost forever - the posting is gone, the thread is not coming back, and nothing
downstream can recover it. A false positive costs one cheap classification that
the model then rejects. So this says yes on weak evidence, and every rule here
should be read as "could this conceivably be about a job", never "is it".

The verdict is stored on the row rather than applied and discarded, because
this WILL be widened once real misses are found, and a stored verdict lets a
widening re-sweep only what it previously dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Hosts that send application mail. The ATS markers are lifted from
# core.ats rather than restated, so a resolver added there is picked up here.
from core.ats import RESOLVERS

# Domains no ATS resolver covers, either because we never fetch their postings
# or because the mail comes from a different host than the listing does.
_EXTRA_ATS_DOMAINS = (
    "greenhouse-mail.io",
    "hire.lever.co",
    "myworkday.com",
    "workablemail.com",
    "jobvite.com",
    "taleo.net",
    "successfactors.com",
    "avature.net",
    "eightfold.ai",
    "phenompeople.com",
    "recruitee.com",
    "teamtailor-mail.com",
    "breezy.hr",
    "workable.com",
    "bamboohr.com",
    "rippling.com",
)

# Phrases that appear in application correspondence across employers. Matched
# on the subject AND the body: a rejection often has a neutral subject
# ("Update on your application") and the substance in the body.
_PHRASES = (
    "your application",
    "application for",
    "applied for",
    "thank you for applying",
    "thanks for applying",
    "we received your application",
    "application received",
    "application status",
    "update on your application",
    "moving forward",
    "move forward with other candidates",
    "not moving forward",
    "unfortunately",
    "we regret",
    "regret to inform",
    "interview",
    "phone screen",
    "recruiter screen",
    "hiring manager",
    "online assessment",
    "coding challenge",
    "take-home",
    "take home assessment",
    "hackerrank",
    "codesignal",
    "codility",
    "karat",
    "availability",
    "schedule a time",
    "offer letter",
    "we would like to invite",
    "next steps",
    "talent acquisition",
    "recruiting team",
    "candidate",
    "job opportunity",
    "position at",
    "role at",
)

# Careers/job hosts that show up as links in application mail even when the
# sender domain is the employer's own.
_URL_MARKERS = (
    "/careers",
    "/jobs/",
    "job-boards",
    "jobs.",
    "careers.",
    "boards.",
    "apply.",
)

_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)


def _ats_domains() -> tuple[str, ...]:
    markers: list[str] = []
    for resolver in RESOLVERS:
        markers.extend(m for m in resolver.markers if "=" not in m)
    return tuple(dict.fromkeys([*markers, *_EXTRA_ATS_DOMAINS]))


ATS_DOMAINS = _ats_domains()


@dataclass(frozen=True)
class Verdict:
    hit: bool
    reason: str


def _domain(address: str | None) -> str:
    if not address or "@" not in address:
        return ""
    return address.rsplit("@", 1)[-1].strip().strip(">").lower()


def looks_job_related(
    *,
    from_email: str | None,
    subject: str | None,
    body: str | None,
) -> Verdict:
    """Cheap, generous, and explains itself.

    The reason string is stored so that a later widening can tell WHY something
    was kept, and so a human reviewing false positives can see which rule is
    too loose rather than guessing at the whole set.
    """
    domain = _domain(from_email)
    for ats in ATS_DOMAINS:
        if domain.endswith(ats) or ats in domain:
            return Verdict(True, f"ats_domain:{ats}")

    haystack = f"{subject or ''}\n{body or ''}".lower()
    for phrase in _PHRASES:
        if phrase in haystack:
            return Verdict(True, f"phrase:{phrase}")

    for url in _URL_RE.findall(body or ""):
        lowered = url.lower()
        for marker in _URL_MARKERS:
            if marker in lowered:
                return Verdict(True, f"url:{marker}")

    return Verdict(False, "no_signal")
