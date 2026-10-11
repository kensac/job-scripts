"""Amazon board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse as _Resp

AMAZON_ROW = {
    "basic_qualifications": (
        "- Valid and active driver's license<br/>"
        "- Experience with computer hardware troubleshooting and repair"
    ),
    "business_category": "aws",
    "company_name": "Amazon Corporate Services Pty Ltd",
    "country_code": "AUS",
    "description": (
        "G'day! You've found an opportunity that could define your next chapter.<br/><br/>"
        "Applicants must be Australian citizens"
    ),
    "description_short": "G'day! You've found an opportunity that could define your next chapter.",
    "id_icims": "10567672",
    "job_category": "Operations, IT, & Support Engineering",
    "job_path": "/en/jobs/10567672/data-center-operation-technician",
    "location": "AU, VIC, Melbourne",
    "locations": [
        '{"normalizedStateName":"Victoria","normalizedCountryCode":"AUS","city":"Melbourne",'
        '"countryIso2a":"AU","type":"ONSITE","normalizedLocation":"Melbourne, Victoria, AUS",'
        '"location":"AU, VIC, Melbourne","region":"VIC"}'
    ],
    "normalized_location": "Melbourne, Victoria, AUS",
    "posted_date": "October  2, 2026",
    "preferred_qualifications": "- Experience in data center",
    "title": "Data Center Operation Technician ",
    "url_next_step": "https://account.amazon.jobs/jobs/10567672/apply",
}

AMAZON_URL = "https://www.amazon.jobs/en/search.json"


def _amazon_board(by_category: dict[str, int], countries: int | None = None, shift=()):
    """amazon.jobs as measured on 2026-10-05: at most 100 rows a request and
    none past row 10,000, each refused with HTTP 200 and an error; `hits`
    stops at 10,000; a facet is a list of one-key objects. A category in
    `shift` loses its first row after the first page, as a posting closing
    mid-read does, so every later page starts one row further on."""
    rows = {
        c: [{"title": f"{c} {i}", "job_path": f"/en/jobs/{c}-{i}/x"} for i in range(n)]
        for c, n in by_category.items()
    }
    total = sum(by_category.values())
    asked = []

    def get(url, params, **kw):
        asked.append(params)
        limit, offset = int(params["result_limit"]), int(params.get("offset", 0))
        if limit > 100:
            error = "Result limit cannot be greater than 100"
            return _Resp({"error": error, "hits": 0, "jobs": None})
        if offset + limit > 10_000:
            error = "Cannot return more than 10000 results at once"
            return _Resp({"error": error, "hits": 0, "jobs": None})
        category = params.get("category[]")
        found = rows[category] if category else [r for rs in rows.values() for r in rs]
        if category in shift and offset:
            found = found[1:]
        return _Resp(
            {
                "error": None,
                "hits": min(len(rows[category]) if category else total, 10_000),
                "jobs": found[offset : offset + limit],
                "facets": {
                    "category_facet": [{c: n} for c, n in by_category.items()],
                    "normalized_country_code_facet": [
                        {"USA": total if countries is None else countries}
                    ],
                },
            }
        )

    return get, asked


def test_amazon_reads_a_board_past_its_window_one_category_at_a_time(monkeypatch):
    """22,295 postings on 2026-10-05 against a search that stops at 10,000:
    paging the unfiltered search returns 10,000 and retires the rest."""
    get, asked = _amazon_board({"Software Development": 6_000, "Operations": 4_100, "Legal": 1})
    monkeypatch.setattr(boards._session, "get", get)
    out = boards.fetch_listings(AMAZON_URL, "Amazon")
    assert len({p.url for p in out}) == 10_101
    assert all(int(p["result_limit"]) <= 100 for p in asked)


def test_an_amazon_row_becomes_a_posting_with_its_full_text(monkeypatch):
    def get(url, params, **kw):
        return _Resp(
            {
                "error": None,
                "hits": 1,
                "jobs": [AMAZON_ROW],
                "facets": {
                    "category_facet": [{"Operations, IT, & Support Engineering": 1}],
                    "normalized_country_code_facet": [{"AUS": 1}],
                },
            }
        )

    monkeypatch.setattr(boards._session, "get", get)
    [p] = boards.fetch_listings(AMAZON_URL, "Amazon")
    assert (p.company, p.title, p.locations) == (
        "Amazon",
        "Data Center Operation Technician",
        ["Melbourne, Victoria, AUS"],
    )
    # The board's own slug link and an aggregator's /apply link are one
    # posting, so both are stored in the form every spelling reduces to.
    assert p.url == "https://www.amazon.jobs/en/jobs/10567672"
    assert p.date_posted == int(datetime.datetime(2026, 10, 2, tzinfo=datetime.UTC).timestamp())
    assert "Applicants must be Australian citizens" in p.description
    assert "Basic qualifications\n\n- Valid and active driver's license" in p.description
    assert "Preferred qualifications\n\n- Experience in data center" in p.description
    assert "description" not in p.raw and "basic_qualifications" not in p.raw
    assert p.raw["id_icims"] == "10567672"


@pytest.mark.parametrize(
    "by_category, countries, shift",
    [
        # A category at the window cannot be read whole.
        ({"Software Development": 10_050, "Legal": 1}, None, ()),
        # The country facet counts a posting no category holds.
        ({"Legal": 3}, 4, ()),
        # A posting closed mid-read and the later pages skipped an open one.
        ({"Legal": 150}, None, ("Legal",)),
    ],
)
def test_an_amazon_pull_that_cannot_prove_it_saw_everything_is_partial(
    monkeypatch, by_category, countries, shift
):
    get, _ = _amazon_board(by_category, countries, shift)
    monkeypatch.setattr(boards._session, "get", get)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(AMAZON_URL, "Amazon")
    assert raised.value.postings
