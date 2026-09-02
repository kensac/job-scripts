from __future__ import annotations

import psycopg
import pytest

from api import db
from api.tasks.embeddings import _CANDIDATES, _store
from core.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from core.pricing import estimate_cost_usd

CONTENT = "a long job description " * 20


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


def _vec(*, seed: float = 0.0) -> list[float]:
    v = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
    v[1] = seed
    return v


class TestSchema:
    def test_column_width_matches_the_live_constant(self):
        """The migration carries a frozen literal so it keeps meaning the same
        thing forever; this is what stops that literal drifting away from the
        constant the code embeds against."""
        row = db.query_one(
            "SELECT atttypmod AS dims FROM pg_attribute "
            "WHERE attrelid = 'job_embeddings'::regclass AND attname = 'embedding'"
        )
        assert row is not None
        assert row["dims"] == EMBEDDING_DIMENSIONS

    def test_the_extension_is_actually_installed(self):
        row = db.query_one("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        assert row is not None


class TestCandidateSelection:
    def test_includes_urls_with_no_job_row(self, f):
        f.make_verdict("https://orphan.test/1", "content", "passed", content=CONTENT)
        urls = [r["url"] for r in db.query(_CANDIDATES, {"cap": 100})]
        assert "https://orphan.test/1" in urls

    def test_skips_already_embedded_urls(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        assert url in [r["url"] for r in db.query(_CANDIDATES, {"cap": 100})]
        f.make_embedding(url)
        assert url not in [r["url"] for r in db.query(_CANDIDATES, {"cap": 100})]

    def test_skips_pages_too_short_to_be_a_posting(self, f):
        f.make_verdict("https://stub.test/1", "content", "passed", content="404")
        assert "https://stub.test/1" not in [r["url"] for r in db.query(_CANDIDATES, {"cap": 100})]


class TestStore:
    def test_round_trip_and_price(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        cost = estimate_cost_usd(EMBEDDING_MODEL, 1132, 0)
        _store(
            [
                {
                    "url": url,
                    "embedding": str(_vec()),
                    "model": EMBEDDING_MODEL,
                    "hash": "abc",
                    "row_id": 1,
                    "tokens": 1132,
                    "cost": cost,
                }
            ]
        )
        row = db.query_one("SELECT * FROM job_embeddings WHERE url = %s", (url,))
        assert row is not None
        assert row["model"] == EMBEDDING_MODEL
        assert row["input_tokens"] == 1132
        # Priced at call time through core.pricing, like every other AI spend,
        # and stored at a precision that actually holds the figure: at (12, 6)
        # this assertion fails, because $0.0000226 rounds to $0.000023.
        assert cost is not None
        assert row["cost_usd"] == cost

    def test_re_embedding_replaces_rather_than_raising(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        for h in ("hash-1", "hash-2"):
            _store(
                [
                    {
                        "url": url,
                        "embedding": str(_vec()),
                        "model": EMBEDDING_MODEL,
                        "hash": h,
                        "row_id": 1,
                        "tokens": 1,
                        "cost": None,
                    }
                ]
            )
        rows = db.query("SELECT content_hash FROM job_embeddings WHERE url = %s", (url,))
        assert [r["content_hash"] for r in rows] == ["hash-2"]

    def test_a_wrong_width_vector_is_refused_by_the_column(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        with pytest.raises(psycopg.errors.DataException):
            _store(
                [
                    {
                        "url": url,
                        "embedding": str([1.0, 2.0]),
                        "model": EMBEDDING_MODEL,
                        "hash": "h",
                        "row_id": 1,
                        "tokens": 1,
                        "cost": None,
                    }
                ]
            )


class TestPricing:
    def test_the_embedding_model_has_a_published_price(self):
        assert estimate_cost_usd(EMBEDDING_MODEL, 1_000_000, 0) is not None

    def test_an_embedding_call_is_charged_on_input_only(self):
        """The completion side is 0.00 because an embeddings response has no
        output tokens; a nonzero rate there would invent a charge."""
        only_input = estimate_cost_usd(EMBEDDING_MODEL, 1_000_000, 0)
        with_output = estimate_cost_usd(EMBEDDING_MODEL, 1_000_000, 500_000)
        assert only_input == with_output


class TestSimilarEndpoint:
    def _board(self, client, headers, f, n: int = 3):
        source = f.make_source()
        f.subscribe(_uid(headers), source)
        jobs = []
        for i in range(n):
            job_id, url = f.make_ready_job(source=source, content=CONTENT)
            f.make_embedding(url, seed=i * 0.5)
            jobs.append((job_id, url))
        return jobs

    def test_returns_nearest_first_and_excludes_the_anchor(self, client, user_headers, f):
        jobs = self._board(client, user_headers, f, n=3)
        anchor_id, anchor_url = jobs[0]
        body = client.get(f"/v1/jobs/{anchor_id}/similar", headers=user_headers).json()
        returned = [n["url"] for n in body["neighbours"]]
        assert anchor_url not in returned
        # seed 0.5 is nearer to seed 0.0 than seed 1.0 is.
        assert returned == [jobs[1][1], jobs[2][1]]
        assert body["neighbours"][0]["similarity"] > body["neighbours"][1]["similarity"]

    def test_neighbours_are_confined_to_the_users_visible_slice(
        self, client, user_headers, other_user_headers, f
    ):
        """Otherwise the route becomes a way to read a posting the user could
        not otherwise see - the object-level authorization bug this codebase
        has already shipped once, in a new place."""
        mine = self._board(client, user_headers, f, n=2)
        theirs = self._board(client, other_user_headers, f, n=2)
        body = client.get(f"/v1/jobs/{mine[0][0]}/similar", headers=user_headers).json()
        returned = {n["url"] for n in body["neighbours"]}
        assert returned == {mine[1][1]}
        assert returned.isdisjoint({u for _, u in theirs})

    def test_another_users_job_id_is_not_addressable(
        self, client, user_headers, other_user_headers, f
    ):
        theirs = self._board(client, other_user_headers, f, n=2)
        r = client.get(f"/v1/jobs/{theirs[0][0]}/similar", headers=user_headers)
        # 404 rather than 403: whether the job exists is not the caller's to know.
        assert r.status_code == 404

    def test_a_posting_with_no_vector_yet_says_so(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        job_id, _ = f.make_ready_job(source=source, content=CONTENT)
        r = client.get(f"/v1/jobs/{job_id}/similar", headers=user_headers)
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "NOT_EMBEDDED"

    def test_requires_authentication(self, client, user_headers, f):
        jobs = self._board(client, user_headers, f, n=2)
        assert client.get(f"/v1/jobs/{jobs[0][0]}/similar").status_code in (401, 403)
