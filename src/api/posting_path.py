"""The path one posting took to each board and filter, and where it stopped.

Every rule that reads, skips, judges or shows a posting is a stage here, in
the order the pipeline applies them. A stage is `recorded` when a row says what
happened (a verdict, a review-gate decision, board membership) and
`evaluated_now` when the rule leaves no row and is run again for this posting:
criteria, title gates, the verification volume gate and reachability decide by
leaving a posting out of a SELECT, so their answer is today's, not the one in
force when the posting was skipped.
"""

from __future__ import annotations

import contextvars
import datetime
from typing import Any, Literal

from pydantic import BaseModel

from api import db
from api.ai import verdicts
from api.board import criteria as board_criteria
from core.managed_board_title_gate import TitleGateConfig
from core.managed_board_title_gate import evaluate as evaluate_title_gate
from core.review_gate import OCCUPATION_SQL_PATTERN, TECHNICAL_SQL_PATTERN, VolumeGate

Outcome = Literal["passed", "failed", "skipped", "pending", "info"]

# Each board and filter asks for the same closed and clearance verdicts; one
# path reads them once.
_LATEST: contextvars.ContextVar[dict[tuple[str, str], Any]] = contextvars.ContextVar("path_latest")
Basis = Literal["recorded", "evaluated_now"]


class PathStep(BaseModel):
    stage: str
    outcome: Outcome
    label: str
    detail: str | None = None
    basis: Basis
    at: datetime.datetime | None = None
    # What an action on this step addresses (re-check, explain).
    check: str | None = None
    prompt_hash: str | None = None
    filter_id: int | None = None


class ConsumerPath(BaseModel):
    kind: Literal["managed_board", "filter", "board"]
    id: int | None
    name: str
    owner: str | None = None
    included: bool | None
    summary: str
    steps: list[PathStep]


class PostingPath(BaseModel):
    job_id: int
    url: str
    title: str | None
    source: str
    steps: list[PathStep]
    consumers: list[ConsumerPath]


# Where a verdict row came from, by config_name, in the words the drawer uses.
_ORIGIN = {
    "verify-batch": "answered when the posting was verified",
    "verify-near-copy": "copied from a twin posting",
    "reverify": "re-checked",
    "reverify-unchanged": "re-check reused: the page had not changed",
    "admin": "set by an admin",
    "filter-batch": "judged by its own run",
    "explain": "asked by a person",
}

_CRITERION_LABELS = {
    "posted_after": "posted after the date the criteria name",
    "max_age": "inside the age window",
    "excluded_locations": "not in an excluded location",
    "included_locations": "in an included location",
    "terms": "has an included work term",
}


def _origin(config_name: str | None) -> str:
    return _ORIGIN.get(config_name or "", (config_name or "recorded").replace("-", " "))


def _job(where: str, value: Any) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT id, url, title, company, source, active, uploaded_by, near_copy_key, "
        f"date_posted, created_at FROM jobs WHERE {where} = %s",
        (value,),
    )


def _latest(url: str, check: str) -> dict[str, Any] | None:
    key = (url, check)
    if key not in _LATEST.get():
        _LATEST.get()[key] = _read_latest(url, check)
    return _LATEST.get()[key]


def _read_latest(url: str, check: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT status, reason, config_name, model, created_at FROM verdicts "
        "WHERE url = %s AND check_type = %s "
        "ORDER BY id DESC LIMIT 1",
        (url, check),
    )


def _twin(job: dict[str, Any]) -> str | None:
    """The posting a near-copy verdict was copied from, found again by its key."""
    if not job["near_copy_key"]:
        return None
    row = db.query_one(
        "SELECT j.url FROM jobs j WHERE j.source = %s AND j.near_copy_key = %s AND j.id <> %s "
        "AND EXISTS (SELECT 1 FROM ai_queries q WHERE q.url = j.url AND q.check_type = 'closed' "
        "AND q.config_name IS DISTINCT FROM 'verify-near-copy') ORDER BY j.id LIMIT 1",
        (job["source"], job["near_copy_key"], job["id"]),
    )
    return row["url"] if row else None


def _catalog_steps(job: dict[str, Any]) -> list[PathStep]:
    if job["uploaded_by"] is not None:
        return [
            PathStep(stage="catalog", outcome="info", label="Added by a person", basis="recorded")
        ]
    steps = []
    listing = db.query_one(
        "SELECT pattern, kept, last_seen_at FROM listings WHERE url = %s", (job["url"],)
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
        event = db.query_one(
            "SELECT at FROM job_listing_events WHERE job_id = %s AND NOT listed "
            "ORDER BY at DESC LIMIT 1",
            (job["id"],),
        )
        steps.append(
            PathStep(
                stage="catalog",
                outcome="failed",
                label="No longer listed by its board",
                detail="Inactive postings are not read, judged or shown",
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
        verdict = _latest(job["url"], check)
        if verdict is None:
            continue
        origin = _origin(verdict["config_name"])
        if verdict["config_name"] == "verify-near-copy":
            twin = twin or _twin(job)
            if twin:
                origin = f"{origin} ({twin})"
        steps.append(
            PathStep(
                stage="verification",
                outcome="failed" if verdict["status"] == "rejected" else "passed",
                label=flagged if verdict["status"] == "rejected" else clear,
                detail="; ".join(part for part in (verdict["reason"], origin) if part),
                basis="recorded",
                at=verdict["created_at"],
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
        "SELECT (j.title !~* %(tech)s AND j.title ~* %(occ)s) AS occupation, "
        "abs(hashtext(j.url)) %% 100 < %(audit)s AS audited FROM jobs j WHERE j.id = %(jid)s",
        {
            "tech": TECHNICAL_SQL_PATTERN,
            "occ": OCCUPATION_SQL_PATTERN,
            "audit": gate.audit_percent,
            "jid": job["id"],
        },
    )
    assert row is not None
    if gate.occupation_titles and row["occupation"]:
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
            return _sampled(
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
            return _sampled(
                row["audited"],
                f"{job['source']}: {source['kept']} of {source['judged']} postings kept "
                f"in {gate.window_days} days",
            )
    return None


def _sampled(audited: bool, why: str) -> PathStep:
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
    closed, clearance = _latest(job["url"], "closed"), _latest(job["url"], "clearance")
    if closed is None:
        return PathStep(
            stage="structural",
            outcome="pending",
            label="Waiting for verification",
            basis="recorded",
        )
    if closed["status"] == "rejected":
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
    if clearance["status"] == "rejected":
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


def _review_step(job: dict[str, Any], where: str, value: int) -> PathStep | None:
    row = db.query_one(
        "SELECT b.stage, b.mode, b.action, b.reason, d.created_at "
        "FROM review_gate_decisions d JOIN review_gate_decision_bodies b ON b.id = d.body_id "
        "WHERE d.url_id = (SELECT id FROM review_gate_urls WHERE url = %s) "
        f"AND d.{where} = %s ORDER BY d.id DESC LIMIT 1",
        (job["url"], value),
    )
    if row is None:
        return None
    if row["action"] == "skip":
        return PathStep(
            stage="review_gate",
            outcome="skipped",
            label=f"Skipped before review by the {row['stage']} rule",
            detail=row["reason"],
            basis="recorded",
            at=row["created_at"],
        )
    return PathStep(
        stage="review_gate",
        outcome="info",
        label="Sent to review",
        detail=f"{row['stage']} stage, {row['mode']} mode"
        + (f": {row['reason']}" if row["reason"] else ""),
        basis="recorded",
        at=row["created_at"],
    )


def _verdict_step(
    job: dict[str, Any], prompt_hash: str, model: str | None, filter_id: int | None
) -> PathStep:
    clause = " AND model = %s" if model else ""
    params: tuple = (job["url"], prompt_hash, model) if model else (job["url"], prompt_hash)
    row = db.query_one(
        "SELECT status, reason, config_name, model, created_at FROM verdicts WHERE url = %s "
        f"AND check_type = 'custom' AND prompt_hash = %s{clause} "
        "ORDER BY id DESC LIMIT 1",
        params,
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
    origin = _origin(row["config_name"])
    if row["config_name"] == "verify-near-copy" and (twin := _twin(job)):
        origin = f"{origin} ({twin})"
    return PathStep(
        stage="verdict",
        outcome="passed" if row["status"] == "passed" else "failed",
        label="Kept" if row["status"] == "passed" else "Rejected",
        detail="; ".join(part for part in (row["reason"], origin, row["model"]) if part),
        basis="recorded",
        at=row["created_at"],
        check="custom",
        prompt_hash=prompt_hash,
        filter_id=filter_id,
    )


def _summary(steps: list[PathStep], included: bool | None) -> str:
    if included:
        return "On it"
    stop = next((s for s in steps if s.outcome in ("failed", "skipped")), None)
    if stop:
        return stop.label
    if any(s.outcome == "pending" for s in steps):
        return next(s.label for s in steps if s.outcome == "pending")
    return "Not on it"


def _managed_board(job: dict[str, Any], board: dict[str, Any], gate: VolumeGate) -> ConsumerPath:
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
            decision = evaluate_title_gate(config, title=job["title"] or "", source=job["source"])
            steps.append(
                PathStep(
                    stage="title_gate",
                    outcome="passed"
                    if decision.keep
                    else ("skipped" if config.mode == "enforce" else "info"),
                    label=(
                        f"Title gate {config.recipe}: "
                        + ("kept" if decision.keep else "dropped")
                        + (
                            " (shadow, not applied)"
                            if config.mode == "shadow" and not decision.keep
                            else ""
                        )
                    ),
                    detail=decision.reason,
                    basis="evaluated_now",
                )
            )
        if review := _review_step(job, "managed_board_id", board["id"]):
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
        summary=_summary(steps, bool(member)),
        steps=steps,
    )


def _filters(
    job: dict[str, Any], user_id: int, gate: VolumeGate, owner: str | None
) -> list[ConsumerPath]:
    settings = db.query_one(
        "SELECT criteria, bypass_sponsorship_filter FROM user_settings WHERE user_id = %s",
        (user_id,),
    )
    subscribed = job["uploaded_by"] == user_id or bool(
        db.query_one(
            "SELECT 1 FROM user_sources WHERE user_id = %s AND source = %s",
            (user_id, job["source"]),
        )
    )
    bypass = settings["bypass_sponsorship_filter"] if settings else True
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
            steps.append(_criteria_step(job, board_criteria.params(settings)))
            if volume := _volume_step(job, gate, f["prompt_hash"]):
                steps.append(volume)
            if review := _review_step(job, "filter_id", f["id"]):
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
                summary=_summary(steps, passed),
                steps=steps,
            )
        )
    return paths


def _board(job: dict[str, Any], user_id: int) -> ConsumerPath:
    """The person's own board: which branch of the visibility predicate admits it."""
    row = db.query_one(
        "SELECT (SELECT computed_at FROM board_visible WHERE user_id = %(uid)s AND job_id = %(jid)s) "
        "AS member, (SELECT COALESCE(status, '') <> '' OR COALESCE(notes, '') <> '' "
        "OR date_applied IS NOT NULL FROM user_jobs WHERE user_id = %(uid)s AND job_id = %(jid)s) "
        "AS acted_on",
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


def _shared(job: dict[str, Any]) -> list[PathStep]:
    return [*_catalog_steps(job), _content_step(job), *_verification_steps(job)]


def _gate() -> VolumeGate:
    return VolumeGate.model_validate(db.get_config("verification_volume_gate"))


def for_user(job_id: int, user_id: int) -> PostingPath | None:
    _LATEST.set({})
    job = _job("id", job_id)
    if job is None:
        return None
    gate = _gate()
    return PostingPath(
        job_id=job["id"],
        url=job["url"],
        title=job["title"],
        source=job["source"],
        steps=_shared(job),
        consumers=[*_filters(job, user_id, gate, None), _board(job, user_id)],
    )


def for_admin(url: str) -> PostingPath | None:
    _LATEST.set({})
    job = _job("url", url)
    if job is None:
        return None
    gate = _gate()
    boards = db.query(
        "SELECT b.id, b.name, b.prompt_hash, b.requested_model, b.criteria, b.title_gate, "
        "b.bypass_sponsorship_filter, COALESCE((SELECT array_agg(s.source) FROM managed_board_sources s "
        "WHERE s.managed_board_id = b.id), '{}') AS sources "
        "FROM managed_boards b WHERE b.published AND b.execution_mode = 'managed_filter' ORDER BY b.id"
    )
    owners = db.query(
        "SELECT DISTINCT u.id, COALESCE(u.email, u.name, 'user ' || u.id) AS label FROM users u "
        "JOIN user_filters f ON f.user_id = u.id AND f.enabled ORDER BY u.id"
    )
    consumers = [_managed_board(job, board, gate) for board in boards]
    for owner in owners:
        consumers.extend(_filters(job, owner["id"], gate, owner["label"]))
    return PostingPath(
        job_id=job["id"],
        url=job["url"],
        title=job["title"],
        source=job["source"],
        steps=_shared(job),
        consumers=consumers,
    )
