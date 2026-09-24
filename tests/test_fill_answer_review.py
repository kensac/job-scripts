from api import db
from tests.test_api_jobs import _uid


def _fill(client, headers):
    response = client.post(
        "/v1/user/apply/resolve",
        headers=headers,
        json={
            "url": "https://example.test/apply",
            "fields": [{"key": "why", "label": "Why this role?", "kind": "long"}],
        },
    )
    assert response.status_code == 200
    return response.json()["fill_id"]


def test_review_preserves_original_and_rejects_stale_edits(client, user_headers):
    fill_id = _fill(client, user_headers)
    path = f"/v1/user/apply/fills/{fill_id}"
    edit = {"key": "why", "revision": 0, "value": "My answer", "feedback": "Use my project example"}
    saved = client.put(path + "/answer", headers=user_headers, json=edit)
    assert saved.status_code == 200, saved.text
    field = saved.json()["fields"][0]
    assert field["review_value"] == "My answer"
    assert field["value"] is None
    assert field["answer_revision"] == 1
    assert field["answer_history"][0]["feedback"] == edit["feedback"]
    assert client.put(path + "/answer", headers=user_headers, json=edit).status_code == 409
    assert client.get(path, headers=user_headers).json()["fields"][0] == field


def test_fill_review_is_owned_and_submitted_fills_are_immutable(
    client, user_headers, other_user_headers
):
    fill_id = _fill(client, user_headers)
    path = f"/v1/user/apply/fills/{fill_id}"
    edit = {"key": "why", "revision": 0, "value": "My answer"}
    assert client.get(path, headers=other_user_headers).status_code == 404
    assert client.put(path + "/answer", headers=other_user_headers, json=edit).status_code == 404
    assert (
        client.post(path + "/submitted", headers=user_headers, json={"fields": []}).status_code
        == 200
    )
    assert client.put(path + "/answer", headers=user_headers, json=edit).status_code == 409
    assert client.get(path + "999", headers=user_headers).status_code == 404


def test_newer_edit_and_submission_fence_late_results(client, user_headers):
    from api.apply import fill_answers

    fill_id = _fill(client, user_headers)
    user_id = _uid()
    token, _ = fill_answers.reserve(user_id, fill_id, ["why"], None)
    fill_answers.edit(user_id, fill_id, "why", 0, "Keep this", "Shorter")
    assert not fill_answers.complete(
        user_id, fill_id, token, ["why"], {"why": "Old"}, {"why": "Old"}, [], review_only=True
    )
    token, _ = fill_answers.reserve(user_id, fill_id, ["why"], None)
    client.post(
        f"/v1/user/apply/fills/{fill_id}/submitted", headers=user_headers, json={"fields": []}
    )
    assert not fill_answers.complete(
        user_id, fill_id, token, ["why"], {"why": "Old"}, {"why": "Old"}, [], review_only=True
    )
    row = db.query_one("SELECT fields FROM application_fills WHERE id = %s", (fill_id,))
    assert row["fields"][0]["review_value"] == "Keep this"
