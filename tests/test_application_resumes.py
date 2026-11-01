from __future__ import annotations

from api import db
from api.apply.drafting import resume_text
from core.answers import DEFAULT_STYLE
from tests.application_support import _available_owner_key as _available_owner_key
from tests.factories import user_id_for_subject
from tests.resume_support import create_text_resume, seed_text_resume


class TestResumes:
    def test_pasted_text_is_kept_and_same_name_replaces_it(self, client, user_headers):
        r = create_text_resume(
            client,
            json={"name": "master", "text": "Alice. Python."},
            headers=user_headers,
        )
        assert r.status_code == 200, r.text
        r = create_text_resume(
            client,
            json={"name": "master", "text": "Alice. Rust."},
            headers=user_headers,
        )
        assert r.status_code == 200
        rows = client.get("/v1/user/resumes", headers=user_headers).json()["resumes"]
        assert [(x["name"], x["chars"]) for x in rows] == [("master", 12)]
        resume_id = r.json()["resume_id"]
        assert (
            client.get(f"/v1/user/resumes/{resume_id}/text/parts/0", headers=user_headers).json()[
                "text"
            ]
            == "Alice. Rust."
        )
        assert db.query_one(
            "SELECT count(*) AS n FROM user_resume_revisions WHERE resume_id = %s",
            (resume_id,),
        ) == {"n": 1}

    def test_old_json_and_base64_writes_are_retired(self, client, user_headers):
        r = client.post(
            "/v1/user/resumes",
            json={"name": "pdf", "pdf_base64": "bm90IGEgcGRm"},
            headers=user_headers,
        )
        assert r.status_code == 410 and r.json()["detail"]["code"] == "USE_RESUME_PARTS"
        r = client.post(
            "/v1/user/resumes",
            json={"name": "master", "text": "old text"},
            headers=user_headers,
        )
        assert r.status_code == 410 and r.json()["detail"]["code"] == "USE_RESUME_PARTS"
        assert client.get("/v1/user/resumes", headers=user_headers).json()["resumes"] == []

    def test_rename_keeps_canonical_text_and_revision(self, client, user_headers):
        resume_id = seed_text_resume(user_id_for_subject("test-user"), "old", "Original text")
        text_change = client.patch(
            f"/v1/user/resumes/{resume_id}",
            json={"text": "replacement"},
            headers=user_headers,
        )
        assert text_change.status_code == 410
        assert text_change.json()["detail"]["code"] == "USE_RESUME_PARTS"
        renamed = client.patch(
            f"/v1/user/resumes/{resume_id}", json={"name": "renamed"}, headers=user_headers
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "renamed"
        assert renamed.json()["text"] == ""
        assert renamed.json()["has_pdf"] is False
        assert (
            client.get(f"/v1/user/resumes/{resume_id}/text/parts/0", headers=user_headers).json()[
                "text"
            ]
            == "Original text"
        )
        assert (
            client.get(f"/v1/user/resumes/{resume_id}/pdf", headers=user_headers).status_code == 404
        )
        assert db.query_one(
            "SELECT pdf_upload_id, text_version_id FROM user_resume_revisions WHERE resume_id = %s",
            (resume_id,),
        ) == {
            "pdf_upload_id": None,
            "text_version_id": db.query_one(
                "SELECT text_version_id FROM user_resumes WHERE id = %s", (resume_id,)
            )["text_version_id"],
        }
        create_text_resume(
            client, json={"name": "taken", "text": "Other text"}, headers=user_headers
        )
        conflict = client.patch(
            f"/v1/user/resumes/{resume_id}", json={"name": "taken"}, headers=user_headers
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "NAME_EXISTS"
        assert db.query_one("SELECT name FROM user_resumes WHERE id = %s", (resume_id,)) == {
            "name": "renamed"
        }

    def test_another_person_cannot_see_or_delete_it(self, client, user_headers, other_user_headers):
        rid = create_text_resume(
            client, json={"name": "master", "text": "Alice."}, headers=user_headers
        ).json()["resume_id"]
        assert client.get("/v1/user/resumes", headers=other_user_headers).json()["resumes"] == []
        assert (
            client.delete(f"/v1/user/resumes/{rid}", headers=other_user_headers).status_code == 404
        )
        assert client.delete(f"/v1/user/resumes/{rid}", headers=user_headers).status_code == 200

    def test_missing_canonical_copy_is_unavailable(self, client, user_headers):
        row = db.query_one(
            "INSERT INTO user_resumes (user_id, name, filename) "
            "VALUES (%s, 'old', 'old.pdf') RETURNING id",
            (user_id_for_subject("test-user"),),
        )
        assert row is not None
        resume_id = row["id"]
        assert resume_text(user_id_for_subject("test-user"), resume_id) is None
        listed = client.get("/v1/user/resumes", headers=user_headers)
        assert listed.status_code == 503
        info = client.get(f"/v1/user/resumes/{resume_id}/text", headers=user_headers)
        assert info.status_code == 503
        part = client.get(f"/v1/user/resumes/{resume_id}/text/parts/0", headers=user_headers)
        assert part.status_code == 503
        pdf = client.get(f"/v1/user/resumes/{resume_id}/pdf", headers=user_headers)
        assert pdf.status_code == 404

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
