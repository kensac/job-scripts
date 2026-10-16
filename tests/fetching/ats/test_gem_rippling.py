"""core.fetching.ats: the Gem and Rippling resolvers, over bodies copied from
the live APIs on 2026-10-16 and trimmed to the keys read."""

from __future__ import annotations

import datetime

from core.fetching import ats


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


_RIP = "ed637533-ed67-46ea-a8bb-b22b0559befb"


def test_rippling_canonical_drops_locale_and_embed():
    want = f"https://ats.rippling.com/loop/jobs/{_RIP}"
    for url in (
        f"https://ats.rippling.com/loop/jobs/{_RIP}",
        f"https://ats.rippling.com/en-GB/loop/jobs/{_RIP}?jobSite=LinkedIn",
        f"https://ats.rippling.com/embed/loop/jobs/{_RIP}",
    ):
        assert ats.canonicalize(url) == want
    assert ats.canonicalize("https://ats.rippling.com/loop/jobs") is None


def test_rippling_reads_the_posting_and_its_date(monkeypatch):
    asked = []

    def get(url):
        asked.append(url)
        return _Resp(
            {
                "name": "Head of Payments",
                "description": {
                    "company": "<meta><p><b>About Loop</b></p><p>Loop fixes freight.</p>",
                    "role": "<meta><p>Own payments.</p>",
                },
                "workLocations": ["San Francisco, CA", "Chicago, IL"],
                "createdOn": "2026-07-07T15:42:07.924000-07:00",
                "companyName": "Loop",
            }
        )

    res = ats.Rippling()
    monkeypatch.setattr(res, "get", get)
    out = res.fetch(f"https://ats.rippling.com/en-GB/loop/jobs/{_RIP}")
    assert out.ok and out.posted == datetime.date(2026, 7, 7)
    assert out.text == (
        "Head of Payments\n\nSan Francisco, CA; Chicago, IL\n\nAbout Loop\nLoop fixes freight."
        "\n\nOwn payments."
    )
    assert asked == [f"https://ats.rippling.com/api/v2/board/loop/jobs/{_RIP}"]


def test_rippling_gone_only_on_the_closed_posting_answer(monkeypatch):
    res = ats.Rippling()
    url = f"https://ats.rippling.com/loop/jobs/{_RIP}"
    closed = {"ok": False, "error": "There was a client validation error"}
    no_board = {"error_code": "RESOURCE_NOT_FOUND", "message": "Job Board not found"}
    for body, status, want in (
        (closed, 404, ats.Status.GONE),
        (no_board, 404, ats.Status.ERROR),
        ({}, 500, ats.Status.ERROR),
    ):
        monkeypatch.setattr(res, "get", lambda u, b=body, s=status: _Resp(b, s))
        assert res.fetch(url).status is want
    monkeypatch.setattr(res, "get", lambda u: None)
    assert res.fetch(url).status is ats.Status.ERROR


_GEM = "https://jobs.gem.com/function-health/am9icG9zdDramAtjvaEx5CaiubYzZIC0"


def test_gem_canonical_is_the_posting_page():
    assert ats.canonicalize(f"{_GEM}/application?src=x") == _GEM
    assert ats.canonicalize("https://jobs.gem.com/bilt/4509720004/") == (
        "https://jobs.gem.com/bilt/4509720004"
    )


def test_gem_reads_the_board_api_posting(monkeypatch):
    asked = []

    def get(url):
        asked.append(url)
        return _Resp(
            {
                "title": "Principal Product Designer",
                "location": {"name": "United States - Remote"},
                "content": "<h3>Company Overview</h3><div>Function Health measures.</div>",
                "first_published_at": "2026-01-28T20:32:42.485Z",
            }
        )

    res = ats.Gem()
    monkeypatch.setattr(res, "get", get)
    out = res.fetch(_GEM)
    assert out.ok and out.posted == datetime.date(2026, 1, 28)
    assert out.text == (
        "Principal Product Designer\n\nUnited States - Remote\n\nCompany Overview\n"
        "Function Health measures."
    )
    assert asked == [
        "https://api.gem.com/job_board/v0/function-health/job_posts/am9icG9zdDramAtjvaEx5CaiubYzZIC0"
    ]


def test_gem_never_says_gone(monkeypatch):
    res = ats.Gem()
    missing = {"code": 404, "message": "Job post with ID x not found"}
    monkeypatch.setattr(res, "get", lambda u: _Resp(missing, 404))
    assert res.fetch(_GEM).status is ats.Status.ERROR


def test_gem_and_rippling_answer_resolve(monkeypatch):
    monkeypatch.setattr(
        ats.Gem, "fetch", lambda self, url: ats.AtsResult(ats.Status.OK, "t", "gem")
    )
    monkeypatch.setattr(
        ats.Rippling, "fetch", lambda self, url: ats.AtsResult(ats.Status.OK, "t", "rippling")
    )
    assert ats.resolve(_GEM).source == "gem"
    assert ats.resolve(f"https://ats.rippling.com/loop/jobs/{_RIP}").source == "rippling"
