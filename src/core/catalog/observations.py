"""Source observations and the stored availability projection."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, LiteralString

from core.pool import connection, pool, transaction

if TYPE_CHECKING:
    from core.fetching.posting import JobPosting

# Whether listings row `{listing}` appeared on its source's latest pull. A pull
# refreshes every row it lists older than refresh_hours, so a listed row is never
# older than the source's newest row minus refresh_hours; a source whose pulls
# fail keeps its rows current, since nothing newer moves its newest row.
# Pass a bound parameter for `{source}` when scanning one source so Postgres
# evaluates the max() once as an InitPlan (correlated over one source's 25k rows
# it did not finish in 120 s). The predicate itself costs retire_switched_off's
# select 1.6 s against 0.6 s without it (production 2026-10-10).
# ponytail: max() per source scans that source's rows; a per-source latest-pull
# column is the upgrade if a source's listings outgrow it.
LISTED_NOW: LiteralString = (
    "{listing}.last_seen_at >= (SELECT max(newest.last_seen_at) FROM listings newest "
    "WHERE newest.source = {source}) - make_interval(hours => %(refresh_hours)s)"
)


def set_active(job_id: int, active: bool) -> bool:
    """Record an admin availability correction under CORRECTION_SOURCE and update
    jobs.available in the same transaction (jobs.active is frozen). Returns False
    if the job does not exist."""
    with transaction(), connection() as conn:
        if conn.execute("SELECT 1 FROM jobs WHERE id = %s", (job_id,)).fetchone() is None:
            return False
        conn.execute(
            "INSERT INTO source_observations (job_id, source, kind) VALUES (%s, %s, %s)",
            (job_id, CORRECTION_SOURCE, "reappeared" if active else "unlisted"),
        )
        _refresh_available(conn, [job_id], skip_locked=False)
        return True


_ADMITTED = frozenset({"appeared", "reappeared"})

# Synthetic source name for admin corrections; no row in `sources` uses it.
CORRECTION_SOURCE = "admin"

# Read at query time so toggling source_title_patterns_enabled never rewrites history.
# Defaults to 'true' when unseeded, matching api.config (pinned by test_source_observations.py).
PATTERNS_ENFORCED: LiteralString = (
    "(COALESCE((SELECT c.value FROM app_config c"
    " WHERE c.key = 'source_title_patterns_enabled'), 'true') = 'true')"
)

# Availability derived from source_observations (parameter-free SQL fragment):
# 1. Latest admin correction wins until a newer source observation arrives.
# 2. True if any active source's latest observation admits the job (appeared,
#    reappeared, not_listed, or filtered while patterns are disabled).
# 3. False if observations exist with none admitting.
# 4. False if no source has observed it and its owning source is switched off,
#    or it is a 'sheet_import' row: imported once 2026-08-24 and nothing pulls
#    it, so it counts as switched off (decided 2026-10-10).
# 5. Otherwise NULL (cannot tell).
AVAILABLE: LiteralString = (
    """(
    COALESCE(
        (SELECT CASE WHEN o.source = '"""
    + CORRECTION_SOURCE
    + """' THEN o.kind = 'reappeared' END
         FROM source_observations o WHERE o.job_id = {job}.id ORDER BY o.id DESC LIMIT 1),
        CASE WHEN EXISTS (
                SELECT 1 FROM (
                    SELECT DISTINCT ON (o.source) o.source, o.kind FROM source_observations o
                    WHERE o.job_id = {job}.id ORDER BY o.source, o.id DESC) latest
                JOIN sources s ON s.name = latest.source AND s.active
                WHERE latest.kind IN ('appeared', 'reappeared', 'not_listed')
                   OR (latest.kind = 'filtered' AND NOT """
    + PATTERNS_ENFORCED
    + """))
             THEN true
             WHEN EXISTS (SELECT 1 FROM source_observations o WHERE o.job_id = {job}.id)
             THEN false
             WHEN EXISTS (SELECT 1 FROM sources s WHERE s.name = {job}.source AND NOT s.active)
             THEN false
             WHEN {job}.source = 'sheet_import' THEN false
        END))"""
)

# Readers query stored `jobs.available` rather than recomputing AVAILABLE per row
# (AI-eligible count 2.0 s vs 6.5 s; board recompute 27 s vs 38 s, production 2026-10-10).
# Every row is stored on insert or by the reconcile, so NULL is a row nothing has
# stored yet and reads as not available.
IS_AVAILABLE: LiteralString = "COALESCE({job}.available, false)"

# Stored availability falls back to frozen `jobs.active` for rows never yet
# observed by a pull: a capped (partial) pull records no absence past its cap,
# and a board not yet pulled has observed nothing. On 2026-10-11 that was 102,662
# active postings, 81,644 on unpulled boards and 14,693 past a partial-pull cap.
_STORED: LiteralString = "COALESCE(" + AVAILABLE + ", {job}.active)"


def _refresh_available(conn, ids: list[int], skip_locked: bool) -> int:
    """Update jobs.available where it differs from _STORED.

    Locks in URL code-point order first, then evaluates in a second statement so
    READ COMMITTED sees observations committed before the lock was acquired.
    Evaluated in the locking statement, the value would come from a snapshot
    older than the lock. Returns the rows written.
    """
    if not ids:
        return 0
    locked = [
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM jobs WHERE id = ANY(%s) ORDER BY url {_LOCK_ORDER} "
            f"FOR NO KEY UPDATE{' SKIP LOCKED' if skip_locked else ''}",
            (ids,),
        ).fetchall()
    ]
    if not locked:
        return 0
    available = _STORED.format(job="jobs")
    return conn.execute(
        f"UPDATE jobs SET available = {available} "
        f"WHERE id = ANY(%s) AND available IS DISTINCT FROM {available}",
        (locked,),
    ).rowcount


def refresh_available(ids: list[int]) -> int:
    """Refresh jobs.available in per-batch transactions to bound row-lock duration."""
    written = 0
    for start in range(0, len(ids), _BATCH):
        with transaction(), connection() as conn:
            written += _refresh_available(conn, ids[start : start + _BATCH], skip_locked=False)
    return written


def reconcile_available() -> int:
    """Reconcile stale jobs.available rows across the catalog (run hourly).

    Catches what observations do not announce: a source switched on or off, or
    the title pattern setting flipped.

    Finds stale IDs in one unlocked scan (21 s with jit=off vs 34 s with jit=on
    on 2.15M jobs, production copy 2026-11-26), then updates in URL-ordered
    batches with SKIP LOCKED so it never deadlocks against concurrent ingests.
    Not id ranges: upserts spend ids, so 2.15M jobs spread over ids 1 to 41.8M
    and the range walk took 11 minutes a run (2026-11-26).
    """
    # Dedicated transaction so SET LOCAL jit = off does not leak to a caller.
    with pool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL jit = off")
        stale = [
            r["id"]
            for r in conn.execute(
                f"SELECT j.id FROM jobs j "
                f"WHERE j.available IS DISTINCT FROM {_STORED.format(job='j')} ORDER BY j.id"
            ).fetchall()
        ]
    written = 0
    for start in range(0, len(stale), _BATCH):
        with transaction(), connection() as conn:
            written += _refresh_available(conn, stale[start : start + _BATCH], skip_locked=True)
    return written


def observe(
    source: str,
    run_id: int | None,
    postings: list[JobPosting],
    kept: set[str],
    absence: str | None,
) -> dict[str, int]:
    """Append source_observations for catalog rows whose state changed on this pull.

    Call after upsert_postings so newly inserted jobs have IDs. `kept` records
    title-pattern matches regardless of the enforcement switch. `absence` is
    'unlisted' (authoritative complete pull), 'not_listed' (aggregator), or None
    (partial/empty pull, recording no absence). A listed posting carrying the
    feed's inactive flag is unlisted. A posting with no catalog row is skipped;
    listings keeps it. Returns inserted counts per kind.
    """
    posting_by_url = {posting.url: posting for posting in postings if posting.url}
    with pool.connection() as conn:
        last_observation_by_job = {
            observation["job_id"]: observation["kind"]
            for observation in conn.execute(
                "SELECT DISTINCT ON (job_id) job_id, kind FROM source_observations "
                "WHERE source = %s ORDER BY job_id, id DESC",
                (source,),
            ).fetchall()
        }
        listed_jobs = conn.execute(
            "SELECT id, url FROM jobs WHERE url = ANY(%s)", (list(posting_by_url),)
        ).fetchall()
        observation_rows: list[tuple[str, int, str]] = []
        for job in listed_jobs:
            posting = posting_by_url[job["url"]]
            previous_kind = last_observation_by_job.pop(job["id"], None)
            if not posting.active:
                observed_kind = "unlisted"
            elif job["url"] not in kept:
                observed_kind = "filtered"
            elif previous_kind is None:
                observed_kind = "appeared"
            elif previous_kind in _ADMITTED:
                continue
            else:
                observed_kind = "reappeared"
            if observed_kind != previous_kind:
                observation_rows.append((job["url"], job["id"], observed_kind))
        if absence == "unlisted":
            # Close currently available rows owned by this source that were never
            # observed before (carried forward from frozen jobs.active), or nothing
            # would close them. Only rows stored available, so closed history stays.
            observation_rows += [
                (job["url"], job["id"], absence)
                for job in conn.execute(
                    "SELECT j.id, j.url FROM jobs j WHERE j.source = %s AND j.available "
                    "AND j.url <> ALL(%s) AND NOT EXISTS (SELECT 1 FROM source_observations o "
                    "WHERE o.job_id = j.id AND o.source = %s)",
                    (source, list(posting_by_url), source),
                ).fetchall()
            ]
        if absence:
            missing_job_ids = [
                job_id
                for job_id, previous_kind in last_observation_by_job.items()
                if previous_kind in _ADMITTED or previous_kind == "filtered"
            ]
            if missing_job_ids:
                observation_rows += [
                    (job["url"], job["id"], absence)
                    for job in conn.execute(
                        "SELECT id, url FROM jobs WHERE id = ANY(%s)", (missing_job_ids,)
                    ).fetchall()
                ]
    # Foreign-key share locks on `jobs` follow URL code-point order (_LOCK_ORDER).
    observation_rows.sort(key=lambda row: row[0])
    if observation_rows:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO source_observations (job_id, source, kind, run_id) "
                "VALUES (%s, %s, %s, %s)",
                [(job_id, source, kind, run_id) for _, job_id, kind in observation_rows],
            )
        # Run after commit so refresh_available sees the new observations.
        refresh_available(sorted({job_id for _, job_id, _ in observation_rows}))
    return dict(Counter(kind for _, _, kind in observation_rows))


_BATCH = 500

# Canonical lock order for multi-row writes to jobs and listings: UTF-8 code-point
# order matches Python str.sort() and prevents deadlocks (36 ingest deadlocks in
# the 60 days to 2026-10-04 before unifying lock order).
_LOCK_ORDER: LiteralString = 'COLLATE "C"'
