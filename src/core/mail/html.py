"""Sanitise mail HTML on read and block remote fetches on render.

nh3 removes markup outside the allowlist. The second pass moves remote image
sources to data-blocked-* attributes. A reader can offer to load them without
contacting the sender automatically.

The stored HTML remains as received. A changed sanitiser can improve the
display of existing messages without replacing their source data."""

from __future__ import annotations

import re

import nh3
from bs4 import BeautifulSoup

# Structure and emphasis, and nothing that executes, navigates on its own, or
# pulls in another document. script/style/iframe/object/embed/form are absent
# by omission rather than by a blocklist: anything not named here is dropped.
_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}

# Do not allow `style`: fixed-position mail CSS could cover the application.
# Local table layout attributes remain so tables are readable.
_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "srcset", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align"},
    "table": {"width"},
}

# Keep links until a person clicks them. The privacy pass below blocks remote
# sources that fetch on render. `cid:` identifies an attached image.
_URL_SCHEMES = {"http", "https", "mailto", "tel", "cid"}

# Block attributes that fetch on render. The allowlist already drops some of
# them, but this list also protects future additions to the allowed tags.
_REMOTE_FETCHING = ("src", "srcset", "background", "poster")

_REMOTE = re.compile(r"^\s*(?:https?:)?//", re.IGNORECASE)


def sanitise(html: str) -> tuple[str, int]:
    """Returns display-safe html, and how many remote fetches were blocked.

    The count is not decoration: a reader that offers "load images" needs to
    know whether there is anything to load, and a person deciding whether to
    trust a message benefits from knowing it wanted to phone home nine times.
    """
    safe = nh3.clean(
        html,
        tags=_TAGS,
        attributes={tag: set(attrs) for tag, attrs in _ATTRS.items()},
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )
    soup = BeautifulSoup(safe, "html.parser")
    blocked = 0
    for element in soup.find_all(True):
        for attr in _REMOTE_FETCHING:
            value = element.get(attr)
            if isinstance(value, str) and _REMOTE.match(value):
                del element[attr]
                element[f"data-blocked-{attr}"] = value
                blocked += 1
    return str(soup), blocked
