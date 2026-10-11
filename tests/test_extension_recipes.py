"""Remote recipe tables: published from the API, served encoded, bundled copy
as the extension's fallback."""

from __future__ import annotations

import base64
import json
import uuid
import zlib

import pytest

from api import db
from api.apply import recipes as extension_recipes
from api.apply.recipe_uploads import process

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
    assert (
        body["revision"]
        == body["digest"]
        == extension_recipes.revision_of(extension_recipes.canonical(recipe()))
    )
    assert len(revision) == 64
    assert body["max_age_seconds"] == 300
    assert response.headers["cache-control"] == "no-store"
    assert decode(body) == recipe()
    assert "//input" not in body["payload"]


def test_publishing_keeps_history_and_rolls_back(client, admin_headers, user_headers):
    first = extension_recipes.publish("workday", recipe(), "test")
    second = extension_recipes.publish(
        "workday", recipe(matches=["https://*.myworkdaysite.com/*"]), "test"
    )
    assert second != first
    second_raw = extension_recipes.revision_of(
        extension_recipes.canonical(recipe(matches=["https://*.myworkdaysite.com/*"]))
    )
    assert client.get(PATH, headers=user_headers).json()["revision"] == second_raw
    rows = client.get(ADMIN, headers=admin_headers).json()["recipes"]
    assert [r["enabled"] for r in rows if r["adapter"] == "workday"] == [True, False]
    back = client.post(f"{ADMIN}/workday/rollback", headers=admin_headers)
    assert back.status_code == 200 and back.json()["revision"] == first
    first_raw = extension_recipes.revision_of(extension_recipes.canonical(recipe()))
    assert client.get(PATH, headers=user_headers).json()["revision"] == first_raw
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
        with pytest.raises(ValueError, match=reason):
            extension_recipes.publish("workday", bad, "test")
    assert (
        client.post(
            "/v1/admin/extension/recipe-uploads",
            headers=user_headers,
            json={"id": str(uuid.uuid4()), "adapter": "workday", "chunk_count": 1},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/v1/extension/recipe?schema_version=2&adapter=workday", headers=user_headers
        ).status_code
        == 404
    )


def test_large_recipe_upload_is_chunked_and_published(client, admin_headers, user_headers):
    body = recipe(fields=[{"name": f"field-{index}", "variants": []} for index in range(55000)])
    raw = json.dumps(body, separators=(",", ":")).encode()
    assert len(raw) > 2_000_000
    pieces = [raw[start : start + 65536] for start in range(0, len(raw), 65536)]
    upload_id = uuid.uuid4()
    start = client.post(
        "/v1/admin/extension/recipe-uploads",
        headers=admin_headers,
        json={"id": str(upload_id), "adapter": "workday", "chunk_count": len(pieces)},
    )
    assert start.status_code == 201, start.text
    for index in reversed(range(len(pieces))):
        path = f"/v1/admin/extension/recipe-uploads/{upload_id}/chunks/{index}"
        assert client.put(path, headers=admin_headers, content=pieces[index]).status_code == 200
    changed = b"x" + pieces[0][1:]
    assert client.put(path, headers=admin_headers, content=changed).status_code == 409
    assert (
        client.post(
            f"/v1/admin/extension/recipe-uploads/{upload_id}/complete", headers=admin_headers
        ).status_code
        == 200
    )
    process(upload_id, 0)
    process(upload_id, 0)
    assert (
        db.query_one("SELECT next_chunk FROM extension_recipe_uploads WHERE id = %s", (upload_id,))[
            "next_chunk"
        ]
        == 1
    )
    next_task = db.query_one(
        "SELECT payload FROM tasks WHERE kind = 'publish_extension_recipe' "
        "AND payload->>'upload_id' = %s AND payload->>'chunk_index' = '1' LIMIT 1",
        (str(upload_id),),
    )
    assert next_task is not None
    for index in range(1, len(pieces)):
        process(upload_id, index)
    receipt = client.get(
        f"/v1/admin/extension/recipe-uploads/{upload_id}", headers=admin_headers
    ).json()
    assert receipt["status"] == "published"
    assert client.get(PATH, headers=user_headers).status_code == 404
    manifest = client.get(
        "/v1/extension/recipe?schema_version=2&adapter=workday", headers=user_headers
    ).json()
    from api.apply.recipe_uploads import advance_digest

    digest = bytes(32)
    for index, piece in enumerate(pieces):
        digest = advance_digest(digest, index, piece)
    assert manifest["revision"] == digest.hex()
    assert manifest["digest_algorithm"] == "chunk-chain-sha256"
    recovered = bytearray()
    for index in range(manifest["chunk_count"]):
        part = client.get(
            "/v1/extension/recipe?schema_version=2&adapter=workday"
            f"&revision={manifest['revision']}&chunk_index={index}",
            headers=user_headers,
        )
        assert part.status_code == 200
        recovered.extend(base64.b64decode(part.json()["payload"]))
    assert bytes(recovered) == raw
    assert (
        db.query_one("SELECT body FROM extension_recipes WHERE upload_id = %s", (upload_id,))[
            "body"
        ]
        is None
    )


def test_incomplete_or_invalid_recipe_never_becomes_current(client, admin_headers):
    upload_id = uuid.uuid4()
    base = f"/v1/admin/extension/recipe-uploads/{upload_id}"
    assert (
        client.post(
            "/v1/admin/extension/recipe-uploads",
            headers=admin_headers,
            json={"id": str(upload_id), "adapter": "workday", "chunk_count": 2},
        ).status_code
        == 201
    )
    too_short = client.put(f"{base}/chunks/0", headers=admin_headers, content=b'{"name":')
    assert too_short.status_code == 400
    assert too_short.json()["detail"]["code"] == "INVALID_CHUNK"
    assert client.get(base, headers=admin_headers).json()["received_chunks"] == 0
    assert (
        client.put(f"{base}/chunks/0", headers=admin_headers, content=b" " * 65536).status_code
        == 200
    )
    incomplete = client.post(f"{base}/complete", headers=admin_headers)
    assert incomplete.status_code == 409
    assert incomplete.json()["detail"]["code"] == "INCOMPLETE_RECIPE"
    assert (
        client.put(
            f"{base}/chunks/1", headers=admin_headers, content=b'{"name":"Workday"'
        ).status_code
        == 200
    )
    assert client.post(f"{base}/complete", headers=admin_headers).status_code == 200
    process(upload_id, 0)
    process(upload_id, 1)
    receipt = client.get(base, headers=admin_headers).json()
    assert receipt["status"] == "rejected"
    assert receipt["received_chunks"] == 2
    assert db.query_one("SELECT count(*) AS n FROM extension_recipes")["n"] == 0


def test_version_two_reads_a_chunk_revision_after_rollback(client, admin_headers):
    older = recipe(matches=["https://older.example/*"])
    raw = extension_recipes.canonical(older)
    revision = extension_recipes.publish("workday", older, "test")
    extension_recipes.publish("workday", recipe(), "test")
    rolled_back = client.post(f"{ADMIN}/workday/rollback", headers=admin_headers)
    assert rolled_back.status_code == 200
    assert rolled_back.json()["revision"] == revision
    manifest = client.get("/v1/extension/recipe?schema_version=2&adapter=workday").json()
    assert manifest["revision"] == revision
    assert manifest["digest_algorithm"] == "chunk-chain-sha256"
    chunks = [
        client.get(
            "/v1/extension/recipe?schema_version=2&adapter=workday"
            f"&revision={revision}&chunk_index={index}"
        ).json()["payload"]
        for index in range(manifest["chunk_count"])
    ]
    assert b"".join(base64.b64decode(chunk) for chunk in chunks) == raw
    legacy = client.get(PATH).json()
    assert decode(legacy) == older
    assert legacy["revision"] == extension_recipes.revision_of(raw)
