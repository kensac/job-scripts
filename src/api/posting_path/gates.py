"""Current criteria, volume, title and verdict steps for a posting."""

from __future__ import annotations

from typing import Any

from api import db
from api.board import criteria as board_criteria
from api.posting_path.models import PathStep
from api.posting_path.verdict_provenance import (
    _latest_verdict,
    _near_copy_source_url,
    _verdict_origin,
)
from core import verdict_reads
from core.screening import Recipe, screen
from core.volume_gate import VolumeGate

_CRITERION_LABELS = {
    "posted_after": "posted after the date the criteria name",
    "max_age": "inside the age window",
    "excluded_locations": "not in an excluded location",
    "included_locations": "in an included location",
    "terms": "has an included work term",
}


def _criteria_failures(job_id: int, params: dict[str, Any]) -> list[str]:
    columns = ", ".join(
        f"{condition} AS {name}" for name, condition in board_criteria.CONDITIONS.items()
    )
    row = db.query_one(
        f"SELECT {columns} FROM jobs j WHERE j.id = %(jid)s", {**params, "jid": job_id}
    )
    return [name for name, ok in (row or {}).items() if not ok]


def _criteria_step(job: dict[str, Any], params: dict[str, Any]) -> PathStep:
    failed = _criteria_failures(job["id"], params)
    if failed:
        return PathStep(
            stage="criteria",
            outcome="failed",
            label="Outside the criteria",
            detail="Not " + ", ".join(_CRITERION_LABELS[name] for name in failed),
            basis="evaluated_now",
        )
    return PathStep(
        stage="criteria", outcome="passed", label="Inside the criteria", basis="evaluated_now"
    )


def _volume_step(job: dict[str, Any], gate: VolumeGate, prompt_hash: str) -> PathStep | None:
    """Whether verification skips this posting for a target in the volume gate.

    The answer depends on the posting, not the target, so it is read once per
    path however many boards and filters opt in."""
    if prompt_hash not in gate.scopes:
        return None
    if "_volume" not in job:
        job["_volume"] = _volume_decision(job, gate)
    return job["_volume"]


def _volume_decision(job: dict[str, Any], gate: VolumeGate) -> PathStep | None:
    row = db.query_one(
        "SELECT abs(hashtext(j.url)) %% 100 < %(audit)s AS audited FROM jobs j WHERE j.id = %(jid)s",
        {"audit": gate.audit_percent, "jid": job["id"]},
    )
    assert row is not None
    if (
        gate.occupation_titles
        and screen("occupation_words_v1", title=job["title"], source=job["source"]).skip
    ):
        return PathStep(
            stage="volume",
            outcome="skipped",
            label="Not read: the title names an unrelated occupation",
            detail="No board or filter has kept a title like this",
            basis="evaluated_now",
        )
    window = {"days": gate.window_days, "source": job["source"]}
    if gate.title_min_judged:
        title = db.query_one(
            "SELECT count(*) AS judged, count(*) FILTER (WHERE q.status = 'passed') AS kept "
            "FROM verdicts q JOIN jobs j ON j.url = q.url WHERE j.source = %(source)s "
            "AND lower(regexp_replace(j.title, '\\s+', ' ', 'g')) = "
            "lower(regexp_replace(%(title)s, '\\s+', ' ', 'g')) "
            "AND q.check_type = 'custom' "
            "AND q.created_at >= now() - make_interval(days => %(days)s)",
            {**window, "title": job["title"] or ""},
        )
        if title and title["judged"] >= gate.title_min_judged and not title["kept"]:
            return _volume_sample_step(
                row["audited"],
                f"This title at {job['source']} was judged {title['judged']} times "
                f"in {gate.window_days} days and never kept",
            )
    if gate.source_min_judged:
        source = db.query_one(
            "SELECT count(DISTINCT q.url) AS judged, "
            "count(DISTINCT q.url) FILTER (WHERE q.status = 'passed') AS kept "
            "FROM verdicts q JOIN jobs j ON j.url = q.url WHERE j.source = %(source)s "
            "AND q.check_type = 'custom' "
            "AND q.created_at >= now() - make_interval(days => %(days)s)",
            window,
        )
        if (
            source
            and source["judged"] >= gate.source_min_judged
            and source["kept"] <= gate.source_max_keep_rate * source["judged"]
        ):
            return _volume_sample_step(
                row["audited"],
                f"{job['source']}: {source['kept']} of {source['judged']} postings kept "
                f"in {gate.window_days} days",
            )
    return None


def _volume_sample_step(audited: bool, why: str) -> PathStep:
    if audited:
        return PathStep(
            stage="volume",
            outcome="info",
            label="Read as part of the audit sample",
            detail=f"{why}; a fixed sample is still read so a change is noticed",
            basis="evaluated_now",
        )
    return PathStep(
        stage="volume",
        outcome="skipped",
        label="Not read: boards and filters do not keep these",
        detail=why,
        basis="evaluated_now",
    )


def _structural_step(job: dict[str, Any], bypass: bool) -> PathStep:
    closed, clearance = (
        _latest_verdict(job["url"], "closed"),
        _latest_verdict(job["url"], "clearance"),
    )
    if closed is None:
        return PathStep(
            stage="structural",
            outcome="pending",
            label="Waiting for verification",
            basis="recorded",
        )
    if closed.status == "rejected":
        return PathStep(
            stage="structural",
            outcome="failed",
            label="Closed postings are not shown",
            basis="recorded",
        )
    if bypass:
        return PathStep(
            stage="structural",
            outcome="passed",
            label="Open; clearance and sponsorship are not filtered here",
            basis="recorded",
        )
    if clearance is None:
        return PathStep(
            stage="structural",
            outcome="pending",
            label="Waiting for the clearance check",
            basis="recorded",
        )
    if clearance.status == "rejected":
        return PathStep(
            stage="structural",
            outcome="failed",
            label="Hidden: needs a clearance, citizenship or has no sponsorship",
            detail="Turn on showing these to see it",
            basis="recorded",
        )
    return PathStep(
        stage="structural",
        outcome="passed",
        label="Open, no clearance requirement",
        basis="recorded",
    )


def _screen_step(job: dict[str, Any], prompt_hash: str) -> PathStep | None:
    """The title screen `title_screens` names for this prompt, if any."""
    recipe: Recipe | None = db.get_config("title_screens").get(prompt_hash)
    if recipe is None:
        return None
    decision = screen(recipe, title=job["title"], source=job["source"])
    return PathStep(
        stage="review_gate",
        outcome="skipped" if decision.skip else "passed",
        label=f"Title screen {recipe}: " + ("not judged" if decision.skip else "judged"),
        detail=decision.reason,
        basis="evaluated_now",
    )


def _verdict_step(
    job: dict[str, Any], prompt_hash: str, model: str | None, filter_id: int | None
) -> PathStep:
    row = verdict_reads.read_latest(
        job["url"], "custom", prompt_hash=prompt_hash, model=model or None
    )
    if row is None:
        return PathStep(
            stage="verdict",
            outcome="pending",
            label="Not judged yet",
            basis="recorded",
            check="custom",
            prompt_hash=prompt_hash,
            filter_id=filter_id,
        )
    origin = _verdict_origin(row.config_name)
    if row.config_name in ("verify-near-copy", "filter-near-copy") and (twin := _near_copy_source_url(job)):
        origin = f"{origin} ({twin})"
    return PathStep(
        stage="verdict",
        outcome="passed" if row.status == "passed" else "failed",
        label="Kept" if row.status == "passed" else "Rejected",
        detail="; ".join(part for part in (row.reason, origin, row.model) if part),
        basis="recorded",
        at=row.created_at,
        check="custom",
        prompt_hash=prompt_hash,
        filter_id=filter_id,
    )
