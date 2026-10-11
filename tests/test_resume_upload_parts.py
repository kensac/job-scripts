"""A PDF transfer can stop, retry, and finish without a whole-file request."""

from __future__ import annotations

import io
import uuid

import pytest
from pypdf import PdfWriter

from api import db
from core.payloads.blob_parts import PART_BYTES, BlobPartStore
from tasks import resume_uploads


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
    objects = FakeClient()
    store = BlobPartStore(objects, "test")
    monkeypatch.setattr(BlobPartStore, "from_env", classmethod(lambda cls: store))
    monkeypatch.setattr(resume_uploads, "_extract", lambda *_: "Complete resume text")
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
    await resume_uploads.handle_process_resume_upload({"upload_id": str(upload_id)})
    state = client.get(path, headers=user_headers).json()
    assert state["status"] == "ready" and state["received_size"] == len(original)
    resume = db.query_one(
        "SELECT id, text FROM user_resumes WHERE pdf_upload_id = %s", (upload_id,)
    )
    assert resume is not None and resume["text"] == "Complete resume text"
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
    monkeypatch.setattr(resume_uploads, "_extract", lambda *_: "Recovered text")
    await resume_uploads.handle_process_resume_upload({"upload_id": str(upload_id)})
    assert client.get(path, headers=user_headers).json()["status"] == "ready"
