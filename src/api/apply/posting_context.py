"""Posting evidence for applications that need not belong to the catalog."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

from api import db
from core.fetching import ats
from core.fetching.forms import posting_urls


async def external_posting(user_id: int, fill_id: int) -> str | None:
    fill = db.query_one(
        "SELECT url FROM application_fills WHERE id = %s AND user_id = %s",
        (fill_id, user_id),
    )
    if not fill:
        return None
    # Only hosted ATS routes whose resolvers construct fixed public API
    # endpoints. Never scrape arbitrary URLs supplied by an application form.
    hosts = {
        "jobs.ashbyhq.com",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
    }
    raw = urlsplit(fill["url"])
    if raw.scheme != "https" or raw.netloc not in hosts:
        return None
    url = ats.canonicalize(posting_urls(fill["url"])[0])
    if not url:
        return None
    # Tokens are path components, not encoded paths or query fragments.
    if not re.fullmatch(r"/[A-Za-z0-9_-]+/(?:jobs/)?[A-Za-z0-9-]+", urlsplit(url).path):
        return None
    result = await asyncio.to_thread(ats.resolve, url)
    return result.text if result.ok else None
