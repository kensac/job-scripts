"""Parse Markdown listing tables into postings."""

from __future__ import annotations

import re

import ftfy

from core.fetching.boards.common import _get, posting_from_listing
from core.fetching.boards.dates import posted_ts
from core.fetching.posting import JobPosting

# --- markdown tables ---------------------------------------------------------
#
# The aggregators (speedyapply, jobright-ai, vanshb03, SimplifyJobs READMEs)
# all publish one shape: a pipe table headed by a Company column, a title
# column, a Location column, sometimes a separate link column, and a date
# column that is either an age ("5d") or a day ("Sep 03"). Which column is
# which is read from the header row, so a new aggregator that keeps the
# conventions needs no code. A "↳" company means "same as the row above".

_TITLE_HEADERS = ("position", "job title", "role")
_DATE_HEADERS = ("age", "date posted", "date")
_LINK = re.compile(r'href="([^"]+)"|\]\((https?://[^)\s]+)\)')
_TAGS = re.compile(r"<[^>]+>")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _text(cell: str) -> str:
    return ftfy.fix_text(_TAGS.sub("", _MD_LINK.sub(r"\1", cell)).replace("**", "")).strip()


def _link(cell: str) -> str:
    m = _LINK.search(cell)
    return (m.group(1) or m.group(2)) if m else ""


def parse_markdown(text: str) -> list[JobPosting]:
    out: list[JobPosting] = []
    columns: dict[str, int] = {}
    company = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        heads = [_text(c).lower() for c in cells]
        if heads and heads[0] == "company":
            columns = {}
            for i, h in enumerate(heads):
                if h in _TITLE_HEADERS:
                    columns["title"] = i
                elif h == "location":
                    columns["location"] = i
                elif h in _DATE_HEADERS:
                    columns["date"] = i
                elif h == "posting" or h.startswith("appl"):
                    columns["link"] = i
            continue
        if not columns or "title" not in columns or set(cells[0]) <= {"-", ":"}:
            continue
        cell = {k: cells[i] for k, i in columns.items() if i < len(cells)}
        company = _text(cells[0]) if _text(cells[0]) != "↳" else company
        url = _link(cell.get("link", "")) or _link(cell.get("title", ""))
        location = _text(cell.get("location", "").replace("</br>", "; "))
        location = re.sub(r"\s*\+\d+\s*$", "", location)
        p = posting_from_listing(
            company,
            _text(cell.get("title", "")),
            location.split(";"),
            url,
            posted_ts(cell.get("date", "")),
        )
        if p:
            out.append(p)
    return out


def _markdown(url: str, company: str) -> list[JobPosting]:
    resp = _get(url)
    resp.raise_for_status()
    return parse_markdown(resp.text)
