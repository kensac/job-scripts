"""Store complete resume text as ordered, immutable parts."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from api import db

TEXT_PART_CHARS = 32_768


def store_text(
    user_id: int, fragments: Iterable[str], *, allow_blank: bool = False
) -> tuple[uuid.UUID, int]:
    version_id = uuid.uuid4()
    db.execute(
        "INSERT INTO resume_text_versions (id, user_id) VALUES (%s, %s)",
        (version_id, user_id),
    )
    buffer = ""
    chars = 0
    index = 0
    has_text = False
    for fragment in fragments:
        has_text = has_text or bool(fragment.strip())
        chars += len(fragment)
        offset = 0
        while offset < len(fragment):
            take = min(TEXT_PART_CHARS - len(buffer), len(fragment) - offset)
            buffer += fragment[offset : offset + take]
            offset += take
            if len(buffer) == TEXT_PART_CHARS:
                db.execute(
                    "INSERT INTO resume_text_parts (version_id, part_index, text) "
                    "VALUES (%s, %s, %s)",
                    (version_id, index, buffer),
                )
                index += 1
                buffer = ""
    if buffer:
        db.execute(
            "INSERT INTO resume_text_parts (version_id, part_index, text) VALUES (%s, %s, %s)",
            (version_id, index, buffer),
        )
        index += 1
    if not has_text and not allow_blank:
        raise ValueError("that PDF has no extractable text")
    db.execute(
        "UPDATE resume_text_versions SET chars = %s, part_count = %s, ready = TRUE WHERE id = %s",
        (chars, index, version_id),
    )
    return version_id, chars


def text_prefix(version_id: uuid.UUID, max_chars: int) -> str:
    parts: list[str] = []
    remaining = max_chars
    index = 0
    while remaining > 0:
        row = db.query_one(
            "SELECT text FROM resume_text_parts WHERE version_id = %s AND part_index = %s",
            (version_id, index),
        )
        if row is None:
            break
        part = row["text"][:remaining]
        parts.append(part)
        remaining -= len(part)
        index += 1
    return "".join(parts)
