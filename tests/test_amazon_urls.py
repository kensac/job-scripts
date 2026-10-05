"""One amazon.jobs posting under every spelling the catalog meets.

The board lists /en/jobs/<id>/<slug> and the aggregators link
/jobs/<id>/apply; both shapes below are copied from those feeds on 2026-10-05
(the board's search.json `job_path`, speedyapply's README link).
"""

from __future__ import annotations

import pytest

from api.mail.match import canonical_urls
from core.fetching import ats
from core.fetching.forms import posting_urls
from core.fetching.urls import normalize_url

CANONICAL = "https://www.amazon.jobs/en/jobs/10567672"


@pytest.mark.parametrize(
    "url",
    [
        # The board's own listing.
        "https://www.amazon.jobs/en/jobs/10567672/data-center-operation-technician",
        # An aggregator's link, with its tracking parameter.
        "https://www.amazon.jobs/jobs/10567672/apply?utm_source=speedyapply",
        "https://www.amazon.jobs/jobs/10567672",
        "https://www.amazon.jobs/en/jobs/10567672",
        "https://www.amazon.jobs/de/jobs/10567672/data-center-operation-technician",
        "https://www.amazon.jobs/en-gb/jobs/10567672/data-center-operation-technician/",
        "https://amazon.jobs/en/jobs/10567672/data-center-operation-technician",
        # The board row's url_next_step.
        "https://account.amazon.jobs/jobs/10567672/apply",
    ],
)
def test_every_spelling_of_an_amazon_posting_is_one_url(url):
    assert normalize_url(url) == CANONICAL
    assert posting_urls(url) == [CANONICAL]


def test_different_amazon_postings_stay_different():
    assert normalize_url("https://www.amazon.jobs/jobs/10526808/apply") != CANONICAL


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.jobs/en/search.json",
        "https://www.amazon.jobs/en/teams/aws",
        # Not Amazon's host, whatever the path says.
        "https://notamazon.jobs/en/jobs/10567672/x",
    ],
)
def test_what_is_not_an_amazon_posting_is_left_alone(url):
    assert ats.canonicalize(url) is None


def test_mail_linking_an_aggregator_spelling_matches_the_board_row():
    body = "Thanks for applying: https://www.amazon.jobs/jobs/10567672/apply?utm_source=x."
    assert canonical_urls(body) == {CANONICAL}
    assert ats.canonicalize(
        "https://www.amazon.jobs/en/jobs/10567672/data-center-operation-technician"
    ) in canonical_urls(body)


def test_amazon_jobs_is_not_an_ats_mail_domain():
    """Canonicalising the host must not mark Amazon's own mail as sent by an
    applicant-tracking system, which a resolver's markers would."""
    assert not ats.is_ats_email_domain("amazon.jobs")
