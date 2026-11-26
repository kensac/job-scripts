"""Postings embedded with the model the fleet serves itself
(core.derivations.local_embeddings, tasks.derivations.local_embeddings)."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from api import db
from core.derivations import local_embeddings as core
from tasks.derivations import local_embeddings
from tests.factories import make_task

CONTENT = "a long job description " * 20
DIMS = core.LOCAL_EMBEDDING_DIMENSIONS


class FakeServer:
    """llama-server's /tokenize, /detokenize and /v1/embeddings: one token per
    word, and a vector whose first component is the chunk's token count."""

    def __init__(self):
        self.embedded: list[str] = []

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
    def __init__(self, body: Any):
        self.body: Any = body

    def raise_for_status(self):
        pass

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


def test_a_failed_posting_is_left_for_the_next_run(f, monkeypatch):
    job_id, url = f.make_ready_job(content=CONTENT)
    f.upload(job_id, f.make_user())

    class Down(FakeServer):
        def post(self, url, json, timeout):
            raise core.requests.ConnectionError("down")

    task_id = _run(monkeypatch, Down())
    assert db.query_one("SELECT 1 FROM posting_embeddings WHERE url = %s", (url,)) is None
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))["progress"]
    assert progress["failed"] == 1


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
    titles, shared = company_text.shared_paragraphs(postings)

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
