"""Project measured company name rows into the response contract."""

from __future__ import annotations

from api import rates
from api.companies.models import (
    Company,
    CompanyApplications,
    CompanyExtraction,
    CompanyOpen,
    CompanyPay,
    CompanyRepost,
    CompanyResponses,
    CurrencyRange,
    TimeToOutcome,
    _CompanyKeyed,
    _CompanyRow,
    _CurrencyRow,
    _OpenRow,
    _RepostRow,
    _ResponseRow,
    _StatusRow,
)
from api.companies.queries import APPLICATIONS_MIN, OPEN_MIN_CHECKED


def _bucket[R: _CompanyKeyed](rows: list[R]) -> dict[str, list[R]]:
    out: dict[str, list[R]] = {}
    for row in rows:
        out.setdefault(row.company_key, []).append(row)
    return out


def _quantile(value: float | None, n: int, min_sample: int) -> float | None:
    if value is None or n < min_sample:
        return None
    return round(value, 1)


def _response_block(row: _ResponseRow | None, min_sample: int) -> CompanyResponses | None:
    """Does this company reply, and how long does it take?

    RATES ARE ALMOST ALWAYS NULL HERE, and that is the honest answer rather than
    a defect. Of 1,283 companies with an application, 449 have two, 87 have
    five and TWO have thirty. At any floor that makes a proportion mean
    something, nearly every company is below it - so the numerator and
    denominator carry the information and the caller renders "2 of 7". A
    company with three applications and one reply does not have a 33% response
    rate; it has one reply.

    TIMING IS TRACKER-ONLY, and this is not a caveat about precision - the
    mail-derived number is not a measurement at all. For all 1,783 mail-derived
    applications, `applied_at` IS the first matched message's `sent_at`,
    exactly, with no exceptions. So "days from applying to the first outcome"
    computed over them measures the gap between the first mail and the first
    DECIDING mail, which is zero whenever a rejection arrives with no
    acknowledgement before it - a median of 0.0 days across 956 applications.
    Publishing that mixed with real dates is how an average becomes 33 days
    when the measurable median is 8.

    Median and p90 rather than a mean, because the tail is long: over the 200
    tracker applications with a clean duration the mean is 30.5 days and the
    median 7.9. The mean is describing the tail, not the wait.
    """
    if row is None:
        return None
    applications = row.applications
    if not applications:
        return None
    timed = row.timed_n
    return CompanyResponses(
        replied=rates.rate(row.replied, applications, min_sample),
        reached_outcome=rates.rate(row.with_outcome, applications, min_sample),
        days_to_first_outcome=TimeToOutcome(
            n=timed,
            # A median needs a sample as much as a proportion does. One timed
            # application produced a "median" of 205 days, which is not a
            # median - it is that one application wearing the word.
            median=_quantile(row.median_days, timed, min_sample),
            p90=_quantile(row.p90_days, timed, min_sample),
            below_floor=timed < min_sample,
            # Named so nobody reads this as covering every application.
            basis="tracker_dated_only",
        ),
    )


def _item(
    base: _CompanyRow,
    *,
    currency: list[_CurrencyRow],
    statuses: list[_StatusRow],
    open_row: _OpenRow | None,
    repost: _RepostRow | None,
    responses: _ResponseRow | None = None,
    min_sample: int = rates.DEFAULT_MIN_SAMPLE,
) -> Company:
    total = base.total_postings_seen
    applications = None
    if base.applications_n >= APPLICATIONS_MIN:
        applications = CompanyApplications(
            n=base.applications_n,
            last_applied_at=base.last_applied_at,
            statuses={r.status: r.n for r in statuses},
            responses=_response_block(responses, min_sample),
        )
    return Company(
        company_key=base.company_key,
        company_name=base.company_name,
        total_postings_seen=total,
        comp=CompanyPay(
            # The qualification travels WITH the number rather than in the
            # response-level caveats list, because a caveat one refactor away
            # from the datum it qualifies is a caveat that will eventually be
            # dropped. n_extracted is a count of what OUR extractor found; it
            # is not a count of what this employer published, and the two are
            # not separable here.
            measures="postings where our extractor found a pay range",
            not_="what this company chooses to disclose",
            n_extracted=base.comp_found,
            n_total=total,
            by_currency=[
                CurrencyRange(
                    currency=row.currency,
                    n=row.n,
                    min=row.min,
                    max=row.max,
                )
                for row in sorted(currency, key=lambda r: -r.n)
            ],
        ),
        # Named for what the extractor did, because that is all these three
        # can honestly claim. They sum to total_postings_seen.
        extraction=CompanyExtraction(
            measures="what our extractor did, per posting",
            found_pay=base.comp_found,
            ran_found_nothing=base.comp_ran_found_nothing,
            not_attempted=base.comp_not_attempted,
        ),
        applications=applications,
        open=(
            CompanyOpen(
                n_open=open_row.n_open,
                n_checked=open_row.n_checked,
                last_checked_at=open_row.last_checked_at,
            )
            if open_row and open_row.n_checked >= OPEN_MIN_CHECKED
            else None
        ),
        repost=(
            CompanyRepost(
                title=repost.title,
                url_count=repost.url_count,
                first_posted_at=repost.first_posted_at,
                last_posted_at=repost.last_posted_at,
                span_days=repost.span_days,
            )
            if repost
            else None
        ),
    )
