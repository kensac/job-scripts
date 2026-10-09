"""When two postings are one company's text posted at several places.

A store, clinic or warehouse role is often listed once per location with the
same description, differing only in a location line and its numbers. Two
postings with the same title and text once those are removed are twins, and
verify_new reuses one's verdicts for the other. Measured on 51,114 postings
verified 2026-10-06 to 10-08: 16.4% had a twin; in 636 twin groups the boards'
verdicts agreed in 635 (Tech New Grad) and 636 (Aerospace), closed and
clearance in 634 each.
"""

from __future__ import annotations

import hashlib
import re

from core.answers import VERIFY_INPUT_CHARS

# A short line naming a place or work arrangement. Long lines are prose and kept.
_PLACE_WORD = re.compile(r"\b(?:remote|hybrid|on-?site|united states|usa|canada)\b", re.IGNORECASE)
_STATE_CODE = re.compile(r",\s*[A-Z]{2}\b")
_PLACE_LINE_CHARS = 200


def key(title: str, content: str) -> str:
    """The text a twin shares, as a digest: title and text, places and numbers removed."""
    lines = [
        line
        for line in re.split(r"\n+", content[:VERIFY_INPUT_CHARS])
        if len(line) > _PLACE_LINE_CHARS
        or not (_PLACE_WORD.search(line) or _STATE_CODE.search(line))
    ]
    text = "\n".join([title, *lines]).lower()
    text = re.sub(r"\s+", " ", re.sub(r"\d+", "#", text)).strip()
    return hashlib.sha256(text.encode()).hexdigest()
