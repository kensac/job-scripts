"""Select posting detail resolvers and expose their stable contract."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import urlparse

from core.fetching.ats.apple import Apple
from core.fetching.ats.ashby import Ashby
from core.fetching.ats.brassring import BrassRing
from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    _session,
    ashby_text,
    clean_html,
    goldman_place,
    goldman_text,
    greenhouse_text,
    lever_text,
    oracle_text,
    source_posting_url,
    workable_text,
)
from core.fetching.ats.common import (
    join_posting_sections as join,
)
from core.fetching.ats.common import (
    parse_posted_date as _iso_date,
)
from core.fetching.ats.eightfold import EIGHTFOLD, Eightfold
from core.fetching.ats.gem import Gem
from core.fetching.ats.goldman import Goldman
from core.fetching.ats.greenhouse import Greenhouse
from core.fetching.ats.ibm import Ibm
from core.fetching.ats.icims import ICims
from core.fetching.ats.lever import Lever
from core.fetching.ats.oracle import Oracle
from core.fetching.ats.rippling import Rippling
from core.fetching.ats.smartrecruiters import SmartRecruiters
from core.fetching.ats.workable import Workable
from core.fetching.ats.workday import Workday
from core.fetching.hosts import EIGHTFOLD_PCSX, EIGHTFOLD_V2

__all__ = [
    "EIGHTFOLD",
    "RESOLVERS",
    "UNSUPPORTED",
    "Apple",
    "Ashby",
    "AtsResolver",
    "AtsResult",
    "BrassRing",
    "Eightfold",
    "Gem",
    "Goldman",
    "Greenhouse",
    "ICims",
    "Ibm",
    "Lever",
    "Oracle",
    "Rippling",
    "SmartRecruiters",
    "Status",
    "Workable",
    "Workday",
    "_iso_date",
    "_session",
    "ashby_text",
    "canonicalize",
    "clean_html",
    "eightfold_listing_prefixes",
    "employer_canonical",
    "goldman_place",
    "goldman_text",
    "greenhouse_text",
    "is_ats_email_domain",
    "join",
    "lever_text",
    "oracle_text",
    "resolve",
    "resolve_listed",
    "source_posting_url",
    "workable_text",
]

logger = logging.getLogger(__name__)

RESOLVERS: list[AtsResolver] = [
    Greenhouse(),
    Lever(),
    Ashby(),
    SmartRecruiters(),
    Workday(),
    Oracle(),
    Workable(),
    Gem(),
    Rippling(),
    ICims(),
    Apple(),
    Goldman(),
    Ibm(),
    BrassRing(),
]


# Domains an applicant-tracking system sends MAIL from. This is NOT the same
# question as which postings we can resolve, and the two lists cannot be
# derived from each other in either direction.
#
# Postings differ from mail: Greenhouse posts on greenhouse.io and mails from
# greenhouse-mail.io; Workday posts on myworkdayjobs.com and mails from
# myworkday.com. Those two are the largest sources of applications in the
# corpus, so a rule that assumed the pair would silently miss most of it.
#
# And most entries below have NO resolver at all - we cannot read their
# postings and they are still applicant-tracking systems when they send mail.
#
# MEMBERSHIP IS EARNED, not assumed from a brand being well known. Each domain
# here has at least 8 messages in the corpus of which at least 75% are
# application LIFECYCLE mail - acknowledgement, rejection, info_request,
# assessment, interview, offer, closure - rather than outreach or marketing.
# The measured share is recorded beside each one so the next reader can
# re-check it rather than trust it.
#
# Four job boards were proposed for this list and are deliberately absent,
# because their mail is mostly marketing rather than news about an application
# you sent: untapped.io (20% lifecycle over 217 messages), ripplematch.com
# (21% over 186), hi.wellfound.com (62% over 85) and codesignal.com (71% over
# 56). Adding them would mark their marketing as near-proof of a real
# application, which is exactly the failure #211 removed for RippleMatch.
# app.bamboohr.com is 100% lifecycle but over only 4 messages, so it waits for
# evidence rather than joining on reputation.
_MAIL_ONLY_DOMAINS = (
    # Mail domain differs from the posting domain of a provider we do resolve.
    "greenhouse-mail.io",
    "myworkday.com",
    # Applicant-tracking systems with no resolver: we cannot read their
    # postings, and their mail is still about an application you sent.
    "successfactors.com",
    "jobvite.com",  # 100% lifecycle, n=35
    "candidates.workablemail.com",  # 100%, n=17
    "appreview.gem.com",  # 100%, n=8
    "applytojob.com",  # 100%, n=10
    "workflow.mail.us2.cloud.oracle.com",  # 100%, n=38
    "welcometothejungle.com",  # 93%, n=14
    # An assessment platform, included on the same evidence: an invitation to
    # a coding assessment is sent because an application exists.
    "hackerrankforwork.com",  # 92%, n=78
)


def is_ats_email_domain(domain: str | None) -> bool:
    """Did this mail come from an applicant-tracking system?

    Near-proof that an application is real: across 1,779 mail-derived
    applications, 98.9% of those whose first message came from an ATS domain
    were genuine, against 85.2% of those that did not.

    Its ABSENCE proves nothing, which is the important half. 46% of genuine
    applications are not ATS-sent, because plenty of employers mail from their
    own domain - Epic Games, MathWorks, Lockheed Martin, Morgan Stanley and
    Citadel all do. Gating on this would discard 884 applications to remove 131
    bad ones, 5.7 real losses per junk removal. Rank on it; never filter on it.
    """
    d = (domain or "").lower().strip().rstrip(".")
    if not d:
        return False
    known = [m for r in RESOLVERS for m in r.markers if "." in m]
    known.extend(_MAIL_ONLY_DOMAINS)
    return any(d == k or d.endswith("." + k) for k in known)


# amazon.jobs serves one posting under many spellings: its board lists
# /en/jobs/10567672/data-center-operation-technician, the aggregators link
# /jobs/10567672/apply, and other locales and the bare id work too. Each
# redirects to /<locale>/jobs/<id>/<slug> (measured 2026-10-05), and the slug
# cannot be had without a request, so the id under /en/ is the one spelling
# every form reduces to. account.amazon.jobs/jobs/<id>/apply is the board's
# own apply link for the same id. Not a resolver: a resolver's markers also
# make its host an ATS mail domain (is_ats_email_domain), and amazon.jobs is
# an employer's site, not an applicant-tracking system.
_AMAZON_JOB = re.compile(r"^/(?:[A-Za-z-]+/)*jobs/(\d+)(?:/|$)")


# Google's careers feed links careers.google.com/jobs/results/<id>-<slug>/,
# which redirects to www.google.com/about/careers/applications/jobs/results/
# with the same tail, and the aggregators link that path with the bare id (111
# of the 121 Google rows in production on 2026-10-16). The bare id answers the
# posting too, so it is the one spelling every form reduces to.
_GOOGLE_JOB = re.compile(r"^/(?:about/careers/applications/)?jobs/results/(\d+)(?:-[^/]*)?/?$")


def employer_canonical(url: str) -> str | None:
    """The one spelling of a posting on an employer's own careers site."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "amazon.jobs" or host.endswith(".amazon.jobs"):
        match = _AMAZON_JOB.match(parsed.path)
        return f"https://www.amazon.jobs/en/jobs/{match.group(1)}" if match else None
    if host in ("www.google.com", "google.com", "careers.google.com"):
        match = _GOOGLE_JOB.match(parsed.path)
        return (
            f"https://www.google.com/about/careers/applications/jobs/results/{match.group(1)}"
            if match
            else None
        )
    return None


def canonicalize(url: str) -> str | None:
    """Canonical clickable URL for a posting."""
    employer = employer_canonical(url)
    if employer:
        return employer
    for resolver in RESOLVERS:
        if not resolver.matches(url):
            continue
        try:
            return resolver.canonical(url)
        except Exception as exc:
            logger.debug(f"[{resolver.name}] canonicalization error {url}: {exc}")
            return None
    return None


def resolve(url: str) -> AtsResult:
    resolver = next((r for r in RESOLVERS if r.matches(url)), None)
    if resolver is None:
        return UNSUPPORTED
    return _logged(resolver, url, lambda: resolver.fetch(url))


def resolve_listed(url: str, listings_url: str) -> AtsResult:
    """An Eightfold posting's text, its tenant known by listings_url: the
    listing of a source on the posting's host, which a URL cannot name."""
    return _logged(EIGHTFOLD, url, lambda: EIGHTFOLD.fetch_listed(url, listings_url) or UNSUPPORTED)


def _logged(resolver: AtsResolver, url: str, fetch: Callable[[], AtsResult]) -> AtsResult:
    try:
        result = fetch()
    except Exception as exc:
        logger.debug(f"[{resolver.name}] resolver error {url}: {exc}")
        return AtsResult(Status.ERROR, source=resolver.name)
    if result.ok:
        logger.info(f"ATS hit [{resolver.name}]: {len(result.text or '')} chars from {url}")
    elif result.status is Status.GONE:
        logger.info(f"ATS reports posting gone [{resolver.name}]: {url}")
    return result


def eightfold_listing_prefixes(url: str) -> tuple[str, str] | None:
    """The listings URLs an Eightfold source on this posting's host starts
    with, or None when the URL is not shaped like an Eightfold posting. The
    caller finds the source; the resolver never guesses a host is Eightfold's."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not Eightfold._JOB.match(parsed.path):
        return None
    origin = f"https://{parsed.netloc.lower()}"
    return f"{origin}{EIGHTFOLD_PCSX}?", f"{origin}{EIGHTFOLD_V2}?"
