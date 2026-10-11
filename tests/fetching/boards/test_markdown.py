"""core.fetching.boards: the listing fetchers, over rows copied from the live feeds.

Every excerpt below is a verbatim row from the board it names, fetched on
2026-09-04, so the fixture is the producer's shape rather than an expectation
of it. The ATS bodies are trimmed to the keys the fetcher reads plus the ones
that sat beside them.
"""

from __future__ import annotations

import datetime

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
    # The key is the posting's one spelling, and the tracking parameter is
    # kept on the raw URL.
    assert speedy[1].url == "https://www.amazon.jobs/en/jobs/10526808"
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
