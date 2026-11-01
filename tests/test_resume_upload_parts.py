"""A PDF transfer can stop, retry, and finish without a whole-file request."""

from __future__ import annotations

import io
import uuid

import pytest
from pypdf import PdfWriter

from api import db
from api.apply.drafting import resume_text
from api.resumes.text_versions import store_text
from core.payloads.blob_parts import PART_BYTES, BlobPartStore
from tasks import resume_uploads
from tests.factories import user_id_for_subject
from tests.resume_support import seed_text_resume


@pytest.fixture(autouse=True)
def enable_verified_pdf_storage():
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('resume_pdf_uploads_enabled', 'true') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )


class FakeClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: object) -> None:
        self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])

    def get_object(self, **kwargs: object) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[str(kwargs["Key"])])}


@pytest.mark.asyncio
async def test_large_transfer_retries_and_streams_download(
    client, user_headers, other_user_headers, monkeypatch
) -> None:
    db.execute("UPDATE app_config SET value = 'false' WHERE key = 'resume_pdf_uploads_enabled'")
    paused = client.post(
        "/v1/user/resume-uploads",
        json={"name": "master", "filename": "master.pdf", "size": 1},
        headers=user_headers,
    )
    assert paused.status_code == 503
    db.execute("UPDATE app_config SET value = 'true' WHERE key = 'resume_pdf_uploads_enabled'")
    objects = FakeClient()
    store = BlobPartStore(objects, "test")
    monkeypatch.setattr(BlobPartStore, "from_env", classmethod(lambda cls: store))
    monkeypatch.setattr(
        resume_uploads,
        "_extract",
        lambda _, user_id, *__: store_text(user_id, ["Complete resume text"])[0],
    )
    previous_id = seed_text_resume(
        user_id_for_subject("test-user"), "master", "Previous resume text"
    )
    previous_version = db.query_one(
        "SELECT text_version_id FROM user_resumes WHERE id = %s", (previous_id,)
    )["text_version_id"]
    original = b"p" * (6 * PART_BYTES + 7)
    started = client.post(
        "/v1/user/resume-uploads",
        json={"name": "master", "filename": "master.pdf", "size": len(original)},
        headers=user_headers,
    )
    assert started.status_code == 201, started.text
    upload_id = uuid.UUID(started.json()["id"])
    path = f"/v1/user/resume-uploads/{upload_id}"
    assert client.get(path, headers=other_user_headers).status_code == 404
    assert client.post(f"{path}/complete", headers=user_headers).status_code == 409
    too_large = client.put(f"{path}/parts/0", content=b"p" * (PART_BYTES + 1), headers=user_headers)
    assert too_large.status_code == 413
    assert client.get(path, headers=user_headers).json()["next_part"] == 0
    for index, offset in enumerate(range(0, len(original), PART_BYTES)):
        part = original[offset : offset + PART_BYTES]
        response = client.put(f"{path}/parts/{index}", content=part, headers=user_headers)
        assert response.status_code == 200, response.text
        assert response.json()["next_part"] == index + 1
        if index == 1:
            assert client.get(path, headers=user_headers).json()["next_part"] == 2
            replay = client.put(f"{path}/parts/{index}", content=part, headers=user_headers)
            assert replay.status_code == 200
            changed = client.put(f"{path}/parts/{index}", content=b"changed", headers=user_headers)
            assert changed.status_code == 409
    assert client.post(f"{path}/complete", headers=user_headers).json()["status"] == "processing"
    assert client.post(f"{path}/complete", headers=user_headers).json()["status"] == "processing"
    db.execute("UPDATE app_config SET value = 'false' WHERE key = 'resume_pdf_uploads_enabled'")
    with pytest.raises(RuntimeError, match="PDF uploads are paused"):
        await resume_uploads.handle_process_resume_upload({"upload_id": str(upload_id)})
    assert db.query_one(
        "SELECT count(*) AS n FROM user_resumes WHERE pdf_upload_id = %s", (upload_id,)
    ) == {"n": 0}
    db.execute("UPDATE app_config SET value = 'true' WHERE key = 'resume_pdf_uploads_enabled'")
    assert client.post(f"{path}/complete", headers=user_headers).json()["status"] == "processing"
    await resume_uploads.handle_process_resume_upload({"upload_id": str(upload_id)})
    state = client.get(path, headers=user_headers).json()
    assert state["status"] == "ready" and state["received_size"] == len(original)
    resume = db.query_one(
        "SELECT id, user_id, text_version_id FROM user_resumes WHERE pdf_upload_id = %s",
        (upload_id,),
    )
    assert resume is not None and resume["text_version_id"] is not None
    info = client.get(f"/v1/user/resumes/{resume['id']}/text", headers=user_headers).json()
    assert info["chars"] == len("Complete resume text") and info["part_count"] == 1
    stored = client.get(f"/v1/user/resumes/{resume['id']}/text/parts/0", headers=user_headers)
    assert stored.json()["text"] == "Complete resume text"
    assert resume_text(resume["user_id"], resume["id"]) == "Complete resume text"
    prior = db.query_one(
        "SELECT filename, pdf_upload_id, text_version_id "
        "FROM user_resume_revisions WHERE resume_id = %s",
        (resume["id"],),
    )
    assert prior == {
        "filename": None,
        "pdf_upload_id": None,
        "text_version_id": previous_version,
    }
    downloaded = client.get(f"/v1/user/resumes/{resume['id']}/pdf", headers=user_headers)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == original
    assert downloaded.headers["content-length"] == str(len(original))


@pytest.mark.asyncio
async def test_failed_extraction_keeps_file_and_can_be_retried(
    client, user_headers, monkeypatch
) -> None:
    objects = FakeClient()
    store = BlobPartStore(objects, "test")
    monkeypatch.setattr(BlobPartStore, "from_env", classmethod(lambda cls: store))
    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    raw = pdf.getvalue()
    started = client.post(
        "/v1/user/resume-uploads",
        json={"name": "blank", "filename": "blank.pdf", "size": len(raw)},
        headers=user_headers,
    )
    upload_id = uuid.UUID(started.json()["id"])
    path = f"/v1/user/resume-uploads/{upload_id}"
    assert client.put(f"{path}/parts/0", content=raw, headers=user_headers).status_code == 200
    assert client.post(f"{path}/complete", headers=user_headers).status_code == 200
    with pytest.raises(ValueError, match="no extractable text"):
        await resume_uploads.handle_process_resume_upload({"upload_id": str(upload_id)})
    assert client.get(path, headers=user_headers).json()["status"] == "failed"
    assert objects
    assert client.post(f"{path}/complete", headers=user_headers).json()["status"] == "processing"
    monkeypatch.setattr(
        resume_uploads,
        "_extract",
        lambda _, user_id, *__: store_text(user_id, ["Recovered text"])[0],
    )
    await resume_uploads.handle_process_resume_upload({"upload_id": str(upload_id)})
    assert client.get(path, headers=user_headers).json()["status"] == "ready"
