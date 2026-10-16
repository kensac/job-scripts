"""Rippling board behavior over replies shaped like the live API on 2026-10-16
(Loop), trimmed to the keys read."""

from __future__ import annotations

import pytest

from core.fetching import boards
from core.fetching.boards import rippling
from tests.fetching.boards.responses import BoardResponse as _Resp

_URL = "https://ats.rippling.com/api/v2/board/loop/jobs"
_PAYMENTS = "ed637533-ed67-46ea-a8bb-b22b0559befb"
_DELIVERY = "39bb252a-aad2-4980-b030-a893f6ef8166"


def _row(job_id, name, place):
    return {
        "id": job_id,
        "name": name,
        "url": f"https://ats.rippling.com/loop/jobs/{job_id}",
        "department": {"name": "Payments"},
        "locations": [{"name": place, "workplaceType": "ON_SITE"}],
        "language": "en-US",
    }


_ROWS = [
    _row(_PAYMENTS, "Head of Payments", "San Francisco, CA"),
    _row(_PAYMENTS, "Head of Payments", "Chicago, IL"),
    _row(_DELIVERY, "Client Operations Delivery Manager", "Chicago, IL"),
    _row(_PAYMENTS, "Head of Payments", "New York, NY"),
]


def _serve(monkeypatch, rows, total, page_size):
    """Pages of page_size rows, each stating total, empty past the end."""
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        page = int(url.split("page=")[1].split("&")[0])
        items = rows[page * page_size : (page + 1) * page_size]
        return _Resp({"items": items, "page": page, "totalItems": total})

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_rippling_folds_a_posting_listed_once_per_place(monkeypatch):
    monkeypatch.setattr(rippling, "_RIPPLING_PAGE", 2)
    asked = _serve(monkeypatch, _ROWS, total=4, page_size=2)
    out = boards.fetch_listings(_URL, "Loop")
    assert [p.title for p in out] == ["Head of Payments", "Client Operations Delivery Manager"]
    assert out[0].locations == ["San Francisco, CA", "Chicago, IL", "New York, NY"]
    assert out[0].url == f"https://ats.rippling.com/loop/jobs/{_PAYMENTS}"
    assert {p.company for p in out} == {"Loop"}
    assert asked == [f"{_URL}?page=0&pageSize=2", f"{_URL}?page=1&pageSize=2"]


def test_rippling_short_of_its_count_is_partial(monkeypatch):
    _serve(monkeypatch, _ROWS[:3], total=4, page_size=1000)
    with pytest.raises(boards.PartialPull) as caught:
        boards.fetch_listings(_URL, "Loop")
    assert len(caught.value.postings) == 2
