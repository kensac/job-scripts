"""core.fetching.boards: the listing fetchers, over rows copied from the live feeds.

Every excerpt below is a verbatim row from the board it names, fetched on
2026-09-04, so the fixture is the producer's shape rather than an expectation
of it. The ATS bodies are trimmed to the keys the fetcher reads plus the ones
that sat beside them.
"""

from __future__ import annotations

import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from core.fetching import boards

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
                "content": "&lt;p&gt;Build &amp;amp; test avionics.&lt;/p&gt;",
            }
        ]
    }
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or _Resp(body))
    (p,) = boards.fetch_listings(
        "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs", company="ignored"
    )
    # One call, with the text: nothing downstream needs to fetch the posting.
    assert asked == [
        "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs?content=true"
    ]
    assert p.description == (
        "2026 Early Career Electrical Engineer\n\n"
        "Costa Mesa, California, United States; Fort Collins, Colorado, United States\n\n"
        "Build & test avionics."
    )
    assert p.raw is not None and p.raw["id"] == 4802172007 and "content" not in p.raw
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
            "description": "<p>Support the team.</p>",
            "lists": [{"text": "What you bring", "content": "<li>Calm</li>"}],
            "additional": "",
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
                "descriptionHtml": "<p>Hunt threats.</p>",
                "compensation": {"compensationTierSummary": "$150K to $200K"},
            },
            {"isListed": False, "jobUrl": "https://jobs.ashbyhq.com/quora/x", "title": "Hidden"},
        ],
    }
    bodies = {"api.lever.co": lever, "api.ashbyhq.com": ashby}
    asked: list[str] = []
    monkeypatch.setattr(
        boards._session,
        "get",
        lambda url, **kw: asked.append(url) or _Resp(bodies[url.split("/")[2]]),
    )
    (p,) = boards.fetch_listings("https://api.lever.co/v0/postings/palantir?mode=json", "Palantir")
    assert (p.company, p.title, p.date_posted) == (
        "Palantir",
        "Administrative Business Partner",
        1711403416,
    )
    # The same text the Lever resolver assembles: title, location, body, lists.
    assert p.description == (
        "Administrative Business Partner\n\nLondon, United Kingdom\n\n"
        "Support the team.\n\nWhat you bring\n\nCalm"
    )
    assert p.raw is not None and "description" not in p.raw and "lists" not in p.raw
    (q,) = boards.fetch_listings("https://api.ashbyhq.com/posting-api/job-board/quora", "Quora")
    assert q.company == "Quora"
    assert q.locations == ["Remote - Multiple Locations", "United States", "Canada"]
    assert (
        asked[-1] == "https://api.ashbyhq.com/posting-api/job-board/quora?includeCompensation=true"
    )
    assert q.description == (
        "Detection & CorpSec Engineer (Remote)\n\nRemote - Multiple Locations\n\n"
        "$150K to $200K\n\nHunt threats."
    )


def test_workday_pages_until_the_first_pages_total_and_builds_the_public_url(monkeypatch):
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
            3: [
                {
                    "externalPath": "/job/z/Structures-Engineer_JR3",
                    "locationsText": "Seattle, WA",
                    "postedOn": "Posted Today",
                    "title": "Structures Engineer",
                }
            ],
        }[json["offset"]]
        # As the live API does: the count rides on the first page only, and
        # every later page says 0 (Boeing, 2026-10-05: 752, then 0, then 0).
        return _Resp({"total": 4 if json["offset"] == 0 else 0, "jobPostings": page})

    monkeypatch.setattr(boards._session, "post", post)
    out = boards.fetch_listings(
        "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs?searchText=new+grad",
        "Boeing",
    )
    assert [p.title for p in out] == [
        "F-18 General Mechanic",
        "Associate Software Engineer",
        "Software Engineer",
        "Structures Engineer",
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
    ] * 3
    assert [(j["offset"], j["searchText"]) for _, j in posts] == [
        (0, "new grad"),
        (2, "new grad"),
        (3, "new grad"),
    ]


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


def test_smartrecruiters_pages_by_offset_and_names_the_company(monkeypatch):
    asked = []

    def get(url, **kw):
        asked.append(url)
        offset = int(url.split("offset=")[1])
        content = {
            0: [
                {
                    "id": "744000147613789",
                    "name": "Software Engineer",
                    "company": {"identifier": "BoschGroup", "name": "Bosch Group"},
                    "releasedDate": "2026-09-05T00:02:29.676Z",
                    "location": {
                        "city": "Guadalajara",
                        "region": "Jal.",
                        "country": "mx",
                        "fullLocation": "Guadalajara, Jal., Mexico",
                    },
                },
            ],
            1: [
                {
                    "id": "87619425",
                    "name": "Software Developer- Full Stack",
                    "company": {"identifier": "Zoro", "name": "Zoro"},
                    "releasedDate": "2015-12-03T15:03:39.000Z",
                    "location": {"city": "Buffalo Grove", "region": "IL", "country": "us"},
                },
            ],
        }[offset]
        return _Resp({"offset": offset, "limit": 100, "totalFound": 2, "content": content})

    monkeypatch.setattr(boards._session, "get", get)
    out = boards.fetch_listings("https://api.smartrecruiters.com/v1/companies/BoschGroup/postings")
    assert [(p.company, p.title, p.locations) for p in out] == [
        ("Bosch Group", "Software Engineer", ["Guadalajara, Jal., Mexico"]),
        ("Zoro", "Software Developer- Full Stack", ["Buffalo Grove, IL, us"]),
    ]
    assert out[0].url == "https://jobs.smartrecruiters.com/BoschGroup/744000147613789"
    assert out[0].date_posted == 1788566549
    assert [u.split("?")[1] for u in asked] == ["limit=100&offset=0", "limit=100&offset=1"]


def test_oracle_reads_the_site_off_the_url_and_pages_by_offset(monkeypatch):
    asked = []

    def get(url, **kw):
        asked.append(url)
        offset = int(url.split("offset=")[1].split(",")[0])
        rows = {
            0: [
                {
                    "Id": "40082",
                    "Title": "Firmware Development Engineer",
                    "PostedDate": "2026-09-05",
                    "PrimaryLocation": "India",
                    "secondaryLocations": [{"Name": "Bangalore, India"}],
                    "ShortDescriptionStr": "Join us.",
                }
            ],
            1: [
                {
                    "Id": "40083",
                    "Title": "Test Engineer",
                    "PostedDate": None,
                    "PrimaryLocation": "Espoo, Finland",
                    "secondaryLocations": [],
                }
            ],
        }[offset]
        return _Resp({"items": [{"TotalJobsCount": 2, "requisitionList": rows}]})

    monkeypatch.setattr(boards._session, "get", get)
    out = boards.fetch_listings(
        "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitions?siteNumber=CX_7",
        "Nokia",
    )
    assert [(p.title, p.locations, p.date_posted) for p in out] == [
        ("Firmware Development Engineer", ["India", "Bangalore, India"], 1788566400),
        ("Test Engineer", ["Espoo, Finland"], 0),
    ]
    assert out[0].url == (
        "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
        "CX_7/job/40082"
    )
    assert all(p.company == "Nokia" for p in out)
    # The finder rides on the URL unencoded, site and offset inside it.
    assert asked[0].endswith(
        "finder=findReqs;siteNumber=CX_7,limit=200,offset=0,sortBy=POSTING_DATES_DESC"
    )
    # The next page starts where the last ended, not at the page size.
    assert "offset=1," in asked[1]


def test_workable_follows_the_next_page_token(monkeypatch):
    posts = []

    def post(url, json, **kw):
        posts.append((url, json))
        if "token" not in json:
            return _Resp(
                {
                    "total": 2,
                    "nextPage": "WzE3ODU5NzQ0MDAwMDAsNDE4MjgyN10=",
                    "results": [
                        {
                            "shortcode": "BCEAC9B2D1",
                            "title": "Tech Talent Sourcer",
                            "published": "2026-09-01T00:00:00.000Z",
                            "remote": False,
                            "location": {
                                "country": "United Kingdom",
                                "city": "London",
                                "region": "England",
                            },
                        }
                    ],
                }
            )
        return _Resp(
            {
                "total": 2,
                "nextPage": None,
                "results": [
                    {
                        "shortcode": "637F3B9521",
                        "title": "Driver",
                        "published": "2026-08-30T00:00:00.000Z",
                        "remote": True,
                        "location": {},
                    }
                ],
            }
        )

    monkeypatch.setattr(boards._session, "post", post)
    out = boards.fetch_listings("https://apply.workable.com/api/v3/accounts/zego/jobs", "Zego")
    assert [(p.title, p.locations) for p in out] == [
        ("Tech Talent Sourcer", ["London, England, United Kingdom"]),
        ("Driver", []),
    ]
    assert out[0].url == "https://apply.workable.com/zego/j/BCEAC9B2D1/"
    assert all(p.company == "Zego" for p in out)
    assert [j.get("token") for _, j in posts] == [None, "WzE3ODU5NzQ0MDAwMDAsNDE4MjgyN10="]


def test_unknown_urls_fall_through_to_the_sheet_era_fetcher(monkeypatch):
    seen = []
    monkeypatch.setattr(boards, "fetch_job_postings", lambda url: seen.append(url) or [])
    assert boards.fetch_listings("https://airtable.com/appX/shrY", "Acme") == []
    assert seen == ["https://airtable.com/appX/shrY"]


def test_workable_paces_its_requests(monkeypatch):
    slept = []
    monkeypatch.setattr(boards.time, "sleep", lambda s: slept.append(round(s, 1)))
    monkeypatch.setattr(
        boards._session, "post", lambda url, json, **kw: _Resp({"total": 0, "results": []})
    )
    boards._last_call.clear()
    boards.set_pace({"apply.workable.com": 6})
    boards.fetch_listings("https://apply.workable.com/api/v3/accounts/a/jobs", "A")
    boards.fetch_listings("https://apply.workable.com/api/v3/accounts/b/jobs", "B")
    # The first call goes straight out; the second waits out the six seconds.
    assert slept and 5.0 < slept[-1] <= 6.0


def test_a_nul_byte_in_a_posting_is_dropped_before_it_reaches_jsonb():
    p = boards._posting(
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
    monkeypatch.setattr(boards.time, "sleep", slept.append)
    boards.set_pace({})
    boards._pace("apply.workable.com")
    boards._pace("apply.workable.com")
    assert slept == []


def test_a_workday_tenant_capped_at_the_window_is_read_in_facet_slices(monkeypatch):
    """Airbus on 2026-10-05: total=2000 on the first page and pages past offset
    2,000 wrap to the first. Slices by the two widest facets whose values are
    each under the window recover the rest, and the pull says it is partial."""
    facets = [
        {
            "facetParameter": "jobFamilyGroup",
            "values": [{"id": "eng", "count": 3}, {"id": "ops", "count": 1}],
        },
        {
            "facetParameter": "locationMainGroup",
            "values": [
                {
                    "facetParameter": "locationCountry",
                    "values": [{"id": "fr", "count": 2}, {"id": "us", "count": 2}],
                }
            ],
        },
        {"facetParameter": "FullPartTime", "values": [{"id": "ft", "count": 2500}]},
    ]
    by_slice = {
        (): ["a", "b"],
        (("jobFamilyGroup", ("eng",)),): ["a", "c", "d"],
        (("jobFamilyGroup", ("ops",)),): ["e"],
        (("locationCountry", ("fr",)),): ["a", "f"],
        (("locationCountry", ("us",)),): ["c", "e"],
    }
    asked = []

    def post(url, json, **kw):
        key = tuple(sorted((k, tuple(v)) for k, v in json["appliedFacets"].items()))
        asked.append(key)
        titles = by_slice[key][json["offset"] : json["offset"] + boards._WORKDAY_PAGE]
        page = [{"title": t, "externalPath": f"/job/x/{t}_JR"} for t in titles]
        first = json["offset"] == 0
        total = boards._WORKDAY_WINDOW if not key else len(by_slice[key])
        return _Resp(
            {"total": total if first else 0, "jobPostings": page, "facets": facets if first else []}
        )

    monkeypatch.setattr(boards._session, "post", post)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://ag.wd3.myworkdayjobs.com/wday/cxs/ag/Airbus/jobs", "Airbus")
    assert sorted(p.title for p in raised.value.postings) == ["a", "b", "c", "d", "e", "f"]
    assert ("FullPartTime", ("ft",)) not in {k for key in asked for k in key}, (
        "a facet with a value at the window cannot be read whole"
    )


def test_a_workday_tenant_under_the_window_is_one_complete_pull(monkeypatch):
    def post(url, json, **kw):
        return _Resp(
            {
                "total": 1 if json["offset"] == 0 else 0,
                "facets": [],
                "jobPostings": [{"title": "t", "externalPath": "/job/x/t_JR"}]
                if json["offset"] == 0
                else [],
            }
        )

    monkeypatch.setattr(boards._session, "post", post)
    out = boards.fetch_listings("https://x.wd1.myworkdayjobs.com/wday/cxs/x/Ext/jobs", "X")
    assert [p.title for p in out] == ["t"]


def test_an_oracle_search_past_its_window_is_a_partial_pull(monkeypatch):
    """AutoZone on 2026-10-05: TotalJobsCount 10,788, and at offset 10,000 the
    page is empty and the count reads 0. Returning what arrived as the whole
    board retired the rest on every pull."""

    def get(url, **kw):
        offset = int(url.split("offset=")[1].split(",")[0])
        if offset >= 2:
            return _Resp({"items": [{"TotalJobsCount": 0, "requisitionList": []}]})
        row = {"Id": str(offset), "Title": f"Role {offset}", "PrimaryLocation": "US"}
        return _Resp({"items": [{"TotalJobsCount": 3, "requisitionList": [row]}]})

    monkeypatch.setattr(boards._session, "get", get)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(
            "https://x.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions?siteNumber=CX_1",
            "AutoZone",
        )
    assert [p.title for p in raised.value.postings] == ["Role 0", "Role 1"]


# Rows as textron.taleo.net, aarcorp.taleo.net and baesystems.taleo.net
# returned them on 2026-10-05. Which column is which is per careersection:
# Textron and AAR send title, locations and date, BAE the title alone, and
# the locations cell is a JSON list inside a string.
_TALEO_ROWS = [
    {
        "hotJob": False,
        "jobId": "1540116",
        "contestNo": "342919",
        "column": [
            "2027 - Systems Engineer (Uncrewed Land & Air) - Hunt Valley, MD",
            '["US-Maryland-Hunt Valley"]',
            "10/02/2026",
        ],
        "linkedColumn": 0,
        "locationsColumns": [1],
    },
    {
        "hotJob": False,
        "jobId": "326546",
        "contestNo": "18884",
        "column": ["A&P Certificated Apprentice", '["United States-Florida-Miami"]', "Oct 5, 2026"],
        "linkedColumn": 0,
        "locationsColumns": [1],
    },
    {
        "hotJob": True,
        "jobId": "1014544",
        "contestNo": "00110645",
        "column": ["Metrology Engineer (Calibration)"],
        "linkedColumn": 0,
        "locationsColumns": [],
    },
]


def _taleo_board(monkeypatch, pages: list[list[dict]], total: int):
    """The search endpoint as it answered live: 25-row pages, the same
    totalCount on every page, and any page past the last answered with the
    last page again (Kautex, pages 5 through 500), so a loop that waits for
    an empty page never ends. More requests than pages fails the test
    instead of hanging it."""
    asked = []

    def post(url, json, headers, **kw):
        assert headers.get("tz"), "without a tz header the endpoint answers 500"
        asked.append((url, json["pageNo"]))
        assert len(asked) <= len(pages), "paged past the last page"
        number = min(json["pageNo"], len(pages))
        return _Resp(
            {
                "requisitionList": pages[number - 1],
                "pagingData": {
                    "currentPageNo": json["pageNo"],
                    "pageSize": 25,
                    "totalCount": total,
                },
            }
        )

    monkeypatch.setattr(boards._session, "post", post)
    return asked


def _taleo_rows(n: int, start: int = 0) -> list[dict]:
    return [
        {**_TALEO_ROWS[2], "contestNo": str(start + i), "column": [f"Role {start + i}"]}
        for i in range(n)
    ]


def test_taleo_pages_to_the_count_and_builds_the_public_url(monkeypatch):
    pages = [_TALEO_ROWS + _taleo_rows(22), _taleo_rows(1, start=22)]
    asked = _taleo_board(monkeypatch, pages, total=26)
    page_html = "<script>var settings = { portalNo: '8140753014', lang: 'en' };</script>"
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp(page_html))
    out = boards.fetch_listings(
        "https://textron.taleo.net/careersection/textron/jobsearch.ftl?lang=en", "Textron"
    )
    assert len(out) == 26
    first, aar, bae = out[:3]
    assert (first.title, first.locations, first.date_posted) == (
        "2027 - Systems Engineer (Uncrewed Land & Air) - Hunt Valley, MD",
        ["US-Maryland-Hunt Valley"],
        int(datetime.datetime(2026, 10, 2, tzinfo=datetime.UTC).timestamp()),
    )
    assert first.url == "https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=342919"
    assert aar.date_posted == int(datetime.datetime(2026, 10, 5, tzinfo=datetime.UTC).timestamp())
    assert (bae.title, bae.locations, bae.date_posted) == (
        "Metrology Engineer (Calibration)",
        [],
        0,
    )
    assert bae.url.endswith("jobdetail.ftl?job=00110645")
    assert all(p.company == "Textron" for p in out)
    # The portal read off the search page rode on the endpoint, one request a
    # page, and nothing past the second.
    assert asked == [
        (
            "https://textron.taleo.net/careersection/rest/jobboard/searchjobs"
            "?lang=en&portal=8140753014",
            n,
        )
        for n in (1, 2)
    ]


def test_a_taleo_pull_short_of_its_count_is_partial(monkeypatch):
    """Textron on 2026-10-05: 751 counted, 691 listed. Pages run short in the
    middle (24 of 25) and the last page the count implies came back empty.
    A loop that stops on the first short page sees 24; one that trusts what
    it saw retires the 11 it was never shown."""
    pages = [_taleo_rows(24), _taleo_rows(25, start=24), []]
    _taleo_board(monkeypatch, pages, total=60)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(
            "https://textron.taleo.net/careersection/textron/jobsearch.ftl?lang=en&portal=1",
            "Textron",
        )
    assert len(raised.value.postings) == 49


class _AppleSearch:
    """jobs.apple.com/api/v1/search as measured on 2026-10-05.

    20 rows a page, the count on every page and 0 on the empty one past the
    end. A body without "format" gets that empty page from the start. The
    managed pipeline roles sort first and their order is drawn afresh by each
    request, here a rotation by five per call, so one pass over them returns
    some twice and misses others. `hidden` rows are counted but never served.
    """

    def __init__(self, managed: int, regular: int, hidden: int = 0):
        self.managed = [
            {
                "id": f"PIPE-1144380{i:02d}",
                "postingTitle": f"Expert {i}",
                "transformedPostingTitle": f"expert-{i}",
                "managedPipelineRole": True,
                "postDateInGMT": "2026-10-05T05:21:50.895303331Z",
                "locations": [{"name": "India", "countryName": "India"}],
            }
            for i in range(managed)
        ]
        self.regular = [
            {
                "id": f"2006865{i:02d}-3916",
                "postingTitle": f"Engineer {i}",
                "transformedPostingTitle": f"engineer-{i}",
                "managedPipelineRole": False,
                "postDateInGMT": "2026-10-04T18:02:11.304Z",
                "locations": [{"name": "Cupertino", "countryName": "United States of America"}],
            }
            for i in range(regular)
        ]
        self.total = managed + regular + hidden
        self.bodies: list[dict] = []

    def post(self, url, json, **kw):
        assert url == "https://jobs.apple.com/api/v1/search"
        shift = 5 * len(self.bodies) % len(self.managed)
        self.bodies.append(json)
        rows = self.managed[shift:] + self.managed[:shift] + self.regular
        start = (json["page"] - 1) * boards._APPLE_PAGE
        page = rows[start : start + boards._APPLE_PAGE] if "format" in json else []
        return _Resp({"res": {"searchResults": page, "totalRecords": self.total if page else 0}})


def test_apple_reads_the_unstable_pages_again_until_every_posting_is_seen(monkeypatch):
    """One pass here returns 45 rows of 45 with five of them twice and five
    never, as one pass of the live board did on 2026-10-05 (6,192 rows, 6,190
    distinct). The fetcher must ask with "format", see the distinct count fall
    short, and re-read the pages the managed roles held."""
    board = _AppleSearch(managed=25, regular=20)
    monkeypatch.setattr(boards._session, "post", board.post)
    out = boards.fetch_listings("https://jobs.apple.com/api/v1/search", "Apple")
    assert len({p.url for p in out}) == len(out) == 45
    # Three pages for one pass, then the two that held managed roles.
    assert [b["page"] for b in board.bodies] == [1, 2, 3, 1, 2]
    assert all(p.company == "Apple" for p in out)
    expert = next(p for p in out if p.title == "Expert 0")
    assert expert.url == "https://jobs.apple.com/en-us/details/114438000/expert-0"
    assert expert.locations == ["India"]
    assert expert.date_posted == 0, "a managed role's timestamp is the request's"
    engineer = next(p for p in out if p.title == "Engineer 0")
    assert engineer.url == "https://jobs.apple.com/en-us/details/200686500-3916/engineer-0"
    assert engineer.locations == ["Cupertino, United States of America"]
    assert engineer.date_posted == int(
        datetime.datetime(2026, 10, 4, 18, 2, 11, 304000, tzinfo=datetime.UTC).timestamp()
    )


def test_apple_short_of_its_count_is_a_partial_pull(monkeypatch):
    board = _AppleSearch(managed=25, regular=20, hidden=1)
    monkeypatch.setattr(boards._session, "post", board.post)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://jobs.apple.com/api/v1/search", "Apple")
    assert len(raised.value.postings) == 45
    # Re-reading stops on the first round that finds nothing new.
    assert [b["page"] for b in board.bodies] == [1, 2, 3, 1, 2, 1, 2]


def _city(*names: tuple[str, str]) -> dict | None:
    """city_info as the search returns it: the city, its parent state and that
    state's parent country, each with an English and a localised name."""
    city = None
    for en, i18n in reversed(names):
        city = {"en_name": en, "i18n_name": i18n, "parent": city}
    return city


# api.lifeattiktok.com and jobs.bytedance.com, 2026-10-05, trimmed to the keys
# the fetcher reads. The ByteDance board answers i18n_name in Chinese.
_TIKTOK_ROWS = [
    {
        "id": "7667946787733244165",
        "code": "A137788B",
        "title": "Multimodal LLM Algorithm Engineer Graduate (Global E-Commerce, Knowledge Graph) - 2027 Start (PhD)",
        "description": "About the Team\nWe are part of the Global E-commerce Algorithm team.",
        "requirement": "Minimum Qualifications\n- Individuals who are completing a PhD degree.",
        "city_info": _city(
            ("Singapore", "Singapore"), ("Singapore", "Singapore"), ("Singapore", "Singapore")
        ),
        "recruit_type": {"id": "201", "en_name": "Regular"},
    },
    *[
        {
            "id": str(7686350939411253509 + i),
            "title": f"Role {i}",
            "description": "d",
            "requirement": "r",
            "city_info": _city(
                ("Jakarta", "Jakarta"), ("Jakarta Raya", "Jakarta Raya"), ("Indonesia", "Indonesia")
            ),
        }
        for i in range(4)
    ],
]
_BYTEDANCE_ROWS = [
    {
        "id": "7624637489953130805",
        "code": "A151689",
        "title": "Data Center Site Acquisition Manager - Infrastructure Power & Energy",
        "description": "To support the fast growth of the platform.",
        "requirement": "Minimum Qualifications: \n- Bachelor's degree in Engineering.",
        "city_info": _city(
            ("San Jose", "圣何塞"),
            ("California", "加利福尼亚州"),
            ("United States of America", "美国"),
        ),
    }
]


def _bytedance_api(boards_by_path: dict, window: int, capped_count: bool = False, asked=None):
    """The search as measured on 2026-10-05: the website-path header picks the
    board and its absence is a 400; the limit asked for is honoured; the count
    rides on every page; and a request whose offset plus limit passes the
    window answers no rows and a count of the window, whatever the board
    holds. Whether a board over the window states its true count on the first
    page is unmeasured, so `capped_count` covers the other answer."""

    def post(url, json, headers=None, **kw):
        board = (headers or {}).get("website-path")
        if asked is not None:
            asked.append((url, board, json["offset"], json["limit"]))
        if board not in boards_by_path:
            raise boards.requests.HTTPError("400 invalid request")
        rows = boards_by_path[board]
        if json["offset"] + json["limit"] > window:
            return _Resp({"code": 0, "data": {"job_post_list": [], "count": window}})
        count = min(len(rows), window) if capped_count else len(rows)
        page = rows[json["offset"] : json["offset"] + json["limit"]]
        return _Resp({"code": 0, "data": {"job_post_list": page, "count": count}})

    return post


_TIKTOK = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts?website-path=tiktok"
_BYTEDANCE = "https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts?website-path=en"


def test_one_fetcher_reads_both_bytedance_boards_by_their_website_path(monkeypatch):
    asked: list = []
    api = {"tiktok": _TIKTOK_ROWS, "en": _BYTEDANCE_ROWS}
    monkeypatch.setattr(boards._session, "post", _bytedance_api(api, 10_000, asked=asked))
    monkeypatch.setattr(boards, "_BYTEDANCE_PAGE", 2)

    tiktok = boards.fetch_listings(_TIKTOK, "TikTok")
    assert len(tiktok) == len(_TIKTOK_ROWS)
    first = tiktok[0]
    assert first.url == "https://lifeattiktok.com/search/7667946787733244165"
    assert first.company == "TikTok"
    assert first.date_posted == 0
    # A city-state names itself three times; once is the place.
    assert first.locations == ["Singapore"]
    assert first.description == (
        f"{_TIKTOK_ROWS[0]['title']}\n\nSingapore\n\n"
        "About the Team\nWe are part of the Global E-commerce Algorithm team.\n\n"
        "Minimum Qualifications\n- Individuals who are completing a PhD degree."
    )
    assert first.raw is not None and first.raw["code"] == "A137788B"
    assert "description" not in first.raw and "requirement" not in first.raw
    # The board rode in the header, not the URL, and pages followed the offset.
    assert {(u, b) for u, b, _, _ in asked} == {
        ("https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts", "tiktok")
    }
    assert [o for _, _, o, _ in asked] == [0, 2, 4]

    (bytedance,) = boards.fetch_listings(_BYTEDANCE, "ByteDance")
    assert bytedance.url == "https://joinbytedance.com/search/7624637489953130805"
    assert bytedance.locations == ["San Jose, California, United States of America"]


def test_a_bytedance_board_the_url_does_not_name_is_refused():
    with pytest.raises(ValueError):
        boards.fetch_listings(_TIKTOK.replace("=tiktok", "=school"), "TikTok")


@pytest.mark.parametrize("capped_count", [False, True])
def test_a_bytedance_board_past_the_search_window_is_a_partial_pull(monkeypatch, capped_count):
    """Past offset plus limit 10,000 the search answers no rows and a count of
    10,000, which a loop paging until an empty page reads as the end of the
    board. The fetcher reads up to the window and says the pull is partial."""
    monkeypatch.setattr(
        boards._session,
        "post",
        _bytedance_api({"tiktok": _TIKTOK_ROWS}, window=4, capped_count=capped_count),
    )
    monkeypatch.setattr(boards, "_BYTEDANCE_PAGE", 3)
    monkeypatch.setattr(boards, "_BYTEDANCE_WINDOW", 4)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(_TIKTOK, "TikTok")
    assert [p.title for p in raised.value.postings] == [r["title"] for r in _TIKTOK_ROWS[:4]]


def test_a_bytedance_board_that_shifts_mid_pull_is_a_partial_pull(monkeypatch):
    """Paging is by offset over a live board: a posting closed between two
    pages moves the next one back onto the page already read."""
    rows = list(_TIKTOK_ROWS)
    api = _bytedance_api({"tiktok": rows}, 10_000)

    def post(url, json, **kw):
        reply = api(url, json, **kw)
        if json["offset"] == 0:
            del rows[0]
        return reply

    monkeypatch.setattr(boards._session, "post", post)
    monkeypatch.setattr(boards, "_BYTEDANCE_PAGE", 2)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(_TIKTOK, "TikTok")
    assert len(raised.value.postings) == len(_TIKTOK_ROWS) - 1


# careers-gdms.icims.com/jobs/search?ss=1&in_iframe=1, 2026-10-05: a card with
# its posted date in the header (exact time in the title attribute), and a
# Joby card whose locations sit in the header, joined by " | ". Verbatim but
# for the description snippet, which the fetcher does not read.
_GDMS_CARD = """
<li class="iCIMS_JobCardItem">
<div class="row">
<div class="col-xs-6 header left">
</div>
<div class="col-xs-6 header right">
<span class="sr-only field-label">Posted Date</span>
<span title="10/4/2026 5:15 PM">
8 hours ago<span class="sr-only">(10/4/2026 5:15 PM)</span></span>
</div>
<div class="col-xs-12 title">
<a href="https://careers-gdms.icims.com/jobs/{id}/sr.-advanced-field-support-specialist-%28ts-sci-clearance-required%29/job?in_iframe=1" class="iCIMS_Anchor" title="{id} - {title}">
<span class="sr-only field-label">Title</span>
<h3 >
{title}</h3>
</a>
</div>
<div class="col-xs-12 description">
Bachelor's degree in a related specialized area...</div>
<div class="col-xs-12 additionalFields">
<dl class="iCIMS_JobHeaderGroup">
<div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField">ID</dt>
<dd class="iCIMS_JobHeaderData"><span >
2026-{id}</span>
</dd>
</div>
<div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField"><span class="glyphicons glyphicons-map-marker" aria-hidden="true"></span>
<span class="sr-only field-label">Job Location</span>
</dt>
<dd class="iCIMS_JobHeaderData"><span >
US-MD-Annapolis Junction</span>
</dd>
</div>
<div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField">Required Clearance</dt>
<dd class="iCIMS_JobHeaderData"><span >
TS/SCI</span>
</dd>
</div>
</dl>
</div>
</div>
</li>
"""

_JOBY_CARD = """
<li class="iCIMS_JobCardItem">
<div class="row">
<div class="col-xs-6 header left">
<span class="sr-only field-label">Job Locations</span>
<span >
US-CA-Marina | US-CA-Watsonville</span>
</div>
<div class="col-xs-6 header right">
</div>
<div class="col-xs-12 title">
<a href="https://careers-jobyaviation.icims.com/jobs/{id}/aircraft-maintenance-training-manager/job?in_iframe=1" class="iCIMS_Anchor" title="{id} - {title}">
<span class="sr-only field-label">Title</span>
<h3 >
{title}</h3>
</a>
</div>
<div class="col-xs-12 additionalFields">
<dl class="iCIMS_JobHeaderGroup">
<div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField">Category</dt>
<dd class="iCIMS_JobHeaderData"><span >
Air Operations</span>
</dd>
</div>
</dl>
</div>
</div>
</li>
"""


def _icims_page(card: str, ids: list[int], page: int, pages: int) -> str:
    """One search page as the portal serves it. Past the last page it answers
    200 with neither cards nor the "Page N of M" heading (GDMS pr=35, Peraton
    pr=31, Joby pr=8, 2026-10-05). The search form above the results carries
    the same heading class with no count in it (Expleo)."""
    if page >= pages:
        return '<div class="iCIMS_SearchResultsHeader"></div>'
    cards = "".join(card.format(id=i, title=f"Role {i}") for i in ids)
    return (
        '<p class="iCIMS_SubHeader iCIMS_SubHeader_Jobs">Search by keyword</p>'
        '<div class="container-fluid iCIMS_SearchResultsHeader"><div class="row">'
        '<div class="pull-left"><h2 class="iCIMS_SubHeader iCIMS_SubHeader_Jobs">\n'
        f"Search Results\nPage {page + 1} of {pages} \n</h2></div></div></div>"
        f'<ul class="container-fluid iCIMS_JobsTable">{cards}</ul>'
    )


def _serve_icims(monkeypatch, card: str, by_page: dict[int, list[int]], pages: int):
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        pr = int(url.split("pr=")[1].split("&")[0])
        return _Resp(_icims_page(card, by_page.get(pr, []), pr, pages))

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_icims_pages_by_zero_based_pr_to_the_count_its_first_page_states(monkeypatch):
    """pr= is zero-based where the heading is one-based, and the page size is
    the tenant's (GDMS 20, Electric Boat 50), so the stated page count is what
    ends the pull. A loop that started at pr=1, or stopped on a short or empty
    page of an assumed size, asks a different sequence."""
    asked = _serve_icims(monkeypatch, _GDMS_CARD, {0: [1, 2, 3], 1: [4, 5, 6], 2: [7]}, pages=3)
    out = boards.fetch_listings(
        "https://careers-gdms.icims.com/jobs/search", "General Dynamics Mission Systems"
    )
    assert [p.title for p in out] == [f"Role {i}" for i in range(1, 8)]
    assert [u.split("pr=")[1] for u in asked] == ["0", "1", "2"]
    assert all("in_iframe=1" in u and "ss=1" in u for u in asked)
    p = out[0]
    # The posting a person opens: the portal page, without the iframe flag or
    # the slug, which iCIMS ignores.
    assert p.url == "https://careers-gdms.icims.com/jobs/1/job"
    assert p.company == "General Dynamics Mission Systems"
    assert p.locations == ["US-MD-Annapolis Junction"]
    assert p.date_posted == int(datetime.datetime(2026, 10, 4, tzinfo=datetime.UTC).timestamp())
    # The card's snippet is not the posting's text; the page fetch supplies it.
    assert p.description == ""
    assert p.raw is not None and p.raw["Required Clearance"] == "TS/SCI"


def test_icims_reads_header_locations_split_on_the_bar(monkeypatch):
    _serve_icims(monkeypatch, _JOBY_CARD, {0: [4815]}, pages=1)
    (p,) = boards.fetch_listings("https://careers-jobyaviation.icims.com/jobs/search", "Joby")
    assert p.locations == ["US-CA-Marina", "US-CA-Watsonville"]
    assert p.date_posted == 0


def test_icims_a_page_that_shifted_under_the_pull_makes_it_partial(monkeypatch):
    """A posting added mid-pull pushes the last card of page one onto page
    two: every page is full and the page count holds, but one posting repeats
    and another was never served. Returning the deduplicated list would retire
    the unseen one as closed."""
    _serve_icims(monkeypatch, _GDMS_CARD, {0: [1, 2], 1: [2, 3], 2: [4]}, pages=3)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://careers-gdms.icims.com/jobs/search", "GDMS")
    assert sorted(p.title for p in raised.value.postings) == [
        "Role 1",
        "Role 2",
        "Role 3",
        "Role 4",
    ]


def test_icims_a_page_cut_short_before_the_last_makes_it_partial(monkeypatch):
    _serve_icims(monkeypatch, _GDMS_CARD, {0: [1, 2], 1: [3], 2: [4]}, pages=3)
    with pytest.raises(boards.PartialPull):
        boards.fetch_listings("https://careers-gdms.icims.com/jobs/search", "GDMS")


def test_an_icims_portal_that_moved_names_its_career_site(monkeypatch):
    # careers-spiritaero.icims.com/jobs/search?ss=1&in_iframe=1, 2026-10-05,
    # verbatim: the whole 200 body.
    body = (
        '<script type="text/javascript">\n'
        "window.top.location.href = 'https:\\/\\/careers.spiritaero.com\\/jobs';\n"
        "</script>\n"
    )
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp(body))
    with pytest.raises(ValueError, match=r"https://careers\.spiritaero\.com/api/jobs"):
        boards.fetch_listings("https://careers-spiritaero.icims.com/jobs/search", "Spirit")


def _jibe_job(slug: str, description: str = "<p>Overview</p>") -> dict:
    """careers.spiritaero.com/api/jobs, 2026-10-05, one job trimmed to the keys
    the fetcher reads and some beside them. The locations are a V2X job's,
    whose full_location repeats a place."""
    return {
        "data": {
            "slug": slug,
            "language": "en-us",
            "req_id": slug,
            "title": f"Role {slug}",
            "city": "Fayetteville",
            "full_location": "Fayetteville, North Carolina; Fayetteville, North Carolina",
            "multipleLocations": True,
            "description": description,
            "qualifications": "<p>Basic Qualifications</p>",
            "responsibilities": "<p>Position Responsibilities</p>",
            "hiring_organization": "Spirit AeroSystems",
            "posted_date": "2026-10-02T20:07:00+0000",
            "apply_url": f"https://careers-spiritaero.icims.com/jobs/{slug}/login",
            "ats_code": "icims",
        }
    }


def _serve_jibe(monkeypatch, slugs: list[str], total: int):
    """page= is one-based, limit= is honoured up to 100, totalCount rides on
    every page, and a page past the end is an empty list (Spirit, 2026-10-05:
    limit=100&page=3 gave 22 of 222, page=24 at the default 10 gave none)."""
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        q = {k: int(v) for k, v in (x.split("=") for x in url.split("?")[1].split("&"))}
        assert q["page"] >= 1, "page=0 is a 422 on the live API"
        start = (q["page"] - 1) * q["limit"]
        jobs = [_jibe_job(s) for s in slugs[start : start + q["limit"]]]
        return _Resp({"jobs": jobs, "totalCount": total, "count": total})

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_jibe_pages_one_based_at_the_largest_page_it_accepts(monkeypatch):
    slugs = [str(17000 + i) for i in range(205)]
    asked = _serve_jibe(monkeypatch, slugs, total=205)
    out = boards.fetch_listings("https://careers.spiritaero.com/api/jobs", "ignored")
    assert asked == [
        f"https://careers.spiritaero.com/api/jobs?limit=100&page={n}" for n in (1, 2, 3)
    ]
    assert len(out) == 205
    p = out[0]
    assert p.url == "https://careers.spiritaero.com/jobs/17000"
    assert p.company == "Spirit AeroSystems"
    assert p.locations == ["Fayetteville, North Carolina"]
    assert p.date_posted == int(
        datetime.datetime(2026, 10, 2, 20, 7, tzinfo=datetime.UTC).timestamp()
    )
    # The description, and the sections it left out (V2X, 17 of 765).
    assert p.description == "Overview\n\nPosition Responsibilities\n\nBasic Qualifications"
    assert p.raw is not None and "qualifications" not in p.raw and "description" not in p.raw


def test_jibe_does_not_repeat_sections_the_description_already_holds(monkeypatch):
    held = "<p>Overview</p><p>Position Responsibilities</p><p>Basic Qualifications</p>"
    monkeypatch.setattr(
        boards._session,
        "get",
        lambda url, **kw: _Resp({"jobs": [_jibe_job("1", held)], "totalCount": 1}),
    )
    (p,) = boards.fetch_listings("https://careers.spiritaero.com/api/jobs", "")
    assert "Basic Qualifications" in p.description
    assert p.description.count("Basic Qualifications") == 1


def test_jibe_short_of_its_stated_total_is_partial(monkeypatch):
    _serve_jibe(monkeypatch, ["1", "2"], total=3)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://careers.spiritaero.com/api/jobs", "")
    assert len(raised.value.postings) == 2


# www-api.ibm.com/search/api/v2 on 2026-10-05: verbatim hits, in _id order.
IBM_HITS = [
    {
        "_id": "0181e51151251524430513cb843db9ee33255526dbc24195bbb2d832f9193640",
        "_source": {
            "title": "Product Management Intern | Infrastructure Growth & Innovation - "
            "Poughkeepsie, NY / Austin, TX - 2027",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=131158",
            "field_text_01": 131158,
            "field_keyword_05": "United States",
            "field_keyword_08": "Product Management",
            "field_keyword_17": "Hybrid",
            "field_keyword_18": "Internship",
            "field_keyword_19": "Multiple Cities",
        },
    },
    {
        "_id": "0630b4c19c3ac0f2d74de9b57566074deaf113cfcae7def7a35ee2e81e0e9533",
        "_source": {
            "title": "Associate Threat Analyst Researcher and Consultant 2027",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=128676",
            "field_text_01": 128676,
            "field_keyword_05": "United States",
            "field_keyword_08": "Consulting",
            "field_keyword_17": "",
            "field_keyword_18": "Entry Level",
            "field_keyword_19": "Austin, US",
        },
    },
    {
        "_id": "0f4831b26e3dd9abad68616071fe498630390e8f5f1090e589c68e94ee552972",
        "_source": {
            "title": "Client Engineering",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=129622",
            "field_text_01": 129622,
            "field_keyword_05": "Switzerland",
            "field_keyword_08": "Sales",
            "field_keyword_17": "Hybrid",
            "field_keyword_18": "Entry Level",
            "field_keyword_19": "Zurich, CH",
        },
    },
]


def _ibm_search(hits, stated, asked):
    """The search as measured: a size above 100 is a 400, and only a
    search_after on _id moves through the index. `from` is ignored here, so a
    walk by it reads the first page again, as the live walk re-read rows."""

    def post(url, json, **kw):
        asked.append({**json})
        if json["size"] > 100:
            raise boards.requests.HTTPError("400 Parameter 'size' has an invalid value")
        cursor = "search_after" in json and json.get("sort") == [{"_id": "asc"}]
        after = json["search_after"][0] if cursor else ""
        page = [{**h, "sort": [h["_id"]]} for h in hits if h["_id"] > after][: json["size"]]
        return _Resp({"hits": {"total": {"value": stated, "relation": "eq"}, "hits": page}})

    return post


def test_ibm_walks_the_index_by_cursor_and_links_the_public_posting(monkeypatch):
    asked = []
    monkeypatch.setattr(boards, "_IBM_PAGE", 1)
    monkeypatch.setattr(boards._session, "post", _ibm_search(IBM_HITS, 3, asked))
    out = boards.fetch_listings("https://www-api.ibm.com/search/api/v2", "IBM")
    assert [(p.title, p.locations) for p in out] == [
        (
            "Product Management Intern | Infrastructure Growth & Innovation - "
            "Poughkeepsie, NY / Austin, TX - 2027",
            # "Multiple Cities" is not a place; the country is.
            ["United States"],
        ),
        ("Associate Threat Analyst Researcher and Consultant 2027", ["Austin, US"]),
        ("Client Engineering", ["Zurich, CH"]),
    ]
    assert out[1].url == "https://careers.ibm.com/careers/JobDetail?jobId=128676"
    assert all(p.company == "IBM" for p in out)
    # The listing carries no complete text, so none is stored as the posting.
    assert [p.description for p in out] == ["", "", ""]
    assert [a.get("search_after") for a in asked] == [
        None,
        [IBM_HITS[0]["_id"]],
        [IBM_HITS[1]["_id"]],
        [IBM_HITS[2]["_id"]],
    ]


def test_an_ibm_cursor_that_ends_short_of_the_stated_count_is_a_partial_pull(monkeypatch):
    monkeypatch.setattr(boards, "_IBM_PAGE", 1)
    monkeypatch.setattr(boards._session, "post", _ibm_search(IBM_HITS, 4, []))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://www-api.ibm.com/search/api/v2", "IBM")
    assert len(raised.value.postings) == 3


# api-higher.gs.com roleSearch on 2026-10-05: verbatim roles, each with the
# experience it was listed under, the first description trimmed.
GOLDMAN_ROLES = [
    (
        "CAMPUS",
        {
            "roleId": "182810_GS_CAMPUS",
            "jobTitle": "2027 | Americas | New York City Area | Executive Office, "
            "Sustainable Finance Group | Summer Analyst",
            "corporateTitle": "Summer Analyst",
            "jobFunction": "",
            "division": "Executive Office Division",
            "status": "POSTED",
            "lastPostedDate": "2026-10-02T18:48:53.034Z",
            "locations": [
                {"primary": True, "state": "NY", "country": "United States", "city": "New York"}
            ],
            "compensation": {"minSalary": 80000.0, "maxSalary": 110000.0, "currency": "USD"},
            "descriptionHtml": "\n<p><b><u>About the program</u></b></p>\n<p>Our Summer "
            "Analyst Program is a nine to ten week summer internship.</p>",
            "externalSource": {"sourceId": "182810"},
        },
    ),
    (
        "PROFESSIONAL",
        {
            "roleId": "183334_GS_MID_CAREER",
            "jobTitle": "Engineering-L2-Bengaluru- Vice president-Software Engineering",
            "corporateTitle": "Vice President",
            "status": "POSTED",
            "lastPostedDate": "2026-10-01T05:58:03.663Z",
            "locations": [
                {"primary": True, "state": "Karnataka", "country": "India", "city": "Bengaluru"}
            ],
            "compensation": {"minSalary": None, "maxSalary": None, "currency": None},
            "descriptionHtml": None,
            "externalSource": {"sourceId": "183334"},
        },
    ),
    (
        "EARLY_CAREER",
        {
            "roleId": "182221_GS_EARLY_CAREER",
            "jobTitle": "Marcus by Goldman Sachs, Complaints Team Leader, Analyst | Draper, UT",
            "corporateTitle": "Call Center Representative",
            "status": "POSTED",
            "lastPostedDate": "2026-09-29T19:50:16.032Z",
            "locations": [
                {"primary": True, "state": "TX", "country": "United States", "city": "Richardson"},
                {"primary": False, "state": "UT", "country": "United States", "city": "Draper"},
            ],
            "compensation": {"minSalary": None, "maxSalary": None, "currency": "USD"},
            "descriptionHtml": "<p>Lead the complaints team.</p>",
            "externalSource": {"sourceId": "182221"},
        },
    ),
]


def _goldman_search(roles, stated, asked):
    """roleSearch as measured: pages numbered from 0, a pageSize above 250
    refused as HTTP 200 with errors and no data, and only the experiences
    asked for in the result."""

    def post(url, json, **kw):
        query = json["variables"]["searchQueryInput"]
        asked.append(query)
        size, number = query["page"]["pageSize"], query["page"]["pageNumber"]
        if size > 250:
            return _Resp({"errors": [{"message": "Validation Exception."}], "data": None})
        rows = [r for e, r in roles if e in query["experiences"]]
        page = rows[number * size : (number + 1) * size]
        return _Resp({"data": {"roleSearch": {"totalCount": stated, "items": page}}})

    return post


def test_goldman_pages_from_zero_across_every_public_experience(monkeypatch):
    asked = []
    monkeypatch.setattr(boards, "_GOLDMAN_PAGE", 1)
    monkeypatch.setattr(boards._session, "post", _goldman_search(GOLDMAN_ROLES, 3, asked))
    out = boards.fetch_listings("https://api-higher.gs.com/gateway/api/v1/graphql", "Goldman Sachs")
    assert [(p.url, p.locations) for p in out] == [
        ("https://higher.gs.com/roles/182810", ["New York, NY, United States"]),
        ("https://higher.gs.com/roles/183334", ["Bengaluru, Karnataka, India"]),
        (
            "https://higher.gs.com/roles/182221",
            ["Richardson, TX, United States", "Draper, UT, United States"],
        ),
    ]
    assert out[0].date_posted == 1790966933
    assert out[0].description.startswith(
        "2027 | Americas | New York City Area | Executive Office, Sustainable Finance Group | "
        "Summer Analyst\n\nNew York, NY, United States\n\nSummer Analyst\n\n"
        "USD 80,000 - 110,000\n\nAbout the program"
    )
    assert out[0].description.endswith("nine to ten week summer internship.")
    # A role whose listing carries no description stores none, rather than
    # its title and place passing for the posting's text.
    assert out[1].description == ""
    assert out[2].raw is not None and out[2].raw["roleId"] == "182221_GS_EARLY_CAREER"
    assert "descriptionHtml" not in out[2].raw
    assert all(p.company == "Goldman Sachs" for p in out)
    assert [q["page"]["pageNumber"] for q in asked] == [0, 1, 2, 3]


def test_a_refused_goldman_search_fails_the_pull(monkeypatch):
    monkeypatch.setattr(boards, "_GOLDMAN_PAGE", 251)
    monkeypatch.setattr(boards._session, "post", _goldman_search(GOLDMAN_ROLES, 3, []))
    with pytest.raises(RuntimeError, match="roleSearch refused"):
        boards.fetch_listings("https://api-higher.gs.com/gateway/api/v1/graphql", "Goldman Sachs")


def test_a_goldman_pull_short_of_its_count_is_a_partial_pull(monkeypatch):
    monkeypatch.setattr(boards._session, "post", _goldman_search(GOLDMAN_ROLES, 4, []))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://api-higher.gs.com/gateway/api/v1/graphql", "Goldman Sachs")
    assert len(raised.value.postings) == 3


# www.amazon.jobs/en/search.json, 2026-10-05: one row, its text trimmed.
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
    assert p.url == "https://www.amazon.jobs/en/jobs/10567672/data-center-operation-technician"
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


# SuccessFactors Career Site Builder, as jobs.ulalaunch.com, jobs.l3harris.com
# and jobs.deere.com answered on 2026-10-05. The first three items are verbatim
# from those feeds (descriptions trimmed); the rest are the same shape with
# their own ids.
_SF_VERBATIM = [
    (
        "Program Control Analyst 4 (Centennial, CO, US, 80112)",
        "https://jobs.ulalaunch.com/job/Centennial-Program-Control-Analyst-4-CO-80112/1407431200/"
        "?feedId=null&amp;utm_source=J2WRSS&amp;utm_medium=rss&amp;utm_campaign=J2W_RSS",
        "Sun, 04 Oct 2026 7:00:00 GMT",
        "<p><b>Requisition ID: </b>1858</p>\n\n<p><b>Location: </b>ULA - Denver<b> </b></p>",
    ),
    (
        "Sr Spec, Quality Engrg (Supplier Quality) (Rochester, NY, US, 14623)",
        "https://jobs.l3harris.com/job/Rochester-Sr-Spec%2C-Quality-Engrg-%28Supplier-Quality%29"
        "-NY-14623/1436563800/?feedId=null&amp;utm_source=J2WRSS&amp;utm_medium=rss&amp;utm_campaign=J2W_RSS",
        "Mon, 05 Oct 2026 0:00:00 GMT",
        "<p>Job Title: Sr Spec, Quality Engrg</p>",
    ),
    (
        "Stagiaire Ingénieur Test et Mesure (F/H) (Arc Les Gray Cedex, Saône (Haute), FR, 70103)",
        "https://jobs.deere.com/eightfold/job/Arc-Les-Gray-Cedex-Stagiaire-Ing%C3%A9nieur-Test-et"
        "-Mesure-%28FH%29-Sa%C3%B4n-70103/1436128200/?feedId=null&amp;utm_source=J2WRSS"
        "&amp;utm_medium=rss&amp;utm_campaign=J2W_RSS",
        "Fri, 02 Oct 2026 0:00:00 GMT",
        "<p>Stage.</p>",
    ),
]
# 25 postings, more than the 20 the feed returns when `rows` is not asked for.
_SF_ITEMS = _SF_VERBATIM + [
    (
        f"Structural Analyst {n} (Decatur, AL, US, 35601)",
        f"https://jobs.ulalaunch.com/job/Decatur-Structural-Analyst-{n}-AL-35601/14128{n:05d}/"
        "?feedId=null&amp;utm_source=J2WRSS",
        "Sat, 03 Oct 2026 7:00:00 GMT",
        "<p>Analyse structures.</p>",
    )
    for n in range(22)
]


def _sf_feed(items):
    body = "".join(
        f"<item><title><![CDATA[{t}]]></title><description><![CDATA[{d}]]></description>"
        f"<pubDate>{p}</pubDate><link>{u}</link><guid>{u}</guid></item>"
        for t, u, p, d in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?><rss version=\'2.0\' '
        "xmlns:atom='http://www.w3.org/2005/Atom'><channel><title>United Launch Alliance - "
        f"Custom Search </title><ttl>720</ttl> {body}</channel></rss>"
    ).encode()


def _sf_sitemap(items, shape):
    if shape == "urlset":
        # ULA's urlset is in Google's old namespace, not sitemaps.org's.
        locs = "".join(
            f"<url><loc>{u.split('?')[0]}</loc><lastmod>2026-10-03</lastmod></url>"
            for _, u, _, _ in items
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?><urlset '
            f'xmlns="http://www.google.com/schemas/sitemap/0.9">{locs}</urlset>'
        ).encode()
    # Deere, Halliburton, Boston Scientific and SAP serve a Google Base feed of
    # every posting there instead, its urls in <link>, and a channel <link>
    # that is not a posting.
    rows = "".join(
        f"<item><title>{t.replace('&', '&amp;')}</title><link>{u.split('?')[0]}</link>"
        f"<g:id>{u.split('?')[0].rstrip('/').rsplit('/', 1)[-1]}</g:id></item>"
        for t, u, _, _ in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0" '
        'xmlns:g="http://base.google.com/ns/1.0"><channel><title>Jobs at John Deere</title>'
        f"<link>https://jobs.deere.com/</link>{rows}</channel></rss>"
    ).encode()


class _Bytes(_Resp):
    @property
    def content(self):
        return self.body


def _sf_board(monkeypatch, feed_items, sitemap_items, feed=None, shape="urlset"):
    """The feed as it behaves live: the first `rows` items, 20 without it,
    startrow and every other paging parameter ignored, and 406 unless the
    request accepts RSS."""
    asked = []

    def get(url, headers=None, **kw):
        asked.append(url)
        parsed = urlparse(url)
        if parsed.path == "/sitemap.xml":
            return _Bytes(_sf_sitemap(sitemap_items, shape))
        assert "rss" in (headers or {}).get("Accept", ""), "the feed answers 406"
        rows = int((parse_qs(parsed.query).get("rows") or ["20"])[0])
        return _Bytes(feed if feed is not None else _sf_feed(feed_items[:rows]))

    monkeypatch.setattr(boards._session, "get", get)
    return asked


@pytest.mark.parametrize("shape", ["urlset", "rss"])
def test_successfactors_reads_every_posting_in_one_call_and_proves_it_on_the_sitemap(
    monkeypatch, shape
):
    asked = _sf_board(monkeypatch, _SF_ITEMS, _SF_ITEMS, shape=shape)
    out = boards.fetch_listings("https://jobs.ulalaunch.com/services/rss/job/", "ULA")
    # All 25: a fetcher that leaves out `rows` gets 20, and one that pages by
    # startrow gets the first page again, and the sitemap says 25.
    assert len({p.url for p in out}) == 25
    assert asked[0] == "https://jobs.ulalaunch.com/sitemap.xml"
    a, b, c = out[:3]
    assert (a.title, a.locations) == ("Program Control Analyst 4", ["Centennial, CO, US, 80112"])
    # The title's own parentheses stay in the title; the last group is the place.
    assert (b.title, b.locations) == (
        "Sr Spec, Quality Engrg (Supplier Quality)",
        ["Rochester, NY, US, 14623"],
    )
    # And the place can carry its own: the group that closes the heading.
    assert (c.title, c.locations) == (
        "Stagiaire Ingénieur Test et Mesure (F/H)",
        ["Arc Les Gray Cedex, Saône (Haute), FR, 70103"],
    )
    # The page a person opens, without the feed's tracking parameters.
    assert a.url == (
        "https://jobs.ulalaunch.com/job/Centennial-Program-Control-Analyst-4-CO-80112/1407431200"
    )
    assert a.date_posted == int(datetime.datetime(2026, 10, 4, 7, tzinfo=datetime.UTC).timestamp())
    assert a.company == "ULA"
    assert a.description == (
        "Program Control Analyst 4\n\nCentennial, CO, US, 80112\n\n"
        "Requisition ID: \n1858\n\nLocation: \nULA - Denver"
    )


@pytest.mark.parametrize("shape", ["urlset", "rss"])
def test_successfactors_is_partial_when_the_sitemap_lists_more_than_the_feed(monkeypatch, shape):
    # A feed that stops short of the board, as one capped above 2,233 would.
    _sf_board(monkeypatch, _SF_ITEMS[:24], _SF_ITEMS, shape=shape)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://jobs.ulalaunch.com/services/rss/job/", "ULA")
    assert len(raised.value.postings) == 24


def test_successfactors_query_error_is_a_failed_pull_not_an_empty_board(monkeypatch):
    error = b"<xml>Error: There is a problem with a jobs query: Query execution failed</xml>"
    _sf_board(monkeypatch, [], [], feed=error)
    with pytest.raises(ValueError):
        boards.fetch_listings("https://jobs.ulalaunch.com/services/rss/job/", "ULA")


def test_successfactors_refuses_a_document_that_declares_entities(monkeypatch):
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><rss><channel/></rss>'
    )
    _sf_board(monkeypatch, [], [], feed=bomb)
    with pytest.raises(ValueError, match="DOCTYPE"):
        boards.fetch_listings("https://jobs.ulalaunch.com/services/rss/job/", "ULA")


# jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com, first row, 2026-10-05.
NGC_ROW = {
    "id": 1340074304655,
    "displayJobId": "R10250443",
    "name": "U103 PRODUCTION COORD A",
    "locations": ["United States-California-Sunnyvale"],
    "standardizedLocations": ["Sunnyvale, CA, US"],
    "postedTs": 1791158400,
    "solrScore": None,
    "stars": 0,
    "department": "NGC - Non - NGJF Union",
    "creationTs": 1789689600,
    "isHot": 0,
    "workLocationOption": "onsite",
    "locationFlexibility": None,
    "atsJobId": "R10250443",
    "positionUrl": "/careers/job/1340074304655",
}


def _pcsx_board(rows: list[dict], asked: list[str]):
    """The live contract, measured on Northrop, Lockheed and Starbucks on
    2026-10-05: ten rows from `start` whatever `num` says, the count on every
    page, and an empty page (still 200, still the count) past the end."""

    def get(url, **kw):
        asked.append(url)
        start = int(parse_qs(urlparse(url).query)["start"][0])
        return _Resp(
            {
                "status": 200,
                "error": {"message": "", "body": ""},
                "data": {"positions": rows[start : start + 10], "count": len(rows)},
            }
        )

    return get


def _ngc_rows(n: int) -> list[dict]:
    return [
        NGC_ROW | {"id": i, "name": f"Engineer {i}", "positionUrl": f"/careers/job/{i}"}
        for i in range(n)
    ]


def test_eightfold_pages_by_the_ten_rows_it_returns_until_an_empty_page(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", _pcsx_board(_ngc_rows(23), asked))
    out = boards.fetch_listings(
        "https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com", "Northrop Grumman"
    )
    # Every row, so a loop that advanced by a page size it asked for, or
    # stopped on a short first page, fails here.
    assert [p.title for p in out] == [f"Engineer {i}" for i in range(23)]
    assert [parse_qs(urlparse(u).query)["start"] for u in asked] == [
        ["0"],
        ["10"],
        ["20"],
        ["23"],
    ]
    assert all(parse_qs(urlparse(u).query)["domain"] == ["ngc.com"] for u in asked)
    p = out[0]
    assert p.url == "https://jobs.northropgrumman.com/careers/job/0"
    assert p.company == "Northrop Grumman"
    # The tenant's own place, not standardizedLocations, which Eightfold
    # derives and gets wrong (Lockheed's Dartmouth, Nova Scotia read
    # "Dartmouth, England, GB").
    assert p.locations == ["United States-California-Sunnyvale"]
    assert p.date_posted == 1791158400
    assert p.raw is not None and p.raw["atsJobId"] == "R10250443"


def test_an_eightfold_pull_that_lost_a_posting_to_a_shifting_page_is_partial(monkeypatch):
    """Microsoft, 2026-10-05: one 3-minute pull read 2,300 rows of which 2,256
    were distinct against a stated 2,297. Here the posting at row 12 is
    re-dated to the top after the first page is read: every later row shifts
    down by one, row 9 comes back on the second page and row 12 is never
    seen. The count never changes, so only counting distinct rows catches it."""
    rows = _ngc_rows(15)
    asked: list[str] = []
    board = _pcsx_board(rows, asked)

    def get(url, **kw):
        if len(asked) == 1:
            rows.insert(0, rows.pop(12))
        return board(url, **kw)

    monkeypatch.setattr(boards._session, "get", get)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://x.eightfold.ai/api/pcsx/search?domain=x.com", "X")
    assert sorted(int(p.url.rsplit("/", 1)[1]) for p in raised.value.postings) == [
        i for i in range(15) if i != 12
    ]


def test_eightfold_v2_reads_its_own_shape(monkeypatch):
    # explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com, 2026-10-05.
    row = {
        "id": 790298014263,
        "name": "AI Engineer 6 - AI Foundation & Tooling, Ads Platform",
        "posting_name": "AI Engineer 6 - AI Foundation & Tooling, Ads Platform",
        "location": "Remote, United States",
        "locations": ["Remote, United States"],
        "hot": 1,
        "department": "Data & Insights",
        "business_unit": "Streaming",
        "t_update": 1779148800,
        "t_create": 1721692800,
        "ats_job_id": "AJRT30201",
        "display_job_id": "AJRT30201",
        "type": "ATS",
        "id_locale": "AJRT30201-en-US",
        "job_description": "",
        "locale": "en-US",
        "stars": 0,
        "medallionProgram": None,
        "location_flexibility": None,
        "work_location_option": "onsite",
        "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790298014263",
        "isPrivate": False,
    }

    def get(url, **kw):
        first = parse_qs(urlparse(url).query)["start"] == ["0"]
        return _Resp({"domain": "netflix.com", "positions": [row] if first else [], "count": 1})

    monkeypatch.setattr(boards._session, "get", get)
    (p,) = boards.fetch_listings(
        "https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com", "Netflix"
    )
    assert p.url == "https://explore.jobs.netflix.net/careers/job/790298014263"
    assert (p.title, p.locations, p.date_posted) == (
        "AI Engineer 6 - AI Foundation & Tooling, Ads Platform",
        ["Remote, United States"],
        1721692800,
    )


def test_every_eightfold_tenant_shares_one_pace(monkeypatch):
    """One WAF fronts tenants on different domains, so two tenants' pages wait
    on each other as one host's would."""
    slept = []
    monkeypatch.setattr(boards.time, "sleep", lambda s: slept.append(round(s, 1)))
    monkeypatch.setattr(boards._session, "get", _pcsx_board([], []))
    monkeypatch.setattr(boards, "_PACE_SECONDS", {})
    monkeypatch.setattr(boards, "_last_call", {})
    boards.set_pace({"eightfold.ai": 1})
    boards.fetch_listings("https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com", "N")
    boards.fetch_listings("https://caci.eightfold.ai/api/pcsx/search?domain=caci.com", "C")
    assert slept and 0.0 < slept[-1] <= 1.0


# --- Avature ----------------------------------------------------------------
#
# Search-result markup copied from each tenant's live page on 2026-10-05, the
# share popups trimmed and the job's slug, id and title made parameters. Each
# tenant designs its own template, so the fetcher is tested against every one
# it was measured on.

AV_BLOOMBERG = (
    '<article class="article article--result" id="article--1"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title title title--04">'
    '<a class="link" href="https://bloomberg.avature.net/careers/JobDetail/{slug}/{id}"> {title} </a>'
    '</h3><div class="article__header__text__subtitle"><span class="list-item-location">'
    'Hong Kong, Hong Kong</span></div></div></div><div class="article__footer">'
    '<a class="button button--primary" href="https://bloomberg.avature.net/careers/JobDetail/'
    '{slug}/{id}" tabindex="0"> Apply </a><a class="button button--secondary" '
    'href="https://bloomberg.avature.net/careers/SaveJob?jobId={id}" tabindex="0"> Save </a>'
    "</div></article>"
)
AV_TWO_SIGMA = (
    '<article class="article article--result" id="article--1"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title title title--065">'
    '<a class="link" href="https://careers.twosigma.com/careers/JobDetail/{slug}/{id}"> {title} </a>'
    '</h3><div class="article__header__content"><div class="article__header__content__text">'
    '<span class="paragraph_inner-span">United States - NY New York</span>'
    '<div class="article__header__content__sub-text"><span class="paragraph_inner-span">'
    'Engineering</span><span class="paragraph_inner-span">Experienced</span></div></div>'
    '<div class="article__footer"><a class="button button--primary button--list-results--mobile" '
    'href="https://careers.twosigma.com/careers/JobDetail/{slug}/{id}" tabindex="0"> View role </a>'
    "</div></div></div></div></article>"
)
AV_SIEMENS = (
    '<article class="article article--result 1" id="article--1"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title title title--h3 '
    'title--white" data-au="ag-h3-6"><a class="link" data-au="ag-a-10" '
    'href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/{id}"> {title} </a></h3>'
    '<div class="article__header__text__subtitle"><span class="list-item-location">'
    '<span class="list-item-jobCity">Bangalore</span><span aria-hidden="true" class="separator">'
    ', </span><span class="list-item-jobState">Karnataka</span><span aria-hidden="true" '
    'class="separator">, </span><span class="list-item-jobCountry">India</span></span>'
    '<span aria-hidden="true" class="separator"> • </span><span class="list-item-jobId">'
    'Job ID: {id}</span></div></div></div><div class="article__footer">'
    '<a aria-label="Learn more" class="button button--primary" data-au="ag-a-12" '
    'href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/{id}" tabindex="0"> Learn more </a>'
    "</div></article>"
)
AV_KOCH = (
    '<article class="article article--result"><div class="article__header">'
    '<div class="article__header__text"><h5 class="article__header__text__subtitle"> Molex </h5>'
    '<div class="article__header__actions"></div><h3 class="article__header__text__title '
    'article__header__text__title--7"><a href="https://koch.avature.net/en_US/careers/JobDetail/'
    '{slug}/{id}"> {title} </a></h3></div></div><div class="article__content">'
    '<div class="article__content__field m--t--s"><div class="article__content__field__label"> '
    'Location: </div><div class="article__content__field__value"> Hanoi, Hanoi </div></div>'
    '<div class="article__content__field m--t--s"><div class="article__content__field__label"> '
    'Job Number: </div><div class="article__content__field__value"> {id} </div></div></div>'
    "</article>"
)
AV_HARMAN = (
    '<article class="article article--result" id="article--1"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title title title--04">'
    '<a class="link" href="https://jobsearch.harman.com/en_US/careers/JobDetail/{slug}/{id}"> '
    '{title} </a></h3><div class="article__header__text__subtitle"><span class="list-item-location">'
    '<strong> Location:</strong> Aarhus - Central Jutland, Denmark</span><span aria-hidden="true" '
    'class="separator"> • </span><span class="list-item-ref"><strong> Ref #</strong> '
    "R-55686-2026</span></div></div></div></article>"
)
AV_CDCN = (
    '<article class="article article--result"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title '
    'article__header__text__title--4"><a href="https://cdcn.avature.net/careers/JobDetail/{slug}/'
    '{id}"> {title} </a></h3><div class="article__header__text__subtitle"><span> MT - Missoula '
    '</span> · <span> Posted 14-Jul-2026 </span></div></div><div class="article__header__actions">'
    '<a class="button button--secondary" href="https://cdcn.avature.net/careers/ApplicationMethods?'
    'jobId={id}" tabindex="0"> Apply </a></div></div></article>'
)
AV_POMERLEAU = (
    '<article class="article article--result"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title '
    'article__header__text__title--4"><a href="https://pomerleau.avature.net/en_US/careers/'
    'JobDetail/{slug}/{id}"> {title} </a></h3><div class="article__header__text__subtitle">'
    '<span class="list-item-ref">Ref #7144</span><span aria-hidden="true" class="separator"> • '
    '</span><span class="list-item-posted">Posted 02-Oct-2026</span></div></div></div></article>'
)


def _av_article(template: str, n: int) -> str:
    return template.format(slug=f"Role-{n}", id=n, title=f"Role {n}")


def _av_page(articles: str, next_link: str = "", legend: str = "") -> str:
    """A search page around its results, laid out as the live pages are."""
    return (
        f'<html><body><div class="list-controls list-controls--top clearfix">{legend}'
        f'<nav aria-label="Pagination Navigation">{next_link}</nav></div>'
        f'<div class="results results--listed">{articles}</div></body></html>'
    )


class _AvResp:
    def __init__(self, url: str, text: str, status: int = 200):
        self.url, self.text, self.status_code = url, text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise boards.requests.HTTPError(str(self.status_code))


def _av_serve(monkeypatch, pages):
    """GET answered by `pages(url)`, every url asked recorded."""
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        return pages(url)

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_avature_follows_the_next_link_the_page_gives(monkeypatch):
    """Bloomberg on 2026-10-05: 12 rows a page whatever jobRecordsPerPage
    asks, "1-12 of 348 results", and a next link to jobOffset=12. Here the
    tenant pages by 2, so a loop that steps by the size it asked for skips
    rows, and one that stops on a short page stops after the first."""
    search = "https://bloomberg.avature.net/careers/SearchJobs"

    def pages(url):
        offset = int(url.split("jobOffset=")[1]) if "jobOffset=" in url else 0
        rows = [n for n in (1, 2, 3) if offset < n <= offset + 2]
        following = (
            '<a class="list-controls__pagination__item link paginationNextLink" '
            f'href="{search}/?jobRecordsPerPage=2&amp;jobOffset={offset + 2}"> Next &gt;&gt; </a>'
            if offset + 2 < 3
            else ""
        )
        legend = (
            '<div class="list-controls__text__legend" aria-label="3 results"> '
            f"{offset + 1}-{offset + len(rows)} of 3 results </div>"
        )
        body = "".join(_av_article(AV_BLOOMBERG, n) for n in rows)
        return _AvResp(url, _av_page(body, following, legend))

    asked = _av_serve(monkeypatch, pages)
    out = boards.fetch_listings(search, "Bloomberg")
    assert [p.title for p in out] == ["Role 1", "Role 2", "Role 3"]
    assert out[0].url == "https://bloomberg.avature.net/careers/JobDetail/Role-1/1"
    assert out[0].locations == ["Hong Kong, Hong Kong"]
    assert all(p.company == "Bloomberg" for p in out)
    assert asked == [search, f"{search}/?jobRecordsPerPage=2&jobOffset=2"]


def test_avature_pages_by_the_offset_the_template_names(monkeypatch):
    """Siemens pages by folderOffset, puts the next link's class on its <li>,
    and answers its first page to every jobOffset (2026-10-05), so a fetcher
    that builds jobOffset itself reads page one forever."""
    search = "https://siemens.avature.net/en_US/externaljobs/SearchJobs"
    final = "https://jobs.siemens.com/en_US/externaljobs/SearchJobs"

    def pages(url):
        second = "folderOffset=2" in url
        rows = (3,) if second else (1, 2)
        following = (
            ""
            if second
            else '<li class="list-controls__pagination__item paginationNextLink"> '
            f'<a href="{final}/?folderRecordsPerPage=2&amp;folderOffset=2"> Next </a> </li>'
        )
        legend = (
            '<div aria-label="999+ results" class="list-controls__text__legend"> '
            "1 - 2 of 999+ results </div>"
        )
        body = "".join(_av_article(AV_SIEMENS, n) for n in rows)
        return _AvResp(final if url == search else url, _av_page(body, following, legend))

    _av_serve(monkeypatch, pages)
    out = boards.fetch_listings(search, "Siemens")
    assert [p.title for p in out] == ["Role 1", "Role 2", "Role 3"]
    assert out[2].url == "https://jobs.siemens.com/en_US/externaljobs/JobDetail/3"
    assert out[0].locations == ["Bangalore, Karnataka, India"]


def test_an_avature_walk_short_of_its_stated_count_is_partial(monkeypatch):
    """The legend says four and the walk ends at three: one posting was not
    seen, which is not evidence it closed."""
    legend = '<div class="list-controls__text__legend"> 1-3 of 4 results </div>'
    body = "".join(_av_article(AV_BLOOMBERG, n) for n in (1, 2, 3))
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body, "", legend)))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://bloomberg.avature.net/careers/SearchJobs", "Bloomberg")
    assert len(raised.value.postings) == 3


def test_an_avature_next_link_back_to_a_page_already_read_is_partial(monkeypatch):
    """Two Sigma states no count. A next link that leads to rows already seen
    means the list moved or wrapped, so the walk proves nothing."""
    search = "https://twosigma.avature.net/careers/OpenRoles"
    following = f'<a class="paginationNextLink" href="{search}/?jobOffset=2"> Next </a>'
    body = "".join(_av_article(AV_TWO_SIGMA, n) for n in (1, 2))
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body, following)))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(search, "Two Sigma")
    assert [p.title for p in raised.value.postings] == ["Role 1", "Role 2"]


def test_an_avature_walk_into_the_window_is_partial(monkeypatch):
    """Koch states no count and answers 406 from offset 2,000; Siemens answers
    an empty page there. Either way the rows past it are unseen."""
    monkeypatch.setattr(boards, "_AVATURE_WINDOW", 2)
    search = "https://koch.avature.net/en_US/careers/SearchJobs"

    def koch(url):
        if "jobOffset=2" in url:
            return _AvResp(url, "<html>406 Not Acceptable</html>", 406)
        following = f'<a class="paginationNextLink" href="{search}/?jobOffset=2"> Next </a>'
        body = "".join(_av_article(AV_KOCH, n) for n in (1, 2))
        return _AvResp(url, _av_page(body, following))

    _av_serve(monkeypatch, koch)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(search, "Koch")
    assert [p.locations for p in raised.value.postings] == [["Hanoi, Hanoi"]] * 2

    # An empty page at the window ends the walk as the end of the list would.
    body = "".join(_av_article(AV_TWO_SIGMA, n) for n in (1, 2))
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body)))
    with pytest.raises(boards.PartialPull):
        boards.fetch_listings("https://twosigma.avature.net/careers/OpenRoles", "Two Sigma")


def test_an_uncounted_avature_walk_under_the_window_is_complete(monkeypatch):
    body = "".join(_av_article(AV_TWO_SIGMA, n) for n in (1, 2))
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body)))
    out = boards.fetch_listings("https://twosigma.avature.net/careers/OpenRoles", "Two Sigma")
    assert [p.locations for p in out] == [["United States - NY New York"]] * 2


@pytest.mark.parametrize(
    "template, expected",
    [
        (AV_BLOOMBERG, ["Hong Kong, Hong Kong"]),
        (AV_TWO_SIGMA, ["United States - NY New York"]),
        (AV_SIEMENS, ["Bangalore, Karnataka, India"]),
        (AV_KOCH, ["Hanoi, Hanoi"]),
        # The label is a <strong> inside the location span.
        (AV_HARMAN, ["Aarhus - Central Jutland, Denmark"]),
        (AV_CDCN, ["MT - Missoula"]),
        # No place on the list page: nothing, not the reference number.
        (AV_POMERLEAU, []),
    ],
)
def test_avature_reads_the_place_from_each_template(monkeypatch, template, expected):
    body = _av_article(template, 1)
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body)))
    [posting] = boards.fetch_listings("https://x.avature.net/careers/SearchJobs", "X")
    assert posting.title == "Role 1"
    assert posting.locations == expected
