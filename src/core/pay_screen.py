"""Pay screens: named rules that settle a filter's pay bar from the posting text.

A filter whose prompt fails any posting whose stated top base is under a bar
pays to read postings that say so in plain figures. A pay screen reads those
figures without a model and answers rejected for that prompt alone
(`pay_screens` maps exact prompt hashes to a screen, so a prose edit drops
it). It answers only when every dollar figure in the text is under the bar,
at least one is a yearly-scale range, and no range is tied to a place: a
range "for candidates in Colorado" says nothing about pay elsewhere, and the
model keeps such postings (Twilio, 4 of 5 re-runs).

Like a title screen, a screen is never edited in place; a different rule is a
new name, measured before it is listed.

stated_base_under_150k_v1, measured 2026-10-18 on every keep of user 1's
filter (1,906) and 12,000 of its rejects: it answers for 1,638 rejects
(13.7%) and one keep, Anduril at $86,000 to $114,000, which re-asking the
same request rejected 5 times of 5.
"""

from __future__ import annotations

import re
from typing import Literal

PayRecipe = Literal["stated_base_under_150k_v1"]

_AMOUNT = r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?([kK]\b)?"
_RANGE = re.compile(_AMOUNT + r"\s*(?:-|\u2013|\u2014|to)\s*" + _AMOUNT)
_ONE = re.compile(_AMOUNT)
_HOURLY = re.compile(r"/\s*h(?:ou)?r|per\s+hour|an\s+hour|hourly|/hr", re.IGNORECASE)
_PLACE = re.compile(
    r"based in|located in|candidates in|residing in|\bzone\b|\btier\s*\d|\btier\s+[ab]\b|"
    r"metro(?:politan)? area|california|new york|washington|colorado|\bnyc\b|\bsf\b|"
    r"bay area|seattle",
    re.IGNORECASE,
)
_HOURS_PER_YEAR = 2080


def _yearly(match: re.Match[str], number: int, text: str) -> float:
    value = float(match.group(number).replace(",", ""))
    if match.group(number + 1):
        value *= 1000
    if value < 300 and _HOURLY.search(text[match.end() : match.end() + 40]):
        value *= _HOURS_PER_YEAR
    return value


def stated_top(text: str) -> float | None:
    """The highest yearly dollar figure the text states, or None when it
    states no yearly-scale range or ties a range to a place."""
    ranges = list(_RANGE.finditer(text))
    if not any(_yearly(r, 3, text) >= 20_000 for r in ranges):
        return None
    if any(_PLACE.search(text[max(0, r.start() - 200) : r.end() + 80]) for r in ranges):
        return None
    return max(_yearly(m, 1, text) for m in _ONE.finditer(text))


_BARS: dict[str, float] = {"stated_base_under_150k_v1": 150_000}


def screen(recipe: str, text: str) -> str | None:
    """The rejection reason when `recipe` settles the posting, else None."""
    bar = _BARS[recipe]
    top = stated_top(text)
    if top is None or top >= bar:
        return None
    return f"Stated pay tops out at ${top:,.0f}, under the ${bar:,.0f} base bar."
