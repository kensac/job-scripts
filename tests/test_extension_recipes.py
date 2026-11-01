"""Remote recipe tables: published from the API, served encoded, bundled copy
as the extension's fallback."""

from __future__ import annotations

import base64
import json
import zlib

import pytest

from api import db
from api.apply import recipes as extension_recipes

PATH = "/v1/extension/recipe?schema_version=1&adapter=workday"


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


def test_publishing_an_earlier_table_again_makes_it_current(client, user_headers):
    first = extension_recipes.publish("workday", recipe(), "test")
    second = extension_recipes.publish(
        "workday", recipe(matches=["https://*.myworkdaysite.com/*"]), "test"
    )
    assert second != first
    second_raw = extension_recipes.revision_of(
        extension_recipes.canonical(recipe(matches=["https://*.myworkdaysite.com/*"]))
    )
    assert client.get(PATH, headers=user_headers).json()["revision"] == second_raw
    # The same table again is the same revision, not a new row.
    assert extension_recipes.publish("workday", recipe(), "test") == first
    first_raw = extension_recipes.revision_of(extension_recipes.canonical(recipe()))
    assert client.get(PATH, headers=user_headers).json()["revision"] == first_raw
    assert db.query_one("SELECT count(*) AS n FROM extension_recipes")["n"] == 2


def test_a_table_that_is_not_the_engines_shape_is_refused(client, user_headers):
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
        client.get(
            "/v1/extension/recipe?schema_version=2&adapter=workday", headers=user_headers
        ).status_code
        == 404
    )


def test_a_large_recipe_is_chunked_and_published(client, user_headers):
    body = recipe(fields=[{"name": f"field-{index}", "variants": []} for index in range(55000)])
    raw = extension_recipes.canonical(body)
    assert len(raw) > 2_000_000
    pieces = [raw[start : start + 65536] for start in range(0, len(raw), 65536)]
    revision = extension_recipes.publish("workday", body, "test")
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
    assert revision == manifest["revision"]


def test_version_two_reads_a_republished_earlier_revision(client):
    older = recipe(matches=["https://older.example/*"])
    raw = extension_recipes.canonical(older)
    revision = extension_recipes.publish("workday", older, "test")
    extension_recipes.publish("workday", recipe(), "test")
    assert extension_recipes.publish("workday", older, "test") == revision
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
