"""Bytedance board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import pytest

from core.fetching import board_bytedance, boards
from tests.board_responses import BoardResponse as _Resp


def _city(*names: tuple[str, str]) -> dict | None:
    """city_info as the search returns it: the city, its parent state and that
    state's parent country, each with an English and a localised name."""
    city = None
    for en, i18n in reversed(names):
        city = {"en_name": en, "i18n_name": i18n, "parent": city}
    return city


# api.lifeattiktok.com and jobs.bytedance.com, 2026-10-05, trimmed to the keys
# the fetcher reads. The ByteDance board answers i18n_name in Chinese.
_TIKTOK_ROWS = [
    {
        "id": "7667946787733244165",
        "code": "A137788B",
        "title": "Multimodal LLM Algorithm Engineer Graduate (Global E-Commerce, Knowledge Graph) - 2027 Start (PhD)",
        "description": "About the Team\nWe are part of the Global E-commerce Algorithm team.",
        "requirement": "Minimum Qualifications\n- Individuals who are completing a PhD degree.",
        "city_info": _city(
            ("Singapore", "Singapore"), ("Singapore", "Singapore"), ("Singapore", "Singapore")
        ),
        "recruit_type": {"id": "201", "en_name": "Regular"},
    },
    *[
        {
            "id": str(7686350939411253509 + i),
            "title": f"Role {i}",
            "description": "d",
            "requirement": "r",
            "city_info": _city(
                ("Jakarta", "Jakarta"), ("Jakarta Raya", "Jakarta Raya"), ("Indonesia", "Indonesia")
            ),
        }
        for i in range(4)
    ],
]
_BYTEDANCE_ROWS = [
    {
        "id": "7624637489953130805",
        "code": "A151689",
        "title": "Data Center Site Acquisition Manager - Infrastructure Power & Energy",
        "description": "To support the fast growth of the platform.",
        "requirement": "Minimum Qualifications: \n- Bachelor's degree in Engineering.",
        "city_info": _city(
            ("San Jose", "圣何塞"),
            ("California", "加利福尼亚州"),
            ("United States of America", "美国"),
        ),
    }
]


def _bytedance_api(boards_by_path: dict, window: int, capped_count: bool = False, asked=None):
    """The search as measured on 2026-10-05: the website-path header picks the
    board and its absence is a 400; the limit asked for is honoured; the count
    rides on every page; and a request whose offset plus limit passes the
    window answers no rows and a count of the window, whatever the board
    holds. Whether a board over the window states its true count on the first
    page is unmeasured, so `capped_count` covers the other answer."""

    def post(url, json, headers=None, **kw):
        board = (headers or {}).get("website-path")
        if asked is not None:
            asked.append((url, board, json["offset"], json["limit"]))
        if board not in boards_by_path:
            raise boards.requests.HTTPError("400 invalid request")
        rows = boards_by_path[board]
        if json["offset"] + json["limit"] > window:
            return _Resp({"code": 0, "data": {"job_post_list": [], "count": window}})
        count = min(len(rows), window) if capped_count else len(rows)
        page = rows[json["offset"] : json["offset"] + json["limit"]]
        return _Resp({"code": 0, "data": {"job_post_list": page, "count": count}})

    return post


_TIKTOK = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts?website-path=tiktok"
_BYTEDANCE = "https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts?website-path=en"


def test_one_fetcher_reads_both_bytedance_boards_by_their_website_path(monkeypatch):
    asked: list = []
    api = {"tiktok": _TIKTOK_ROWS, "en": _BYTEDANCE_ROWS}
    monkeypatch.setattr(boards._session, "post", _bytedance_api(api, 10_000, asked=asked))
    monkeypatch.setattr(board_bytedance, "_BYTEDANCE_PAGE", 2)

    tiktok = boards.fetch_listings(_TIKTOK, "TikTok")
    assert len(tiktok) == len(_TIKTOK_ROWS)
    first = tiktok[0]
    assert first.url == "https://lifeattiktok.com/search/7667946787733244165"
    assert first.company == "TikTok"
    assert first.date_posted == 0
    # A city-state names itself three times; once is the place.
    assert first.locations == ["Singapore"]
    assert first.description == (
        f"{_TIKTOK_ROWS[0]['title']}\n\nSingapore\n\n"
        "About the Team\nWe are part of the Global E-commerce Algorithm team.\n\n"
        "Minimum Qualifications\n- Individuals who are completing a PhD degree."
    )
    assert first.raw is not None and first.raw["code"] == "A137788B"
    assert "description" not in first.raw and "requirement" not in first.raw
    # The board rode in the header, not the URL, and pages followed the offset.
    assert {(u, b) for u, b, _, _ in asked} == {
        ("https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts", "tiktok")
    }
    assert [o for _, _, o, _ in asked] == [0, 2, 4]

    (bytedance,) = boards.fetch_listings(_BYTEDANCE, "ByteDance")
    assert bytedance.url == "https://joinbytedance.com/search/7624637489953130805"
    assert bytedance.locations == ["San Jose, California, United States of America"]


def test_a_bytedance_board_the_url_does_not_name_is_refused():
    with pytest.raises(ValueError):
        boards.fetch_listings(_TIKTOK.replace("=tiktok", "=school"), "TikTok")


@pytest.mark.parametrize("capped_count", [False, True])
def test_a_bytedance_board_past_the_search_window_is_a_partial_pull(monkeypatch, capped_count):
    """Past offset plus limit 10,000 the search answers no rows and a count of
    10,000, which a loop paging until an empty page reads as the end of the
    board. The fetcher reads up to the window and says the pull is partial."""
    monkeypatch.setattr(
        boards._session,
        "post",
        _bytedance_api({"tiktok": _TIKTOK_ROWS}, window=4, capped_count=capped_count),
    )
    monkeypatch.setattr(board_bytedance, "_BYTEDANCE_PAGE", 3)
    monkeypatch.setattr(board_bytedance, "_BYTEDANCE_WINDOW", 4)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(_TIKTOK, "TikTok")
    assert [p.title for p in raised.value.postings] == [r["title"] for r in _TIKTOK_ROWS[:4]]


def test_a_bytedance_board_that_shifts_mid_pull_is_a_partial_pull(monkeypatch):
    """Paging is by offset over a live board: a posting closed between two
    pages moves the next one back onto the page already read."""
    rows = list(_TIKTOK_ROWS)
    api = _bytedance_api({"tiktok": rows}, 10_000)

    def post(url, json, **kw):
        reply = api(url, json, **kw)
        if json["offset"] == 0:
            del rows[0]
        return reply

    monkeypatch.setattr(boards._session, "post", post)
    monkeypatch.setattr(board_bytedance, "_BYTEDANCE_PAGE", 2)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(_TIKTOK, "TikTok")
    assert len(raised.value.postings) == len(_TIKTOK_ROWS) - 1


# careers-gdms.icims.com/jobs/search?ss=1&in_iframe=1, 2026-10-05: a card with
# its posted date in the header (exact time in the title attribute), and a
