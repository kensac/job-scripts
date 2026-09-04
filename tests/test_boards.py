"""core.boards: the listing fetchers, over rows copied from the live feeds.

Every excerpt below is a verbatim row from the board it names, fetched on
2026-09-04, so the fixture is the producer's shape rather than an expectation
of it. The ATS bodies are trimmed to the keys the fetcher reads plus the ones
that sat beside them.
"""

from __future__ import annotations

import datetime

import pytest

from core import boards

NOW = datetime.datetime(2026, 9, 4, 12, tzinfo=datetime.UTC)

# speedyapply/2027-SWE-College-Jobs NEW_GRAD_USA.md: link in its own column, age.
SPEEDY = """
### FAANG+

| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://www.tiktok.com"><strong>TikTok</strong></a> | Machine Learning Engineer Graduate - E-Commerce Knowledge Graph - 2027 Start | San Jose, CA | $202k/yr | <a href="https://lifeattiktok.com/search/7679156878833682693"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply" width="70"/></a> | 5d |

### Other

| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <a href="https://www.amazon.jobs"><strong>Amazon</strong></a> | Software Dev Engineer I - Graviton Software - Annapurna Labs | Austin, TX | <a href="https://www.amazon.jobs/jobs/10526808/apply?utm_source=speedyapply"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply" width="70"/></a> | 2mo |
"""

# jobright-ai/2026-Software-Engineer-New-Grad README.md: link inside the title,
# a day without a year, and ↳ for "same company as above".
JOBRIGHT = """
| Company | Job Title | Location | Work Model | Date Posted |
| ----- | --------- |  --------- | ---- | ------- |
| **[Cognizant](https://www.cognizant.com)** | **[Full Stack Software Developer](https://jobright.ai/jobs/info/6a99df4fad752e2ad55029c0?utm_campaign=Software%20Engineering&utm_source=1103)** | Plano, TX, United States | Hybrid | Sep 03 |
| ↳ | **[Full Stack Software Developer](https://jobright.ai/jobs/info/6a4d8491d27b2c4dda9b7f8c?utm_campaign=Software%20Engineering&utm_source=1103)** | United States | Remote | Sep 03 |
"""

# vanshb03/New-Grad-2027 README.md: several locations in one cell, and a 🔒
# row whose link column carries no link because the posting closed.
VANSH = """
| Company | Role | Location | Application/Link | Date Posted |
| --- | --- | --- | :---: | :---: |
| **Chicago Trading Company** | New Grad 2027: Associate Engineer | Chicago, IL</br>New York, NY | <a href="https://job-boards.greenhouse.io/ctccampusboard/jobs/4709991005?utm_source=vansh"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Aug 01 |
| **Fidelity Investments** | Software Engineer | Westlake, TX</br>Durham, NC | 🔒 | Jul 31 |
"""


def test_markdown_reads_the_columns_from_the_header():
    speedy = boards.parse_markdown(SPEEDY)
    assert [(p.company, p.title, p.locations) for p in speedy] == [
        (
            "TikTok",
            "Machine Learning Engineer Graduate - E-Commerce Knowledge Graph - 2027 Start",
            ["San Jose, CA"],
        ),
        ("Amazon", "Software Dev Engineer I - Graviton Software - Annapurna Labs", ["Austin, TX"]),
    ]
    assert speedy[0].url == "https://lifeattiktok.com/search/7679156878833682693"
    # The tracking parameter is gone from the key and kept on the raw URL.
    assert speedy[1].url == "https://www.amazon.jobs/jobs/10526808/apply"
    assert speedy[1].raw_url.endswith("?utm_source=speedyapply")
    # Two tables with different column counts in one file, each read from its
    # own header: the second has no Salary column and the link still lands.

    jobright = boards.parse_markdown(JOBRIGHT)
    assert [(p.company, p.title) for p in jobright] == [
        ("Cognizant", "Full Stack Software Developer"),
        ("Cognizant", "Full Stack Software Developer"),
    ]
    assert jobright[1].url == "https://jobright.ai/jobs/info/6a4d8491d27b2c4dda9b7f8c"

    vansh = boards.parse_markdown(VANSH)
    assert [(p.company, p.locations) for p in vansh] == [
        ("Chicago Trading Company", ["Chicago, IL", "New York, NY"])
    ]
    assert vansh[0].url == "https://job-boards.greenhouse.io/ctccampusboard/jobs/4709991005"


def test_markdown_rejects_rows_it_cannot_place():
    # A closed row has no posting link; a table without a Company header has
    # no columns to read; a row before any header belongs to nothing.
    closed_only = "\n".join(line for line in VANSH.splitlines() if "Chicago" not in line)
    assert boards.parse_markdown(closed_only) == []
    assert (
        boards.parse_markdown(
            '| Name | Link |\n|---|---|\n| Acme | <a href="https://x.test/1">go</a> |'
        )
        == []
    )
    assert (
        boards.parse_markdown('| Acme | Engineer | NYC | <a href="https://x.test/1">go</a> | 5d |')
        == []
    )


def test_posted_ts_reads_every_form_the_boards_write():
    day = lambda y, m, d: int(datetime.datetime(y, m, d, tzinfo=datetime.UTC).timestamp())
    assert boards.posted_ts("Sep 03", NOW) == day(2026, 9, 3)
    assert boards.posted_ts("Sep 3, 2025", NOW) == day(2025, 9, 3)
    # A day without a year that would be in the future is last year's.
    assert boards.posted_ts("Dec 25", NOW) == day(2025, 12, 25)
    assert boards.posted_ts("5d", NOW) == int(NOW.timestamp()) - 5 * 86400
    assert boards.posted_ts("2mo", NOW) == int(NOW.timestamp()) - 60 * 86400
    assert boards.posted_ts("Posted Today", NOW) == int(NOW.timestamp())
    assert boards.posted_ts("Posted 14 Days Ago", NOW) == int(NOW.timestamp()) - 14 * 86400
    # Older than the window is unknown, not "31 days".
    assert boards.posted_ts("Posted 30+ Days Ago", NOW) == 0
    assert boards.posted_ts("", NOW) == 0
    assert boards.posted_ts("Feb 30", NOW) == 0


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body

    @property
    def text(self):
        return self.body


def test_greenhouse_names_the_company_from_the_board(monkeypatch):
    # boards-api.greenhouse.io/v1/boards/andurilindustries/jobs, first job.
    body = {
        "jobs": [
            {
                "absolute_url": "https://boards.greenhouse.io/andurilindustries/jobs/4802172007?gh_jid=4802172007",
                "company_name": "Anduril Industries",
                "first_published": "2025-08-11T13:41:15-04:00",
                "id": 4802172007,
                "location": {
                    "name": "Costa Mesa, California, United States; Fort Collins, Colorado, United States"
                },
                "title": "2026 Early Career Electrical Engineer",
                "updated_at": "2026-09-02T20:46:09-04:00",
            }
        ]
    }
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp(body))
    (p,) = boards.fetch_listings(
        "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs", company="ignored"
    )
    assert p.company == "Anduril Industries"
    assert p.locations == [
        "Costa Mesa, California, United States",
        "Fort Collins, Colorado, United States",
    ]
    assert p.url == "https://boards.greenhouse.io/andurilindustries/jobs/4802172007"
    assert p.date_posted == int(
        datetime.datetime.fromisoformat("2025-08-11T13:41:15-04:00").timestamp()
    )


def test_lever_and_ashby_take_the_company_from_the_source(monkeypatch):
    lever = [
        {
            "categories": {
                "allLocations": ["London, United Kingdom"],
                "location": "London, United Kingdom",
            },
            "createdAt": 1711403416463,
            "hostedUrl": "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c",
            "text": "Administrative Business Partner",
        }
    ]
    ashby = {
        "apiVersion": "1",
        "jobs": [
            {
                "isListed": True,
                "jobUrl": "https://jobs.ashbyhq.com/quora/b2462dc7-08d7-4060-8ceb-9a1fa16615fb",
                "location": "Remote - Multiple Locations",
                "publishedAt": "2026-05-18T21:03:00.213+00:00",
                "secondaryLocations": [{"location": "United States"}, {"location": "Canada"}],
                "title": "Detection & CorpSec Engineer (Remote)",
            },
            {"isListed": False, "jobUrl": "https://jobs.ashbyhq.com/quora/x", "title": "Hidden"},
        ],
    }
    bodies = {"api.lever.co": lever, "api.ashbyhq.com": ashby}
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp(bodies[url.split("/")[2]]))
    (p,) = boards.fetch_listings("https://api.lever.co/v0/postings/palantir?mode=json", "Palantir")
    assert (p.company, p.title, p.date_posted) == (
        "Palantir",
        "Administrative Business Partner",
        1711403416,
    )
    (q,) = boards.fetch_listings("https://api.ashbyhq.com/posting-api/job-board/quora", "Quora")
    assert q.company == "Quora"
    assert q.locations == ["Remote - Multiple Locations", "United States", "Canada"]


def test_workday_pages_until_the_total_and_builds_the_public_url(monkeypatch):
    posts = []

    def post(url, json, **kw):
        posts.append((url, json))
        page = {
            0: [
                {
                    "externalPath": "/job/USA---NAS-JRB-New-Orleans-LA/F-18-General-Mechanic_JR2026517674-2",
                    "locationsText": "USA - NAS JRB New Orleans, LA",
                    "postedOn": "Posted 14 Days Ago",
                    "title": "F-18 General Mechanic",
                },
                {
                    "externalPath": "/job/x/Associate-Software-Engineer_JR1",
                    "locationsText": "Seattle, WA",
                    "postedOn": "Posted 30+ Days Ago",
                    "title": "Associate Software Engineer",
                },
            ],
            2: [
                {
                    "externalPath": "/job/y/Software-Engineer_JR2",
                    "locationsText": "Everett, WA",
                    "postedOn": "Posted Today",
                    "title": "Software Engineer",
                }
            ],
        }[json["offset"]]
        return _Resp({"total": 3, "jobPostings": page})

    monkeypatch.setattr(boards._session, "post", post)
    out = boards.fetch_listings(
        "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs?searchText=new+grad",
        "Boeing",
    )
    assert [p.title for p in out] == [
        "F-18 General Mechanic",
        "Associate Software Engineer",
        "Software Engineer",
    ]
    assert out[0].url == (
        "https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS/job/USA---NAS-JRB-New-Orleans-LA/"
        "F-18-General-Mechanic_JR2026517674-2"
    )
    assert out[1].date_posted == 0
    assert all(p.company == "Boeing" for p in out)
    # The search rode in the body, not the URL, and the second page started
    # where the first ended.
    assert [u for u, _ in posts] == [
        "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs"
    ] * 2
    assert [(j["offset"], j["searchText"]) for _, j in posts] == [(0, "new grad"), (2, "new grad")]


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
    ],
)
def test_kind_is_read_off_the_url(url, expected):
    assert boards.kind(url) == expected


def test_boards_that_never_name_a_company_are_the_ones_that_need_one():
    assert {"lever", "ashby", "workday"} == boards.NEEDS_COMPANY


def test_unknown_urls_fall_through_to_the_sheet_era_fetcher(monkeypatch):
    seen = []
    monkeypatch.setattr(boards, "fetch_job_postings", lambda url: seen.append(url) or [])
    assert boards.fetch_listings("https://airtable.com/appX/shrY", "Acme") == []
    assert seen == ["https://airtable.com/appX/shrY"]
