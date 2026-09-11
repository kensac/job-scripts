"""The tunables an admin may change, served with the registry that
describes them."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, JsonValue

from api import db
from api.auth import AuthedUser
from api.config import CONFIG_KEYS
from api.routers.admin.shared import require_admin

router = APIRouter()


_CONFIG_KEYS = CONFIG_KEYS


@router.get("/config")
def get_config(user: AuthedUser = Depends(require_admin)):
    rows = db.query("SELECT key, value FROM app_config ORDER BY key")
    return {
        "config": {r["key"]: r["value"] for r in rows},
        # The whole registry entry travels: the page picks its control
        # from kind and type, files the key under section, and marks a
        # value that differs from default as changed, none of which it
        # can infer from the help sentence without guessing.
        "keys": {
            key: {
                "type": spec.type.__name__,
                "kind": spec.kind,
                "section": spec.section,
                "default": spec.default,
                "help": spec.help,
                "choices": list(spec.choices),
            }
            for key, spec in _CONFIG_KEYS.items()
        },
    }


class ConfigPut(BaseModel):
    value: JsonValue


@router.put("/config/{key}")
def put_config(key: str, body: ConfigPut, user: AuthedUser = Depends(require_admin)):
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
    db.execute(
        "INSERT INTO app_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, db.jsonb(value)),
    )
    return {"key": key, "value": value}
