"""Immutable, verified payloads outside the operational database."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config


class PayloadUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PayloadRef:
    bucket: str
    key: str
    sha256: str
    size: int
    version: int = 1


class PayloadStore:
    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_env(cls) -> PayloadStore:
        # Explicit credentials prevent an accidental fallback to another
        # account through the SDK credential discovery chain.
        prefix = "JOBTRACKER_S3_"
        client = boto3.client(
            "s3",
            endpoint_url=os.environ[prefix + "ENDPOINT"],
            region_name=os.environ[prefix + "REGION"],
            aws_access_key_id=os.environ[prefix + "ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ[prefix + "SECRET_ACCESS_KEY"],
            config=Config(
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        )
        return cls(client, os.environ[prefix + "BUCKET"])

    def put_verified(self, value: Any) -> PayloadRef:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        ref = PayloadRef(self.bucket, f"payloads/v1/sha256/{digest}.json.gz", digest, len(raw))
        self.client.put_object(
            Bucket=ref.bucket,
            Key=ref.key,
            Body=gzip.compress(raw, mtime=0),
            ContentType="application/gzip",
        )
        # A successful PUT or ETag alone does not prove the bytes can be read.
        if self.get(ref) != value:
            raise PayloadUnavailable("Payload round-trip mismatch")
        return ref

    def get(self, ref: PayloadRef) -> Any:
        if (
            ref.version != 1
            or ref.bucket != self.bucket
            or ref.key != f"payloads/v1/sha256/{ref.sha256}.json.gz"
            or ref.size < 0
        ):
            raise PayloadUnavailable("Invalid payload reference")
        try:
            response = self.client.get_object(Bucket=ref.bucket, Key=ref.key)
            body = response["Body"]
            try:
                with gzip.GzipFile(fileobj=body) as stream:
                    raw = stream.read(ref.size + 1)
            finally:
                body.close()
            if len(raw) != ref.size or hashlib.sha256(raw).hexdigest() != ref.sha256:
                raise PayloadUnavailable("Payload integrity check failed")
            return json.loads(raw)
        except PayloadUnavailable:
            raise
        except Exception as exc:
            # Do not expose endpoint responses, signed URLs or credentials.
            raise PayloadUnavailable("Payload unavailable from object storage") from exc
