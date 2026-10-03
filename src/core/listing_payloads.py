"""A listing's description and raw record: inline, or a member of a verified
bundle object.

A field held by reference has three columns set: `<field>_sha256`, the SHA-256
of `encode_payload(value)`, which also names the value's member in the bundle;
`<field>_object`, the bundle's own SHA-256; and `<field>_object_size`, its
length. The inline column then holds its default ('' or '{}'), which is not
the value. An empty description is never stored by reference: '' inline with
no reference is "the board never carried the text".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.payload_objects import BundleCache, PayloadStore, PayloadUnavailable
from core.pool import in_transaction

FIELDS = ("description", "raw")
_TYPES = {"description": str, "raw": dict}
# Everything resolve needs, for a reader's SELECT list. Never select the
# inline columns alone: a reference-held row reads '' and '{}' there.
COLUMNS = ", ".join(
    f"{field}, {field}_sha256, {field}_object, {field}_object_size" for field in FIELDS
)


def resolve(
    rows: Iterable[Mapping[str, Any]], store: PayloadStore | None = None
) -> list[dict[str, Any]]:
    """Each row with `description` and `raw` as written, the one reader of both
    shapes. A reference that cannot be read, or reads something else, raises
    PayloadUnavailable; it is never a listing without text."""
    cache: BundleCache = {}
    resolved = []
    for stored in rows:
        row = dict(stored)
        for field in FIELDS:
            if row[f"{field}_object"] is None:
                continue
            if in_transaction():
                raise RuntimeError("A listing payload cannot be read inside a database transaction")
            store = store or PayloadStore.from_env()
            value = store.get_digest_member(
                bytes(row[f"{field}_object"]).hex(),
                row[f"{field}_object_size"],
                bytes(row[f"{field}_sha256"]).hex(),
                cache,
            )
            if not isinstance(value, _TYPES[field]):
                raise PayloadUnavailable(f"Listing {field} is not a {_TYPES[field].__name__}")
            row[field] = value
        resolved.append(row)
    return resolved
