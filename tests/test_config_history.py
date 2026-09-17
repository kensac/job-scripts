from concurrent.futures import ThreadPoolExecutor

import pytest

from api import db
from api.auth import AuthedUser
from api.routers.admin.config import ConfigPut, put_config


def test_config_history_records_actual_changes_and_actor(client, admin_headers):
    path = "/v1/admin/config/embedding_visible_only"
    assert client.put(path, json={"value": False}, headers=admin_headers).status_code == 200
    assert client.put(path, json={"value": False}, headers=admin_headers).status_code == 200
    assert client.put(path, json={"value": True}, headers=admin_headers).status_code == 200
    response = client.get("/v1/admin/config/history", headers=admin_headers)
    assert response.status_code == 200
    rows = response.json()["items"]
    assert [(r["old_value"], r["new_value"]) for r in rows] == [(False, True), (True, False)]
    admin = db.query_one("SELECT id FROM users WHERE sub = 'test-admin'")
    assert admin
    assert all(r["actor_user_id"] == admin["id"] and r["old_value_present"] for r in rows)
    assert all("email" not in r and "name" not in r for r in rows)
    assert response.json()["next_before_id"] is None


def test_config_history_missing_previous_row_is_explicit(client, admin_headers):
    db.execute("DELETE FROM app_config WHERE key = 'embedding_visible_only'")
    response = client.put(
        "/v1/admin/config/embedding_visible_only", json={"value": True}, headers=admin_headers
    )
    assert response.status_code == 200
    row = client.get("/v1/admin/config/history", headers=admin_headers).json()["items"][0]
    assert row["old_value"] is None
    assert row["old_value_present"] is False
    assert row["new_value"] is True


def test_invalid_config_writes_do_not_append(client, admin_headers):
    for key, value in [("embedding_visible_only", "false"), ("unknown_secret", "secret")]:
        response = client.put(
            f"/v1/admin/config/{key}", json={"value": value}, headers=admin_headers
        )
        assert response.status_code == 400
    assert client.get("/v1/admin/config/history", headers=admin_headers).json()["items"] == []


def test_history_pagination_and_key_filter(client, admin_headers):
    for key, value in [
        ("embedding_visible_only", False),
        ("signups_enabled", False),
        ("embedding_visible_only", True),
    ]:
        assert (
            client.put(
                f"/v1/admin/config/{key}", json={"value": value}, headers=admin_headers
            ).status_code
            == 200
        )
    first = client.get(
        "/v1/admin/config/history?key=embedding_visible_only&limit=1", headers=admin_headers
    ).json()
    assert first["items"][0]["new_value"] is True
    second = client.get(
        f"/v1/admin/config/history?key=embedding_visible_only&limit=1&before_id={first['next_before_id']}",
        headers=admin_headers,
    ).json()
    assert second["items"][0]["new_value"] is False
    assert second["next_before_id"] is None


def test_history_and_scopes_are_admin_only(client, user_headers):
    for path in ("history", "filter-scopes"):
        assert client.get(f"/v1/admin/config/{path}", headers=user_headers).status_code == 403


def test_config_and_history_roll_back_together(monkeypatch, f):
    user_id = f.make_user()
    actor = AuthedUser(user_id, "actor", "", "", ["infra-admins"])
    original = db.execute

    def fail_projection(sql, params=None):
        if sql.startswith("INSERT INTO app_config ("):
            raise RuntimeError("projection unavailable")
        return original(sql, params)

    monkeypatch.setattr(db, "execute", fail_projection)
    with pytest.raises(RuntimeError, match="projection unavailable"):
        put_config("embedding_visible_only", ConfigPut(value=False), actor)
    assert db.get_config("embedding_visible_only") is True
    assert db.query("SELECT id FROM app_config_changes") == []


def test_concurrent_saves_keep_a_serial_predecessor_chain(f):
    actor = AuthedUser(f.make_user(), "actor", "", "", ["infra-admins"])
    key = "fetch_retry_after_hours"
    old = db.get_config(key)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda v: put_config(key, ConfigPut(value=v), actor), [17, 19]))
    rows = db.query("SELECT old_value, new_value FROM app_config_changes ORDER BY id")
    assert len(rows) == 2
    assert rows[0]["old_value"] == old
    assert rows[1]["old_value"] == rows[0]["new_value"]
    assert db.get_config(key) == rows[1]["new_value"]


def test_filter_scope_options_are_named_scoped_and_revision_bound(client, admin_headers, f):
    first, second = f.make_user(), f.make_user()
    for user_id in (first, second):
        db.execute(
            "INSERT INTO user_filters (user_id, name, prompt, prompt_hash, enabled) "
            "VALUES (%s, 'Personal choice', 'private prompt', 'shared-hash', false)",
            (user_id,),
        )
    db.execute(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model, published, revision) "
        "VALUES ('tech', 'Tech early career', %s, 'private prompt', 'shared-hash', 'test', false, 3)",
        (first,),
    )
    response = client.get(f"/v1/admin/config/filter-scopes?user={first}", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["scopes"]) == 2
    assert {s["kind"] for s in body["scopes"]} == {"managed_board", "personal_filter"}
    assert {s["user_id"] for s in body["scopes"]} == {first}
    assert {s["revision"] for s in body["scopes"]} == {3, None}
    assert all(s["active"] is False and s["prompt_hash"] == "shared-hash" for s in body["scopes"])
    assert "private prompt" not in response.text
    assert body["title_recipes"] == ["nontechnical_occupations_v1"]
    assert body["profile_recipes"] == ["nontechnical_families_v1"]
    assert body["filters"] == {"user": [str(first)]}
