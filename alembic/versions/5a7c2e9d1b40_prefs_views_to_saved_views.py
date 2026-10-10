"""prefs.views to saved_views

Board views live only in saved_views. The tracker used to keep them in
user_settings.prefs.views[page] as flat params; the frontend copies a page's
prefs views up the first time it reads an empty saved_views page. This
converts every page that copy has not reached, the same way, so nothing named
is lost when the frontend stops reading prefs.views.

Only a page with no saved_views rows is converted, because that is the only
page the frontend's copy would still act on: a page with rows was read at
least once, so its prefs views were copied then, and a name missing now was
refused or deleted since. Safe to run twice: the second run finds every page
holding rows.

Production on 2026-10-10: 2 settings rows, 1 with prefs.views, 1 view on 1
page, and that page already holds its saved_views row. This converts nothing
there; it exists for every other database holding the old shape.

The schema does not change; there is no downgrade of the data.

Revision ID: 5a7c2e9d1b40
Revises: d4813d05adcc
Create Date: 2026-10-10 01:00:00.000000

"""
import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '5a7c2e9d1b40'
down_revision: Union[str, None] = 'd4813d05adcc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The API's own limits on a view (api/routers/views.py): a row it would refuse
# is not written here either.
_MAX_NAME = 80
_MAX_STATE_BYTES = 32_000


def params_to_state(params: dict[str, Any]) -> dict[str, Any]:
    """paramsToState in the frontend's lib/saved-views.ts: flat URL params to
    the stored shape. Empty values drop out; sort and dir are comma stacks;
    q or search is the search; columns is JSON; column_filters is one value;
    every other key is a comma list filter."""
    p = {
        k: str(v)
        for k, v in sorted(params.items())
        if v is not None and v != ""
    }
    keys = [x.strip() for x in p.get("sort", "").split(",") if x.strip()]
    dirs = [x.strip() for x in p.get("dir", "").split(",")]
    state: dict[str, Any] = {
        "filters": {},
        "sorts": [
            {"key": k, "dir": "asc" if i < len(dirs) and dirs[i] == "asc" else "desc"}
            for i, k in enumerate(keys)
        ],
    }
    for k, v in p.items():
        if k in ("sort", "dir"):
            continue
        if k in ("q", "search"):
            state["search"] = v
        elif k == "columns":
            try:
                state["columns"] = json.loads(v)
            except ValueError:
                pass
        elif k == "column_filters":
            state["filters"][k] = [v]
        else:
            state["filters"][k] = [x.strip() for x in v.split(",") if x.strip()]
    return state


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT s.user_id, v.page, v.views
            FROM user_settings s, jsonb_each(s.prefs->'views') AS v(page, views)
            WHERE jsonb_typeof(s.prefs->'views') = 'object'
              AND jsonb_typeof(v.views) = 'array'
              AND NOT EXISTS (SELECT 1 FROM saved_views sv
                              WHERE sv.user_id = s.user_id AND sv.page = v.page)
            ORDER BY s.user_id, v.page
            """
        )
    ).all()
    for user_id, page, views in rows:
        has_default = False
        position = 0
        for view in views:
            if not isinstance(view, dict) or not isinstance(view.get("params"), dict):
                continue
            name = str(view.get("name") or "").strip()
            state = json.dumps(params_to_state(view["params"]))
            if not name or len(name) > _MAX_NAME or len(state) > _MAX_STATE_BYTES:
                continue
            is_default = view.get("is_default") is True and not has_default
            inserted = conn.execute(
                sa.text(
                    """
                    INSERT INTO saved_views (user_id, page, name, state, is_default, position)
                    VALUES (:uid, :page, :name, CAST(:state AS jsonb), :is_default, :position)
                    ON CONFLICT (user_id, page, name) DO NOTHING
                    """
                ),
                {
                    "uid": user_id,
                    "page": page,
                    "name": name,
                    "state": state,
                    "is_default": is_default,
                    "position": position,
                },
            ).rowcount
            if inserted:
                has_default = has_default or is_default
                position += 1


def downgrade() -> None:
    pass
