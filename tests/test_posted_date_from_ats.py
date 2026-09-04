"""A resolver that learns the posting's start date writes it onto a job the
board left undated, and never over a date the board stated."""

from __future__ import annotations

import datetime

import pytest

from api import db, verdicts
from core import ats


def _job(url: str, posted: datetime.date | None) -> int:
    row = db.query_one(
        "INSERT INTO jobs (url, raw_url, company, title, source, active, date_posted) "
        "VALUES (%s, %s, 'Acme', 'Engineer', 'src-wd', true, %s) RETURNING id",
        (url, url, posted),
    )
    assert row is not None
    return row["id"]


def test_workday_start_date_is_read_and_a_missing_one_is_none(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {
                "jobPostingInfo": {
                    "title": "Engineer",
                    "location": "Austin",
                    "startDate": "2026-08-12",
                    "jobDescription": "<p>" + "Build things. " * 50 + "</p>",
                }
            }

    wd = ats.Workday()
    monkeypatch.setattr(wd, "get", lambda url: Resp())
    res = wd.fetch("https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/Austin/Engineer_R1")
    assert res.ok and res.posted == datetime.date(2026, 8, 12)
    assert ats._iso_date("Posted 30+ Days Ago") is None and ats._iso_date(None) is None


@pytest.mark.asyncio
async def test_refresh_content_dates_an_undated_job_and_keeps_a_stated_date(monkeypatch):
    undated = "https://acme.wd1.myworkdayjobs.com/Careers/job/Austin/A"
    dated = "https://acme.wd1.myworkdayjobs.com/Careers/job/Austin/B"
    _job(undated, None)
    _job(dated, datetime.date(2026, 9, 1))
    monkeypatch.setattr(
        ats,
        "resolve",
        lambda url: ats.AtsResult(
            ats.Status.OK, "Engineer\n\n" + "text " * 400, "workday", datetime.date(2026, 8, 12)
        ),
    )
    await verdicts.refresh_content(undated)
    await verdicts.refresh_content(dated)
    rows = {
        r["url"]: r["date_posted"]
        for r in db.query(
            "SELECT url, date_posted FROM jobs WHERE url IN (%s, %s)", (undated, dated)
        )
    }
    assert rows[undated].date() == datetime.date(2026, 8, 12)
    assert rows[dated].date() == datetime.date(2026, 9, 1)
