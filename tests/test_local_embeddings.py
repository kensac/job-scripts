"""Postings embedded with the model the fleet serves itself
(core.derivations.local_embeddings, tasks.derivations.local_embeddings)."""

from __future__ import annotations

import asyncio
import math
import threading
from typing import Any

import pytest

from api import db
from core.derivations import local_embeddings as core
from tasks.derivations import local_embeddings
from tests.factories import make_task

CONTENT = "a long job description " * 20
DIMS = core.LOCAL_EMBEDDING_DIMENSIONS


class FakeServer:
    """llama-server's /props, /tokenize, /detokenize and /v1/embeddings: one
    token per word, and a vector whose first component is the chunk's token
    count. `down` names servers that refuse connections."""

    def __init__(self, down: frozenset[str] = frozenset(), slots: int = 2):
        self.embedded: list[str] = []
        self.down = down
        self.slots = slots

    def request(self, method, url, timeout, json=None):
        server, path = url.rsplit("/", 1)[0], url.split("/", 3)[-1]
        if any(url.startswith(d) for d in self.down):
            raise core.requests.ConnectionError(f"{server} refused")
        if path == "props":
            return _Response({"total_slots": self.slots})
        return self.post(url, json, timeout)

    def post(self, url, json, timeout):
        path = url.split("/", 3)[-1]
        if path == "tokenize":
            body = {"tokens": list(range(len(json["content"].split())))}
        elif path == "detokenize":
            body = {"content": " ".join("w" for _ in json["tokens"])}
        else:
            self.embedded.extend(json["input"])
            body = {
                "data": [
                    {"index": i, "embedding": [float(len(t.split())), 1.0] + [0.0] * (DIMS - 2)}
                    for i, t in enumerate(json["input"])
                ]
            }
        return _Response(body)


class _Response:
    def __init__(self, body: Any, status_code: int = 200):
        self.body: Any = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise core.requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.body


def test_a_long_posting_is_embedded_whole_in_weighted_chunks(monkeypatch):
    monkeypatch.setattr(core, "CHUNK_TOKENS", 10)
    server = FakeServer()
    embedded = core.embed_document(server, "http://emb", "word " * 25)

    assert embedded.chunks == 3
    assert all(text.startswith(core.DOCUMENT_PREFIX) for text in server.embedded)
    # Chunks of 10, 10 and 5 tokens, each embedded with the one-word prefix,
    # so the fake's first component reads 11, 11 and 6: weighted by the
    # chunks' token counts.
    first = (10 * 11 + 10 * 11 + 5 * 6) / 25
    norm = math.hypot(first, 1.0)
    assert embedded.vector[:2] == pytest.approx([first / norm, 1.0 / norm])


def test_a_vector_of_another_width_is_refused():
    class Narrow(FakeServer):
        def post(self, url, json, timeout):
            response = super().post(url, json, timeout)
            if url.endswith("/v1/embeddings"):
                response.body["data"][0]["embedding"] = [1.0, 2.0]
            return response

    with pytest.raises(core.EmbeddingUnavailable):
        core.embed_document(Narrow(), "http://emb", "a few words")


def test_the_column_width_is_the_models():
    row = db.query_one(
        "SELECT atttypmod AS dims FROM pg_attribute "
        "WHERE attrelid = 'posting_embeddings'::regclass AND attname = 'embedding'"
    )
    assert row == {"dims": DIMS}


def test_a_host_without_a_server_never_claims_the_kind():
    from api.worker_lifecycle import claims

    assert "embed_postings_local" in claims.unmet_needs({})
    assert "embed_postings_local" not in claims.unmet_needs({core.URLS_ENV: "http://emb:8080"})


def _run(monkeypatch, server) -> int:
    monkeypatch.setenv(core.URLS_ENV, "http://emb-a,http://emb-b")
    monkeypatch.setattr(local_embeddings, "_session", lambda: server)
    task_id = make_task(local_embeddings.KIND, status="running")
    asyncio.run(local_embeddings.handle_embed_postings_local(task_id, {}))
    return task_id


def test_a_visible_posting_is_embedded_once_and_an_unchanged_rescrape_is_restamped(f, monkeypatch):
    job_id, url = f.make_ready_job(content=CONTENT)
    f.upload(job_id, f.make_user())
    server = FakeServer()
    _run(monkeypatch, server)

    row = db.query_one(
        "SELECT model, content_row_id FROM posting_embeddings WHERE url = %s", (url,)
    )
    assert row is not None and row["model"] == core.LOCAL_EMBEDDING_MODEL
    calls = len(server.embedded)

    f.make_fetch(url, content=CONTENT)
    _run(monkeypatch, server)
    assert len(server.embedded) == calls, "an unchanged page is not embedded again"
    restamped = db.query_one("SELECT content_row_id FROM posting_embeddings WHERE url = %s", (url,))
    assert restamped is not None and restamped["content_row_id"] > row["content_row_id"]


def test_a_posting_every_server_errors_on_fails_for_the_run_only(f, monkeypatch):
    _, good = f.make_ready_job(content=CONTENT)
    _, bad = f.make_ready_job(content=CONTENT + " unembeddable")
    broken = {"on": True}

    class RefusesOne(FakeServer):
        def post(self, url, json, timeout):
            if broken["on"] and url.endswith("/tokenize") and "unembeddable" in json["content"]:
                return _Response({"error": "bad input"}, status_code=500)
            return super().post(url, json, timeout)

    monkeypatch.setenv(core.URLS_ENV, "http://emb-a,http://emb-b")
    monkeypatch.setattr(local_embeddings, "_session", RefusesOne)
    counts = local_embeddings.run(make_task(local_embeddings.KIND, status="running"), cap=2000)
    assert counts["written"] == 1 and counts["failed"] == 1
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (good,))

    broken["on"] = False
    again = local_embeddings.run(make_task(local_embeddings.KIND, status="running"), cap=2000)
    assert again["written"] == 1, "the next run tries it again"
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (bad,))


def test_a_misconfigured_server_hands_its_postings_to_the_others(f, monkeypatch):
    urls = []
    for _ in range(6):
        job_id, url = f.make_ready_job(content=CONTENT)
        f.upload(job_id, f.make_user())
        urls.append(url)

    class OneBroken(FakeServer):
        def post(self, url, json, timeout):
            if url.startswith("http://emb-b"):
                return _Response({"error": "context too small"}, status_code=500)
            return super().post(url, json, timeout)

    task_id = _run(monkeypatch, OneBroken())
    stored = db.query("SELECT url FROM posting_embeddings WHERE url = ANY(%s)", (urls,))
    assert len(stored) == 6
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["failed"] == 0


def test_a_posting_two_servers_go_down_on_fails_without_stopping_the_run(f, monkeypatch):
    _, good = f.make_ready_job(content=CONTENT)
    _, killer = f.make_ready_job(content=CONTENT + " crashes the server")

    class Crashes(FakeServer):
        def post(self, url, json, timeout):
            if url.endswith("/tokenize") and "crashes" in json["content"]:
                raise core.requests.ConnectionError("server died")
            return super().post(url, json, timeout)

    monkeypatch.setenv(core.URLS_ENV, "http://emb-a,http://emb-b,http://emb-c")
    monkeypatch.setattr(local_embeddings, "_session", Crashes)
    counts = local_embeddings.run(make_task(local_embeddings.KIND, status="running"), cap=2000)
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (good,))
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (killer,)) is None
    assert counts["failed"] == 1


def test_a_page_whose_text_cannot_be_read_fails_alone(f, monkeypatch):
    _, good = f.make_ready_job(content=CONTENT)
    _, unreadable = f.make_ready_job(content=CONTENT + " lost")
    resolve = local_embeddings.page_fetches.resolve

    def flaky(rows, **kwargs):
        if any(row.get("url") == unreadable for row in rows):
            raise local_embeddings.PayloadUnavailable("bundle missing")
        return resolve(rows, **kwargs)

    monkeypatch.setattr(local_embeddings.page_fetches, "resolve", flaky)
    monkeypatch.setenv(core.URLS_ENV, "http://emb-a")
    monkeypatch.setattr(local_embeddings, "_session", FakeServer)
    counts = local_embeddings.run(make_task(local_embeddings.KIND, status="running"), cap=2000)
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (good,))
    assert counts["failed"] == 1


def test_object_storage_not_answering_fails_the_run_rather_than_the_pages(monkeypatch):
    def down(rows, **kwargs):
        try:
            raise ConnectionError("storage unreachable")
        except ConnectionError as exc:
            raise local_embeddings.PayloadUnavailable("unavailable") from exc

    monkeypatch.setattr(local_embeddings.page_fetches, "resolve", down)
    with pytest.raises(local_embeddings.PayloadUnavailable):
        local_embeddings._readable([{"url": "u1"}, {"url": "u2"}])


def test_a_missing_bundle_member_is_the_pages_fault():
    missing = local_embeddings.PayloadUnavailable("Payload bundle has no such member")
    assert local_embeddings._page_fault(missing)
    try:
        raise TimeoutError("read timed out")
    except TimeoutError as exc:
        try:
            raise local_embeddings.PayloadUnavailable("unavailable") from exc
        except local_embeddings.PayloadUnavailable as outage:
            assert not local_embeddings._page_fault(outage)


def test_an_unreadable_profile_sample_is_left_out_of_the_profile(f, monkeypatch):
    urls = []
    for role in ["Backend Engineer", "Account Executive", "Data Analyst"]:
        job_id, url = f.make_ready_job(content=_posting(role) * 2)
        db.execute(
            "UPDATE jobs SET company = 'Acme Robotics', title = %s WHERE id = %s", (role, job_id)
        )
        urls.append(url)
    resolve = local_embeddings.page_fetches.resolve

    def one_lost(rows, **kwargs):
        if any(row.get("title") == "Data Analyst" for row in rows):
            raise local_embeddings.PayloadUnavailable("bundle member missing")
        return resolve(rows, **kwargs)

    monkeypatch.setattr(local_embeddings.page_fetches, "resolve", one_lost)
    local_embeddings.refresh_profiles({"acme robotics"})
    profile = db.query_one(
        "SELECT titles FROM company_text_profiles WHERE company_key = 'acme robotics'"
    )
    assert profile is not None


def test_a_props_answer_that_is_not_llama_servers_leaves_the_server_out(f, monkeypatch):
    _, url = f.make_ready_job(content=CONTENT)

    class Impostor(FakeServer):
        def request(self, method, url, timeout, json=None):
            if url.startswith("http://emb-b") and url.endswith("/props"):
                return _Response([])
            return super().request(method, url, timeout, json=json)

    task_id = _run(monkeypatch, Impostor())
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["unreachable"] == ["http://emb-b"]
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (url,))


def test_a_server_that_does_not_answer_is_left_out_and_its_postings_go_to_the_others(
    f, monkeypatch
):
    urls = []
    for _ in range(3):
        job_id, url = f.make_ready_job(content=CONTENT)
        f.upload(job_id, f.make_user())
        urls.append(url)
    task_id = _run(monkeypatch, FakeServer(down=frozenset({"http://emb-b"})))
    stored = db.query("SELECT url FROM posting_embeddings WHERE url = ANY(%s)", (urls,))
    assert len(stored) == 3
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["servers"] == ["http://emb-a"]
    assert progress["unreachable"] == ["http://emb-b"]
    assert progress["failed"] == 0


def test_a_server_that_stops_answering_hands_back_its_posting(f, monkeypatch):
    urls = []
    for _ in range(4):
        job_id, url = f.make_ready_job(content=CONTENT)
        f.upload(job_id, f.make_user())
        urls.append(url)

    slept = threading.Event()

    class Sleeps(FakeServer):
        """emb-b reports its slots, then goes to sleep holding a posting;
        emb-a waits for that, so emb-b has taken one before the queue drains."""

        def post(self, url, json, timeout):
            if url.startswith("http://emb-b"):
                slept.set()
                raise core.requests.ConnectionError("asleep")
            assert slept.wait(timeout=10)
            return super().post(url, json, timeout)

    task_id = _run(monkeypatch, Sleeps())
    stored = db.query("SELECT url FROM posting_embeddings WHERE url = ANY(%s)", (urls,))
    assert len(stored) == 4, "every posting emb-b took was embedded on emb-a"
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["failed"] == 0
    assert progress["unreachable"] == ["http://emb-b"]


def test_no_server_answering_fails_the_run(f, monkeypatch):
    job_id, _ = f.make_ready_job(content=CONTENT)
    f.upload(job_id, f.make_user())
    with pytest.raises(core.EmbeddingUnavailable):
        _run(monkeypatch, FakeServer(down=frozenset({"http://emb-a", "http://emb-b"})))


def test_one_run_drains_the_backlog_visible_postings_first(f, monkeypatch):
    visible, visible_url = f.make_ready_job(content=CONTENT + " visible")
    f.upload(visible, f.make_user())
    hidden = [f.make_ready_job(content=CONTENT + f" hidden {i}")[1] for i in range(3)]
    monkeypatch.setenv(core.URLS_ENV, "http://emb-a")
    monkeypatch.setattr(local_embeddings, "_session", FakeServer)
    batches: list[list[str]] = []
    select = local_embeddings.candidates

    def recorded(cap, below, skip=None):
        rows, unreadable, exhausted = select(cap, below, skip)
        batches.append([row["url"] for row in rows])
        return rows, unreadable, exhausted

    monkeypatch.setattr(local_embeddings, "candidates", recorded)

    counts = local_embeddings.run(make_task(local_embeddings.KIND, status="running"), cap=1)
    stored = {r["url"] for r in db.query("SELECT url FROM posting_embeddings")}
    assert stored == {visible_url, *hidden}
    assert batches[0] == [visible_url], "the visible posting leads, though it is the oldest"
    assert batches[1:4] == [[u] for u in reversed(hidden)], "then the rest, newest first"
    # One batch a posting, and a last one that finds nothing.
    assert counts["batches"] == 5 and counts["written"] == 4


def test_a_posting_nobody_can_see_is_embedded_once(f, monkeypatch):
    _, url = f.make_ready_job(content=CONTENT)
    server = FakeServer()
    _run(monkeypatch, server)
    calls = len(server.embedded)
    assert calls
    f.make_fetch(url, content=CONTENT + " rewritten")
    _run(monkeypatch, server)
    assert len(server.embedded) == calls, "a later page of an unseen posting is not re-selected"


def test_a_server_that_times_out_hands_its_posting_back(f, monkeypatch):
    _, url = f.make_ready_job(content=CONTENT)

    class Stalls(FakeServer):
        def post(self, url, json, timeout):
            if url.startswith("http://emb-b"):
                raise core.requests.ReadTimeout("no answer")
            return super().post(url, json, timeout)

    monkeypatch.setenv(core.URLS_ENV, "http://emb-b,http://emb-a")
    monkeypatch.setattr(local_embeddings, "_session", Stalls)
    counts = local_embeddings.run(make_task(local_embeddings.KIND, status="running"), cap=2000)
    assert counts["failed"] == 0
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (url,))


def test_switched_off_embeds_nothing(f, monkeypatch, set_config):
    job_id, url = f.make_ready_job(content=CONTENT)
    f.upload(job_id, f.make_user())
    set_config("local_embedding_enabled", False)
    _run(monkeypatch, FakeServer())
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (url,)) is None


ABOUT = "Acme Robotics builds warehouse robots used by the largest retailers in the world."
EEO = "Acme Robotics is an equal opportunity employer and values a diverse workforce."


def _posting(role: str) -> str:
    return f"{role} at Acme Robotics\nYou will {role.lower()} every day, owning the {role} roadmap and its delivery.\n{ABOUT}\n{EEO}"


def test_paragraphs_a_company_repeats_across_roles_are_shared_and_one_roles_are_not():
    from core.derivations import company_text

    roles = ["Backend Engineer", "Account Executive", "Data Analyst", "Recruiter"]
    postings = [(r, _posting(r)) for r in roles] + [
        ("Backend Engineer", _posting("Backend Engineer"))
    ]
    titles, shared = company_text.shared_paragraphs("Acme Robotics, Inc.", postings)

    assert titles == 4
    assert {company_text.paragraph_key(ABOUT), company_text.paragraph_key(EEO)} <= shared
    own = company_text.paragraph_key(
        "You will backend engineer every day, owning the Backend Engineer roadmap and its delivery."
    )
    assert own not in shared, "a repost repeats its own role's text; that is not the company's"

    text = company_text.embedding_text(_posting("Data Analyst") * 3, "Acme Robotics, Inc.", shared)
    assert ABOUT not in text and EEO not in text
    assert "acme" not in text.lower() and "the company" in text
    assert "warehouse" not in text, "the shared about-us paragraph is gone"


LICENSE = "RN - Registered Nurse - State Licensure required for this position."
BENEFITS = "Medical, dental and vision coverage from your first day, and a matched 401k."


def test_a_role_requirement_repeated_by_a_few_titles_is_kept_and_unnamed_company_text_is_not():
    """A hospital's nurse titles share their licensure line; it names no
    company and repeats under a few of its titles, so it is the role's. The
    benefits line repeats under most of them without naming the company,
    and is the company's."""
    from core.derivations import company_text

    nurse = ["RN - ICU", "RN - Emergency", "RN - Surgery"]
    other = ["Financial Analyst", "Security Officer", "Lab Technician", "Marketing Coordinator"]
    postings = [(t, f"{t} duties for this team.\n{LICENSE}\n{BENEFITS}") for t in nurse] + [
        (t, f"{t} duties for this team.\n{BENEFITS}") for t in other
    ]
    _, shared = company_text.shared_paragraphs("Mercy Health", postings)

    assert company_text.paragraph_key(LICENSE) not in shared
    assert company_text.paragraph_key(BENEFITS) in shared


def test_masking_the_name_keeps_the_field_words_in_it():
    from core.derivations import company_text

    text = company_text.mask_company(
        "Acme Robotics builds robotics. At Acme we love it. ACME ROBOTICS, INC. hires.",
        "Acme Robotics, Inc.",
    )
    assert "acme" not in text.lower()
    assert "builds robotics" in text


def test_a_posting_left_too_short_by_stripping_is_embedded_as_written():
    from core.derivations import company_text

    shared = {company_text.paragraph_key(ABOUT), company_text.paragraph_key(EEO)}
    text = company_text.embedding_text(f"Intern\n{ABOUT}\n{EEO}", "Acme Robotics", shared)
    assert "warehouse robots" in text


def test_a_company_profile_is_rebuilt_when_it_gains_titles(f):
    for role in ["Backend Engineer", "Account Executive", "Data Analyst"]:
        job_id, _ = f.make_ready_job(content=_posting(role) * 2)
        db.execute(
            "UPDATE jobs SET company = 'Acme Robotics', title = %s WHERE id = %s", (role, job_id)
        )
    assert local_embeddings.refresh_profiles({"acme robotics"}) == 1
    first = db.query_one(
        "SELECT titles FROM company_text_profiles WHERE company_key = 'acme robotics'"
    )
    assert first == {"titles": 3}
    shared = local_embeddings.profiles({"acme robotics"})[0]["acme robotics"]
    assert len(shared) == 2
    assert local_embeddings.refresh_profiles({"acme robotics"}) == 0, "nothing new, nothing reread"

    job_id, _ = f.make_ready_job(content=_posting("Recruiter") * 2)
    db.execute(
        "UPDATE jobs SET company = 'Acme Robotics', title = 'Recruiter' WHERE id = %s", (job_id,)
    )
    assert local_embeddings.refresh_profiles({"acme robotics"}) == 1


def test_a_rebuilt_profile_re_embeds_the_postings_it_now_strips(f, monkeypatch):
    first, first_url = f.make_ready_job(content=_posting("Backend Engineer") * 2)
    f.upload(first, f.make_user())
    db.execute(
        "UPDATE jobs SET company = 'Acme Robotics', title = 'Backend Engineer' WHERE id = %s",
        (first,),
    )
    server = FakeServer()
    _run(monkeypatch, server)
    before = db.query_one(
        "SELECT content_hash FROM posting_embeddings WHERE url = %s", (first_url,)
    )
    assert before is not None

    # Two more roles make the company's about-us and EEO text shared.
    for role in ["Account Executive", "Data Analyst"]:
        job_id, _ = f.make_ready_job(content=_posting(role) * 2)
        db.execute(
            "UPDATE jobs SET company = 'Acme Robotics', title = %s WHERE id = %s", (role, job_id)
        )
        f.upload(job_id, f.make_user())
    # The run that embeds them rebuilds the profile; the next one re-selects
    # the posting embedded under the old one.
    _run(monkeypatch, server)
    _run(monkeypatch, server)
    after = db.query_one("SELECT content_hash FROM posting_embeddings WHERE url = %s", (first_url,))
    assert after is not None and after["content_hash"] != before["content_hash"]


def test_a_profile_built_under_an_older_rule_is_rebuilt(f):
    from core.derivations import company_text

    for role in ["Backend Engineer", "Account Executive", "Data Analyst"]:
        job_id, _ = f.make_ready_job(content=_posting(role) * 2)
        db.execute(
            "UPDATE jobs SET company = 'Acme Robotics', title = %s WHERE id = %s", (role, job_id)
        )
    assert local_embeddings.refresh_profiles({"acme robotics"}) == 1
    db.execute("UPDATE company_text_profiles SET rule = %s", (company_text.PROFILE_RULE - 1,))
    assert local_embeddings.refresh_profiles({"acme robotics"}) == 1
    row = db.query_one("SELECT rule FROM company_text_profiles WHERE company_key = 'acme robotics'")
    assert row == {"rule": company_text.PROFILE_RULE}


def test_an_older_rules_profile_is_rebuilt_without_any_new_posting(f, monkeypatch):
    """A rule bump reaches companies whose postings are all embedded: the
    run rebuilds their profiles, and the next run re-selects the postings."""
    from core.derivations import company_text

    job_id, url = f.make_ready_job(content=_posting("Backend Engineer") * 2)
    f.upload(job_id, f.make_user())
    db.execute(
        "UPDATE jobs SET company = 'Acme Robotics', title = 'Backend Engineer' WHERE id = %s",
        (job_id,),
    )
    server = FakeServer()
    _run(monkeypatch, server)
    db.execute("UPDATE company_text_profiles SET rule = %s", (company_text.PROFILE_RULE - 1,))
    calls = len(server.embedded)

    _run(monkeypatch, server)
    rule = db.query_one("SELECT rule FROM company_text_profiles")
    assert rule == {"rule": company_text.PROFILE_RULE}
    _run(monkeypatch, server)
    stamped = db.query_one(
        "SELECT e.profiled_at = p.profiled_at AS current FROM posting_embeddings e "
        "JOIN company_text_profiles p ON p.company_key = 'acme robotics' WHERE e.url = %s",
        (url,),
    )
    assert stamped == {"current": True}, "re-selected and re-stamped under the rebuilt profile"
    assert len(server.embedded) == calls, "its embedded text did not change, so nothing is paid"


def test_the_first_word_of_a_name_alone_does_not_name_the_company():
    from core.derivations import company_text

    assert not company_text.names_company(
        "Advanced degree in Electrical Engineering preferred.", "Advanced Micro Devices, Inc."
    )
    assert company_text.names_company(
        "Advanced Micro Devices is an equal opportunity employer.", "Advanced Micro Devices, Inc."
    )


def test_a_small_companys_shared_duties_under_a_role_heading_are_kept():
    """A company whose few roles are all one kind shares its duties under
    most titles; under a role heading they are the role's, and only the
    benefits under a company heading go."""
    from core.derivations import company_text

    duty = "Build evaluation sets that are representative and reproducible from real workflows."
    perk = "Free meals, coffee and snacks, and a generous learning budget every year."
    titles = [
        "Research Engineer",
        "Member of Technical Staff",
        "ML Engineer, Evals",
        "Founding Engineer",
    ]
    postings = [
        (t, f"{t} for our evals team.\nWhat you'll do:\n{duty}\nBenefits\n{perk}") for t in titles
    ]
    _, shared = company_text.shared_paragraphs("Ambral Labs", postings)

    assert company_text.paragraph_key(duty) not in shared
    assert company_text.paragraph_key(perk) in shared


def test_a_benefits_list_left_inside_one_unclosed_role_section_is_still_the_companys():
    from core.derivations import company_text

    perk = "Competitive offer packages and a defined-benefit pension plan for every employee."
    closed = [
        (t, f"Responsibilities\nRun {t} reports for the clinical teams.\nWhy join us?\n{perk}")
        for t in ["Analyst", "Nurse", "Technician", "Coordinator"]
    ]
    unclosed = [("Scheduler", f"Responsibilities\nRun schedules for the clinical teams.\n{perk}")]
    _, shared = company_text.shared_paragraphs("General Hospital", closed + unclosed)

    assert company_text.paragraph_key(perk) in shared


def _stripped(company: str, posting) -> set:
    from core.derivations import company_text

    titles = ["Engineer", "Analyst", "Technician", "Coordinator"]
    _, shared = company_text.shared_paragraphs(company, [(t, posting(t)) for t in titles])
    return shared


def test_an_eeo_statement_after_an_unclosed_role_section_is_still_the_companys():
    from core.derivations import company_text

    eeo = "We are an equal opportunity employer and welcome applicants of every background."
    shared = _stripped("Acme", lambda t: f"Responsibilities\nRun {t} work for the plant.\n{eeo}")
    assert company_text.paragraph_key(eeo) in shared


def test_about_heading_with_the_short_name_closes_the_role_section():
    from core.derivations import company_text

    blurb = "We are building the evaluation layer that enterprise AI agents run on today."
    shared = _stripped(
        "Ambral Labs, Inc.",
        lambda t: f"What you'll do\nShip {t} evaluation work weekly.\nAbout Ambral\n{blurb}",
    )
    assert company_text.paragraph_key(blurb) in shared


def test_bullets_are_not_headings():
    """A requirement bullet with a role word stays in its section, a bullet
    with a company word does not close it, and pay text under a bullet that
    merely contains a role word is not protected."""
    from core.derivations import company_text

    skill = "Strong written communication skills"
    duty = "Maintain cell lines and run assays for the discovery team every week."
    pay = "Reps earn a base plus uncapped commission with quarterly accelerators."
    shared = _stripped(
        "Acme Bio",
        lambda t: (
            f"Requirements\n{skill}\nExperience with cell culture\n{duty}\n"
            f"Uncapped commissions\n{pay}"
        ),
    )
    assert company_text.paragraph_key(skill) not in shared
    assert company_text.paragraph_key(duty) not in shared
    assert company_text.paragraph_key(pay) not in shared, "it sits in the role section, kept"
