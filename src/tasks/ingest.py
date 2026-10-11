"""Source ingest: fetch a board's postings and cache their pages.

Deliberately runs no AI - it arrives through the task queue, which makes it
scheduled work, and scheduled work batches.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from api import db, filter_runs, hosts, metrics, queue, telemetry, user_settings
from api.ai import verdicts
from core import page_fetches
from core.fetching.hosts import pace_key
from tasks.board import content_ready_urls
from tasks.runtime import Deferred, cancelled, set_progress

logger = logging.getLogger(__name__)


def _switch_off_if_given_up(source: str, exc: Exception) -> None:
    """This failed pull, with the run before it, reached
    ingest_give_up_after_failures: the board is switched off, which makes
    its postings unavailable (catalog.AVAILABLE) and opens source_switched_off
    so an administrator sees why. Switched back on, the board is pulled at
    the next cycle and one more failure switches it off again."""
    run = queue.failure_runs([source]).get(source)
    failures = (run["failures"] if run else 0) + 1  # this pull is still running
    if failures < int(db.get_config("ingest_give_up_after_failures")):
        return
    if not db.execute_count(
        "UPDATE sources SET active = false WHERE name = %s AND active", (source,)
    ):
        return
    logger.warning(f"Ingest {source}: switched off after {failures} failed pulls in a row")
    telemetry.capture(
        "source_switched_off",
        properties={"source": source, "failures": failures, "error": str(exc)[:500]},
    )


async def handle_ingest_source(task_id: int, payload: dict[str, Any]) -> None:
    from core import catalog
    from core.fetching import boards, client
    from core.fetching.posting import FALLBACK_CUTOFF_TS

    source = db.query_one("SELECT * FROM sources WHERE name = %s AND active", (payload["source"],))
    if not source:
        raise LookupError(queue.INACTIVE_SOURCE_ERROR)

    client.set_pace(db.get_config("ingest_host_pace_seconds") or {})
    host = pace_key(source["listings_url"])
    # This address's slot for the host, or the task waits for it: the claim
    # checks the same row, so this is the race of two workers on one address.
    opens = hosts.take(host)
    if opens is not None:
        raise Deferred(opens)
    complete = True
    try:
        postings = await asyncio.to_thread(
            boards.fetch_listings, source["listings_url"], source["company"]
        )
    except boards.PartialPull as partial:
        postings, complete = partial.postings, False
    except Exception as exc:
        response = getattr(exc, "response", None)
        # An AWS WAF challenge is the same refusal in another shape: Eightfold
        # answers 405 with x-amzn-waf-action: captcha once an address has
        # asked too much (on 2026-10-05, every tenant tried but Microsoft), and
        # counted as a failure it would switch the boards off.
        if getattr(response, "status_code", None) == 429 or (
            response is not None and response.headers.get("x-amzn-waf-action")
        ):
            # The host said too many for THIS address: its gap doubles and its
            # slot closes, but the pull itself goes back almost at once, so an
            # address the host is not refusing takes it. Holding the pull
            # until the refused address reopened kept work waiting on the one
            # address that could not do it (hetzner, 0 for 18, 2026-09-06).
            hosts.refused(host)
            raise Deferred(hosts.soon()) from exc
        # The board itself failed to answer: the source, the host and the
        # HTTP status when there was one, so a board going dark is a query
        # rather than a traceback search.
        telemetry.capture(
            "ingest_pull_failed",
            properties={
                "source": source["name"],
                "fetch_host": host,
                "status": getattr(response, "status_code", None),
                "error_class": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )
        _switch_off_if_given_up(source["name"], exc)
        raise
    hosts.succeeded(host)
    fetched = len(postings)
    listed = postings
    pattern_enforced = bool(db.get_config("source_title_patterns_enabled"))
    if source["title_pattern"]:
        keep = re.compile(source["title_pattern"], re.IGNORECASE)
        pattern_matched = [p for p in listed if keep.search(p.title)]
    else:
        pattern_matched = listed
    postings = pattern_matched if pattern_enforced else listed
    # Everything the board returned stays on record, admitted or not, with
    # the text the listing carried: a candidate pattern is judged against the
    # titles the board actually listed, a posting a better pattern admits
    # arrives here on the next pull, and a backfill reads the text from here
    # instead of scraping the page again.
    listings_inline = catalog.record_listings(
        listed,
        source["name"],
        source["title_pattern"] or "",
        {p.url for p in pattern_matched},
        int(db.get_config("listings_seen_refresh_hours")),
    )
    if listings_inline:
        # Object storage was unavailable, so this pull's new text and raw
        # records went inline. Nothing is lost and the next pull moves them,
        # but an outage that lasts is a table growing back, so it is said.
        telemetry.capture(
            "listings_stored_inline",
            properties={"source": source["name"], "values": listings_inline},
        )
    upserted = catalog.upsert_postings(postings, source["name"])
    # What the pull says, as facts: availability is read from them
    # (catalog.AVAILABLE, stored in jobs.available). A complete pull of a
    # board that lists every open posting (boards.AUTHORITATIVE) records the
    # rows it left out as unlisted, which makes them unavailable; an
    # aggregator's absence never closes anything; and a pull that cannot show
    # it saw everything (partial, or empty, which is a broken fetch rather
    # than an empty board) records no absence. jobs.active is frozen and not
    # written (docs/agents/architecture-migration.md, the jobs.active
    # contract).
    authoritative = boards.kind(source["listings_url"]) in boards.AUTHORITATIVE
    observed = catalog.observe(
        source["name"],
        task_id,
        listed,
        {p.url for p in pattern_matched},
        None if not (fetched and complete) else "unlisted" if authoritative else "not_listed",
    )
    retired = observed.get("unlisted", 0)
    metrics.INGEST_JOBS.labels(source["name"], "retired").inc(retired)
    metrics.INGEST_JOBS.labels(source["name"], "fetched").inc(fetched)
    metrics.INGEST_JOBS.labels(source["name"], "title_pattern_missed").inc(
        fetched - len(pattern_matched)
    )
    metrics.INGEST_JOBS.labels(source["name"], "title_excluded").inc(fetched - len(postings))
    metrics.INGEST_JOBS.labels(source["name"], "upserted").inc(upserted)
    logger.info(
        f"Ingest {source['name']}: fetched {fetched}, "
        f"pattern missed {fetched - len(pattern_matched)}, "
        f"admitted {len(postings)}, upserted {upserted}"
    )

    # Ingest caches pages but runs NO AI. It reached here through the task
    # queue, which makes it scheduled work, and the rule for scheduled work is
    # that it batches: the hourly verify_new task settles closed+clearance as
    # one half-price call per job. Checking inline here bypassed that. It was
    # why ~90% of closed/clearance verdicts were still full-price sync calls,
    # since verify_new only ever saw the jobs ingest had not already reached.
    #
    # Caching the page stays here on purpose. verify_new can only batch a job
    # whose text is already stored, and leaving that to fetch_missing_content
    # (100/cycle) would throttle verification far below the ingest rate.
    # refresh_content also tags where the text came from, which is what keeps
    # the ats_text_collapse detector fed with live-ingest data.
    candidates = [p for p in postings if p.active and p.url and p.date_posted >= FALLBACK_CUTOFF_TS]
    # One query to learn which postings already have text, instead of a
    # round trip per posting. The largest source carries ~2,800 active jobs
    # and almost all of them are already cached, so the per-posting form was
    # ~2,800 sequential queries pulling ~15MB to compute a boolean, hourly.
    total = len(candidates)
    have_content = content_ready_urls([p.url for p in candidates])
    # A posting whose fetch keeps coming back empty waits longer each time
    # and is eventually given up on (verdicts.fetch_parked_sql).
    tried_recently = verdicts.fetch_parked_urls(
        [p.url for p in candidates if p.url not in have_content]
    )
    cached = fetch_failed = gone = 0
    for i, p in enumerate(candidates):
        if i % 10 == 0 and cancelled(task_id):
            logger.info(f"Task {task_id} cancelled mid-ingest")
            return
        if p.url in have_content or p.url in tried_recently:
            continue
        if p.description:
            # The listing call already carried the posting's text, in the
            # same shape the ATS resolver would have fetched it. Storing it
            # is one insert; fetching it again is the request that gets a
            # worker blocked.
            page_fetches.record(p.url, "passed", "listing text", p.description)
            cached += 1
            metrics.INGEST_JOBS.labels(source["name"], "cached").inc()
            continue
        try:
            content, closure = await verdicts.refresh_content(
                p.url, company=p.company, job_title=p.title, context="ingest"
            )
        except Exception as exc:
            fetch_failed += 1
            logger.warning(f"Ingest {source['name']}: content fetch failed for {p.url}")
            # Swallowed on purpose so one page cannot fail the pull, which is
            # exactly why it must be recorded somewhere else.
            telemetry.capture_exception(
                exc, properties={"source": source["name"], "url": p.url, "context": "ingest"}
            )
            continue
        if closure:
            gone += 1
            continue
        if not content:
            fetch_failed += 1
            continue
        cached += 1
        metrics.INGEST_JOBS.labels(source["name"], "cached").inc()
        if cached % 5 == 0:
            set_progress(task_id, i + 1, total, source["name"])
    # The counts the health detectors read: a feed that returned nothing, a
    # pattern that admits nothing, a worker whose fetches stopped landing.
    # Kept on the task because nothing else records what one ingest saw.
    set_progress(
        task_id,
        total,
        total,
        source["name"],
        extra={
            "fetched": fetched,
            "kept": len(pattern_matched),
            "admitted": len(postings),
            "pattern_enforced": pattern_enforced,
            "already_cached": len(have_content),
            "skipped_recent_failure": len(tried_recently),
            "cached": cached,
            "fetch_failed": fetch_failed,
            "gone": gone,
            "retired": retired,
            "complete": complete,
            "observed": observed,
            "listings_inline": listings_inline,
        },
    )

    schedule_filter_runs(payload.get("cycle", "manual"))


def schedule_filter_runs(cycle: str) -> None:
    """One filter run per entitled user per ingest cycle.

    A run that has not split yet (pending or running) blocks the next: two
    splitters would select the same jobs. A run that HAS split and is waiting
    on its chunks does not block, because the splitter excludes every url a
    live chunk holds (board.in_flight_urls) and so judges only what arrived
    since. Before that exclusion the guard covered 'waiting' too, and one
    chunk parked on a straggler batch kept a user's new postings unjudged
    until the provider finished it - 6,226 postings for 14 hours on
    2026-09-04.
    """
    users = db.query(
        f"""
        SELECT DISTINCT u.id FROM users u
        JOIN user_source_set us ON us.user_id = u.id
        JOIN user_filters uf ON uf.user_id = u.id AND uf.enabled
        WHERE {user_settings.has_own_key_sql("u.id")}
           OR u.groups && ARRAY(SELECT group_name FROM group_budgets)::text[]
        """
    )
    for u in users:
        filter_runs.enqueue(
            u["id"], None, policy="scheduled", dedupe_key=f"runall:{u['id']}:{cycle}"
        )


async def handle_retire_switched_off(task_id: int, payload: dict[str, Any]) -> None:
    """Brings jobs.available to catalog.AVAILABLE over the whole catalog
    (catalog.reconcile_available). A switched-off source's postings stop
    being available here, within the hour, unless a switched-on source lists
    them; so does a title pattern setting flip. The task keeps its name,
    which the worker's schedule and its history key on."""
    from core import catalog

    reconciled = await asyncio.to_thread(catalog.reconcile_available)
    set_progress(
        task_id,
        reconciled,
        reconciled,
        f"stored availability changed on {reconciled} postings",
        extra={"available_reconciled": reconciled},
    )
