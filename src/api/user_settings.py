"""The one owner of `user_settings`.

The row holds six kinds of a person's data, each 1:1 with the person:
the board layout and page preferences (`column_layout`, `prefs`), their own
AI credentials (`api_key_enc`, `ai_provider`, `ai_base_url`, `ai_model`,
`ai_params`), their posting criteria (`criteria`,
`bypass_sponsorship_filter`), the digest state (`email_digest`,
`digest_token`, `last_digest_at`), the apply profile (`profile`) and their
writing style (`writing_style`). Every read and write of the table is here,
typed per kind; a query that needs a person's settings beside other tables
takes its SQL piece from here. `tests/test_user_settings_owner.py` fails on
a mention of the table anywhere else.

The kinds stay in one row, measured 2026-10-10: production holds 2 rows,
no stored API key and 1 digest subscriber, and every kind is read by person
id. A table per kind would add a join to each reader and remove nothing.
"""

from __future__ import annotations

import datetime
import secrets
from dataclasses import dataclass
from typing import Any

from api import db

# A person with no row reads these. GET /user/settings and every reader below
# agree on them because they all come from here.
DEFAULT_PROVIDER = "openai"
DEFAULT_BYPASS_SPONSORSHIP = True


@dataclass(frozen=True)
class Criteria:
    """What a person's board admits: `criteria` in api.models.Criteria's
    shape, as stored, and whether sponsorship screening is skipped."""

    criteria: dict[str, Any]
    bypass_sponsorship_filter: bool


@dataclass(frozen=True)
class Credentials:
    """The person's own AI key and model choice. `api_key_enc` is the
    encrypted key; decrypt it only where a call is made."""

    api_key_enc: bytes | None
    ai_provider: str
    ai_base_url: str | None
    ai_model: str | None
    ai_params: dict[str, Any]

    @property
    def has_key(self) -> bool:
        return self.api_key_enc is not None


@dataclass(frozen=True)
class DigestRecipient:
    user_id: int
    email: str
    digest_token: str | None
    last_digest_at: datetime.datetime | None


# The columns GET /user/settings serves, in the shape the route returns.
_SERVED = (
    "column_layout, prefs, ai_provider, ai_base_url, ai_model, ai_params, "
    "bypass_sponsorship_filter, criteria, email_digest, writing_style, "
    "api_key_enc IS NOT NULL AS has_byo_key"
)
SERVED_DEFAULTS: dict[str, Any] = {
    "column_layout": None,
    "prefs": {},
    "ai_provider": DEFAULT_PROVIDER,
    "ai_base_url": None,
    "ai_model": None,
    "ai_params": {},
    "bypass_sponsorship_filter": DEFAULT_BYPASS_SPONSORSHIP,
    "criteria": {},
    "email_digest": False,
    "writing_style": None,
    "has_byo_key": False,
}


def _one(columns: str, user_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {columns} FROM user_settings WHERE user_id = %s", (user_id,))


# ---- SQL pieces, for queries that read settings beside other tables ----


def join(alias: str, user_id_sql: str) -> str:
    """A LEFT JOIN of the person's settings row as `alias`. A person with no
    row reads NULL in every column, so the caller states its own default."""
    return f"LEFT JOIN user_settings {alias} ON {alias}.user_id = {user_id_sql}"


def has_own_key_sql(user_id_sql: str) -> str:
    """True when the person has stored their own AI key."""
    return (
        "EXISTS (SELECT 1 FROM user_settings own_key "
        f"WHERE own_key.user_id = {user_id_sql} AND own_key.api_key_enc IS NOT NULL)"
    )


# Every location a person named in their criteria, one text per row.
CRITERIA_LOCATIONS_SQL = """
        SELECT DISTINCT btrim(e)
        FROM user_settings s,
             jsonb_array_elements_text(
                 COALESCE(s.criteria->'excluded_locations', '[]'::jsonb)
                 || COALESCE(s.criteria->'included_locations', '[]'::jsonb)) AS e
        WHERE btrim(e) <> ''
"""


# ---- typed reads, one per kind ----


def served(user_id: int) -> dict[str, Any]:
    """The settings GET /user/settings serves, defaults for a person with no row."""
    return {**(_one(_SERVED, user_id) or SERVED_DEFAULTS)}


def prefs(user_id: int) -> dict[str, Any]:
    return (_one("prefs", user_id) or {}).get("prefs") or {}


def criteria(user_id: int) -> Criteria:
    row = _one("criteria, bypass_sponsorship_filter", user_id)
    if not row:
        return Criteria(criteria={}, bypass_sponsorship_filter=DEFAULT_BYPASS_SPONSORSHIP)
    return Criteria(
        criteria=row["criteria"] or {},
        bypass_sponsorship_filter=row["bypass_sponsorship_filter"],
    )


def credentials(user_id: int) -> Credentials:
    row = _one("api_key_enc, ai_provider, ai_base_url, ai_model, ai_params", user_id) or {}
    return Credentials(
        api_key_enc=row.get("api_key_enc"),
        ai_provider=row.get("ai_provider") or DEFAULT_PROVIDER,
        ai_base_url=row.get("ai_base_url"),
        ai_model=row.get("ai_model"),
        ai_params=row.get("ai_params") or {},
    )


def has_own_key(user_id: int) -> bool:
    row = _one("api_key_enc IS NOT NULL AS has_key", user_id)
    return bool(row and row["has_key"])


def profile(user_id: int) -> dict[str, Any]:
    return (_one("profile", user_id) or {}).get("profile") or {}


def writing_style(user_id: int) -> str | None:
    return (_one("writing_style", user_id) or {}).get("writing_style") or None


def digest_recipients(*, force: bool, only: int | None) -> list[DigestRecipient]:
    """Everyone a digest goes to: subscribed (or everyone, forced) with a
    deliverable address, optionally one person."""
    where_user = "AND u.id = %(only)s" if only else ""
    rows = db.query(
        f"""
        SELECT u.id AS user_id, u.email, s.digest_token, s.last_digest_at
        FROM users u JOIN user_settings s ON s.user_id = u.id
        WHERE (s.email_digest OR %(force)s) AND u.email LIKE '%%@%%' {where_user}
        """,
        {"force": force, "only": only},
    )
    return [DigestRecipient(**r) for r in rows]


# ---- writes ----


def ensure(user_id: int) -> None:
    """A row with every default, for a person who has none."""
    db.execute(
        "INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )


@dataclass
class Update:
    """The fields PUT /user/settings may change. A field that is None is
    left alone, except where its `*_set` flag says the caller sent it: then
    None is an explicit reset (column_layout, ai_model, writing_style)."""

    column_layout: Any = None
    column_layout_set: bool = False
    prefs: dict[str, Any] | None = None
    ai_model: str | None = None
    ai_model_set: bool = False
    ai_params: dict[str, Any] | None = None
    bypass_sponsorship_filter: bool | None = None
    criteria: dict[str, Any] | None = None
    email_digest: bool | None = None
    writing_style: str | None = None
    writing_style_set: bool = False


def save(user_id: int, u: Update) -> None:
    db.execute(
        """
        INSERT INTO user_settings (user_id, column_layout, prefs, ai_model, ai_params,
                                   bypass_sponsorship_filter, criteria,
                                   email_digest, writing_style, updated_at)
        VALUES (%(uid)s, %(layout)s, COALESCE(%(prefs)s, '{}'::jsonb),
                %(model)s, COALESCE(%(params)s, '{}'::jsonb),
                COALESCE(%(bypass)s, TRUE), COALESCE(%(criteria)s, '{}'::jsonb),
                COALESCE(%(digest)s, FALSE), NULLIF(%(style)s, ''), now())
        ON CONFLICT (user_id) DO UPDATE SET
            -- Presence distinguishes an explicit reset from an omitted field.
            column_layout = CASE WHEN %(layout_set)s THEN EXCLUDED.column_layout
                                 ELSE user_settings.column_layout END,
            prefs = COALESCE(%(prefs)s, user_settings.prefs),
            ai_model = CASE WHEN %(model_set)s THEN EXCLUDED.ai_model
                            ELSE user_settings.ai_model END,
            ai_params = COALESCE(%(params)s, user_settings.ai_params),
            bypass_sponsorship_filter = COALESCE(%(bypass)s, user_settings.bypass_sponsorship_filter),
            criteria = COALESCE(%(criteria)s, user_settings.criteria),
            email_digest = COALESCE(%(digest)s, user_settings.email_digest),
            -- Null and an empty string both clear the saved override.
            writing_style = CASE WHEN %(style_set)s THEN EXCLUDED.writing_style
                                 ELSE user_settings.writing_style END,
            updated_at = now()
        """,
        {
            "uid": user_id,
            "layout": db.jsonb(u.column_layout) if u.column_layout is not None else None,
            "layout_set": u.column_layout_set,
            "prefs": db.jsonb(u.prefs) if u.prefs is not None else None,
            "model": u.ai_model,
            "model_set": u.ai_model_set,
            "params": db.jsonb(u.ai_params) if u.ai_params is not None else None,
            "bypass": u.bypass_sponsorship_filter,
            "criteria": db.jsonb(u.criteria) if u.criteria is not None else None,
            "digest": u.email_digest,
            "style": u.writing_style.strip() if u.writing_style is not None else None,
            "style_set": u.writing_style_set,
        },
    )
    if u.email_digest:
        ensure_digest_token(user_id)


def merge_pref(user_id: int, key: str, entry: str, value: str) -> None:
    """Set prefs[key][entry] = value, keeping every other key."""
    db.execute(
        """
        INSERT INTO user_settings (user_id, prefs)
        VALUES (%(uid)s, jsonb_build_object(%(key)s::text, jsonb_build_object(%(entry)s::text, %(value)s::text)))
        ON CONFLICT (user_id) DO UPDATE SET prefs =
            COALESCE(user_settings.prefs, '{}'::jsonb)
            || jsonb_build_object(%(key)s::text,
                 COALESCE(user_settings.prefs->%(key)s, '{}'::jsonb)
                 || jsonb_build_object(%(entry)s::text, %(value)s::text)),
            updated_at = now()
        """,
        {"uid": user_id, "key": key, "entry": entry, "value": value},
    )


def save_profile(user_id: int, value: dict[str, Any]) -> None:
    db.execute(
        """
        INSERT INTO user_settings (user_id, profile, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (user_id) DO UPDATE SET profile = EXCLUDED.profile, updated_at = now()
        """,
        (user_id, db.jsonb(value)),
    )


def save_api_key(user_id: int, api_key_enc: bytes, provider: str, base_url: str | None) -> None:
    """A new key resets the model choice: a model chosen for one provider
    means nothing on another."""
    db.execute(
        """
        INSERT INTO user_settings (user_id, api_key_enc, ai_provider, ai_base_url, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (user_id) DO UPDATE SET
            api_key_enc = EXCLUDED.api_key_enc,
            ai_provider = EXCLUDED.ai_provider,
            ai_base_url = EXCLUDED.ai_base_url,
            ai_model = NULL,
            updated_at = now()
        """,
        (user_id, api_key_enc, provider, base_url),
    )


def clear_api_key(user_id: int) -> None:
    db.execute(
        "UPDATE user_settings SET api_key_enc = NULL, ai_base_url = NULL, "
        "ai_provider = %s, ai_model = NULL, updated_at = now() WHERE user_id = %s",
        (DEFAULT_PROVIDER, user_id),
    )


def ensure_digest_token(user_id: int) -> str:
    """The person's unsubscribe token, minted once."""
    row = db.query_one(
        """
        INSERT INTO user_settings (user_id, digest_token) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE
            SET digest_token = COALESCE(user_settings.digest_token, EXCLUDED.digest_token)
        RETURNING digest_token
        """,
        (user_id, secrets.token_urlsafe(24)),
    )
    assert row  # an upsert with RETURNING returns its row
    return row["digest_token"]


def mark_digest_sent(user_id: int) -> None:
    db.execute("UPDATE user_settings SET last_digest_at = now() WHERE user_id = %s", (user_id,))


def unsubscribe_digest(token: str) -> bool:
    """Turn the digest off for whoever holds this token; False when nobody does."""
    row = db.query_one(
        "UPDATE user_settings SET email_digest = FALSE, updated_at = now() "
        "WHERE digest_token = %s RETURNING user_id",
        (token,),
    )
    return row is not None
