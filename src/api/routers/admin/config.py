"""The tunables an admin may change, served with the registry that
describes them."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, JsonValue

from api import db, scoping
from api.auth import AuthedUser
from api.config import CONFIG_KEYS
from api.routers.admin.shared import require_admin
from core.review_gate import ReviewGateScope

router = APIRouter()


_CONFIG_KEYS = CONFIG_KEYS


class Tunable(BaseModel):
    """One registry entry, as `api.config.ConfigKey` holds it. `type` is the
    name of a Python type rather than the type, because it travels as JSON and
    the page only needs to tell an int box from a checkbox."""

    type: str
    kind: str
    section: str
    default: JsonValue
    help: str
    choices: list[str]


class Tunables(BaseModel):
    """The stored values beside the registry that describes them, both keyed
    by the config key. `config` holds only keys app_config has a row for;
    `keys` holds every key the registry declares, which is what lets the page
    render one it has never seen."""

    config: dict[str, JsonValue]
    keys: dict[str, Tunable]


@router.get("/config")
def get_config(user: AuthedUser = Depends(require_admin)) -> Tunables:
    rows = db.query("SELECT key, value FROM app_config ORDER BY key")
    return Tunables(
        config={r["key"]: r["value"] for r in rows},
        # The whole registry entry travels: the page picks its control
        # from kind and type, files the key under section, and marks a
        # value that differs from default as changed, none of which it
        # can infer from the help sentence without guessing.
        keys={
            key: Tunable(
                type=spec.type.__name__,
                kind=spec.kind,
                section=spec.section,
                default=spec.default,
                help=spec.help,
                choices=list(spec.choices),
            )
            for key, spec in _CONFIG_KEYS.items()
        },
    )


class ConfigPut(BaseModel):
    value: JsonValue


class ConfigChangeRecord(BaseModel):
    id: int
    key: str
    old_value: JsonValue
    new_value: JsonValue
    old_value_present: bool
    actor_user_id: int
    created_at: datetime


class ConfigHistory(BaseModel):
    items: list[ConfigChangeRecord]
    next_before_id: int | None


@router.get("/config/history")
def config_history(
    key: str | None = None,
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: AuthedUser = Depends(require_admin),
) -> ConfigHistory:
    rows = db.query_as(
        ConfigChangeRecord,
        "SELECT id, key, old_value, new_value, old_value_present, actor_user_id, created_at "
        "FROM app_config_changes WHERE (%s::text IS NULL OR key = %s) "
        "AND (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT %s",
        (key, key, before_id, before_id, limit + 1),
    )
    items = rows[:limit]
    return ConfigHistory(items=items, next_before_id=items[-1].id if len(rows) > limit else None)


class FilterScopeOption(BaseModel):
    kind: Literal["managed_board", "personal_filter"]
    id: int
    name: str
    user_id: int
    prompt_hash: str
    active: bool
    revision: int | None


class FilterScopeOptions(BaseModel):
    scopes: list[FilterScopeOption]
    title_recipes: list[str]
    profile_recipes: list[str]
    filters: dict[str, list[str]]
    filterable: list[str]


@router.get("/config/filter-scopes")
def filter_scopes(
    user_filter: str | None = Query(default=None, alias="user"),
    user: AuthedUser = Depends(require_admin),
) -> FilterScopeOptions:
    ids = scoping.user_ids(user_filter)
    predicate = scoping.column("user_id") if ids else "TRUE"
    scopes = db.query_as(
        FilterScopeOption,
        "SELECT kind, id, name, user_id, prompt_hash, active, revision FROM ("
        "SELECT 'managed_board' AS kind, id, name, sponsor_user_id AS user_id, "
        "prompt_hash, published AS active, revision FROM managed_boards "
        "WHERE execution_mode = 'managed_filter' UNION ALL "
        "SELECT 'personal_filter' AS kind, id, name, user_id, prompt_hash, "
        "enabled AS active, NULL::bigint AS revision FROM user_filters) scopes "
        f"WHERE {predicate} ORDER BY kind, name, id",
        {"user_ids": ids},
    )
    # The model's vocabulary is the writer's validation contract, not a
    # separately maintained list for the scope picker.
    properties = ReviewGateScope.model_json_schema()["properties"]

    def recipes(field: str) -> list[str]:
        return [
            value
            for choice in properties[field]["anyOf"]
            for value in ([choice["const"]] if "const" in choice else choice.get("enum", []))
        ]

    return FilterScopeOptions(
        scopes=scopes,
        title_recipes=recipes("title_recipe"),
        profile_recipes=recipes("profile_recipe"),
        filters={"user": scoping.echo(ids)},
        filterable=["user"],
    )


class TunableWritten(BaseModel):
    """The value as stored, which is not always the value as sent: a spec
    normalises what it validates, so the caller is told what landed."""

    key: str
    value: JsonValue


@router.put("/config/{key}")
def put_config(
    key: str, body: ConfigPut, user: AuthedUser = Depends(require_admin)
) -> TunableWritten:
    spec = _CONFIG_KEYS.get(key)
    if spec is None:
        raise HTTPException(
            400,
            detail={"code": "UNKNOWN_KEY", "message": f"key must be one of {sorted(_CONFIG_KEYS)}"},
        )
    try:
        value = spec.validate(body.value)
    except ValueError as exc:
        raise HTTPException(
            400,
            detail={"code": "INVALID_VALUE", "message": f"{key}: {exc}"},
        ) from exc
    with db.transaction():
        # Lock the key even before its first row exists. Concurrent saves
        # must record the actual predecessor, not the same stale old value.
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended('app_config:' || %s, 0))", (key,))
        old = db.query_one("SELECT value FROM app_config WHERE key = %s FOR UPDATE", (key,))
        if old is None or old["value"] != value:
            db.execute(
                "INSERT INTO app_config_changes "
                "(key, old_value, new_value, old_value_present, actor_user_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    key,
                    db.jsonb(old["value"] if old else None),
                    db.jsonb(value),
                    old is not None,
                    user.id,
                ),
            )
            db.execute(
                "INSERT INTO app_config (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, db.jsonb(value)),
            )
    return TunableWritten(key=key, value=value)
