"""Work that reported success while doing nothing."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.health.evidence import (
    _int_from,
)

logger = logging.getLogger(__name__)


# The sweeps whose "done" is a row written. A poll that finds no batch
# terminal and a mail sync with nothing new both finish done with 0 of N and
# are right to; on 2026-09-06 they opened 463 and 5 warnings between them.
SWEEP_KINDS = frozenset(
    {
        "extract_comp",
        "extract_requirements",
        "verify_new",
        "reverify_chunk",
        "run_filter_chunk",
        "run_filter_batch_chunk",
        "classify_locations",
        "classify_mail",
        "embed_postings",
        "embed_postings_batch",
        "fetch_missing_content",
    }
)


def _detect_silent() -> list[dict[str, Any]]:
    """Work that reported success while doing nothing, from the audit of
    2026-09-05. Every check here is one indexed pass over tasks or the tiny
    health_alerts table, and every one can be made to fire in a test."""
    found: list[dict[str, Any]] = []

    # A sweep that finished done with work in front of it and none written.
    # comp and requirements used to count every line as done; now a line
    # counts only when its row lands, so this reads the honest number.
    for r in db.query(
        f"""
        SELECT kind, COUNT(*) AS n, MAX(id) AS task_id,
               MAX({_int_from("progress", "total")}) AS total
        FROM tasks
        WHERE status = 'done' AND finished_at > now() - interval '24 hours'
          AND {_int_from("progress", "total")} > 0 AND {_int_from("progress", "done")} = 0
          AND kind = ANY(%(kinds)s)
        GROUP BY kind
        """,
        {"kinds": sorted(SWEEP_KINDS)},
    ):
        found.append(
            {
                "kind": "sweep_did_nothing",
                "subject": r["kind"],
                "severity": "warning",
                "message": (
                    f"{r['n']} {r['kind']} sweep(s) in 24h finished done with up to "
                    f"{r['total']} items in front of them and none completed (latest task "
                    f"{r['task_id']}). The work was selected, paid for if batched, and "
                    "nothing was written."
                ),
                "detail": dict(r),
            }
        )

    # A kind failing repeatedly. ingest_source has its own per-board and
    # per-host detectors; everything else failed in silence: a rotated
    # encryption key fails probe_credentials three times an hour with no
    # invalid_at and no alert.
    for r in db.query(
        """
        SELECT kind, COUNT(*) AS n, MAX(LEFT(error, 200)) AS sample_error
        FROM tasks
        WHERE status = 'failed' AND finished_at > now() - interval '3 hours'
          AND kind <> 'ingest_source'
        GROUP BY kind HAVING COUNT(*) >= 3
        """
    ):
        found.append(
            {
                "kind": "task_kind_failing",
                "subject": r["kind"],
                "severity": "critical",
                "message": (
                    f"{r['n']} {r['kind']} tasks failed in 3h; last error: "
                    f"{r['sample_error'] or ''}"
                ),
                "detail": dict(r),
            }
        )

    # A task the reaper keeps handing back. A graceful exit requeues with
    # attempts - 1, so a task that kills its worker every run never reaches
    # the attempt ceiling and never fails.
    for r in db.query(
        """
        SELECT id, kind, attempts, created_at
        FROM tasks
        WHERE status IN ('pending', 'running') AND attempts >= 3
          AND created_at < now() - interval '6 hours'
        """
    ):
        found.append(
            {
                "kind": "task_requeued_forever",
                "subject": str(r["id"]),
                "severity": "warning",
                "message": (
                    f"task {r['id']} ({r['kind']}) is on attempt {r['attempts']} and has "
                    f"been in the queue since {r['created_at']:%Y-%m-%d %H:%M}. Nothing "
                    "ends a task that keeps coming back."
                ),
                "detail": {"id": r["id"], "kind": r["kind"], "attempts": r["attempts"]},
            }
        )

    # An address a host has shut out: refused repeatedly, never accepted. The
    # budget cannot open it and the work flows to the other addresses, but a
    # person should know one lane is dead against one host.
    from api import hosts as _hosts

    for r in db.query(
        """
        SELECT host, array_agg(egress_group ORDER BY egress_group) AS addresses,
               sum(refused) AS refused
        FROM host_budget WHERE ok = 0 AND refused >= %(n)s
        GROUP BY host
        """,
        {"n": _hosts.BLOCKED_AFTER},
    ):
        found.append(
            {
                "kind": "address_blocked_by_host",
                "subject": r["host"],
                "severity": "warning",
                "message": (
                    f"{r['host']} has refused every pull from {', '.join(r['addresses'])} "
                    f"({r['refused']} refusals, nothing accepted). Other addresses carry "
                    "its boards; this one needs a different exit or to be left alone."
                ),
                "detail": {"host": r["host"], "addresses": list(r["addresses"])},
            }
        )

    # An alert nobody was told about. _notify returns quietly when mail is not
    # configured or no admin has an address; notified_at was written but read
    # nowhere.
    from api import mail

    if mail.configured():
        r = db.query_one(
            """
            SELECT COUNT(*) AS n, MIN(first_seen) AS oldest
            FROM health_alerts
            WHERE resolved_at IS NULL AND notified_at IS NULL
              AND first_seen < now() - interval '1 hour'
            """
        )
        if r and r["n"]:
            found.append(
                {
                    "kind": "alerts_unnotified",
                    "subject": "_notify",
                    "severity": "warning",
                    "message": (
                        f"{r['n']} open alert(s) were never mailed, the oldest from "
                        f"{r['oldest']:%Y-%m-%d %H:%M}. Mail is configured, so the send "
                        "itself is failing or no admin has an address."
                    ),
                    "detail": {"n": r["n"]},
                }
            )
    return found
