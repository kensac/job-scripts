"""An uploaded file part must survive a read and detect changed bytes."""

from __future__ import annotations

import io
import uuid

import pytest

from core.payloads.blob_parts import PART_BYTES, BlobPartStore
from core.payloads.objects import PayloadUnavailable


class FakeBody(io.BytesIO):
    pass


class FakeClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: object) -> None:
        self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])

    def get_object(self, **kwargs: object) -> dict[str, FakeBody]:
        return {"Body": FakeBody(self.objects[str(kwargs["Key"])])}


def test_parts_round_trip_and_detect_missing_or_changed_bytes() -> None:
    client = FakeClient()
    store = BlobPartStore(client, "test")
    upload_id = uuid.uuid4()
    first = store.put(upload_id, 0, b"one")
    second = store.put(upload_id, 1, b"two")
    assembled = io.BytesIO()
    store.copy_to(first, assembled)
    store.copy_to(second, assembled)
    assert assembled.getvalue() == b"onetwo"
    client.objects[first.key] = b"changed"
    with pytest.raises(PayloadUnavailable, match="integrity"):
        store.read(first)
    del client.objects[second.key]
    with pytest.raises(PayloadUnavailable, match="unavailable"):
        store.read(second)


def test_replaying_a_part_keeps_the_same_reference() -> None:
    store = BlobPartStore(FakeClient(), "test")
    upload_id = uuid.uuid4()
    assert store.put(upload_id, 0, b"same") == store.put(upload_id, 0, b"same")


def test_more_than_the_old_pdf_limit_can_be_stored_in_bounded_parts() -> None:
    store = BlobPartStore(FakeClient(), "test")
    upload_id = uuid.uuid4()
    parts = [store.put(upload_id, index, b"p" * PART_BYTES) for index in range(6)]
    assert sum(part.size for part in parts) > 5 * 1024 * 1024
    assert all(len(store.read(part)) == PART_BYTES for part in parts)
    with pytest.raises(ValueError):
        store.put(upload_id, 6, b"p" * (PART_BYTES + 1))
