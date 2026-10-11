"""Verified object storage for parts of an uploaded file."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any, BinaryIO

from core.payloads.objects import PayloadStore, PayloadUnavailable

# Bounds memory for one request or read. An upload may have any number of parts.
PART_BYTES = 1024 * 1024


@dataclass(frozen=True)
class BlobPart:
    upload_id: uuid.UUID
    index: int
    sha256: str
    size: int

    @property
    def key(self) -> str:
        return f"uploads/v1/{self.upload_id}/{self.index}/{self.sha256}"


class BlobPartStore:
    """Stores each received part independently and verifies every read."""

    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_env(cls) -> BlobPartStore:
        store = PayloadStore.from_env()
        return cls(store.client, store.bucket)

    def put(self, upload_id: uuid.UUID, index: int, data: bytes) -> BlobPart:
        if index < 0 or not data or len(data) > PART_BYTES:
            raise ValueError("a part needs a nonnegative index and 1 to 1048576 bytes")
        part = BlobPart(upload_id, index, hashlib.sha256(data).hexdigest(), len(data))
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=part.key,
                Body=data,
                ContentType="application/octet-stream",
                Metadata={"sha256": part.sha256},
            )
        except Exception as exc:
            raise PayloadUnavailable("File part upload failed") from exc
        if self.read(part) != data:
            raise PayloadUnavailable("File part round-trip mismatch")
        return part

    def read(self, part: BlobPart) -> bytes:
        if (
            not isinstance(part.upload_id, uuid.UUID)
            or part.index < 0
            or part.size < 1
            or part.size > PART_BYTES
            or re.fullmatch(r"[0-9a-f]{64}", part.sha256) is None
        ):
            raise PayloadUnavailable("Invalid file part reference")
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=part.key)
            body = response["Body"]
            try:
                raw = body.read(part.size + 1)
            finally:
                body.close()
        except Exception as exc:
            raise PayloadUnavailable("File part unavailable") from exc
        if len(raw) != part.size or hashlib.sha256(raw).hexdigest() != part.sha256:
            raise PayloadUnavailable("File part integrity check failed")
        return raw

    def copy_to(self, part: BlobPart, target: BinaryIO) -> None:
        target.write(self.read(part))
