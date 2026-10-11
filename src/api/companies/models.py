"""Database row shapes and published company response models."""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from api.rates import Rate


# Query rows are keyed on the normalized company name. Keep their declared
# columns aligned with the SQL, so a missing field fails at the query boundary.
class _CompanyKeyed(BaseModel):
    company_key: str


class _CompanyRow(_CompanyKeyed):
    company_name: str
    total_postings_seen: int
    comp_found: int
    comp_ran_found_nothing: int
    comp_not_attempted: int
    applications_n: int
    last_applied_at: datetime.date | None
    total_names: int


class _CurrencyRow(_CompanyKeyed):
    currency: str | None
    n: int
    min: int | None
    max: int | None


class _StatusRow(_CompanyKeyed):
    status: str
    n: int


class _OpenRow(_CompanyKeyed):
    n_checked: int
    n_open: int
    last_checked_at: datetime.datetime


class _RepostRow(_CompanyKeyed):
    title: str
    url_count: int
    first_posted_at: datetime.datetime
    last_posted_at: datetime.datetime
    span_days: int


class _ResponseRow(_CompanyKeyed):
    applications: int
    replied: int
    with_outcome: int
    timed_n: int
    median_days: float | None
    p90_days: float | None


class _Count(BaseModel):
    c: int


class CurrencyRange(BaseModel):
    """What our extractor read, per currency it read it in.

    Currency is present on 452 of 11,442 extracted rows, so a null currency is
    the ordinary case and gets its own entry rather than being folded into USD:
    an amount whose currency was never captured is not a dollar figure.
    """

    currency: str | None
    n: int
    min: int | None
    max: int | None


class CompanyPay(BaseModel):
    """`measures` and `not` travel WITH the number rather than in the
    response-level caveats, because a caveat one refactor away from the datum
    it qualifies is a caveat that will eventually be dropped. A posting that
    stated pay we failed to read is indistinguishable from one that stated
    none, so `n_extracted` is a count of what we found, never of what this
    employer discloses.
    """

    measures: str
    # `not` is a keyword, so the field carries the trailing underscore and the
    # wire keeps the name it has always had.
    not_: str = Field(serialization_alias="not")
    n_extracted: int
    n_total: int
    by_currency: list[CurrencyRange]


class CompanyExtraction(BaseModel):
    """Named for what the extractor did, because that is all these three can
    honestly claim. They sum to total_postings_seen."""

    measures: str
    found_pay: int
    ran_found_nothing: int
    not_attempted: int


class TimeToOutcome(BaseModel):
    """How long this company took to decide something.

    `basis` is tracker_dated_only, and that is not a caveat about precision.
    For every mail-derived application `applied_at` IS the first matched
    message's sent_at, so a duration over those measures the gap between the
    first mail and the first DECIDING mail - zero whenever a rejection arrives
    with no acknowledgement before it.

    Median and p90 rather than a mean: over the 200 tracker applications with a
    clean duration the mean is 30.5 days and the median 7.9, so the mean is
    describing the tail rather than the wait. A median needs a sample as much
    as a proportion does, so below the floor both are null and `n` carries what
    there is.
    """

    n: int
    median: float | None
    p90: float | None
    below_floor: bool
    basis: str


class CompanyResponses(BaseModel):
    """Does this company reply, and how long does it take?

    RATES ARE ALMOST ALWAYS NULL HERE, and that is the honest answer rather
    than a defect: of 1,283 companies with an application, 449 have two, 87
    have five and TWO have thirty. The numerator and denominator carry the
    information and the caller renders "2 of 7". A company with three
    applications and one reply does not have a 33% response rate; it has one
    reply.

    A reply is any classified mail. `reached_outcome` counts only the mail that
    DECIDED something - an acknowledgement is a mail server saying it received
    the form, and counting it would report a 100% response rate for every
    company running an ATS.
    """

    replied: Rate
    reached_outcome: Rate
    days_to_first_outcome: TimeToOutcome


class CompanyApplications(BaseModel):
    """The calling user's applications under this name, counted from
    `applications` and not from the board: the board only knows postings
    tracked by hand, while `applications` also holds the ones only email knows
    about.

    `statuses` is still what the user typed on the board, so an application
    with no board row counts in `n` and in no status - a real gap between what
    mail knows and what he has recorded, left visible rather than reconciled
    away.
    """

    n: int
    last_applied_at: datetime.date | None
    statuses: dict[str, int]
    responses: CompanyResponses | None = None


class CompanyOpen(BaseModel):
    """The latest closed verdict per posting, the same instrument the board row
    and the per-job signal use. Null below OPEN_MIN_CHECKED, which is where
    "9 of 14 still answer" stops being worth a sentence.
    """

    n_open: int
    n_checked: int
    last_checked_at: datetime.datetime


class CompanyRepost(BaseModel):
    """One title repeated under several urls on a SINGLE source AT ONE
    LOCATION, more than signals.REPOST_MIN_SPAN_DAYS apart.

    Location is in the key because without it a chain listing one role across
    its estate reads as one enormous repost. This is the largest surviving
    group, so `url_count` is reported: a high count is an evergreen
    requisition, not a repost cycle.
    """

    title: str
    url_count: int
    first_posted_at: datetime.datetime
    last_posted_at: datetime.datetime
    span_days: int


class Company(BaseModel):
    """One company NAME, not one company. `company_key` is
    lower(btrim(company)) over free text, so spelling variants are separate
    rows and a distinct-name count is an upper bound on the number of real
    employers.

    `applications`, `open` and `repost` are null when the name is below the
    floor that would make them mean anything, and so is `responses` inside the
    first. The server stays the single authority on what is well powered; a
    caller renders a null block as nothing at all and never computes the floor
    itself.

    They were ABSENT rather than null until this was declared. Dropping them in
    a serialiser keeps that wire and erases the schema - pydantic derives it
    from the serialiser's return type, so the model becomes an open object,
    which is the generated type this phase exists to stop producing. So the
    null wins, and the consumers are checked instead.
    """

    company_key: str
    company_name: str
    total_postings_seen: int
    comp: CompanyPay
    extraction: CompanyExtraction
    applications: CompanyApplications | None = None
    open: CompanyOpen | None = None
    repost: CompanyRepost | None = None


class CompanyCoverage(BaseModel):
    """A company that has not replied may simply not have been READ yet.
    Silence is only evidence once the mailbox is classified, so the size of the
    backlog travels with the numbers that depend on it rather than being
    something a reader has to know to ask about."""

    messages_awaiting_classification: int
    min_sample: int


class CompanyList(BaseModel):
    """`total_names` counts what the lenses show rather than the whole catalog:
    same CTEs, same cuts, no page. `next_cursor` is an offset and says so."""

    items: list[Company]
    has_more: bool
    next_cursor: str | None
    coverage: CompanyCoverage
    total_names: int
    caveats: list[str]
    filters: dict[str, list[str]]
    filterable: list[str]
    sorts: list[dict[str, str]]
    sortable: list[str]
