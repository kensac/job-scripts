"""Managed board, filter and personal board paths for a posting."""

from __future__ import annotations

from typing import Any

from api import db, user_settings
from api.board import criteria as board_criteria
from api.mail import applications
from api.posting_path.gates import (
    _criteria_step,
    _screen_step,
    _structural_step,
    _verdict_step,
    _volume_step,
)
from api.posting_path.models import ConsumerPath, PathStep
from core.screening import TitleGateConfig, screen
from core.volume_gate import VolumeGate


def _consumer_summary(steps: list[PathStep], included: bool | None) -> str:
    if included:
        return "On it"
    stop = next((s for s in steps if s.outcome in ("failed", "skipped")), None)
    if stop:
        return stop.label
    if any(s.outcome == "pending" for s in steps):
        return next(s.label for s in steps if s.outcome == "pending")
    return "Not on it"


def _managed_board_path(
    job: dict[str, Any], board: dict[str, Any], gate: VolumeGate
) -> ConsumerPath:
    steps: list[PathStep] = []
    if job["source"] not in (board["sources"] or []):
        steps.append(
            PathStep(
                stage="source",
                outcome="failed",
                label="Its source is not one this board reads",
                basis="evaluated_now",
            )
        )
    else:
        steps.append(_criteria_step(job, board_criteria.params({"criteria": board["criteria"]})))
        if volume := _volume_step(job, gate, board["prompt_hash"]):
            steps.append(volume)
        if board["title_gate"]:
            config = TitleGateConfig.model_validate(board["title_gate"])
            decision = screen(config.recipe, title=job["title"], source=job["source"])
            steps.append(
                PathStep(
                    stage="title_gate",
                    outcome="skipped" if decision.skip else "passed",
                    label=f"Title gate {config.recipe}: "
                    + ("dropped" if decision.skip else "kept"),
                    detail=decision.reason,
                    basis="evaluated_now",
                )
            )
        if review := _screen_step(job, board["prompt_hash"]):
            steps.append(review)
        steps.append(_structural_step(job, board["bypass_sponsorship_filter"]))
        steps.append(_verdict_step(job, board["prompt_hash"], board["requested_model"], None))
    member = db.query_one(
        "SELECT projected_at FROM managed_board_jobs WHERE managed_board_id = %s AND job_id = %s",
        (board["id"], job["id"]),
    )
    steps.append(
        PathStep(
            stage="membership",
            outcome="passed" if member else "info",
            label="On the board" if member else "Not on the board",
            basis="recorded",
            at=member["projected_at"] if member else None,
        )
    )
    return ConsumerPath(
        kind="managed_board",
        id=board["id"],
        name=board["name"],
        included=bool(member),
        summary=_consumer_summary(steps, bool(member)),
        steps=steps,
    )


def _filter_paths(
    job: dict[str, Any], user_id: int, gate: VolumeGate, owner: str | None
) -> list[ConsumerPath]:
    settings = user_settings.criteria(user_id)
    subscribed = job["uploaded_by"] == user_id or bool(
        db.query_one(
            "SELECT 1 FROM user_source_set WHERE user_id = %s AND source = %s",
            (user_id, job["source"]),
        )
    )
    bypass = settings.bypass_sponsorship_filter
    paths = []
    for f in db.query(
        "SELECT id, name, prompt_hash FROM user_filters WHERE user_id = %s AND enabled ORDER BY id",
        (user_id,),
    ):
        steps: list[PathStep] = []
        if not subscribed:
            steps.append(
                PathStep(
                    stage="source",
                    outcome="failed",
                    label="Not from a source you follow",
                    basis="evaluated_now",
                )
            )
        else:
            steps.append(
                _criteria_step(job, board_criteria.params({"criteria": settings.criteria}))
            )
            if volume := _volume_step(job, gate, f["prompt_hash"]):
                steps.append(volume)
            if review := _screen_step(job, f["prompt_hash"]):
                steps.append(review)
            steps.append(_structural_step(job, bypass))
            # A person's board counts the latest verdict under any model.
            steps.append(_verdict_step(job, f["prompt_hash"], None, f["id"]))
        passed = any(s.stage == "verdict" and s.outcome == "passed" for s in steps)
        paths.append(
            ConsumerPath(
                kind="filter",
                id=f["id"],
                name=f["name"],
                owner=owner,
                included=passed,
                summary=_consumer_summary(steps, passed),
                steps=steps,
            )
        )
    return paths


def _personal_board_path(job: dict[str, Any], user_id: int) -> ConsumerPath:
    """The person's own board: which branch of the visibility predicate admits it."""
    row = db.query_one(
        "SELECT (SELECT computed_at FROM board_visible WHERE user_id = %(uid)s AND job_id = %(jid)s) "
        "AS member, (SELECT COALESCE(uj.status, '') <> '' OR COALESCE(uj.notes, '') <> '' "
        f"OR {applications.applied_on('uj')} IS NOT NULL FROM user_jobs uj "
        "WHERE uj.user_id = %(uid)s AND uj.job_id = %(jid)s) AS acted_on",
        {"uid": user_id, "jid": job["id"]},
    )
    assert row is not None
    member = row["member"] is not None
    if job["uploaded_by"] == user_id:
        why = "You added it"
    elif row["acted_on"]:
        why = (
            "You acted on it (a status, note or date applied), so it stays whatever the filters say"
        )
    elif member:
        why = "Every enabled filter kept it and it passes your criteria"
    else:
        why = "Not every enabled filter kept it, or it is outside your criteria"
    return ConsumerPath(
        kind="board",
        id=None,
        name="Your board",
        included=member or job["uploaded_by"] == user_id or bool(row["acted_on"]),
        summary=why,
        steps=[
            PathStep(
                stage="membership",
                outcome="passed" if member else "info",
                label="On your board" if member else "Not on your board",
                detail=why,
                basis="recorded",
                at=row["member"],
            )
        ],
    )
