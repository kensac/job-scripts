from __future__ import annotations

import pytest

from api import db
from api.tasks.requirements import (
    _CANDIDATES,
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
        )
        _store(
            url, RequirementsExtract(has_requirements=True, skills_required=["Python"]), "hash-2"
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
                f"{CONTENT_LATERAL.format(url=expr)} "
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


class TestGapEndpoint:
    def _slice(self, user_headers, f, n_python: int = 2, n_rust: int = 1):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        urls = []
        for i in range(n_python + n_rust):
            _, url = f.make_ready_job(source=source, content=CONTENT)
            f.make_requirements(url, skills_required=["Python"] if i < n_python else ["Rust"])
            urls.append(url)
        return source, urls

    def _set_background(self, client, headers, background):
        r = client.put("/v1/user/settings", headers=headers, json={"background": background})
        assert r.status_code == 200, r.text

    def test_splits_the_market_by_what_the_user_has(self, client, user_headers, f):
        self._slice(user_headers, f)
        self._set_background(client, user_headers, {"skills": ["python"]})
        body = client.get("/v1/requirements/gap", headers=user_headers).json()
        assert [s["skill"] for s in body["matching_skills"]] == ["python"]
        assert [s["skill"] for s in body["missing_skills"]] == ["rust"]

    def test_matches_on_canonical_form_not_the_users_spelling(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        _, url = f.make_ready_job(source=source, content=CONTENT)
        f.make_requirements(url, skills_required=["Kubernetes"])
        self._set_background(client, user_headers, {"skills": ["K8s"]})
        body = client.get("/v1/requirements/gap", headers=user_headers).json()
        assert [s["skill"] for s in body["matching_skills"]] == ["kubernetes"]
        assert body["missing_skills"] == []

    def test_names_skills_the_market_never_asks_for(self, client, user_headers, f):
        self._slice(user_headers, f, n_rust=0)
        self._set_background(client, user_headers, {"skills": ["python", "COBOL"]})
        body = client.get("/v1/requirements/gap", headers=user_headers).json()
        assert body["unused_skills"] == ["cobol"]

    def test_counts_postings_that_ask_for_more_years(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        for years in (1, 5, None):
            _, url = f.make_ready_job(source=source, content=CONTENT)
            f.make_requirements(url, yoe_min=years)
        self._set_background(client, user_headers, {"yoe": 2})
        body = client.get("/v1/requirements/gap", headers=user_headers).json()
        # Only the 5-year posting is out of reach. The silent one is NOT
        # counted as a shortfall: saying nothing is not saying no.
        assert body["blockers"]["years_short"] == 1
        assert body["blockers"]["years_max_asked"] == 5

    def test_degree_gap_compares_levels_not_strings(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        for level in ("high_school", "bachelors", "phd"):
            _, url = f.make_ready_job(source=source, content=CONTENT)
            f.make_requirements(url, degree_min=level, degree_required=True)
        self._set_background(client, user_headers, {"degree": "bachelors"})
        body = client.get("/v1/requirements/gap", headers=user_headers).json()
        assert body["blockers"]["degree_short"] == 1

    def test_a_preferred_degree_is_not_a_gap(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        _, url = f.make_ready_job(source=source, content=CONTENT)
        f.make_requirements(url, degree_min="phd", degree_required=False)
        self._set_background(client, user_headers, {"degree": "bachelors"})
        body = client.get("/v1/requirements/gap", headers=user_headers).json()
        assert body["blockers"]["degree_short"] == 0

    def test_clearance_and_authorisation_blockers(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        _, cleared = f.make_ready_job(source=source, content=CONTENT)
        _, citizens = f.make_ready_job(source=source, content=CONTENT)
        _, no_visa = f.make_ready_job(source=source, content=CONTENT)
        f.make_requirements(cleared, clearance="top_secret")
        f.make_requirements(citizens, citizenship_required=True)
        f.make_requirements(no_visa, sponsorship="not_offered")
        self._set_background(
            client,
            user_headers,
            {"clearance": "none", "citizen": False, "needs_sponsorship": True},
        )
        blockers = client.get("/v1/requirements/gap", headers=user_headers).json()["blockers"]
        assert blockers["clearance_short"] == 1
        assert blockers["citizenship_blocked"] == 1
        assert blockers["sponsorship_blocked"] == 1

    def test_an_unset_background_field_measures_nothing(self, client, user_headers, f):
        source = f.make_source()
        f.subscribe(_uid(user_headers), source)
        _, url = f.make_ready_job(source=source, content=CONTENT)
        f.make_requirements(url, yoe_min=9, degree_min="phd", degree_required=True)
        self._set_background(client, user_headers, {"skills": ["python"]})
        blockers = client.get("/v1/requirements/gap", headers=user_headers).json()["blockers"]
        # Not "you pass", but "you did not say" - a zero here would read as
        # reachable and is exactly the claim the data cannot support.
        assert blockers["years_short"] == 0
        assert blockers["degree_short"] == 0

    def test_does_not_leak_another_users_slice(self, client, user_headers, other_user_headers, f):
        self._slice(other_user_headers, f)
        self._set_background(client, user_headers, {"skills": ["python"]})
        body = client.get("/v1/requirements/gap", headers=user_headers).json()
        assert body["postings"] == 0
        assert body["missing_skills"] == []


class TestBackgroundSettings:
    def test_round_trips_through_settings(self, client, user_headers):
        r = client.put(
            "/v1/user/settings",
            headers=user_headers,
            json={"background": {"yoe": 3, "degree": "Bachelors", "skills": ["Python"]}},
        )
        assert r.status_code == 200
        body = client.get("/v1/user/settings", headers=user_headers).json()
        assert body["background"]["yoe"] == 3
        assert body["background"]["degree"] == "bachelors"

    def test_rejects_a_level_outside_the_vocabulary(self, client, user_headers):
        r = client.put(
            "/v1/user/settings", headers=user_headers, json={"background": {"degree": "wizardry"}}
        )
        assert r.status_code == 422

    def test_other_settings_survive_a_background_write(self, client, user_headers):
        client.put("/v1/user/settings", headers=user_headers, json={"email_digest": True})
        client.put("/v1/user/settings", headers=user_headers, json={"background": {"yoe": 1}})
        body = client.get("/v1/user/settings", headers=user_headers).json()
        assert body["email_digest"] is True
        assert body["background"]["yoe"] == 1


class TestVocabularies:
    def test_levels_are_ordered_floors(self):
        assert DEGREE_LEVELS.index("bachelors") < DEGREE_LEVELS.index("phd")
        assert CLEARANCE_LEVELS.index("secret") < CLEARANCE_LEVELS.index("ts_sci")


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]
