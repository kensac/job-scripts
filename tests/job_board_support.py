from __future__ import annotations

from api import db
from core.store import add_ai_result
from tests import factories


def _uid(headers: dict) -> int:
    return db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))["id"]


def _insert_job(
    source: str,
    url: str,
    active: bool = True,
    uploaded_by=None,
    locations=None,
    terms=None,
    date_posted=None,
    company: str = "Acme",
    title: str = "Engineer",
) -> int:
    row = db.query_one(
        """
        INSERT INTO jobs (url, raw_url, company, title, locations, terms, source, active,
                          available, date_posted)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            url,
            url,
            company,
            title,
            locations or [],
            terms or [],
            source,
            active,
            active,  # stored availability, as a source listing it leaves it
            date_posted,
        ),
    )
    if uploaded_by is not None:
        factories.upload(row["id"], uploaded_by)
    return row["id"]


def _subscribe(uid: int, source: str) -> None:
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (uid, source),
    )


def _pass_closed(url: str) -> None:
    add_ai_result(url, "passed", check_type="closed")


def _job_ids(payload: dict) -> set:
    return {r["job_id"] for r in payload["rows"]}
