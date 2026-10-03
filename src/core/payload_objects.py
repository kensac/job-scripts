"""Immutable, verified payloads outside the operational database."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

import boto3
from botocore.config import Config


class PayloadUnavailable(RuntimeError):
    pass


def encode_payload(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise PayloadUnavailable("Payload is not finite JSON") from exc


@dataclass(frozen=True)
class PayloadRef:
    bucket: str
    key: str
    sha256: str
    size: int
    version: int = 1

    @classmethod
    def parse(cls, value: Any) -> PayloadRef:
        try:
            ref = cls(**value)
            if (
                not isinstance(ref.bucket, str)
                or not ref.bucket
                or not isinstance(ref.sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", ref.sha256) is None
                or type(ref.size) is not int
                or ref.size < 0
                or type(ref.version) is not int
                or ref.version not in (1, 2)
                or ref.key
                != f"payloads/v{ref.version}/sha256/{ref.sha256}.json"
                + (".gz" if ref.version == 1 else "")
            ):
                raise ValueError("invalid reference")
            return ref
        except (TypeError, ValueError) as exc:
            raise PayloadUnavailable("Invalid payload reference") from exc


class PayloadStore:
    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_env(cls) -> PayloadStore:
        # Explicit credentials prevent an accidental fallback to another
        # account through the SDK credential discovery chain.
        prefix = "JOBTRACKER_S3_"
        try:
            required = ("ENDPOINT", "REGION", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
            if any(not os.environ[prefix + name].strip() for name in required):
                raise ValueError("empty storage configuration")
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
        except Exception as exc:
            raise PayloadUnavailable("Object storage configuration unavailable") from exc

    def put_verified(self, value: Any) -> PayloadRef:
        raw = encode_payload(value)
        digest = hashlib.sha256(raw).hexdigest()
        ref = PayloadRef(
            self.bucket, f"payloads/v2/sha256/{digest}.json", digest, len(raw), version=2
        )
        try:
            self.client.put_object(
                Bucket=ref.bucket,
                Key=ref.key,
                Body=raw,
                ContentType="application/json",
            )
        except Exception as exc:
            raise PayloadUnavailable("Payload upload failed") from exc
        # A successful PUT or ETag alone does not prove the bytes can be read.
        if self.get(ref) != value:
            raise PayloadUnavailable("Payload round-trip mismatch")
        return ref

    def get(self, ref: PayloadRef) -> Any:
        ref = PayloadRef.parse(asdict(ref))
        if ref.bucket != self.bucket:
            raise PayloadUnavailable("Payload reference belongs to a different bucket")
        try:
            response = self.client.get_object(Bucket=ref.bucket, Key=ref.key)
            body = response["Body"]
            try:
                if ref.version == 1:
                    with gzip.GzipFile(fileobj=body) as stream:
                        raw = stream.read(ref.size + 1)
                else:
                    raw = body.read(ref.size + 1)
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
