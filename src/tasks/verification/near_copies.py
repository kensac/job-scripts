"""Give a new posting the verdicts of a verified near copy instead of asking again."""

from __future__ import annotations

from typing import Any

from api import db, metrics
from core import catalog, near_copy, verdict_reads
from core.store import add_ai_results, ai_result_row
from tasks.verification.selection import _in_flight


def _reuse_near_copies(
    task_id: int, candidate_postings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Give a posting its verified twin's verdicts; return the ones still to ask.

    A twin is the same source, title and text once places and numbers are
    removed (core.near_copy). A candidate whose twin already holds closed and
    clearance verdicts takes those and the twin's verdicts under every live
    board and filter prompt, as rows marked verify-near-copy that cost nothing.
    One whose twin is in this sweep or in a parked one waits for it, so the
    text is read once. The posting's own location, date and gates still apply
    where each board and filter reads the verdict.
    """
    near_copy_keys = {
        posting["url"]: near_copy.key(posting["title"] or "", posting["input_content"] or "")
        for posting in candidate_postings
    }
    catalog.set_near_copy_keys(near_copy_keys)
    source_keys = {
        (posting["source"], near_copy_keys[posting["url"]]) for posting in candidate_postings
    }
    sources, digests = (
        [source_key[0] for source_key in source_keys],
        [source_key[1] for source_key in source_keys],
    )
    verified_twins = {
        (row["source"], row["near_copy_key"]): row["url"]
        for row in db.query(
            "SELECT DISTINCT ON (j.source, j.near_copy_key) j.source, j.near_copy_key, j.url "
            "FROM jobs j WHERE (j.source, j.near_copy_key) IN "
            "(SELECT * FROM unnest(%s::text[], %s::text[])) AND NOT (j.url = ANY(%s::text[])) "
            f"AND {verdict_reads.has_verdict('j.url', 'closed')} "
            f"AND {verdict_reads.has_verdict('j.url', 'clearance')} "
            "ORDER BY j.source, j.near_copy_key, j.id DESC",
            (sources, digests, list(near_copy_keys)),
        )
    }
    in_flight_source_keys = {
        (row["source"], row["near_copy_key"])
        for row in db.query(
            "SELECT source, near_copy_key FROM jobs WHERE url = ANY(%s) "
            "AND near_copy_key IS NOT NULL",
            (_in_flight(task_id),),
        )
    }
    postings_to_verify, twin_reuse, selected_source_keys = [], [], set()
    for posting in candidate_postings:
        source_key = (posting["source"], near_copy_keys[posting["url"]])
        if source_key in verified_twins:
            twin_reuse.append((posting, verified_twins[source_key]))
        elif source_key in in_flight_source_keys or source_key in selected_source_keys:
            continue
        else:
            selected_source_keys.add(source_key)
            postings_to_verify.append(posting)
    if twin_reuse:
        _copy_twin_verdicts(twin_reuse)
    return postings_to_verify


def _copy_twin_verdicts(reuse: list[tuple[dict[str, Any], str]]) -> None:
    twin_urls = sorted({twin for _, twin in reuse})
    checks = verdict_reads.latest_checks(twin_urls)
    customs: dict[str, list[dict[str, Any]]] = {}
    for row in db.query(
        verdict_reads.latest_per(
            "url, prompt_hash, model",
            "url, prompt_hash, model, filter_name, status, reason, parsed_json",
            "url = ANY(%s) AND check_type = 'custom' AND prompt_hash IN ("
            "SELECT prompt_hash FROM managed_boards WHERE published "
            "UNION SELECT prompt_hash FROM user_filters WHERE enabled)",
        ),
        (twin_urls,),
    ):
        customs.setdefault(row["url"], []).append(row)
    decided = {
        (row["url"], row["prompt_hash"], row["model"])
        for row in db.query(
            "SELECT DISTINCT url, prompt_hash, model FROM verdicts WHERE url = ANY(%s) "
            "AND check_type = 'custom'",
            ([job["url"] for job, _ in reuse],),
        )
    }
    rows = []
    for job, twin in reuse:
        for check in ("closed", "clearance"):
            verdict = checks.get(twin, {}).get(check)
            if job[f"needs_{check}"] and verdict:
                rows.append(
                    ai_result_row(
                        job["url"],
                        verdict.status,
                        verdict.reason,
                        check,
                        company=job["company"],
                        job_title=job["title"],
                        config_name="verify-near-copy",
                    )
                )
        for verdict in customs.get(twin, []):
            if (job["url"], verdict["prompt_hash"], verdict["model"]) in decided:
                continue
            rows.append(
                ai_result_row(
                    job["url"],
                    verdict["status"],
                    verdict["reason"],
                    "custom",
                    model=verdict["model"],
                    filter_name=verdict["filter_name"],
                    prompt_hash=verdict["prompt_hash"],
                    parsed_json=verdict["parsed_json"],
                    company=job["company"],
                    job_title=job["title"],
                    config_name="verify-near-copy",
                )
            )
    add_ai_results(rows)
    for row in rows:
        metrics.CHECKS.labels(row["check_type"], row["status"]).inc()
