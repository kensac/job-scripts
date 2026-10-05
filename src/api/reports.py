"""The posting-report vocabulary shared by user and admin surfaces."""

import datetime

from pydantic import BaseModel

from api import queue


class ReportKind(BaseModel):
    kind: str
    label: str


REPORT_KINDS = ("stale", "wrong_data", "closed", "other")
_REPORT_LABELS = {
    "stale": "Posting is stale",
    "wrong_data": "Details are wrong",
    "closed": "Posting is closed",
    "other": "Something else",
}


def report_kinds() -> list[ReportKind]:
    """The kinds a report can carry, with the label the form shows; one copy,
    served to the board's report modal and the admin reports page."""
    return [ReportKind(kind=kind, label=_REPORT_LABELS[kind]) for kind in REPORT_KINDS]


def request_recheck(job: dict) -> None:
    """Ask the fleet to re-verify one posting because a person doubts its verdict.

    closed and clearance verdicts live in ai_queries with no user column and
    every board reads the latest row per (url, check_type), so a person's own
    model run (possibly a personal key at any base_url) must never write one.
    A person's claim is a reason to look again, not a verdict: this queues the
    single-chunk re-verification the daily sweep runs, on the fleet's model
    and fetcher. Forced, because the standing answer is what is in doubt and
    an unforced chunk skips a posting checked in the last day. One per url per
    UTC day, so repeating the claim costs nothing and the fleet's spend is
    bounded by distinct postings.
    """
    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    queue.enqueue(
        "reverify_chunk",
        {
            "parent_id": None,
            "rows": [{"url": job["url"], "company": job["company"], "title": job["title"]}],
            "force": True,
        },
        dedupe_key=f"recheck:{day}:{job['url']}",
    )
