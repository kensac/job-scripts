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

# botocore's default connection pool size, named so callers can bound their
# concurrency by it: a thread beyond the pool waits for a connection, and the
# pool discards the extra connection it opens.
MAX_CONNECTIONS = 10


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


_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class BundleMemberRef:
    """One value inside a version 3 bundle: a JSON object of named members.

    A distinct type so PayloadRef.parse keeps rejecting it: a reader that
    predates bundles fails closed instead of returning the whole bundle.
    """

    bucket: str
    key: str
    sha256: str
    size: int
    member: str
    member_sha256: str
    member_size: int
    version: int = 3

    @classmethod
    def parse(cls, value: Any) -> BundleMemberRef:
        try:
            ref = cls(**value)
            if (
                not isinstance(ref.bucket, str)
                or not ref.bucket
                or not isinstance(ref.sha256, str)
                or _DIGEST.fullmatch(ref.sha256) is None
                or type(ref.size) is not int
                or ref.size < 0
                or type(ref.version) is not int
                or ref.version != 3
                or ref.key != f"payloads/v3/sha256/{ref.sha256}.json"
                or not isinstance(ref.member, str)
                or not ref.member
                or not isinstance(ref.member_sha256, str)
                or _DIGEST.fullmatch(ref.member_sha256) is None
                or type(ref.member_size) is not int
                or ref.member_size < 0
            ):
                raise ValueError("invalid reference")
            return ref
        except (TypeError, ValueError) as exc:
            raise PayloadUnavailable("Invalid bundle member reference") from exc


def parse_ref(value: Any) -> PayloadRef | BundleMemberRef:
    if isinstance(value, dict) and value.get("version") == 3:
        return BundleMemberRef.parse(value)
    return PayloadRef.parse(value)


# Decoded bundles keyed by object key and size, owned by one call that
# resolves many members; never shared across calls or held between them.
BundleCache = dict[tuple[str, int], dict[str, Any]]


class PayloadStore:
    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_env(cls, max_connections: int = MAX_CONNECTIONS) -> PayloadStore:
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
                    max_pool_connections=max_connections,
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"mode": "standard", "total_max_attempts": 3},
                ),
            )
            return cls(client, os.environ[prefix + "BUCKET"])
        except Exception as exc:
            raise PayloadUnavailable("Object storage configuration unavailable") from exc

    def _put(self, raw: bytes, key: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=raw, ContentType="application/json"
            )
        except Exception as exc:
            raise PayloadUnavailable("Payload upload failed") from exc

    def put_verified(self, value: Any) -> PayloadRef:
        raw = encode_payload(value)
        digest = hashlib.sha256(raw).hexdigest()
        ref = PayloadRef(
            self.bucket, f"payloads/v2/sha256/{digest}.json", digest, len(raw), version=2
        )
        self._put(raw, ref.key)
        # A successful PUT or ETag alone does not prove the bytes can be read.
        if self.get(ref) != value:
            raise PayloadUnavailable("Payload round-trip mismatch")
        return ref

    def put_bundle(self, members: dict[str, Any]) -> dict[str, BundleMemberRef]:
        """Upload many values as one verified object, returning a reference per member."""
        if not members or not all(isinstance(name, str) and name for name in members):
            raise PayloadUnavailable("A bundle needs named members")
        raw = encode_payload(members)
        digest = hashlib.sha256(raw).hexdigest()
        key = f"payloads/v3/sha256/{digest}.json"
        refs = {}
        for name, value in members.items():
            encoded = encode_payload(value)
            refs[name] = BundleMemberRef(
                self.bucket,
                key,
                digest,
                len(raw),
                name,
                hashlib.sha256(encoded).hexdigest(),
                len(encoded),
            )
        self._put(raw, key)
        # Read every member back through the reader, not only the bundle digest.
        cache: BundleCache = {}
        if any(self.get_member(ref, cache) != members[name] for name, ref in refs.items()):
            raise PayloadUnavailable("Payload round-trip mismatch")
        return refs

    def _read(self, bucket: str, key: str, size: int, sha256: str, *, gz: bool) -> Any:
        if bucket != self.bucket:
            raise PayloadUnavailable("Payload reference belongs to a different bucket")
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            try:
                if gz:
                    with gzip.GzipFile(fileobj=body) as stream:
                        raw = stream.read(size + 1)
                else:
                    raw = body.read(size + 1)
            finally:
                body.close()
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != sha256:
                raise PayloadUnavailable("Payload integrity check failed")
            return json.loads(raw)
        except PayloadUnavailable:
            raise
        except Exception as exc:
            # Do not expose endpoint responses, signed URLs or credentials.
            raise PayloadUnavailable("Payload unavailable from object storage") from exc

    def get(self, ref: PayloadRef) -> Any:
        ref = PayloadRef.parse(asdict(ref))
        return self._read(ref.bucket, ref.key, ref.size, ref.sha256, gz=ref.version == 1)

    def get_bundle(self, ref: BundleMemberRef, cache: BundleCache | None = None) -> dict[str, Any]:
        ref = BundleMemberRef.parse(asdict(ref))
        return self._bundle(ref.bucket, ref.sha256, ref.size, cache)

    def _bundle(
        self, bucket: str, sha256: str, size: int, cache: BundleCache | None
    ) -> dict[str, Any]:
        key = f"payloads/v3/sha256/{sha256}.json"
        members = None if cache is None else cache.get((key, size))
        if members is None:
            members = self._read(bucket, key, size, sha256, gz=False)
            if not isinstance(members, dict):
                raise PayloadUnavailable("Payload bundle is not an object")
            if cache is not None:
                cache[key, size] = members
        return members

    def get_digest_member(
        self, bundle_sha256: str, bundle_size: int, member_sha256: str, cache: BundleCache
    ) -> Any:
        """A member of a bundle whose members are named by their own SHA-256.

        The bundle stands for the bucket this store writes to: a row that holds
        only the two digests and the size names no bucket of its own.
        """
        if (
            _DIGEST.fullmatch(bundle_sha256) is None
            or _DIGEST.fullmatch(member_sha256) is None
            or type(bundle_size) is not int
            or bundle_size < 0
        ):
            raise PayloadUnavailable("Invalid bundle member reference")
        members = self._bundle(self.bucket, bundle_sha256, bundle_size, cache)
        if member_sha256 not in members:
            raise PayloadUnavailable("Payload bundle has no such member")
        value = members[member_sha256]
        if hashlib.sha256(encode_payload(value)).hexdigest() != member_sha256:
            raise PayloadUnavailable("Payload bundle member integrity check failed")
        return value

    def get_member(self, ref: BundleMemberRef, cache: BundleCache | None = None) -> Any:
        members = self.get_bundle(ref, cache)
        if ref.member not in members:
            raise PayloadUnavailable("Payload bundle has no such member")
        value = members[ref.member]
        raw = encode_payload(value)
        if len(raw) != ref.member_size or hashlib.sha256(raw).hexdigest() != ref.member_sha256:
            raise PayloadUnavailable("Payload bundle member integrity check failed")
        return value

    def get_ref(self, ref: PayloadRef | BundleMemberRef, cache: BundleCache | None = None) -> Any:
        if isinstance(ref, BundleMemberRef):
            return self.get_member(ref, cache)
        return self.get(ref)
