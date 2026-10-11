"""Create complete text resumes through the public part upload contract."""

from __future__ import annotations

from api import db
from api.resumes.text_versions import store_text


def seed_text_resume(user_id: int, name: str, text: str) -> int:
    with db.transaction():
        version_id, _ = store_text(user_id, [text])
        row = db.query_one(
            "INSERT INTO user_resumes (user_id, name, text_version_id) "
            "VALUES (%s, %s, %s) RETURNING id",
            (user_id, name, version_id),
        )
    assert row is not None
    return row["id"]


def create_text_resume(client, *, json: dict[str, str], headers: dict[str, str]):
    started = client.post("/v1/user/resume-text-versions", headers=headers)
    assert started.status_code == 201, started.text
    path = f"/v1/user/resume-text-versions/{started.json()['id']}"
    value = json["text"]
    parts = [value[index : index + 16_384] for index in range(0, len(value), 16_384)]
    for index, part in enumerate(parts):
        sent = client.put(f"{path}/parts/{index}", content=part, headers=headers)
        assert sent.status_code == 200, sent.text
    return client.post(
        f"{path}/complete",
        json={"name": json["name"], "part_count": len(parts), "chars": len(value)},
        headers=headers,
    )
