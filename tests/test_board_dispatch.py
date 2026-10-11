"""Dispatch board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import pytest

from core.fetching import board_common, boards, client


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://boards-api.greenhouse.io/v1/boards/spacex/jobs", "greenhouse"),
        ("https://api.lever.co/v0/postings/palantir?mode=json", "lever"),
        ("https://api.ashbyhq.com/posting-api/job-board/quora", "ashby"),
        (
            "https://ngc.wd1.myworkdayjobs.com/wday/cxs/ngc/Northrop_Grumman_External_Site/jobs",
            "workday",
        ),
        (
            "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md",
            "markdown",
        ),
        (
            "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
            "sheet_era",
        ),
        # The public Workday page is not the search endpoint.
        ("https://ngc.wd1.myworkdayjobs.com/Northrop_Grumman_External_Site", "sheet_era"),
        ("https://api.smartrecruiters.com/v1/companies/BoschGroup/postings", "smartrecruiters"),
        (
            "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions?siteNumber=CX_1",
            "oracle",
        ),
        ("https://apply.workable.com/api/v3/accounts/zego/jobs", "workable"),
        ("https://bloomberg.avature.net/careers/SearchJobs", "avature"),
        ("https://koch.avature.net/en_US/careers/SearchJobs", "avature"),
        # Two Sigma's own domain is Avature underneath, but nothing in the
        # URL says so; the source stores twosigma.avature.net, which redirects.
        ("https://careers.twosigma.com/careers/OpenRoles", "sheet_era"),
        # Eightfold serves from each tenant's own domain; the path is the mark.
        ("https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com", "eightfold"),
        ("https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com", "eightfold"),
        ("https://jobs.northropgrumman.com/careers?domain=ngc.com", "sheet_era"),
        (
            "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts?website-path=tiktok",
            "bytedance",
        ),
        (
            "https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts?website-path=en",
            "bytedance",
        ),
        ("https://lifeattiktok.com/search/7686350939411253509", "sheet_era"),
        ("https://careers-gdms.icims.com/jobs/search", "icims"),
        ("https://expleo-jobs-us-en.icims.com/jobs/search", "icims"),
        ("https://careers.spiritaero.com/api/jobs", "jibe"),
        # A posting on a portal, or a career site's own page, is not its listing.
        ("https://careers-gdms.icims.com/jobs/75243/job", "sheet_era"),
        ("https://careers.spiritaero.com/jobs", "sheet_era"),
        # The public pages of those three are not their APIs.
        ("https://jobs.smartrecruiters.com/BoschGroup", "sheet_era"),
        ("https://apply.workable.com/zego/", "sheet_era"),
        ("https://www.amazon.jobs/en/search.json", "amazon"),
        # A posting page on the same host is not the search.
        ("https://www.amazon.jobs/en/jobs/10567672/data-center-operation-technician", "sheet_era"),
        (
            "https://textron.taleo.net/careersection/textron/jobsearch.ftl?lang=en&portal=8140753014",
            "taleo",
        ),
        # A Taleo posting page and the search endpoint itself are not boards.
        ("https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=342919", "sheet_era"),
        (
            "https://textron.taleo.net/careersection/rest/jobboard/searchjobs?portal=8140753014",
            "sheet_era",
        ),
        ("https://jobs.apple.com/api/v1/search", "apple"),
        # Apple's public search page is not its API.
        ("https://jobs.apple.com/en-us/search", "sheet_era"),
        ("https://www-api.ibm.com/search/api/v2", "ibm"),
        ("https://api-higher.gs.com/gateway/api/v1/graphql", "goldman"),
        # IBM's and Goldman's public search pages are not their APIs.
        ("https://www.ibm.com/careers/search", "sheet_era"),
        ("https://higher.gs.com/results", "sheet_era"),
        ("https://jobs.l3harris.com/services/rss/job/", "successfactors"),
        # A Career Site Builder search page is not its feed.
        ("https://jobs.l3harris.com/search/?q=", "sheet_era"),
    ],
)
def test_kind_is_read_off_the_url(url, expected):
    assert boards.kind(url) == expected


def test_boards_that_never_name_a_company_are_the_ones_that_need_one():
    assert {
        "lever",
        "ashby",
        "workday",
        "oracle",
        "workable",
        "taleo",
        "apple",
        "bytedance",
        "icims",
        "ibm",
        "goldman",
        "amazon",
        "successfactors",
        "eightfold",
        "avature",
    } == boards.NEEDS_COMPANY
    # Every board that needs a company is one whose absence closes a posting.
    assert boards.NEEDS_COMPANY <= boards.AUTHORITATIVE


def test_unknown_urls_fall_through_to_the_sheet_era_fetcher(monkeypatch):
    seen = []
    monkeypatch.setattr(boards, "fetch_job_postings", lambda url: seen.append(url) or [])
    assert boards.fetch_listings("https://airtable.com/appX/shrY", "Acme") == []
    assert seen == ["https://airtable.com/appX/shrY"]


def test_a_nul_byte_in_a_posting_is_dropped_before_it_reaches_jsonb():
    p = board_common.posting_from_listing(
        "Northwell",
        "Nurse\x00",
        ["NY"],
        "https://x.test/1",
        0,
        raw={
            "Id": "1",
            "ShortDescriptionStr": "care\x00giver",
            "secondaryLocations": [{"Name": "a\x00b"}],
        },
        description="text\x00here",
    )
    assert p is not None and p.title == "Nurse" and p.description == "texthere"
    assert p.raw == {
        "Id": "1",
        "ShortDescriptionStr": "caregiver",
        "secondaryLocations": [{"Name": "ab"}],
    }


def test_an_unpaced_host_is_not_slowed(monkeypatch):
    slept = []
    monkeypatch.setattr(client.time, "sleep", slept.append)
    client.set_pace({})
    client.pace("https://apply.workable.com/api/v3/accounts/a/jobs")
    client.pace("https://apply.workable.com/api/v3/accounts/a/jobs")
    assert slept == []
