"""Resume text parts remain complete through replacement and deletion."""

from __future__ import annotations

from api import db
from api.apply.drafting import resume_text
from tests.job_board_support import _uid


def test_text_parts_retry_and_keep_previous_resume(client, user_headers, other_user_headers):
    first = client.post(
        "/v1/user/resumes",
        json={"name": "main", "text": "First complete text"},
        headers=user_headers,
    )
    assert first.status_code == 201, first.text
    resume_id = first.json()["id"]
    db.execute(
        "UPDATE user_resumes SET pdf = %s, filename = %s WHERE id = %s",
        (b"original PDF", "first.pdf", resume_id),
    )

    started = client.post("/v1/user/resume-text-versions", headers=user_headers)
    assert started.status_code == 201, started.text
    version_id = started.json()["id"]
    path = f"/v1/user/resume-text-versions/{version_id}"
    first_part = "A" * 40_000
    second_part = "B" * 40_000 + "🧭"
    assert (
        client.put(f"{path}/parts/0", content=first_part, headers=user_headers).status_code == 200
    )
    assert (
        client.put(f"{path}/parts/0", content=first_part, headers=user_headers).status_code == 200
    )
    assert client.put(f"{path}/parts/0", content="changed", headers=user_headers).status_code == 409
    assert (
        client.post(
            f"{path}/complete",
            json={"name": "main", "part_count": 2, "chars": 80_001},
            headers=user_headers,
        ).status_code
        == 409
    )
    assert (
        client.put(f"{path}/parts/1", content=second_part, headers=user_headers).status_code == 200
    )
    made = client.post(
        f"{path}/complete",
        json={"name": "main", "part_count": 2, "chars": 80_001},
        headers=user_headers,
    )
    assert made.status_code == 200, made.text
    assert made.json()["resume_id"] == resume_id
    listed = client.get("/v1/user/resumes", headers=user_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["resumes"][0]["chars"] == 80_001
    assert listed.json()["resumes"][0]["text_version_id"] == version_id
    assert (
        client.post(
            f"{path}/complete",
            json={"name": "main", "part_count": 2, "chars": 80_001},
            headers=user_headers,
        ).status_code
        == 200
    )
    info = client.get(f"/v1/user/resumes/{resume_id}/text", headers=user_headers).json()
    assert info["chars"] == 80_001 and info["part_count"] == 2
    assert (
        client.get(
            f"/v1/user/resumes/{resume_id}/text/parts/0", headers=other_user_headers
        ).status_code
        == 404
    )
    actual = "".join(
        client.get(f"/v1/user/resumes/{resume_id}/text/parts/{index}", headers=user_headers).json()[
            "text"
        ]
        for index in range(2)
    )
    assert actual == first_part + second_part
    assert resume_text(_uid(user_headers), resume_id) == "A" * 12_000
    prior = db.query_one(
        "SELECT text, pdf, filename FROM user_resume_revisions WHERE resume_id = %s",
        (resume_id,),
    )
    assert prior == {
        "text": "First complete text",
        "pdf": b"original PDF",
        "filename": "first.pdf",
    }

    newer = client.post(f"/v1/user/resumes/{resume_id}/text-versions", headers=user_headers)
    newer_id = newer.json()["id"]
    edit_path = f"/v1/user/resumes/{resume_id}/text-versions/{newer_id}"
    assert (
        client.put(f"{edit_path}/parts/0", content="Edited text", headers=user_headers).status_code
        == 200
    )
    edited = client.post(
        f"{edit_path}/complete",
        json={"part_count": 1, "chars": 11, "base_updated_at": info["updated_at"]},
        headers=user_headers,
    )
    assert edited.status_code == 200, edited.text
    archived = db.query_one(
        "SELECT text_version_id FROM user_resume_revisions "
        "WHERE resume_id = %s ORDER BY id DESC LIMIT 1",
        (resume_id,),
    )
    assert archived is not None and str(archived["text_version_id"]) == version_id
    assert db.query_one(
        "SELECT count(*) AS n FROM resume_text_parts WHERE version_id = %s", (version_id,)
    ) == {"n": 2}
    assert client.delete(f"/v1/user/resumes/{resume_id}", headers=user_headers).status_code == 200
    assert db.query_one(
        "SELECT count(*) AS n FROM user_resume_revisions WHERE resume_id = %s", (resume_id,)
    ) == {"n": 3}
