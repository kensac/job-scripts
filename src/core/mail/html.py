"""Make a stranger's HTML safe to put in front of a person.

Two separate jobs, and they are not the same job.

SAFETY is nh3 (the Rust `ammonia` bindings): an allowlist sanitiser, not a
blocklist. Hand-written stripping is how XSS ships - the interesting inputs are
the ones nobody thought of - so the allowlist is delegated to a library whose
entire purpose is that, and this module only decides the policy.

PRIVACY is the second pass, and nh3 deliberately does not do it: a sanitiser's
job is to stop markup executing, and a remote image does not execute anything.
It just phones home. Every remote image in recruiter and marketing mail is a
read receipt, so rendering one tells the sender the moment he opened it -
which is not something a later change can take back. Remote sources are moved
to data-blocked-* attributes rather than deleted, so a reader can offer to
load them and the choice stays the person's.

Sanitisation happens on READ, not at import. The stored html is the message as
it arrived: keeping the original means a better sanitiser improves every
message ever received, and it is the same reason body_html exists at all -
this codebase has already destroyed one irreplaceable copy by deriving in
place.
"""

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

# `style` is NOT here. Mail CSS is written assuming it owns the document, and
# a `position: fixed` rule in a recruiter's template would escape the reader
# and lay itself over the application. Layout attributes that cannot leave
# their own element are kept so tables still read as tables.
_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "srcset", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align"},
    "table": {"width"},
}

# http and https are allowed so a URL SURVIVES to the privacy pass below. A
# link is not a fetch - it goes nowhere until the person clicks it - and
# stripping hrefs would leave a message whose "apply here" led nowhere. The
# things that fetch on render are handled by _REMOTE_FETCHING, not here.
#
# `cid:` is an image attached to the message itself, so it phones nobody.
_URL_SCHEMES = {"http", "https", "mailto", "tel", "cid"}

# Attributes that make the browser fetch the moment it renders, as opposed to
# when a person acts. Only the ones on tags the allowlist keeps can appear at
# all - `background` and `poster` are dropped wholesale with their tags - but
# they are listed so adding a tag later cannot quietly reintroduce a fetch.
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
