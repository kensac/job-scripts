"""Declared administrative mail inspection and analytics shapes."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel

from api.mail.match import CurrentMatch
from api.rates import Rate


class MailRow(BaseModel):
    """One message as the debug list shows it: what arrived, what the
    classifier last said, and what the matcher last did with it.

    Every field after `prefilter_reason` is the CURRENT row of an append-only
    log and can be null for its own reason - no classification yet, no match
    attempted, a match that found nothing. They are not interchangeable and
    `_where` filters on the difference."""

    id: int
    provider_message_id: str
    source: str
    from_email: str | None
    subject: str | None
    sent_at: datetime.datetime | None
    prefilter_hit: bool | None
    prefilter_reason: str | None
    kind: str | None
    confidence: str | None
    deadline_at: datetime.datetime | None
    deadline_inferred: bool | None
    model: str | None
    application_id: int | None
    method: str | None
    match_confidence: str | None
    rationale: str | None
    company_name: str | None
    title: str | None


class MailList(BaseModel):
    """The page, plus what it was sorted and filtered by.

    `sortable` and `filterable` are the vocabulary a client builds its controls
    from, and `sorts`/`filters` echo what was actually applied, so the page
    never has to duplicate the default or guess the keys."""

    sorts: list[dict[str, str]]
    sortable: list[str]
    rows: list[MailRow]
    page: int
    page_size: int
    total: int
    has_more: bool
    filters: dict[str, list[str]]
    filterable: list[str]


class ConsistencyGroup(BaseModel):
    """One body the classifier saw more than once, and the answers it gave."""

    sender: str | None
    subject: str | None
    copies: int
    kinds: list[str]
    example_message_id: int


class ConsistencyCoverage(BaseModel):
    """What the rate is a rate OF. Only a repeated body can be checked this
    way, so this is a canary over part of the corpus and never a measure of
    it - a rate quoted without this would describe a canary as a census."""

    messages_covered: int
    messages_classified: int
    min_copies: int
    min_body_chars: int


class ClassificationConsistency(BaseModel):
    groups: Rate
    messages: Rate
    coverage: ConsistencyCoverage
    worst: list[ConsistencyGroup]


class ClassificationCell(BaseModel):
    """What one model said, at one confidence, about how many messages - with
    the two things that go wrong inside a correct-looking answer: no company
    extracted, and a deadline the model inferred rather than read."""

    kind: str
    model: str | None
    confidence: str | None
    messages: int
    no_company: int
    inferred_deadlines: int


class MatchingCell(BaseModel):
    """How many messages of a kind the matcher settled, and by which tier.
    `never attempted` is a method value here because an absence in a
    distribution is a gap nobody can read."""

    kind: str
    method: str
    messages: int


class PrefilterCell(BaseModel):
    prefilter_hit: bool
    job_related: bool
    messages: int


class SenderDomainRow(BaseModel):
    domain: str | None
    messages: int
    matched: int


class SourceRow(BaseModel):
    source: str
    messages: int


class MailCorpus(BaseModel):
    """The whole import, deliberately NOT windowed: how much mail exists, when
    it starts, and how much nothing has looked at are properties of the
    import rather than of a slice."""

    messages: int
    unclassified: int
    oldest: datetime.datetime | None
    newest: datetime.datetime | None
    by_source: list[SourceRow]


class Population(BaseModel):
    """The denominator a section counts over, and what it leaves out. Each
    section below counts a DIFFERENT population and nothing in the rows says
    so, which invites a subtraction that means nothing."""

    messages: int
    excludes: list[str]


class DomainPopulation(Population):
    """The sender breakdown is also TRUNCATED, so it says how many domains it
    shows against how many exist."""

    domains_shown: int
    domains_total: int


class Populations(BaseModel):
    classification: Population
    matching: Population
    prefilter: Population
    sender_domains: DomainPopulation


class PrefilterSummary(BaseModel):
    """A rate the prefilter could never report about itself, and the reason it
    was kept as a signal rather than deleted: the mail a gate WOULD have
    dropped is the one unrecoverable failure."""

    cells: list[PrefilterCell]
    job_related_a_gate_would_have_dropped: int
    job_related_total: int


class MailAnalytics(BaseModel):
    window_days: int | None
    corpus: MailCorpus
    populations: Populations
    classification: list[ClassificationCell]
    matching: list[MatchingCell]
    prefilter: PrefilterSummary
    sender_domains: list[SenderDomainRow]


class AdminMailMessage(BaseModel):
    """The stored message, whole, for the one surface that may see everybody's.

    The columns were a `SELECT *`, so what reached the client was whatever the
    table happened to hold on the day. `body_html` is the markup as it
    arrived: the user-facing reader sanitises on read, this one does not,
    because an administrator debugging a classification needs what the
    classifier saw."""

    id: int
    user_id: int
    provider_message_id: str
    provider_thread_id: str | None
    thread_topic: str | None
    source: str
    from_email: str | None
    from_name: str | None
    to_emails: list[str] | None
    subject: str | None
    sent_at: datetime.datetime | None
    body_text: str | None
    body_html: str | None
    headers: dict[str, Any] | None
    prefilter_hit: bool | None
    prefilter_reason: str | None
    imported_at: datetime.datetime


class AdminMailEvent(BaseModel):
    """One classification, as written. The log is append-only, so a message
    with three of these was corrected twice and the last one is in force."""

    id: int
    message_id: int
    kind: str
    confidence: str | None
    occurred_at: datetime.datetime | None
    deadline_at: datetime.datetime | None
    deadline_inferred: bool
    detail: dict[str, Any] | None
    # Which machine wrote it; null means a person did, and actor_user_id says
    # which one.
    model: str | None
    actor_user_id: int | None
    created_at: datetime.datetime


class AdminMailMatch(BaseModel):
    """One match attempt, with the application it named. Append-only as well,
    so a tier that keeps being corrected stays visible as history rather than
    being papered over one row at a time."""

    id: int
    message_id: int
    application_id: int | None
    method: str
    confidence: str | None
    rationale: str | None
    actor_user_id: int | None
    created_at: datetime.datetime
    company_name: str | None
    title: str | None
    job_id: int | None


class AdminMailDetail(BaseModel):
    message: AdminMailMessage
    events: list[AdminMailEvent]
    matches: list[AdminMailMatch]
    # What tier 1 would see, so a missed exact-link match can be diagnosed
    # without re-running the matcher and guessing at why.
    canonical_urls: list[str]


class MatchOverride(BaseModel):
    application_id: int | None
    # WHICH no-match this is, when application_id is null. Without it every
    # admin no-match was recorded as method='manual' with a null application,
    # which the unmatched predicate reads as a matcher FAILURE - so an admin
    # who correctly decided a recruiter approach belongs to no application put
    # it straight back into the queue of things needing attention, and nothing
    # said so. `not_an_application` is not `unmatched`: deliberately attached
    # to nothing versus looked and found nothing, a distinction that has now
    # mattered on six surfaces.
    #
    # Default keeps the old behaviour for callers that do not send it: a
    # no-match with no reason given is a failure to find one, which is the
    # weaker claim and the safe one.
    outcome: Literal["no_application_found", "not_an_application"] = "no_application_found"


class MatchOverridden(BaseModel):
    """The match now in force, which is what the append produced rather than
    what was asked for - a refusal is recorded as the matcher's own refusal
    method, so agreeing with the matcher looks like agreeing with it."""

    ok: bool
    current: CurrentMatch | None
