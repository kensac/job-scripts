"""Model suggestions for unresolved form fields and admin exclusions."""

from __future__ import annotations

from api import db
from tests.apply_support import ada_profile


def test_the_model_fills_what_the_rules_could_not_in_one_call(client, user_headers, monkeypatch):
    """A known fact whose option wording the matcher does not recognise goes
    to the model as a hint with the options; an unknown field goes with
    them in the same call. The answer must be one of the options."""
    from api import ai

    seen = {}

    async def fake_parse(cfg, instructions, input_text, model, timeout=120.0):
        seen["input"] = input_text
        return model(
            answers=[
                {"key": "eth", "answer": "Asian/Pacific Islander"},
                {"key": "office", "answer": "Yes"},
                {"key": "made-up", "answer": "Purple"},
            ]
        ), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(ai, "parse", fake_parse)
    monkeypatch.setattr(
        "api.budget.resolve_ai_config",
        lambda uid, ent: type("Cfg", (), {"model": "m", "key_source": "owner"})(),
    )
    client.put(
        "/v1/user/profile",
        json={**ada_profile(), "ethnicity": "South Asian | Asian"},
        headers=user_headers,
    )
    eth_options = ["White", "Asian (Not Hispanic or Latino)", "Decline"]
    resolved = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://x.test/apply",
            "fields": [{"key": "eth", "label": "Race", "kind": "select", "options": eth_options}],
        },
        headers=user_headers,
    ).json()["fields"][0]
    # "Asian" is a whole word of that option, so the rule lands it directly.
    assert resolved["rung"] == "profile" and resolved["value"] == "Asian (Not Hispanic or Latino)"

    res = client.post(
        "/v1/user/apply/suggest",
        json={
            "fields": [
                {
                    "key": "eth",
                    "label": "Race",
                    "kind": "select",
                    "options": ["Caucasian", "Asian/Pacific Islander"],
                    "hint": "South Asian | Asian",
                },
                {
                    "key": "office",
                    "label": "Excited about the office?",
                    "kind": "yesno",
                    "options": ["Yes", "No"],
                },
                {
                    "key": "made-up",
                    "label": "Favourite colour",
                    "kind": "select",
                    "options": ["Red", "Blue"],
                },
            ]
        },
        headers=user_headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["answers"] == {"eth": "Asian/Pacific Islander", "office": "Yes"} or (
        res.json()["answers"] == {"office": "Yes"}
    )
    assert "South Asian | Asian" in seen["input"] and "Ada" in seen["input"]


def test_the_admin_list_keeps_the_model_off_a_field(client, user_headers, monkeypatch):
    """The model never fills what the admin list names (location by default,
    matched as a whole word in label or key); the call goes out without
    those fields and the reply names them, so the panel lists them as the
    person's. A list that covers every field costs no call at all."""
    from api import ai

    seen = {}

    async def fake_parse(cfg, rules, text, schema):
        seen["input"] = text
        return schema(answers=[{"key": "why", "answer": "Because the work is measured."}]), {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }

    monkeypatch.setattr(ai, "parse", fake_parse)
    monkeypatch.setattr(
        "api.budget.resolve_ai_config",
        lambda uid, ent: type("Cfg", (), {"model": "m", "key_source": "owner"})(),
    )
    client.put("/v1/user/profile", json=ada_profile(), headers=user_headers)
    fields = [
        {"key": "location", "label": "Location (City)", "kind": "select"},
        {
            "key": "q1",
            "label": "Do you reside in one of these locations?",
            "kind": "select",
            "options": ["Yes", "No"],
        },
        {"key": "why", "label": "Why this role?", "kind": "long"},
    ]
    fill_id = client.post(
        "/v1/user/apply/resolve",
        json={"url": "https://x.test/apply", "fields": fields},
        headers=user_headers,
    ).json()["fill_id"]
    res = client.post(
        "/v1/user/apply/suggest",
        json={"fields": fields, "fill_id": fill_id},
        headers=user_headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["skipped"] == ["location"]
    assert res.json()["answers"] == {"why": "Because the work is measured."}
    assert "Location (City)" not in seen["input"] and "reside" in seen["input"]
    # The ledger row says what the model said, submitted or not.
    row = db.query_one("SELECT fields FROM application_fills WHERE id = %s", (fill_id,))
    by_key = {f["key"]: f for f in row["fields"]}
    assert by_key["why"]["rung"] == "ai"
    assert by_key["why"]["ai_answer"] == "Because the work is measured."
    assert by_key["location"]["never_ai"] is True and "ai_answer" not in by_key["q1"]

    seen.clear()
    res = client.post("/v1/user/apply/suggest", json={"fields": fields[:1]}, headers=user_headers)
    assert res.json() == {"answers": {}, "skipped": ["location"], "model": None}
    assert not seen
