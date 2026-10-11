"""Database rows shared by pipeline API behavior tests."""

from __future__ import annotations

import datetime
import itertools

from api import db
from tests import mail_log

_seq = itertools.count(1)


def next_message_suffix():
    return next(_seq)


def insert_application(uid, *, company, title=None, prov="tracker", job_id=None, applied=None):
    return db.query_one(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "applied_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (uid, job_id, company, title, prov, applied),
    )["id"]


def insert_takeout_message(uid):
    return db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
        "VALUES (%s,%s,'takeout','s',%s) RETURNING id",
        (uid, f"p-{next_message_suffix()}", datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)),
    )["id"]


def insert_mail_event(mid, kind):
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,%s,'high')", (mid, kind)
    )


def insert_message_match(mid, app_id, method="ats_company"):
    mail_log.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s,%s,%s,'high')",
        (mid, app_id, method),
    )


def insert_board_job(uid, company, title, status="Application Submitted"):
    job = db.query_one(
        "INSERT INTO jobs (url, raw_url, source, company, title, active) "
        "VALUES (%s,%s,'fulltime',%s,%s,TRUE) RETURNING id",
        (
            f"https://x/{next_message_suffix()}",
            f"https://x/{next_message_suffix()}",
            company,
            title,
        ),
    )["id"]
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s,%s,%s)", (uid, job, status)
    )
    return job


def insert_gmail_message(uid, *, subject="s", sender="a@b.com", kind=None, company=None):
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, from_email, "
        "sent_at) VALUES (%s,%s,'gmail',%s,%s,now()) RETURNING id",
        (uid, f"um-{next_message_suffix()}", subject, sender),
    )["id"]
    if kind:
        mail_log.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail) "
            "VALUES (%s,%s,'high',%s)",
            (mid, kind, db.jsonb({"company": company} if company else {})),
        )
    return mid


def insert_board_application(uid, company, title, status="Application Submitted"):
    job = db.query_one(
        "INSERT INTO jobs (url, raw_url, source, company, title, active) "
        "VALUES (%s,%s,'fulltime',%s,%s,TRUE) RETURNING id",
        (
            f"https://s/{next_message_suffix()}",
            f"https://s/{next_message_suffix()}",
            company,
            title,
        ),
    )["id"]
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s,%s,%s)",
        (uid, job, status),
    )
    app_id = db.query_one(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "applied_at) VALUES (%s,%s,%s,%s,'tracker',now()) RETURNING id",
        (uid, job, company, title),
    )["id"]
    return job, app_id


_ASKED_BY = {"respond_to_offer": "offer", "complete_assessment": "assessment_invite"}


def insert_action_request(uid, app_id, kind="respond_to_offer"):
    """An ask: a message whose current event asks it, on the application.
    Its id is the event's."""
    mid = insert_gmail_message(uid, kind=_ASKED_BY[kind], company="Acme")
    insert_message_match(mid, app_id)
    return db.query_one("SELECT id FROM email_events WHERE message_id = %s", (mid,))["id"]


def insert_gmail_thread(uid, thread, n, kind="rejection", company="Acme"):
    ids = []
    for i in range(n):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, "
            "source, subject, from_email, sent_at) "
            "VALUES (%s,%s,%s,'gmail',%s,'a@acme.com',now()) RETURNING id",
            (uid, f"th-{next_message_suffix()}", thread, f"msg {i}"),
        )["id"]
        mail_log.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail) "
            "VALUES (%s,%s,'high',%s)",
            (mid, kind, db.jsonb({"company": company})),
        )
        ids.append(mid)
    return ids
