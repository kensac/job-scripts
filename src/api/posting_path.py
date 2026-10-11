"""The path one posting took to each board and filter, and where it stopped.

Each stage reports whether it reads a stored record or evaluates a current
rule. The user entry point is limited to the caller's filters and board.
"""

from __future__ import annotations

from typing import Any

from api import db
from api.posting_path_catalog import _catalog_steps, _content_step, _verification_steps
from api.posting_path_common import _LATEST
from api.posting_path_common import Basis as Basis
from api.posting_path_common import ConsumerPath as ConsumerPath
from api.posting_path_common import Outcome as Outcome
from api.posting_path_common import PathStep as PathStep
from api.posting_path_common import PostingPath as PostingPath
from api.posting_path_consumers import _filter_paths, _managed_board_path, _personal_board_path
from core import catalog
from core.volume_gate import VolumeGate


def _posting_row(where: str, value: Any) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT j.id, j.url, j.title, j.company, j.source, "
        f"{catalog.IS_AVAILABLE.format(job='j')} AS active, "
        "(SELECT p.uploaded_by FROM posting_uploads p WHERE p.job_id = j.id) AS uploaded_by, "
        f"j.near_copy_key, j.date_posted, j.created_at FROM jobs j WHERE j.{where} = %s",
        (value,),
    )


def _shared_steps(job: dict[str, Any]) -> list[PathStep]:
    return [*_catalog_steps(job), _content_step(job), *_verification_steps(job)]


def _configured_volume_gate() -> VolumeGate:
    return VolumeGate.model_validate(db.get_config("verification_volume_gate"))


def for_user(job_id: int, user_id: int) -> PostingPath | None:
    _LATEST.set({})
    job = _posting_row("id", job_id)
    if job is None:
        return None
    gate = _configured_volume_gate()
    return PostingPath(
        job_id=job["id"],
        url=job["url"],
        title=job["title"],
        source=job["source"],
        steps=_shared_steps(job),
        consumers=[*_filter_paths(job, user_id, gate, None), _personal_board_path(job, user_id)],
    )


def for_admin(url: str) -> PostingPath | None:
    _LATEST.set({})
    job = _posting_row("url", url)
    if job is None:
        return None
    gate = _configured_volume_gate()
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
    consumers = [_managed_board_path(job, board, gate) for board in boards]
    for owner in owners:
        consumers.extend(_filter_paths(job, owner["id"], gate, owner["label"]))
    return PostingPath(
        job_id=job["id"],
        url=job["url"],
        title=job["title"],
        source=job["source"],
        steps=_shared_steps(job),
        consumers=consumers,
    )
