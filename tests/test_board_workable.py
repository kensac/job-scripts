"""Workable board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

from core.fetching import boards, client
from tests.board_responses import BoardResponse as _Resp


def test_workable_follows_the_next_page_token(monkeypatch):
    posts = []

    def post(url, json, **kw):
        posts.append((url, json))
        if "token" not in json:
            return _Resp(
                {
                    "total": 2,
                    "nextPage": "WzE3ODU5NzQ0MDAwMDAsNDE4MjgyN10=",
                    "results": [
                        {
                            "shortcode": "BCEAC9B2D1",
                            "title": "Tech Talent Sourcer",
                            "published": "2026-09-01T00:00:00.000Z",
                            "remote": False,
                            "location": {
                                "country": "United Kingdom",
                                "city": "London",
                                "region": "England",
                            },
                        }
                    ],
                }
            )
        return _Resp(
            {
                "total": 2,
                "nextPage": None,
                "results": [
                    {
                        "shortcode": "637F3B9521",
                        "title": "Driver",
                        "published": "2026-08-30T00:00:00.000Z",
                        "remote": True,
                        "location": {},
                    }
                ],
            }
        )

    monkeypatch.setattr(boards._session, "post", post)
    out = boards.fetch_listings("https://apply.workable.com/api/v3/accounts/zego/jobs", "Zego")
    assert [(p.title, p.locations) for p in out] == [
        ("Tech Talent Sourcer", ["London, England, United Kingdom"]),
        ("Driver", []),
    ]
    assert out[0].url == "https://apply.workable.com/zego/j/BCEAC9B2D1/"
    assert all(p.company == "Zego" for p in out)
    assert [j.get("token") for _, j in posts] == [None, "WzE3ODU5NzQ0MDAwMDAsNDE4MjgyN10="]


def test_workable_paces_its_requests(monkeypatch):
    slept = []
    monkeypatch.setattr(client.time, "sleep", lambda s: slept.append(round(s, 1)))
    monkeypatch.setattr(
        boards._session, "post", lambda url, json, **kw: _Resp({"total": 0, "results": []})
    )
    client._last_call.clear()
    client.set_pace({"apply.workable.com": 6})
    boards.fetch_listings("https://apply.workable.com/api/v3/accounts/a/jobs", "A")
    boards.fetch_listings("https://apply.workable.com/api/v3/accounts/b/jobs", "B")
    # The first call goes straight out; the second waits out the six seconds.
    assert slept and 5.0 < slept[-1] <= 6.0
