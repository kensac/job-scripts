"""Remote recipe tables: published from the API, served encoded, bundled copy
as the extension's fallback."""

from __future__ import annotations

import base64
import json
import zlib

from api import db, extension_recipes

PATH = "/v1/extension/recipe?schema_version=1&adapter=workday"
ADMIN = "/v1/admin/extension/recipes"


def recipe(**overrides):
    body = {
        "name": "Workday",
        "matches": ["https://*.myworkdayjobs.com/*"],
        "fields": [{"name": "email", "fact": "email", "variants": [{"paths": ["//input"]}]}],
        "continue": [],
        "submit": [],
    }
    body.update(overrides)
    return body


def decode(config: dict) -> dict:
    assert config["encoding"] == "deflate+base64"
    raw = zlib.decompress(base64.b64decode(config["payload"]))
    return json.loads(raw)


def test_a_published_table_is_served_encoded_and_decodes_to_itself(client, user_headers):
    assert client.get(PATH, headers=user_headers).status_code == 404
    revision = extension_recipes.publish("workday", recipe(), "test")
    response = client.get(PATH, headers=user_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1 and body["adapter"] == "workday"
    assert body["revision"] == revision == body["digest"] and len(revision) == 64
    assert body["max_age_seconds"] == 300
    assert response.headers["cache-control"] == "no-store"
    assert decode(body) == recipe()
    assert "//input" not in body["payload"]


def test_publishing_keeps_history_and_rolls_back(client, admin_headers, user_headers):
    first = extension_recipes.publish("workday", recipe(), "test")
    second = client.put(
        f"{ADMIN}/workday",
        headers=admin_headers,
        json={"recipe": recipe(matches=["https://*.myworkdaysite.com/*"])},
    )
    assert second.status_code == 200, second.text
    assert second.json()["revision"] != first
    assert client.get(PATH, headers=user_headers).json()["revision"] == second.json()["revision"]
    rows = client.get(ADMIN, headers=admin_headers).json()["recipes"]
    assert [r["enabled"] for r in rows if r["adapter"] == "workday"] == [True, False]
    back = client.post(f"{ADMIN}/workday/rollback", headers=admin_headers)
    assert back.status_code == 200 and back.json()["revision"] == first
    assert client.get(PATH, headers=user_headers).json()["revision"] == first
    assert client.post(f"{ADMIN}/workday/rollback", headers=admin_headers).status_code == 404
    # The same table again is the same revision, not a new row.
    assert extension_recipes.publish("workday", recipe(), "test") == first
    assert db.query_one("SELECT count(*) AS n FROM extension_recipes")["n"] == 2


def test_a_table_that_is_not_the_engines_shape_is_refused(client, admin_headers, user_headers):
    for bad, reason in (
        (recipe(name="Lever"), "adapter id"),
        (recipe(matches=[]), "matches"),
        (recipe(matches=["http://x/*"]), "matches"),
        (recipe(fields="no"), "fields"),
        (recipe(fields=[{"variants": []}]), "name"),
        ([], "object"),
    ):
        response = client.put(f"{ADMIN}/workday", headers=admin_headers, json={"recipe": bad})
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "INVALID_RECIPE"
        assert reason in response.json()["detail"]["message"]
    assert (
        client.put(f"{ADMIN}/workday", headers=user_headers, json={"recipe": recipe()}).status_code
        == 403
    )
    assert (
        client.get(
            "/v1/extension/recipe?schema_version=2&adapter=workday", headers=user_headers
        ).status_code
        == 409
    )
