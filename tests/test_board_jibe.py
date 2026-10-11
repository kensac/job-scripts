"""Jibe board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.board_responses import BoardResponse as _Resp


def _jibe_job(slug: str, description: str = "<p>Overview</p>") -> dict:
    """careers.spiritaero.com/api/jobs, 2026-10-05, one job trimmed to the keys
    the fetcher reads and some beside them. The locations are a V2X job's,
    whose full_location repeats a place."""
    return {
        "data": {
            "slug": slug,
            "language": "en-us",
            "req_id": slug,
            "title": f"Role {slug}",
            "city": "Fayetteville",
            "full_location": "Fayetteville, North Carolina; Fayetteville, North Carolina",
            "multipleLocations": True,
            "description": description,
            "qualifications": "<p>Basic Qualifications</p>",
            "responsibilities": "<p>Position Responsibilities</p>",
            "hiring_organization": "Spirit AeroSystems",
            "posted_date": "2026-10-02T20:07:00+0000",
            "apply_url": f"https://careers-spiritaero.icims.com/jobs/{slug}/login",
            "ats_code": "icims",
        }
    }


def _serve_jibe(monkeypatch, slugs: list[str], total: int):
    """page= is one-based, limit= is honoured up to 100, totalCount rides on
    every page, and a page past the end is an empty list (Spirit, 2026-10-05:
    limit=100&page=3 gave 22 of 222, page=24 at the default 10 gave none)."""
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        q = {k: int(v) for k, v in (x.split("=") for x in url.split("?")[1].split("&"))}
        assert q["page"] >= 1, "page=0 is a 422 on the live API"
        start = (q["page"] - 1) * q["limit"]
        jobs = [_jibe_job(s) for s in slugs[start : start + q["limit"]]]
        return _Resp({"jobs": jobs, "totalCount": total, "count": total})

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_jibe_pages_one_based_at_the_largest_page_it_accepts(monkeypatch):
    slugs = [str(17000 + i) for i in range(205)]
    asked = _serve_jibe(monkeypatch, slugs, total=205)
    out = boards.fetch_listings("https://careers.spiritaero.com/api/jobs", "ignored")
    assert asked == [
        f"https://careers.spiritaero.com/api/jobs?limit=100&page={n}" for n in (1, 2, 3)
    ]
    assert len(out) == 205
    p = out[0]
    assert p.url == "https://careers.spiritaero.com/jobs/17000"
    assert p.company == "Spirit AeroSystems"
    assert p.locations == ["Fayetteville, North Carolina"]
    assert p.date_posted == int(
        datetime.datetime(2026, 10, 2, 20, 7, tzinfo=datetime.UTC).timestamp()
    )
    # The description, and the sections it left out (V2X, 17 of 765).
    assert p.description == "Overview\n\nPosition Responsibilities\n\nBasic Qualifications"
    assert p.raw is not None and "qualifications" not in p.raw and "description" not in p.raw


def test_jibe_does_not_repeat_sections_the_description_already_holds(monkeypatch):
    held = "<p>Overview</p><p>Position Responsibilities</p><p>Basic Qualifications</p>"
    monkeypatch.setattr(
        boards._session,
        "get",
        lambda url, **kw: _Resp({"jobs": [_jibe_job("1", held)], "totalCount": 1}),
    )
    (p,) = boards.fetch_listings("https://careers.spiritaero.com/api/jobs", "")
    assert "Basic Qualifications" in p.description
    assert p.description.count("Basic Qualifications") == 1


def test_jibe_short_of_its_stated_total_is_partial(monkeypatch):
    _serve_jibe(monkeypatch, ["1", "2"], total=3)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://careers.spiritaero.com/api/jobs", "")
    assert len(raised.value.postings) == 2


# www-api.ibm.com/search/api/v2 on 2026-10-05: verbatim hits, in _id order.
