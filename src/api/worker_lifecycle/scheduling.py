"""Leaderless cycle scheduling for sources, boards and derived work."""

from __future__ import annotations

import datetime
import logging
import os

from api import db, managed_boards, queue, telemetry
from api.board import visibility
from api.mail import match as mail_match
from api.queue import enqueue
from core.fetching.hosts import pace_key
from tasks import DERIVATIONS
from tasks.resume_backfill import schedule_next as schedule_resume_backfill

logger = logging.getLogger(__name__)


# How often to ask the provider whether parked batches have landed. One
# minute, matching the housekeeping tick, which is the floor: a finer bucket
# cannot fire more often than the loop that reads it.
#
# It shares no bucket with the ingest cycle deliberately. On the hourly one a
# parked task could sit an hour after its batch had finished, and across a
# multi-round backfill that latency exceeds the provider's own turnaround.
#
# Cost is one provider call per OPEN batch and nothing at all when there are
# none, so the frequency is bounded by politeness rather than expense.
BATCH_POLL_MINUTES = int(os.environ.get("JOBTRACKER_BATCH_POLL_MINUTES", "1"))
_managed_board_schedule_attempts: set[tuple[str, int]] = set()


def schedule_ingest_cycle() -> None:
    """Leaderless hourly scheduler: every worker calls this each poll; the
    dedupe key (source + time bucket) guarantees one task per source per cycle
    across the whole fleet."""
    now = datetime.datetime.now(datetime.UTC)
    interval = int(db.get_config("ingest_interval_minutes"))
    bucket = now.replace(
        minute=(now.minute // interval) * interval if interval < 60 else 0,
        second=0,
        microsecond=0,
    )
    cycle = bucket.strftime("%Y-%m-%dT%H:%M")
    # One set-based query, then ON CONFLICT inserts; the per-cycle dedupe key
    # is what makes this race-safe across the fleet. It must stay one query:
    # every worker runs this on EVERY poll, and #483 (2026-09-09) replaced it
    # with one locked transaction per active source (FOR UPDATE on the source
    # row plus four round trips), 389 of them a poll. Beside the database that
    # took a second; on oci, transmission, desktop and gcp-vps at 100 to
    # 200 ms a round trip it took minutes, the loop began the next poll's
    # pass as soon as it finished, and four of six workers never reached
    # worker_status or a claim again, with healthy containers and clean
    # logs. task_admission.enqueue stays for the request-triggered kinds,
    # which run once per request, not per poll.
    #
    # A source whose last cycle's ingest has not been claimed yet gets no
    # second one. Without this a queue that falls behind the hour grows by
    # one task per source per hour and never catches up: 69 boards added at
    # once on 2026-09-04 were still pending 40 minutes later behind two
    # aggregator ingests, with the next cycle due to add 79 more. A RUNNING
    # ingest does not block the next cycle's task, because that one will be
    # claimed as soon as it finishes.
    #
    # A source on a longer interval than the cycle is skipped while its last
    # successful or in-flight ingest is younger than that interval. Hourly
    # sources (interval 1) are governed by the per-cycle dedupe alone, which
    # is exact where an age check drifts.
    #
    # A failed pull counts toward the interval too, and a run of them backs
    # off (queue.backing_off). It used to retry next cycle: twelve boards,
    # most answering 404, were pulled every hour for up to eleven days, and
    # every board together failed 1,509 pulls in the week to 2026-10-04. Asked
    # in a second query of only the sources this one selects, because over
    # every source it is 0.68 s, every minute, on every worker.
    due = db.query(
        """
        SELECT name, listings_url FROM sources s WHERE active
          AND NOT EXISTS (
            SELECT 1 FROM tasks t WHERE t.kind = 'ingest_source' AND t.status = 'pending'
              AND t.payload->>'source' = s.name)
          AND (s.ingest_interval_hours <= 1 OR NOT EXISTS (
            SELECT 1 FROM tasks t WHERE t.kind = 'ingest_source'
              AND t.status IN ('running', 'done')
              AND t.payload->>'source' = s.name
              AND t.created_at > now() - make_interval(hours => s.ingest_interval_hours)))
        """
    )
    waiting = queue.backing_off([s["name"] for s in due])
    for s in due:
        if s["name"] in waiting:
            continue
        enqueue(
            "ingest_source",
            {"source": s["name"], "cycle": cycle, "host": pace_key(s["listings_url"])},
            dedupe_key=f"ingest:{s['name']}:{cycle}",
        )
    # The other half of the query above: a source it no longer selects is
    # never pulled, so no observation says its postings changed. The hourly
    # reconcile stores what catalog.AVAILABLE says of them, and writes
    # nothing once the catalog agrees (catalog.reconcile_available).
    enqueue("retire_switched_off", {"cycle": cycle}, dedupe_key=f"retire-off:{cycle}")
    # Messages whose current event or match pointer lags its log
    # (tasks.mail.pointers). Counting them is a pass over every message,
    # 0.9 s on production on 2026-10-10, so the task counts once a cycle
    # rather than every worker on every poll; a run with none to fill only
    # reads. One at a time.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'backfill_mail_pointers' "
        "AND status IN ('pending', 'running') LIMIT 1"
    ):
        enqueue("backfill_mail_pointers", {"cycle": cycle}, dedupe_key=f"mail-pointers:{cycle}")
    if db.get_config("resume_backfill_enabled") and not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'backfill_resumes' "
        "AND status IN ('pending', 'running') LIMIT 1"
    ):
        schedule_resume_backfill(cycle)
    # Each batch bundle's fields moved off its member rows onto batch_objects
    # (tasks.batch_objects). Offered until a run reports done; a run waiting
    # on an older image ends and the next cycle's run checks again.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'consolidate_batch_objects' "
        "AND (status IN ('pending', 'running') "
        "     OR (status = 'done' AND payload->'state'->>'phase' = 'done')) LIMIT 1"
    ):
        enqueue("consolidate_batch_objects", {"cycle": cycle}, dedupe_key=f"batch-objects:{cycle}")
    # Board membership for every person who can have one, every
    # board_refresh_minutes, so new verdicts reach a board without anyone
    # touching a preference. Bucketed like the ingest cycle; a person's own
    # preference write asks sooner. A users row with no subscription, no
    # board row and no upload admits nothing under the predicate, and one
    # such row (a probe that signed in once) drew 825 full recomputes in a
    # day, 60 percent of the real person's, each producing zero rows.
    #
    # A person with a recompute already pending gets no second one: the
    # pending one reads everything as of when it runs, so a second has nothing
    # of its own to do. Without this a queue that falls behind grows by one
    # per person per bucket; on 2026-10-10 66 were stacked for three people.
    # A running one does not block, because it may have read what changed.
    refresh = max(1, int(db.get_config("board_refresh_minutes")))
    rbucket = now.replace(minute=(now.minute // refresh) * refresh, second=0, microsecond=0)
    rcycle = rbucket.strftime("%Y-%m-%dT%H:%M")
    for u in db.query(
        f"""
        SELECT id FROM users u
        WHERE (EXISTS (SELECT 1 FROM user_source_set s WHERE s.user_id = u.id)
               OR EXISTS (SELECT 1 FROM user_jobs j WHERE j.user_id = u.id)
               OR EXISTS (SELECT 1 FROM posting_uploads p WHERE p.uploaded_by = u.id))
          AND NOT {visibility.RECOMPUTE_PENDING}
        ORDER BY id
        """
    ):
        enqueue(
            "recompute_board",
            {"user_id": u["id"], "cycle": rcycle},
            dedupe_key=f"board:{u['id']}:{rcycle}",
        )
    _managed_board_schedule_attempts.intersection_update(
        {key for key in _managed_board_schedule_attempts if key[0] == cycle}
    )
    for board in db.query("SELECT id FROM managed_boards WHERE published ORDER BY id"):
        attempt = (cycle, board["id"])
        if attempt in _managed_board_schedule_attempts:
            continue
        try:
            managed_boards.admit(board["id"], dedupe_key=f"managed-board:{board['id']}:{cycle}")
        except managed_boards.RunRefusal as exc:
            logger.info(
                "Managed board %s not scheduled: %s (%s)",
                board["id"],
                exc.message,
                exc.code,
            )
            telemetry.capture(
                "managed_board_schedule_refused",
                properties={"managed_board_id": board["id"], "code": exc.code},
            )
        _managed_board_schedule_attempts.add(attempt)
    # Application answers ahead of need, hourly, for each person who has put
    # a resume in: the forms of the postings on their board are read and
    # every question without a draft rides one half-price batch, so the
    # answer is there when the posting is opened. See tasks.application.
    for u in db.query("SELECT DISTINCT user_id AS id FROM user_resumes ORDER BY 1"):
        enqueue(
            "application_sweep",
            {"user_id": u["id"], "cycle": cycle},
            dedupe_key=f"appsweep:{u['id']}:{cycle}",
        )
    day = now.strftime("%Y-%m-%d")
    enqueue("reverify_open", {"cycle": day}, dedupe_key=f"reverify:{day}")
    enqueue("sync_gmail", {"cycle": cycle}, dedupe_key=f"gmail:{cycle}")
    # Importing mail nobody classifies is an inbox, not an ingest. The sync
    # above runs hourly, so without these two the pipeline would fill
    # email_messages forever and derive nothing from it.
    #
    # Both carry the same cross-cycle guard as the batched extractions: each
    # parks on the Batch API and can outlive its own cycle, so the dedupe key
    # alone would stack a pass an hour on top of one still waiting. Separate
    # checks so a slow classification does not stop matching from running -
    # matching is cheap and re-runs improve on themselves as new board rows
    # appear, which is exactly when it should not be blocked.
    if db.get_config("mail_classification_enabled") and not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'classify_mail' "
        "AND status IN ('pending', 'running', 'waiting', 'awaiting_batch') LIMIT 1"
    ):
        enqueue("classify_mail", {"cycle": cycle}, dedupe_key=f"mailclassify:{cycle}")
    # And only when something a sweep reads has changed since the last one
    # started: otherwise it would decide everything exactly as before.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'match_mail' "
        "AND status IN ('pending', 'running', 'waiting') LIMIT 1"
    ) and mail_match.changed_since(mail_match.last_sweep_start()):
        enqueue("match_mail", {"cycle": cycle}, dedupe_key=f"mailmatch:{cycle}")
    # Its own kind and its own key, NOT folded into the sync. Dead-credential
    # detection is discovery-on-use, so if it only happened inside the sync
    # then a sync that stops running also stops noticing it cannot run - the
    # alarm wired to the thing it is alarming about.
    enqueue("probe_credentials", {"cycle": cycle}, dedupe_key=f"credprobe:{cycle}")
    # Every derived fact (pay, requirements, job profiles, embeddings,
    # locations): one pass each, only when switched on and none is in flight.
    for derivation in DERIVATIONS:
        derivation.schedule(cycle)
    enqueue("send_digests", {"cycle": day}, dedupe_key=f"digest:{day}")
    enqueue("data_health", {"cycle": cycle}, dedupe_key=f"health:{cycle}")
    # Polling gets its OWN bucket, far finer than the ingest cycle. Sharing the
    # hourly one meant a parked task could sit up to an hour after its batch
    # had actually finished, and in a multi-round backfill that latency is the
    # dominant term - larger than the provider's own turnaround.
    #
    # The poll costs one provider call per OPEN batch and nothing when there
    # are none, so the frequency is bounded by politeness rather than expense.
    poll_bucket = now.replace(
        minute=(now.minute // BATCH_POLL_MINUTES) * BATCH_POLL_MINUTES,
        second=0,
        microsecond=0,
    ).strftime("%Y-%m-%dT%H:%M")
    # The dedupe key stops two polls per BUCKET; this stops a queue of them
    # across buckets. A poll is idempotent and stateless - it reports on
    # whatever is open right now - so a second one waiting behind the first has
    # nothing of its own to do, and eleven had piled up behind an hour of
    # scraping. That is not merely wasteful: each holds a worker slot when it
    # finally runs, and the thing it is competing with is the collection of
    # batches that have already been paid for.
    #
    # Same distinction as the classify bug: "has it run" and "has it been
    # enqueued" are different questions, and scheduling on the second one
    # re-queues work that is already in flight.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'poll_batches' "
        "AND status IN ('pending', 'running') LIMIT 1"
    ):
        enqueue("poll_batches", {"cycle": poll_bucket}, dedupe_key=f"pollbatch:{poll_bucket}")
    # Hourly sweep for jobs the ingest pipeline left unverified (inline AI
    # checks disabled fleet-side): closed+clearance in one batched call each.
    # Its predicate (no closed/clearance verdict) stays true while a batch is in
    # flight, so a second sweep would re-select the same jobs and pay again;
    # tasks.verify._in_flight excludes what a parked sweep already submitted.
    # A parked sweep therefore does not block the next one: a straggling batch
    # held the only sweep for 15 hours on 2026-10-08, and no new posting
    # reached a board in that time. Only an unparked sweep (pending, running,
    # waiting) does, so two never select at once.
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'verify_new' "
        "AND status IN ('pending', 'running', 'waiting') LIMIT 1"
    ):
        enqueue("verify_new", {"cycle": cycle}, dedupe_key=f"verify:{cycle}")
    # Backlog walker: jobs ingested before content-caching existed (or whose
    # scrape failed) can never be checked until their page is cached.
    enqueue("fetch_missing_content", {"cycle": cycle}, dedupe_key=f"content:{cycle}")
