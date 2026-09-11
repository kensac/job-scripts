"""Configuration for sponsor-owned boards that are not personal boards."""

from __future__ import annotations

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from api import db, managed_board_runs
from api.auth import AuthedUser
from api.models import Criteria
from api.routers.admin.shared import require_admin
from api.updates import NonNullUpdate
from core import providers
from core.filters import ON_AMBIGUOUS_VALUES, compute_filter_hash

router = APIRouter()

_BOOTSTRAP_MODEL = "gpt-5.6-luna"
_BOOTSTRAP_CRITERIA = Criteria(
    max_age_days=7,
    included_locations=["United States", "Canada", "Remote"],
)
_BOOTSTRAP_DEFINITIONS = (
    (
        "software-engineering-internships",
        "Selective Tech Internships",
        "Prestigious software engineering and product management internships at tech and tech-adjacent companies.",
        """Managed board bootstrap prompt v1.
Include only internship opportunities in software engineering or product management at technology or technology-adjacent companies. The opportunity must satisfy the existing high-achiever standard: either the company qualifies for the prestigious company tier, or the particular role is demonstrably selective and top-tier. Exclude full-time, new-grad, entry-level, apprenticeship, unrelated functions, and opportunities whose prestige or selectivity is unclear.""",
    ),
    (
        "software-engineering-new-grad",
        "Selective Tech New Grad",
        "Prestigious full-time software engineering and product management opportunities for new graduates at tech and tech-adjacent companies.",
        """Managed board bootstrap prompt v1.
Include only full-time entry-level or new-graduate opportunities in software engineering or product management at technology or technology-adjacent companies. The opportunity must satisfy the existing high-achiever standard: either the company qualifies for the prestigious company tier, or the particular role is demonstrably selective and top-tier. Exclude internships, apprenticeships, experienced roles, unrelated functions, and opportunities whose prestige or selectivity is unclear.""",
    ),
)

_BOARD_COLS = (
    "b.id, b.slug, b.name, b.description, b.sponsor_user_id, b.prompt, b.prompt_hash, "
    "b.requested_model, b.on_ambiguous, b.fail_closed, b.criteria, b.published, b.revision, "
    "b.public_revision, b.projection_updated_at, b.published_at, b.unpublished_at, "
    "b.created_at, b.updated_at, "
    "COALESCE((SELECT array_agg(s.source ORDER BY s.source) FROM managed_board_sources s "
    "WHERE s.managed_board_id = b.id), '{}') AS sources"
)


class ManagedBoard(BaseModel):
    """A sponsor-owned board definition.

    ``on_ambiguous=filter`` excludes postings whose criteria result is
    ambiguous; ``keep`` retains them.
    """

    id: int
    slug: str
    name: str
    description: str
    sponsor_user_id: int
    prompt: str
    prompt_hash: str
    requested_model: str
    on_ambiguous: str
    fail_closed: bool
    criteria: Criteria
    published: bool
    revision: int
    public_revision: int | None
    projection_updated_at: datetime.datetime | None
    published_at: datetime.datetime | None
    unpublished_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    sources: list[str]


class ManagedBoards(BaseModel):
    boards: list[ManagedBoard]


class ManagedBoardLatestRun(BaseModel):
    run: managed_board_runs.ManagedBoardRun | None


class _Name(BaseModel):
    name: str


class _Id(BaseModel):
    id: int


class _Count(BaseModel):
    n: int


class ManagedBoardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    sponsor_user_id: int | None = None
    prompt: str = Field(min_length=1, max_length=8000)
    requested_model: str
    on_ambiguous: str = "keep"
    fail_closed: bool = False
    criteria: Criteria = Field(default_factory=Criteria)
    sources: list[str] = Field(default_factory=list, max_length=5000)


class ManagedBoardPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    name: NonNullUpdate[Annotated[str, Field(min_length=1, max_length=120)]] = None
    description: NonNullUpdate[Annotated[str, Field(max_length=1000)]] = None
    prompt: NonNullUpdate[Annotated[str, Field(min_length=1, max_length=8000)]] = None
    requested_model: NonNullUpdate[str] = None
    on_ambiguous: NonNullUpdate[str] = None
    fail_closed: NonNullUpdate[bool] = None
    criteria: NonNullUpdate[Criteria] = None
    sources: NonNullUpdate[Annotated[list[str], Field(max_length=5000)]] = None
    published: NonNullUpdate[bool] = None


def _validate_ambiguity(value: str) -> None:
    if value not in ON_AMBIGUOUS_VALUES:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_ON_AMBIGUOUS",
                "message": "on_ambiguous must be 'keep' or 'filter'; filter excludes ambiguity",
            },
        )


def _validate_model(value: str) -> None:
    if value not in providers.MODELS:
        raise HTTPException(
            400, detail={"code": "UNKNOWN_MODEL", "message": "unknown requested model"}
        )


def _validate_sources(sources: list[str]) -> list[str]:
    if len(sources) != len(set(sources)):
        raise HTTPException(
            400, detail={"code": "DUPLICATE_SOURCE", "message": "sources must be unique"}
        )
    ordered = sorted(sources)
    if not ordered:
        return ordered
    known = {
        row.name
        for row in db.query_as(_Name, "SELECT name FROM sources WHERE name = ANY(%s)", (ordered,))
    }
    unknown = [source for source in ordered if source not in known]
    if unknown:
        raise HTTPException(
            400,
            detail={"code": "UNKNOWN_SOURCE", "message": f"unknown sources: {unknown}"},
        )
    return ordered


def _get(board_id: int, *, lock: bool = False) -> ManagedBoard | None:
    suffix = " FOR UPDATE" if lock else ""
    return db.query_one_as(
        ManagedBoard,
        f"SELECT {_BOARD_COLS} FROM managed_boards b WHERE b.id = %s{suffix}",
        (board_id,),
    )


@router.get("/managed-boards")
def list_managed_boards(user: AuthedUser = Depends(require_admin)) -> ManagedBoards:
    return ManagedBoards(
        boards=db.query_as(
            ManagedBoard, f"SELECT {_BOARD_COLS} FROM managed_boards b ORDER BY b.slug"
        )
    )


@router.post("/managed-boards/bootstrap")
def bootstrap_managed_boards(
    user: AuthedUser = Depends(require_admin),
) -> ManagedBoards:
    """Create the two versioned starter boards as drafts for this sponsor."""
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(hashtext('managed-boards-bootstrap-v1'))")
        sources = [
            row.name
            for row in db.query_as(_Name, "SELECT name FROM sources WHERE active ORDER BY name")
        ]
        criteria_json = _BOOTSTRAP_CRITERIA.model_dump(mode="json")
        boards: list[ManagedBoard] = []
        for slug, name, description, prompt in _BOOTSTRAP_DEFINITIONS:
            board = db.query_one_as(
                ManagedBoard,
                f"SELECT {_BOARD_COLS} FROM managed_boards b WHERE b.slug = %s FOR UPDATE",
                (slug,),
            )
            if board is not None:
                matches = (
                    board.name == name
                    and board.description == description
                    and board.sponsor_user_id == user.id
                    and board.prompt == prompt
                    and board.prompt_hash == compute_filter_hash(prompt, "filter")
                    and board.requested_model == _BOOTSTRAP_MODEL
                    and board.on_ambiguous == "filter"
                    and board.fail_closed is True
                    and board.criteria.model_dump(mode="json") == criteria_json
                    and board.sources == sources
                    and board.published is False
                    and board.revision == 1
                )
                if not matches:
                    raise HTTPException(
                        409,
                        detail={
                            "code": "BOOTSTRAP_DRIFT",
                            "message": f"{slug} differs from bootstrap v1",
                        },
                    )
                boards.append(board)
                continue
            row = db.query_one_as(
                _Id,
                "INSERT INTO managed_boards "
                "(slug, name, description, sponsor_user_id, prompt, prompt_hash, requested_model, "
                "on_ambiguous, fail_closed, criteria) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'filter', true, %s) RETURNING id",
                (
                    slug,
                    name,
                    description,
                    user.id,
                    prompt,
                    compute_filter_hash(prompt, "filter"),
                    _BOOTSTRAP_MODEL,
                    db.jsonb(criteria_json),
                ),
            )
            assert row is not None
            if sources:
                db.executemany(
                    "INSERT INTO managed_board_sources (managed_board_id, source) VALUES (%s, %s)",
                    [(row.id, source) for source in sources],
                )
            board = _get(row.id)
            assert board is not None
            boards.append(board)
    return ManagedBoards(boards=boards)


@router.get("/managed-boards/{board_id}")
def get_managed_board(board_id: int, user: AuthedUser = Depends(require_admin)) -> ManagedBoard:
    board = _get(board_id)
    if board is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown managed board"})
    return board


@router.post("/managed-boards/{board_id}/run")
def run_managed_board(
    board_id: int, user: AuthedUser = Depends(require_admin)
) -> managed_board_runs.ManagedBoardRunQueued:
    try:
        return managed_board_runs.admit(board_id)
    except managed_board_runs.RunRefusal as exc:
        status = 404 if exc.code == "NOT_FOUND" else 409
        raise HTTPException(
            status,
            detail={"code": exc.code, "message": exc.message, "task_id": exc.task_id},
        ) from exc


@router.get("/managed-boards/{board_id}/runs/latest")
def latest_managed_board_run(
    board_id: int, user: AuthedUser = Depends(require_admin)
) -> ManagedBoardLatestRun:
    if _get(board_id) is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown managed board"})
    return ManagedBoardLatestRun(run=managed_board_runs.latest(board_id))


@router.get("/managed-boards/{board_id}/cost")
def managed_board_cost(
    board_id: int, user: AuthedUser = Depends(require_admin)
) -> managed_board_runs.ManagedBoardCost:
    if _get(board_id) is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown managed board"})
    return managed_board_runs.cost(board_id)


@router.post("/managed-boards")
def create_managed_board(
    body: ManagedBoardCreate, user: AuthedUser = Depends(require_admin)
) -> ManagedBoard:
    _validate_ambiguity(body.on_ambiguous)
    _validate_model(body.requested_model)
    sponsor_user_id = body.sponsor_user_id or user.id
    try:
        with db.transaction():
            sources = _validate_sources(body.sources)
            if (
                db.query_one_as(_Id, "SELECT id FROM users WHERE id = %s", (sponsor_user_id,))
                is None
            ):
                raise HTTPException(
                    400, detail={"code": "UNKNOWN_SPONSOR", "message": "unknown sponsor"}
                )
            row = db.query_one_as(
                _Id,
                "INSERT INTO managed_boards "
                "(slug, name, description, sponsor_user_id, prompt, prompt_hash, requested_model, "
                "on_ambiguous, fail_closed, criteria) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    body.slug,
                    body.name,
                    body.description,
                    sponsor_user_id,
                    body.prompt,
                    compute_filter_hash(body.prompt, body.on_ambiguous),
                    body.requested_model,
                    body.on_ambiguous,
                    body.fail_closed,
                    db.jsonb(body.criteria.model_dump(mode="json")),
                ),
            )
            assert row is not None
            if sources:
                db.executemany(
                    "INSERT INTO managed_board_sources (managed_board_id, source) VALUES (%s, %s)",
                    [(row.id, source) for source in sources],
                )
            board = _get(row.id)
            assert board is not None
    except UniqueViolation as exc:
        raise HTTPException(
            409, detail={"code": "DUPLICATE_SLUG", "message": "managed board slug exists"}
        ) from exc
    return board


@router.patch("/managed-boards/{board_id}")
def patch_managed_board(
    board_id: int, body: ManagedBoardPatch, user: AuthedUser = Depends(require_admin)
) -> ManagedBoard:
    fields = body.model_dump(exclude_unset=True)
    fields.pop("expected_revision")
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    if "on_ambiguous" in fields:
        _validate_ambiguity(fields["on_ambiguous"])
    if "requested_model" in fields:
        _validate_model(fields["requested_model"])
    requested_sources = fields.pop("sources") if "sources" in fields else None
    with db.transaction():
        existing = _get(board_id, lock=True)
        if existing is None:
            raise HTTPException(
                404, detail={"code": "NOT_FOUND", "message": "unknown managed board"}
            )
        if existing.revision != body.expected_revision:
            raise HTTPException(
                409,
                detail={
                    "code": "STALE_REVISION",
                    "message": "managed board changed; reload it before saving",
                    "current_revision": existing.revision,
                },
            )
        sources = _validate_sources(requested_sources) if requested_sources is not None else None
        if sources is not None:
            db.execute("DELETE FROM managed_board_sources WHERE managed_board_id = %s", (board_id,))
            if sources:
                db.executemany(
                    "INSERT INTO managed_board_sources (managed_board_id, source) VALUES (%s, %s)",
                    [(board_id, source) for source in sources],
                )
        effective_published = fields.get("published", existing.published)
        if effective_published:
            source_count = db.query_one_as(
                _Count,
                "SELECT count(*) AS n FROM managed_board_sources WHERE managed_board_id = %s",
                (board_id,),
            )
            if source_count is None or source_count.n == 0:
                raise HTTPException(
                    400,
                    detail={"code": "NO_SOURCES", "message": "a published board needs a source"},
                )
        prompt = fields.get("prompt", existing.prompt)
        on_ambiguous = fields.get("on_ambiguous", existing.on_ambiguous)
        fields["criteria"] = (
            db.jsonb(fields["criteria"].model_dump(mode="json"))
            if "criteria" in fields
            else db.jsonb(existing.criteria.model_dump(mode="json"))
        )
        fields["prompt_hash"] = compute_filter_hash(prompt, on_ambiguous)
        fields["published"] = effective_published
        next_revision = existing.revision + 1
        advance_public = existing.published or effective_published != existing.published
        next_public_revision = (
            (existing.public_revision or 0) + 1 if advance_public else existing.public_revision
        )
        assignments = ", ".join(f"{key} = %({key})s" for key in fields)
        db.execute(
            f"UPDATE managed_boards SET {assignments}, revision = %(next_revision)s, "
            "public_revision = %(next_public_revision)s, "
            "published_at = CASE WHEN %(published)s AND NOT published THEN now() "
            "ELSE published_at END, "
            "unpublished_at = CASE WHEN %(published)s THEN NULL "
            "WHEN published THEN now() ELSE unpublished_at END, "
            "updated_at = now() WHERE id = %(board_id)s",
            {
                "board_id": board_id,
                "next_revision": next_revision,
                "next_public_revision": next_public_revision,
                **fields,
            },
        )
        board = _get(board_id)
        assert board is not None
    return board
