"""Resume text and revision storage."""

from __future__ import annotations


def attached_name(filename: str | None) -> str:
    """The name a resume's PDF is served under, and so the name the
    extension attaches to a form and reads back at submit."""
    return (filename or "resume.pdf").replace('"', "")
