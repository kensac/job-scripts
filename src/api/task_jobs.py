"""A task's frozen job list: inline `jobs`, or a verified object behind `jobs_ref`."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Literal

from api import db, queue
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable, encode_payload
from core.pool import in_transaction


@dataclass(frozen=True)
class Population:
    """The task kinds whose payload carries a job list, and whether the list's
    URLs stay inline beside the reference.

    `urls` is for lists read inside the database: a SQL reader cannot follow a
    reference, so the URLs it needs stay in the payload and only the rest of
    each job moves to the object.
    """

    name: str
    kinds: tuple[str, ...]
    urls: bool

    @property
    def kinds_sql(self) -> str:
        # Spelled as literals, not a bound array, so a prepared statement's
        # generic plan can still prove a partial index's predicate.
        return "kind IN (" + ", ".join(f"'{kind}'" for kind in self.kinds) + ")"


MANAGED_BOARD_RUNS = Population(
    "managed_board", ("run_managed_board", "run_managed_board_batch"), urls=False
)
# tasks.board.in_flight_urls and submission_exclusions read these chunks' URLs
# in SQL, for every active chunk of a user.
FILTER_CHUNKS = Population(
    "filter_chunk", ("run_filter_chunk", "run_filter_batch_chunk"), urls=True
)
POPULATIONS = {population.name: population for population in (MANAGED_BOARD_RUNS, FILTER_CHUNKS)}


def reference(
    population: Population, jobs: list[dict[str, Any]], ref: PayloadRef
) -> dict[str, Any]:
    """The payload keys that stand in for an inline `jobs` list."""
    keys: dict[str, Any] = {"jobs_ref": asdict(ref), "candidate_count": len(jobs)}
    if population.urls:
        keys["urls"] = [job["url"] for job in jobs]
    return keys


def run_jobs(payload: Mapping[str, Any], store: PayloadStore | None = None) -> list[dict[str, Any]]:
    """The task's frozen job list, the one reader of both payload shapes.

    Tasks written before the reference existed carry the list inline until
    `migrate` externalizes it. No writer produces a payload with neither, so
    reaching one is a required-input failure, never an empty list that would
    project an empty board or decide nothing.
    """
    if "jobs" in payload:
        return payload["jobs"]
    reference = payload.get("jobs_ref")
    if reference is None:
        raise PayloadUnavailable("Task job list is no longer retained")
    if in_transaction():
        raise RuntimeError("A task job list cannot be read inside a database transaction")
    jobs = (store or PayloadStore.from_env()).get(PayloadRef.parse(reference))
    if (
        not isinstance(jobs, list)
        or not all(isinstance(job, dict) for job in jobs)
        or len(jobs) != payload.get("candidate_count")
        or ("urls" in payload and [job.get("url") for job in jobs] != payload["urls"])
    ):
        raise PayloadUnavailable("Task job list is invalid")
    return jobs


type Mode = Literal["count", "externalize", "verify", "restore"]
# A task left unconverted for a reason a retry can clear. A writing invocation
# stops its cursor before the first one, so resuming from `after` retries it.
_RETRY = frozenset({"changed", "unavailable"})
# Reported by id. `conflict` (inline with reference keys beside it, or a list
# run_jobs would refuse) and `missing` (neither shape) are not what any writer
# produced and need a person, so they do not hold the cursor.
_FAILED = _RETRY | {"conflict", "missing"}
_REFERENCE_KEYS = ("jobs_ref", "candidate_count", "urls")


def _shape(task_id: int, *, jobs: bool, lock: bool = False) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT payload ? 'jobs' AS inline, payload->'jobs_ref' AS ref, "
        "payload->'candidate_count' AS count, payload->'urls' AS urls"
        + (", payload->'jobs' AS jobs" if jobs else "")
        + " FROM tasks WHERE id = %s"
        + (" FOR UPDATE" if lock else ""),
        (task_id,),
    )


def _lock(task_id: int, *, jobs: bool) -> dict[str, Any] | None:
    db.execute("SET LOCAL lock_timeout = '2s'")
    db.execute("SET LOCAL statement_timeout = '5s'")
    return _shape(task_id, jobs=jobs, lock=True)


def _no_reference(row: Mapping[str, Any]) -> bool:
    return row["ref"] is None and row["count"] is None and row["urls"] is None


def _migrate_one(
    population: Population, task_id: int, mode: Mode, store: PayloadStore | None
) -> str:
    row = _shape(task_id, jobs=mode == "externalize")
    if row is None:
        return "changed"
    if row["inline"]:
        if not _no_reference(row):
            return "conflict"
        if mode != "externalize":
            return "inline"
        jobs = row["jobs"]
        if not isinstance(jobs, list) or not all(
            isinstance(job, dict) and (not population.urls or isinstance(job.get("url"), str))
            for job in jobs
        ):
            return "conflict"
        assert store is not None
        try:
            ref = store.put_verified(jobs)
        except PayloadUnavailable:
            return "unavailable"
        with db.transaction():
            current = _lock(task_id, jobs=True)
            # put_verified read the object back equal to `jobs`, and its bytes
            # are encode_payload(jobs); equal canonical bytes here prove the
            # object holds exactly what the locked row holds.
            if (
                current is None
                or not current["inline"]
                or not _no_reference(current)
                or encode_payload(current["jobs"]) != encode_payload(jobs)
            ):
                return "changed"
            queue.merge_payload(task_id, reference(population, jobs, ref), drop=["jobs"])
        return "externalized"
    if row["ref"] is None:
        return "missing"
    if population.urls != (row["urls"] is not None):
        return "conflict"
    if mode in ("count", "externalize"):
        return "referenced"
    try:
        jobs = run_jobs(
            {
                "jobs_ref": row["ref"],
                "candidate_count": row["count"],
                **({"urls": row["urls"]} if row["urls"] is not None else {}),
            },
            store,
        )
    except PayloadUnavailable:
        return "unavailable"
    if mode == "verify":
        return "verified"
    with db.transaction():
        current = _lock(task_id, jobs=False)
        if current != row:
            return "changed"
        queue.merge_payload(task_id, {"jobs": jobs}, drop=_REFERENCE_KEYS)
    return "restored"


def migrate(
    population: Population,
    mode: Mode,
    *,
    after: int,
    through: int,
    limit: int,
    store: PayloadStore | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    """Move the next `limit` tasks of a population after `after` between the
    inline job list and the reference its writer produces, one task per short
    transaction.

    `externalize` uploads a task's exact inline list with put_verified outside
    any transaction, then locks the row, requires the identical list still
    there, and swaps it for the reference keys in one UPDATE. `restore` is its
    inverse, `verify` reads every reference through run_jobs, and `count`
    classifies without reading objects. Nothing else in a payload is touched,
    and a converted task is skipped when selected again.
    """
    if in_transaction():
        raise RuntimeError("Task job list migration cannot run inside a database transaction")
    if mode != "count" and store is None:
        store = PayloadStore.from_env()
    ids = [
        row["id"]
        for row in db.query(
            f"SELECT id FROM tasks WHERE {population.kinds_sql} AND id > %s AND id <= %s "
            "ORDER BY id LIMIT %s",
            (after, through, limit),
        )
    ]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        outcomes = list(
            executor.map(lambda task_id: _migrate_one(population, task_id, mode, store), ids)
        )
    stop = len(ids)
    if mode in ("externalize", "restore"):
        stop = next((i for i, outcome in enumerate(outcomes) if outcome in _RETRY), stop)
    exhausted = stop == len(ids) < limit
    return {
        "counts": dict(Counter(outcomes)),
        "failed": [i for i, outcome in zip(ids, outcomes, strict=True) if outcome in _FAILED],
        "after": through if exhausted else ids[stop - 1] if stop else after,
        "exhausted": exhausted,
    }
