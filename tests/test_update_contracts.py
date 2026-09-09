from __future__ import annotations

import pytest
from pydantic import ValidationError

from api import db
from api.models import FilterPatch


@pytest.mark.parametrize("field", ["name", "prompt", "on_ambiguous", "fail_closed", "enabled"])
def test_filter_patch_rejects_null_but_preserves_omission(field):
    with pytest.raises(ValidationError):
        FilterPatch.model_validate({field: None})
    assert FilterPatch().model_dump(exclude_unset=True) == {}
    assert FilterPatch(enabled=False).model_dump(exclude_unset=True) == {"enabled": False}


@pytest.mark.parametrize(
    "field,value", [("ai_model", "gpt-5-nano"), ("writing_style", "Be direct.")]
)
def test_nullable_setting_reset_distinguishes_omission(client, user_headers, field, value):
    uid = db.query_one("SELECT id FROM users WHERE sub=%s", (user_headers["X-User-Sub"],))["id"]
    db.execute(
        f"INSERT INTO user_settings(user_id,{field}) VALUES (%s,%s) "
        f"ON CONFLICT (user_id) DO UPDATE SET {field}=EXCLUDED.{field}",
        (uid, value),
    )
    unchanged = client.put("/v1/user/settings", json={"email_digest": False}, headers=user_headers)
    assert unchanged.status_code == 200
    assert unchanged.json()[field] == value
    reset = client.put("/v1/user/settings", json={field: None}, headers=user_headers)
    assert reset.status_code == 200
    assert reset.json()[field] is None
    assert (
        db.query_one(f"SELECT {field} FROM user_settings WHERE user_id=%s", (uid,))[field] is None
    )


def test_unknown_profile_field_cannot_replace_saved_facts(client, user_headers):
    original = client.put(
        "/v1/user/profile",
        json={"first_name": "Ada", "notes": "Use my middle name."},
        headers=user_headers,
    )
    assert original.status_code == 200
    rejected = client.put("/v1/user/profile", json={"firstName": "Grace"}, headers=user_headers)
    assert rejected.status_code == 422
    assert client.get("/v1/user/profile", headers=user_headers).json() == original.json()
    replaced = client.put("/v1/user/profile", json={"first_name": "Grace"}, headers=user_headers)
    assert replaced.json()["notes"] == ""
    assert replaced.json()["first_name"] == "Grace"
