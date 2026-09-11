from __future__ import annotations

from api import db
from api.routers.job_board import default_sort


def test_the_board_opens_newest_posted_first_then_newest_seen():
    """Two keys, because one is not enough.

    `date_posted` is what a person wants, but a large share of the catalog has
    no `date_posted` at all and every sort here is NULLS LAST, so those would
    sink together into an arbitrary block. `added_at` breaks that tie with the
    next best thing: when we first saw it.
    """
    assert default_sort() == [
        {"key": "date_posted", "dir": "desc"},
        {"key": "added_at", "dir": "desc"},
    ]


def test_a_configured_key_the_board_cannot_sort_by_is_dropped_not_raised():
    """An admin editing this should get a board that ignores the typo, not one
    that 500s: `sorting.clause` looks the key up in `_SORTABLE` and would raise
    on a read path."""
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('board_default_sort', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb([{"key": "nonsense", "dir": "desc"}, {"key": "company", "dir": "asc"}]),),
    )
    try:
        assert default_sort() == [{"key": "company", "dir": "asc"}]
    finally:
        db.execute("DELETE FROM app_config WHERE key = 'board_default_sort'")


def test_a_default_of_nothing_usable_still_orders_the_board():
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('board_default_sort', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb([{"key": "nonsense", "dir": "desc"}]),),
    )
    try:
        assert default_sort() == [{"key": "added_at", "dir": "desc"}]
    finally:
        db.execute("DELETE FROM app_config WHERE key = 'board_default_sort'")


def test_the_board_reports_the_sort_it_applied(client, user_headers):
    """`sorts` is what the page renders its header state from, so an unasked
    sort has to come back named rather than silently applied."""
    body = client.get("/v1/user/jobs?limit=1", headers=user_headers).json()

    assert body["sorts"] == [
        {"key": "date_posted", "dir": "desc"},
        {"key": "added_at", "dir": "desc"},
    ]


def test_an_explicit_sort_still_wins(client, user_headers):
    body = client.get("/v1/user/jobs?limit=1&sort=company&dir=asc", headers=user_headers).json()

    assert body["sorts"] == [{"key": "company", "dir": "asc"}]


def test_settings_serves_the_same_order_the_board_opens_on(client, user_headers):
    """One answer, so the page has no literal of its own to drift."""
    settings = client.get("/v1/user/settings", headers=user_headers).json()

    assert settings["default_sort"] == default_sort()
