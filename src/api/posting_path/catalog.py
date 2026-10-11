"""Catalog, stored content and verification steps for one posting."""

from __future__ import annotations

from typing import Any

from api import db
from api.ai import verdicts
from api.posting_path.models import PathStep
from api.posting_path.verdict_provenance import (
    _latest_verdict,
    _near_copy_source_url,
    _verdict_origin,
)


def _catalog_steps(job: dict[str, Any]) -> list[PathStep]:
    if job["uploaded_by"] is not None:
        return [
            PathStep(stage="catalog", outcome="info", label="Added by a person", basis="recorded")
        ]
    steps = []
    listing = db.query_one(
        "SELECT t.pattern, l.kept, l.last_seen_at FROM listings l "
        "JOIN title_patterns t ON t.id = l.pattern_id WHERE l.url = %s",
        (job["url"],),
    )
    if listing:
        steps.append(
            PathStep(
                stage="catalog",
                outcome="passed" if listing["kept"] else "info",
                label=f"Listed by {job['source']}",
                detail=None
                if listing["kept"]
                else f"Title outside the source's title pattern ({listing['pattern']}); "
                "admitted because pattern enforcement was off",
                basis="recorded",
                at=listing["last_seen_at"],
            )
        )
    if not job["active"]:
        # When the latest source said so; job_listing_events holds what came
        # before source_observations began.
        event = db.query_one(
            "SELECT max(at) AS at FROM ("
            " (SELECT at FROM source_observations WHERE job_id = %(id)s ORDER BY id DESC LIMIT 1)"
            " UNION ALL"
            " (SELECT at FROM job_listing_events WHERE job_id = %(id)s AND NOT listed"
            "  ORDER BY at DESC LIMIT 1)) e",
            {"id": job["id"]},
        )
        steps.append(
            PathStep(
                stage="catalog",
                outcome="failed",
                label="No switched-on source lists it",
                detail="Postings no longer available are not read, judged or shown",
                basis="recorded",
                at=event["at"] if event else None,
            )
        )
    return steps


def _content_step(job: dict[str, Any]) -> PathStep:
    row = db.query_one(
        "SELECT method AS reason, created_at FROM page_fetches WHERE url = %s "
        "AND status = 'passed' ORDER BY id DESC LIMIT 1",
        (job["url"],),
    )
    failures = verdicts.fetch_failure_streaks([job["url"]]).get(job["url"], 0)
    give_up = int(db.get_config("fetch_give_up_after_failures"))
    if row and not failures:
        return PathStep(
            stage="content",
            outcome="passed",
            label="Posting text stored",
            detail=f"From {row['reason']}" if row["reason"] else None,
            basis="recorded",
            at=row["created_at"],
        )
    if failures >= give_up:
        return PathStep(
            stage="content",
            outcome="failed",
            label="Posting page could not be fetched",
            detail=f"Gave up after {failures} failed fetches in a row",
            basis="recorded",
        )
    if failures:
        return PathStep(
            stage="content",
            outcome="pending",
            label="Posting page fetch is retrying",
            detail=f"{failures} failed fetches in a row",
            basis="recorded",
        )
    return PathStep(
        stage="content",
        outcome="pending",
        label="Posting text not fetched yet",
        basis="recorded",
    )


def _verification_steps(job: dict[str, Any]) -> list[PathStep]:
    steps = []
    twin = None
    for check, flagged, clear in (
        ("closed", "Closed", "Open"),
        (
            "clearance",
            "Needs a clearance, citizenship or has no sponsorship",
            "No clearance or citizenship requirement",
        ),
    ):
        verdict = _latest_verdict(job["url"], check)
        if verdict is None:
            continue
        origin = _verdict_origin(verdict.config_name)
        if verdict.config_name == "verify-near-copy":
            twin = twin or _near_copy_source_url(job)
            if twin:
                origin = f"{origin} ({twin})"
        steps.append(
            PathStep(
                stage="verification",
                outcome="failed" if verdict.status == "rejected" else "passed",
                label=flagged if verdict.status == "rejected" else clear,
                detail="; ".join(part for part in (verdict.reason, origin) if part),
                basis="recorded",
                at=verdict.created_at,
                check=check,
            )
        )
    if steps:
        return steps
    in_flight = db.query_one(
        "SELECT 1 FROM batch_requests r JOIN tasks t ON t.id = r.task_id "
        "WHERE t.kind = 'verify_new' AND t.status IN ('pending', 'running', 'waiting', "
        "'awaiting_batch') AND r.custom_id = %s LIMIT 1",
        (job["url"],),
    )
    if in_flight:
        label = "In a verification batch that has not come back yet"
    else:
        label = "Not verified yet"
    return [
        PathStep(
            stage="verification",
            outcome="pending",
            label=label,
            detail=None
            if in_flight
            else "Verification reads a posting only when a board or filter below would; "
            "each one says whether it would",
            basis="recorded" if in_flight else "evaluated_now",
        )
    ]
