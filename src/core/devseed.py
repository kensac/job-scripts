"""Fabricated rows shaped like production, for the tests that assert shapes.

NOT the dev API's data source. That is a throwaway COPY of production
(`scripts/sync_testdb.py --dev-role`, then `make dev-api`), because a copy
cannot get the shape wrong - it IS the shape, including the awkward cases
nobody has found yet. This module exists for the case a copy cannot serve: CI,
which has no production credentials and must still be able to assert that the
API's responses carry the shapes the frontend depends on.

The tests over this seed read it back THROUGH THE API rather than out of the
tables. That is the whole point. A fixture cannot falsify the assumption it
was built from, and the mock layer this replaced produced a 422 on every
resolve assignment (the client typed a field as an object, the router takes a
bare id, and the fixture encoded the same wrong belief so nothing testing
against it could catch it), four envelope-key mismatches, an "infinite append"
bug that was a stub ignoring its page parameter, and a "63,598 unmatched"
figure that was somebody's own fixture read back as a production observation.

EVERY ODDITY BELOW IS MEASURED, not invented, because tidy rows would
reproduce none of those:

  comp_currency is NULL on 96% of rows carrying an amount (452 of 11,442)
  body_text held raw HTML on 54.8% of messages (36,820 of 67,177)
  72% of messages carry at least one remote tracker, 2.0 fetches each
  email_events.confidence is a STRING - 'high' 81,788, 'medium' 138, 'low' 22
  detail.role_title EXISTS as a key and is NULL on 90% (73,892 of 81,948)
  jobs.active is feed state: it can be false while the closed check says open
  two filters can share one prompt_hash ("default" and "general" do)
  a configured source can have zero postings (six of them do)
  no open action item has a future deadline; the corpus is ~99% historical
"""

from __future__ import annotations

import datetime
from typing import Any

from api import db

DEV_SUB = "dev-user"
DEV_EMAIL = "dev@example.test"


def _assert_disposable() -> str:
    """Refuse to seed anything whose name does not mark it disposable.

    Same rule the test harness uses, and for a stronger reason: this writes
    fabricated rows. A dev API that can reach the production database is worse
    than no dev API, and the database NAME is the one thing a caller cannot
    get wrong by accident.
    """
    # Asked of the connection rather than parsed from a URL: the connection is
    # the thing that will actually be written to, and a URL can be overridden
    # anywhere between here and the socket.
    row = db.query_one("SELECT current_database() AS name")
    name = row["name"] if row else ""
    if not (name.endswith(("_dev", "_test", "_ci")) or name.startswith(("dev_", "test_"))):
        raise RuntimeError(
            f"refusing to seed database {name!r}: it writes fabricated rows. "
            "Name it *_dev, *_test or *_ci."
        )
    return name


def _days_ago(n: int) -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=n)


def _one(sql: str, params: Any = None) -> Any:
    row = db.query_one(sql, params)
    assert row is not None
    return next(iter(row.values()))


def seed() -> dict[str, int]:
    """Idempotent: keyed on natural keys so a re-run updates rather than
    duplicates, the same way the real import paths behave."""
    _assert_disposable()
    user_id = _one(
        "INSERT INTO users (sub, email, name, groups) VALUES (%s, %s, 'Dev User', %s) "
        "ON CONFLICT (sub) DO UPDATE SET email = EXCLUDED.email RETURNING id",
        (DEV_SUB, DEV_EMAIL, ["infra-admins", "jobtracker-users-internal"]),
    )
    counts = {"jobs": 0, "messages": 0, "applications": 0}

    # A source that produces nothing is a real state and the surface that
    # retires a board depends on telling it apart from one that is merely new.
    for name, active in (("devboard", True), ("silent_board", True), ("retired_board", False)):
        db.execute(
            "INSERT INTO sources (name, listings_url, active) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active",
            (name, f"https://{name}.test/list.json", active),
        )
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, 'devboard') ON CONFLICT DO NOTHING",
        (user_id,),
    )

    jobs = [
        # (company, title, active, comp_min, comp_max, currency, closed_verdict)
        ("Northwind", "Backend Engineer", True, 180000, 220000, None, "passed"),
        ("Northwind", "Backend Engineer", True, 180000, 220000, "USD", "passed"),
        ("Contoso", "Platform Engineer", True, None, None, None, "passed"),
        # active=false while the closed check says OPEN. 114 real applications
        # sit in exactly this state, and reading `active` as closure is the
        # bug that put a red badge on all of them.
        ("Fabrikam", "Data Engineer", False, 150000, 190000, None, "passed"),
        # genuinely closed
        ("Tailspin", "SRE", False, None, None, None, "rejected"),
        # never checked: NULL is a third state and must not render as closed
        ("Adventure Works", "ML Engineer", True, 200000, 260000, None, None),
    ]
    job_ids = []
    for i, (company, title, active, low, high, currency, verdict) in enumerate(jobs):
        url = f"https://devboard.test/job/{i}"
        job_id = _one(
            """
            INSERT INTO jobs (url, raw_url, source, company, title, active, date_posted,
                              comp_min, comp_max, comp_currency, comp_extracted)
            VALUES (%s, %s, 'devboard', %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO UPDATE SET active = EXCLUDED.active,
                comp_currency = EXCLUDED.comp_currency
            RETURNING id
            """,
            (
                url,
                url,
                company,
                title,
                active,
                _days_ago(i * 9 + 2),
                low,
                high,
                currency,
                low is not None,
            ),
        )
        job_ids.append(job_id)
        counts["jobs"] += 1
        for check in ("closed", "clearance"):
            status = verdict if check == "closed" else "passed"
            if status:
                db.execute(
                    "INSERT INTO ai_queries (url, check_type, status, reason, model) "
                    "VALUES (%s, %s, %s, %s, 'gpt-5-nano')",
                    (
                        url,
                        check,
                        status,
                        "still accepting" if status == "passed" else "position filled",
                    ),
                )
    # Materialised as board rows, which is what a completed filter run does -
    # and it is also what makes the awkward jobs VISIBLE. Seeded through the
    # visibility predicate rather than around it: a job that only exists in
    # `jobs` never reaches the wire, so a seed that stopped there would look
    # populated in the database and empty in the app.
    statuses = ["Application Submitted", None, None, "No Longer Interested", None, None]
    for job_id, status in zip(job_ids, statuses, strict=True):
        db.execute(
            "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, job_id) DO UPDATE SET status = EXCLUDED.status",
            (user_id, job_id, status),
        )

    # Two filters sharing one prompt_hash, because the real corpus has exactly
    # that and a row-per-name double counts the same decisions.
    from core.filters import build_custom_instructions, compute_prompt_hash

    shared = compute_prompt_hash(build_custom_instructions("pay over 200k", "keep"))
    for name, enabled in (("default", True), ("general", False)):
        db.execute(
            """
            INSERT INTO user_filters (user_id, name, prompt, on_ambiguous, enabled, prompt_hash)
            VALUES (%s, %s, 'pay over 200k', 'keep', %s, %s)
            ON CONFLICT (user_id, name) DO UPDATE SET enabled = EXCLUDED.enabled
            """,
            (user_id, name, enabled, shared),
        )
    for i, job_id in enumerate(job_ids[:4]):
        url = _one("SELECT url FROM jobs WHERE id = %s", (job_id,))
        # A rejection with no reason recorded is the state the batched paths
        # produced for weeks; the share denominator depends on seeing it.
        db.execute(
            "INSERT INTO ai_queries (url, check_type, status, reason, prompt_hash, model) "
            "VALUES (%s, 'custom', %s, %s, %s, 'gpt-5-nano')",
            (
                url,
                "rejected" if i else "passed",
                "" if i == 2 else "Salary not disclosed; cannot confirm the bar.",
                shared,
            ),
        )

    messages = [
        # (subject, from, plain_text, html, kind, confidence, role_title)
        (
            "Your application to Northwind",
            "no-reply@greenhouse.io",
            "Thanks for applying. We will be in touch.",
            None,
            "acknowledgement",
            "high",
            "Backend Engineer",
        ),
        # HTML body with two trackers - 72% of real messages carry at least one
        (
            "Interview scheduled",
            "recruiter@contoso.test",
            None,
            "<html><body><p>Hi, we would like to meet <b>Tuesday</b>.</p>"
            '<img src="https://tracker.test/open.gif?u=1">'
            '<img src="https://pixel.test/p.png"></body></html>',
            "interview_invite",
            "high",
            None,
        ),
        # role_title present as a key and NULL as a value: 90% of real rows
        (
            "Update on your application",
            "careers@fabrikam.test",
            "We are moving forward with other candidates.",
            None,
            "rejection",
            "medium",
            None,
        ),
        # a club acceptance the classifier reads as a job offer
        (
            "ACM Officer Application Decision",
            "officers@psu.edu",
            "Congratulations, you have been selected as an officer.",
            None,
            "offer",
            "high",
            None,
        ),
        (
            "low confidence one",
            "unknown@example.test",
            "hard to tell",
            None,
            "not_job_related",
            "low",
            None,
        ),
    ]
    message_ids = []
    for i, (subject, sender, plain, html, kind, confidence, role) in enumerate(messages):
        body_text = plain or "Hi, we would like to meet Tuesday."
        message_id = _one(
            """
            INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source,
                                        from_email, subject, sent_at, body_text, body_html,
                                        prefilter_hit)
            VALUES (%s, %s, 'dev-thread-1', 'olm', %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (user_id, provider_message_id) DO UPDATE SET body_html = EXCLUDED.body_html
            RETURNING id
            """,
            (user_id, f"dev-msg-{i}", sender, subject, _days_ago(30 + i * 5), body_text, html),
        )
        message_ids.append(message_id)
        counts["messages"] += 1
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
            "VALUES (%s, %s, %s, %s, 'gpt-5.6-luna')",
            (
                message_id,
                kind,
                confidence,
                db.jsonb({"company": subject.split()[-1], "role_title": role}),
            ),
        )

    application_id = _one(
        """
        INSERT INTO applications (user_id, job_id, company_name, title, source_provenance,
                                  applied_at)
        VALUES (%s, %s, 'Northwind', 'Backend Engineer', 'tracker', %s) RETURNING id
        """,
        (user_id, job_ids[0], _days_ago(40)),
    )
    counts["applications"] += 1
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, %s, 'ats_company', 'high')",
        (message_ids[0], application_id),
    )
    # Historical and unresolvable: no open action item in the real corpus has a
    # future deadline, and respond_to_offer has never once auto-resolved.
    for kind, due in (("complete_assessment", _days_ago(200)), ("respond_to_offer", None)):
        db.execute(
            "INSERT INTO action_items (user_id, application_id, kind, due_at) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, application_id, kind, due),
        )
    return counts
