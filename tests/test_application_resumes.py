from __future__ import annotations

import base64

from core.answers import DEFAULT_STYLE
from tests.application_support import _available_owner_key as _available_owner_key


class TestResumes:
    def test_pasted_text_is_kept_and_same_name_replaces_it(self, client, user_headers):
        r = client.post(
            "/v1/user/resumes",
            json={"name": "master", "text": "Alice. Python."},
            headers=user_headers,
        )
        assert r.status_code == 201, r.text
        r = client.post(
            "/v1/user/resumes",
            json={"name": "master", "text": "Alice. Rust."},
            headers=user_headers,
        )
        assert r.status_code == 201
        rows = client.get("/v1/user/resumes", headers=user_headers).json()["resumes"]
        assert [(x["name"], x["text"]) for x in rows] == [("master", "Alice. Rust.")]

    def test_a_pdf_is_read_at_upload_and_a_bad_one_is_refused(self, client, user_headers):
        r = client.post(
            "/v1/user/resumes",
            json={"name": "pdf", "pdf_base64": base64.b64encode(b"not a pdf").decode()},
            headers=user_headers,
        )
        assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_PDF"
        r = client.post("/v1/user/resumes", json={"name": "empty"}, headers=user_headers)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "NO_TEXT"

    def test_another_person_cannot_see_or_delete_it(self, client, user_headers, other_user_headers):
        rid = client.post(
            "/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers
        ).json()["id"]
        assert client.get("/v1/user/resumes", headers=other_user_headers).json()["resumes"] == []
        assert (
            client.delete(f"/v1/user/resumes/{rid}", headers=other_user_headers).status_code == 404
        )
        assert client.delete(f"/v1/user/resumes/{rid}", headers=user_headers).status_code == 200

    def test_the_writing_style_is_a_setting_and_empty_clears_it(self, client, user_headers):
        r = client.put(
            "/v1/user/settings", json={"writing_style": "Short. Casual."}, headers=user_headers
        )
        assert r.status_code == 200 and r.json()["writing_style"] == "Short. Casual."
        assert (
            client.get("/v1/user/settings", headers=user_headers).json()["writing_style"]
            == "Short. Casual."
        )
        r = client.put("/v1/user/settings", json={"prefs": {}}, headers=user_headers)
        assert r.json()["writing_style"] == "Short. Casual."
        r = client.put("/v1/user/settings", json={"writing_style": ""}, headers=user_headers)
        assert r.json()["writing_style"] is None
        # The built-in default travels with the field it stands in for.
        assert r.json()["default_style"] == DEFAULT_STYLE
