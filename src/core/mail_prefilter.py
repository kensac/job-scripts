"""Whether a message looks like it could be about a job application.

THIS IS A SIGNAL, NOT A GATE. Nothing is skipped because of it.

It began as a cost filter: 38,685 messages in the Takeout export, 21.7% of
them matching, so classifying only the matches would cost ~$3 batched instead
of ~$15. Kanishk's call was that $12 is not worth it for a one-time backfill,
and he is right - a filtered-out email is an outcome lost for good, because
the posting is closed and the thread is not coming back. Every other failure
in this pipeline is recoverable by re-running something. That one is not.

So the whole mailbox goes to the classifier and this stays for the three
things it is actually good at: ordering the sweep so likely job mail is
classified first, explaining in the admin view why a message was or was not
expected to matter, and measuring after the fact how much a cheap filter would
have missed - which is the only honest way to decide whether the ongoing feed
can ever use one as a gate.

The bias is still RECALL, because those measurements are only useful if the
rules err the same way a gate would have to.
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
