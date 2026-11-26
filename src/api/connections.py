"""Lists are connections: a client asks for the `first` nodes `after` a
cursor and is given the nodes, `page_info` (the cursor of the last node and
whether more follow) and `total_count`. No page numbers and no offsets: a
cursor names a position in the order, so rows added or recomputed between
two requests neither repeat nor go missing (docs/agents/visibility.md).

A cursor is opaque to the client: url-safe base64 of a JSON object that
names the query it was read under and the last node's position in it, the
encoding the public job lists already used. A cursor from another query is
refused rather than read in the wrong order."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from pydantic import BaseModel

from api.problem import refuse

# The most nodes one request may ask for. A guard on the response, not the
# page a client shows: the largest nodes (a source's analytics, about 2.9 KB)
# make 500 about 1.5 MB, where all 9,571 of them made 25 MB.
MAX_FIRST = 500


class PageInfo(BaseModel):
    end_cursor: str | None
    has_next_page: bool


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str, query: dict[str, Any]) -> Any:
    """The position a cursor names, if it was read under `query`."""
    try:
        parsed = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(parsed, dict) or set(parsed) != {"query", "position"}:
            raise ValueError
        if parsed["query"] != query:
            raise ValueError
        return parsed["position"]
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise refuse(400, "INVALID_CURSOR", "the cursor is not from this query") from exc


def cursor_for(query: dict[str, Any], position: Any) -> str:
    return encode_cursor({"query": query, "position": position})
