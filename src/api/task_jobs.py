"""A task's frozen job list: inline `jobs`, or a verified object behind `jobs_ref`.

A live filter chunk keeps its short list inline; every other writer stores the
reference (observability.md). Both shapes are written today."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable
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

    A live filter chunk carries its list inline. No writer produces a payload
    with neither, so reaching one is a required-input failure, never an empty
    list that would project an empty board or decide nothing.
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
