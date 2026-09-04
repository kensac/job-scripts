from __future__ import annotations

import hashlib

import pytest

from api import db
from api.tasks.requirements import (
    _CANDIDATES,
    REQUIREMENTS_INPUT_CHARS,
    RequirementsExtract,
    _store,
    _years,
)
from core import skills
from core.requirements import CLEARANCE_LEVELS, DEGREE_LEVELS, MAX_PLAUSIBLE_YOE

CONTENT = "a long job description " * 20


def _candidates(cap: int = 100) -> list[str]:
    return [r["url"] for r in db.query(_CANDIDATES, {"cap": cap})]


class TestSkillCanonicalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Python", "python"),
            ("  PYTHON  ", "python"),
            ("JS", "javascript"),
            ("K8s", "kubernetes"),
            ("Postgres", "postgresql"),
            ("Node.js", "node.js"),
            ("NodeJS", "node.js"),
            ("C++", "c++"),
            ("C#", "c#"),
            (".NET Core", ".net"),
            ("Python 3", "python"),
            ("Kubernetes (EKS)", "kubernetes"),
            ("Experience with Plaxis", "plaxis"),
            ("Strong hands-on experience with Terraform", "terraform"),
            ("Knowledge of SQL", "sql"),
        ],
    )
    def test_collapses_spellings(self, raw, expected):
        assert skills.canonical(raw) == expected

    @pytest.mark.parametrize("raw", ["communication", "Teamwork", "Attention to detail", "", "   "])
    def test_drops_non_skills(self, raw):
        assert skills.canonical(raw) == ""

    def test_drops_sentences_rather_than_bucketing_them(self):
        sentence = "a demonstrated ability to work across many teams and time zones"
        assert len(sentence) > skills.MAX_SKILL_CHARS
        assert skills.canonical(sentence) == ""

    def test_lead_in_and_bare_name_land_together(self):
        assert skills.canonical("Experience with Kubernetes") == skills.canonical("Kubernetes")


class TestYearsNormalisation:
    def test_floor_only(self):
        assert _years(RequirementsExtract(has_requirements=True, yoe_min=3)) == (3, None)

    def test_range(self):
        assert _years(RequirementsExtract(has_requirements=True, yoe_min=3, yoe_max=5)) == (3, 5)

    def test_unstated_stays_unstated(self):
        assert _years(RequirementsExtract(has_requirements=True)) == (None, None)

    def test_ceiling_without_floor_gets_a_zero_floor(self):
        """'0-3 years' comes back as a max alone often enough to matter, and a
        NULL floor would hide the posting from every "roles I qualify for"
        comparison, which is the opposite of what the posting says."""
        assert _years(RequirementsExtract(has_requirements=True, yoe_max=3)) == (0, 3)

    def test_inverted_range_is_ordered(self):
        assert _years(RequirementsExtract(has_requirements=True, yoe_min=5, yoe_max=2)) == (2, 5)

    def test_implausible_floor_is_dropped_not_stored(self):
        absurd = MAX_PLAUSIBLE_YOE + 1
        assert _years(RequirementsExtract(has_requirements=True, yoe_min=absurd)) == (None, None)


class TestStore:
    def test_round_trip(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        _store(
            url,
            RequirementsExtract(
                has_requirements=True,
                yoe_min=2,
                degree_min="bachelors",
                degree_required=True,
                degree_fields=["Computer Science"],
                seniority="entry",
                employment_type="full_time",
                clearance="secret",
                sponsorship="not_offered",
                skills_required=["Python", "Experience with Kubernetes"],
                skills_preferred=["Go"],
            ),
            "hash",
            1,
        )
        row = db.query_one("SELECT * FROM job_requirements WHERE url = %s", (url,))
        assert row is not None
        assert (row["yoe_min"], row["yoe_max"]) == (2, None)
        assert row["degree_min"] == "bachelors"
        assert row["clearance"] == "secret"
        assert row["degree_fields"] == ["Computer Science"]
        rows = db.query(
            "SELECT kind, skill, skill_raw FROM job_skills WHERE url = %s ORDER BY skill", (url,)
        )
        assert [(r["kind"], r["skill"]) for r in rows] == [
            ("preferred", "go"),
            ("required", "kubernetes"),
            ("required", "python"),
        ]
        # The raw text survives beside the canonical form, so a better alias
        # table is an UPDATE rather than another paid extraction.
        assert {r["skill_raw"] for r in rows} == {"Python", "Experience with Kubernetes", "Go"}

    def test_out_of_vocabulary_becomes_unstated_not_its_own_bucket(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        _store(
            url,
            RequirementsExtract(
                has_requirements=True, degree_min="Bachelors Degree", clearance="very secret"
            ),
            "hash",
            1,
        )
        row = db.query_one(
            "SELECT degree_min, clearance FROM job_requirements WHERE url = %s", (url,)
        )
        assert row is not None
        assert row["degree_min"] is None
        assert row["clearance"] is None

    def test_a_page_with_no_requirements_stores_nothing_but_the_fact(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        _store(
            url,
            RequirementsExtract(
                has_requirements=False, yoe_min=3, degree_min="phd", skills_required=["Python"]
            ),
            "hash",
            1,
        )
        row = db.query_one("SELECT * FROM job_requirements WHERE url = %s", (url,))
        assert row is not None
        assert row["has_requirements"] is False
        assert row["yoe_min"] is None and row["degree_min"] is None
        assert db.query("SELECT 1 FROM job_skills WHERE url = %s", (url,)) == []

    def test_re_extraction_replaces_skills_rather_than_accumulating(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        _store(
            url,
            RequirementsExtract(has_requirements=True, skills_required=["Python", "Perl"]),
            "hash-1",
            1,
        )
        _store(
            url,
            RequirementsExtract(has_requirements=True, skills_required=["Python"]),
            "hash-2",
            2,
        )
        rows = db.query("SELECT skill FROM job_skills WHERE url = %s", (url,))
        # Perl is gone because the posting no longer asks for it; a left-over
        # row would keep answering the market query with a dead requirement.
        assert [r["skill"] for r in rows] == ["python"]
        row = db.query_one("SELECT content_hash FROM job_requirements WHERE url = %s", (url,))
        assert row is not None and row["content_hash"] == "hash-2"

    def test_two_spellings_of_one_skill_do_not_collide(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        _store(
            url,
            RequirementsExtract(has_requirements=True, skills_required=["Python", "python"]),
            "hash",
            1,
        )
        rows = db.query("SELECT skill, skill_raw FROM job_skills WHERE url = %s", (url,))
        assert {r["skill"] for r in rows} == {"python"}
        assert len(rows) == 2


class TestCandidateSelection:
    def test_includes_urls_with_no_job_row(self, f):
        """The whole reason this is url-keyed. A quarter of the corpus is
        postings whose job row is gone and whose page can never be re-scraped;
        a job-keyed sweep would silently skip them."""
        f.make_verdict("https://orphan.test/1", "content", "passed", content=CONTENT)
        assert "https://orphan.test/1" in _candidates()

    def test_skips_urls_already_extracted(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        assert url in _candidates()
        f.make_requirements(url)
        assert url not in _candidates()

    def test_skips_pages_too_short_to_be_a_posting(self, f):
        f.make_verdict("https://stub.test/1", "content", "passed", content="404")
        assert "https://stub.test/1" not in _candidates()

    def test_prefers_the_raw_content_row_over_a_checks_copy(self, f):
        url = "https://prefer.test/1"
        f.make_verdict(url, "content", "passed", content="RAW PAGE " * 40)
        f.make_verdict(url, "closed", "passed", content="CHECK COPY " * 40)
        row = db.query(_CANDIDATES, {"cap": 100})
        picked = next(r for r in row if r["url"] == url)
        assert picked["input_content"].startswith("RAW PAGE")

    def test_respects_the_cap(self, f):
        for i in range(4):
            f.make_verdict(f"https://capped.test/{i}", "content", "passed", content=CONTENT)
        assert len(_candidates(cap=2)) == 2


class TestContentLateralParity:
    def test_every_sweep_reads_the_same_page_for_a_url(self, f):
        """comp, verify and requirements each pick "the page text for this url".
        They carried three copies of that decision and one had already drifted
        from get_content; this pins them to one answer."""
        from api.tasks.comp import EXTRACT_COMP_PER_CYCLE  # noqa: F401
        from core.store import CONTENT_LATERAL

        job_id, url = f.make_ready_job(content="RAW PAGE " * 40)
        f.make_verdict(url, "closed", "passed", content="CHECK COPY " * 40)
        for source, expr in (("jobs j", "j.url"), ("(SELECT %(u)s::text AS url) c", "c.url")):
            rows = db.query(
                f"SELECT q.input_content FROM {source} "
                f"{CONTENT_LATERAL.format(url=expr, columns='input_content')} "
                f"WHERE {expr} = %(u)s",
                {"u": url},
            )
            assert len(rows) == 1
            assert rows[0]["input_content"].startswith("RAW PAGE")
        assert job_id


class TestMarketEndpoint:
    def test_counts_only_jobs_this_user_can_see(self, client, user_headers, other_user_headers, f):
        source = f.make_source()
        mine, my_url = f.make_ready_job(source=source, content=CONTENT)
        _, their_url = f.make_ready_job(source=f.make_source(), content=CONTENT)
        f.subscribe(_uid(user_headers), source)
        f.make_requirements(my_url, skills_required=["Python"])
        f.make_requirements(their_url, skills_required=["Python", "Rust"])

        body = client.get("/v1/requirements/market", headers=user_headers).json()
        assert body["postings"] == 1
        assert [s["skill"] for s in body["skills"]["required"]] == ["python"]
        assert mine

    def test_empty_slice_answers_zero_rather_than_erroring(self, client, user_headers):
        body = client.get("/v1/requirements/market", headers=user_headers).json()
        assert body["postings"] == 0
        assert body["skills"]["required"] == []

    def test_pages_that_state_nothing_are_excluded_from_the_market(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        _, stated = f.make_ready_job(source=source, content=CONTENT)
        _, silent = f.make_ready_job(source=source, content=CONTENT)
        f.make_requirements(stated, skills_required=["Python"])
        f.make_requirements(silent, has_requirements=False)
        body = client.get("/v1/requirements/market", headers=user_headers).json()
        assert body["postings"] == 1

    def test_slices_by_seniority(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        _, junior = f.make_ready_job(source=source, content=CONTENT)
        _, senior = f.make_ready_job(source=source, content=CONTENT)
        f.make_requirements(junior, seniority="entry", skills_required=["Python"])
        f.make_requirements(senior, seniority="senior", skills_required=["Rust"])
        body = client.get(
            "/v1/requirements/market", headers=user_headers, params={"seniority": "senior"}
        ).json()
        assert body["postings"] == 1
        assert [s["skill"] for s in body["skills"]["required"]] == ["rust"]

    def test_unstated_is_reported_apart_from_stated(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        _, with_degree = f.make_ready_job(source=source, content=CONTENT)
        _, without = f.make_ready_job(source=source, content=CONTENT)
        f.make_requirements(with_degree, degree_min="bachelors", degree_required=True)
        f.make_requirements(without)
        body = client.get("/v1/requirements/market", headers=user_headers).json()
        assert body["postings"] == 2
        assert body["flags"]["states_degree"] == 1
        assert sum(d["postings"] for d in body["degree"]) == 1


class TestSettingsRejectsBackground:
    """The column and its endpoint are gone (#301); a client still sending it
    must be told, not answered 200 for a write that stored nothing."""

    def test_a_background_key_is_rejected_not_ignored(self, client, user_headers):
        r = client.put("/v1/user/settings", headers=user_headers, json={"background": {"yoe": 3}})
        assert r.status_code == 422

    def test_the_settings_response_has_no_background(self, client, user_headers):
        client.put("/v1/user/settings", headers=user_headers, json={"email_digest": True})
        body = client.get("/v1/user/settings", headers=user_headers).json()
        assert "background" not in body

    def test_the_gap_endpoint_is_gone(self, client, user_headers):
        assert client.get("/v1/requirements/gap", headers=user_headers).status_code == 404


class TestVocabularies:
    def test_levels_are_ordered_floors(self):
        assert DEGREE_LEVELS.index("bachelors") < DEGREE_LEVELS.index("phd")
        assert CLEARANCE_LEVELS.index("secret") < CLEARANCE_LEVELS.index("ts_sci")


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


class TestRescrapedPages:
    """The bug this closes: content_hash was written and read by nothing, so a
    posting scraped again kept its first extraction forever."""

    def _extracted(self, f, url: str, row_id: int | None):
        _store(
            url,
            RequirementsExtract(has_requirements=True, skills_required=["Perl"]),
            "hash-of-the-old-page",
            row_id,
        )

    def _current_row(self, url: str) -> int:
        row = db.query_one(
            "SELECT id FROM ai_queries WHERE url = %s AND input_content IS NOT NULL "
            "AND length(input_content) > 200 "
            "ORDER BY (check_type = 'content') DESC, id DESC LIMIT 1",
            (url,),
        )
        assert row is not None
        return row["id"]

    def test_a_rescraped_page_is_extracted_again(self, f):
        _, url = f.make_ready_job(content=CONTENT)
        self._extracted(f, url, self._current_row(url))
        assert url not in _candidates()
        # The page is scraped again with different text.
        f.make_verdict(url, "content", "passed", content="a DIFFERENT description " * 20)
        assert url in _candidates()

    def test_an_unchanged_rescrape_is_not_paid_for_again(self, f):
        """A re-scrape that changed nothing is the common case, and the id
        moving is not evidence the text did. Re-extracting on the id alone
        would re-pay for the catalog every time a refresh ran."""
        from api.tasks.requirements import _drop_unchanged_rescrapes

        _, url = f.make_ready_job(content=CONTENT)
        rows = db.query(_CANDIDATES, {"cap": 10})
        row = next(r for r in rows if r["url"] == url)
        _store(
            url,
            RequirementsExtract(has_requirements=True),
            hashlib.sha256(row["input_content"][:REQUIREMENTS_INPUT_CHARS].encode()).hexdigest(),
            row["content_row_id"],
        )
        # Same text arrives again under a new row id.
        f.make_verdict(url, "content", "passed", content=CONTENT)
        candidates = db.query(_CANDIDATES, {"cap": 10})
        assert url in [r["url"] for r in candidates], "the newer row makes it a candidate"
        assert url not in [r["url"] for r in _drop_unchanged_rescrapes(candidates)]

    def test_an_unchanged_rescrape_stops_coming_back(self, f):
        """Re-stamped rather than merely skipped, or it is re-examined every
        cycle forever."""
        from api.tasks.requirements import _drop_unchanged_rescrapes

        _, url = f.make_ready_job(content=CONTENT)
        rows = db.query(_CANDIDATES, {"cap": 10})
        row = next(r for r in rows if r["url"] == url)
        _store(
            url,
            RequirementsExtract(has_requirements=True),
            hashlib.sha256(row["input_content"][:REQUIREMENTS_INPUT_CHARS].encode()).hexdigest(),
            row["content_row_id"],
        )
        f.make_verdict(url, "content", "passed", content=CONTENT)
        _drop_unchanged_rescrapes(db.query(_CANDIDATES, {"cap": 10}))
        assert url not in _candidates()

    def test_a_row_that_does_not_know_its_page_is_re_read_once(self, f):
        """Every row predating this migration has content_row_id NULL, which
        reads as "we do not know which page this came from". Guessing a row
        would pin a stale answer permanently; re-reading once costs one pass.
        """
        _, url = f.make_ready_job(content=CONTENT)
        self._extracted(f, url, None)
        assert url in _candidates()
