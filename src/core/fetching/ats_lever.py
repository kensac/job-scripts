"""Resolve Lever posting detail and availability."""

from __future__ import annotations

import re

from core.fetching.ats_common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    lever_text,
)


class Lever(AtsResolver):
    name = "lever"
    markers = ("lever.co",)

    def canonical(self, url: str) -> str | None:
        match = re.search(r"(jobs(?:\.eu)?\.lever\.co)/([^/?]+)/([0-9a-f-]{36})", url)
        return (
            f"https://{match.group(1).lower()}/{match.group(2)}/{match.group(3)}" if match else None
        )

    def fetch(self, url: str) -> AtsResult:
        match = re.search(r"lever\.co/([^/]+)/([0-9a-f-]{36})", url)
        if not match:
            return UNSUPPORTED
        api_host = "api.eu.lever.co" if ".eu.lever.co" in url else "api.lever.co"
        resp = self.get(f"https://{api_host}/v0/postings/{match.group(1)}/{match.group(2)}")
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        return self.result(lever_text(resp.json()))
