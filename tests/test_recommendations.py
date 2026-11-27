"""The For you page (api.routers.recommendations): a person's recent tech
postings in taste order, and the postings nearest to one."""

from __future__ import annotations

from api import db, for_you
from core import recommendations
from core.derivations.local_embeddings import LOCAL_EMBEDDING_DIMENSIONS


def _vector(weights: dict[int, float]) -> str:
    values = [0.0] * LOCAL_EMBEDDING_DIMENSIONS
    for axis, value in weights.items():
        values[axis] = value
    return "[" + ",".join(str(v) for v in values) + "]"


def _posting(f, name: str, weights: dict[int, float], *, in_scope: bool = True) -> int:
    url = f"https://jobs.test/{name}"
    job_id = f.make_job(url=url, title=f"Software Engineer {name}")
    db.execute(
        "INSERT INTO posting_embeddings (url, embedding, model) VALUES (%s, %s::halfvec, 'm')",
        (url, _vector(weights)),
    )
    db.execute(
        "INSERT INTO posting_scopes (url, in_scope, decided_by) VALUES (%s, %s, 'title')",
        (url, in_scope),
    )
    return job_id


def _admin_id() -> int:
    row = db.query_one("SELECT id FROM users WHERE email = 'admin@example.com'")
    assert row is not None
    return row["id"]


def _taste(user_id: int, weights: dict[int, float]) -> int:
    row = db.query_one(
        "INSERT INTO ranking_models (kind, user_id, embedding_model, weights, intercept, "
        "examples, evaluation) VALUES ('taste', %s, 'm', %s::halfvec, 0, "
        """'{"applied": 3, "background": 10, "window_days": 30}', """
        """'{"held_out_roc_auc": 0.9, "held_out_in_top_share": 0.8, "top_share": 0.05}') """
        "RETURNING id",
        (user_id, _vector(weights)),
    )
    assert row is not None
    return row["id"]


def test_only_admins_see_the_page(client, user_headers):
    assert client.get("/v1/user/recommendations", headers=user_headers).status_code == 403


def test_with_no_model_a_fit_is_asked_for(client, admin_headers):
    body = client.get("/v1/user/recommendations", headers=admin_headers).json()
    assert body["nodes"] == [] and body["model"] is None and body["training"] is True
    assert db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'train_taste' AND payload->>'user_id' = %s",
        (str(_admin_id()),),
    )


def _rank(user_id: int, run_id: int = 1) -> None:
    for_you.compute(user_id, run_id, 30)


def test_a_model_without_a_ranking_asks_for_one(client, admin_headers, f):
    _posting(f, "a", {0: 1.0})
    model = _taste(_admin_id(), {0: 1.0})
    body = client.get("/v1/user/recommendations", headers=admin_headers).json()
    assert body["nodes"] == [] and body["training"] is True
    assert db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'rank_for_you' AND payload->>'model_id' = %s",
        (str(model),),
    )


def test_postings_come_in_taste_order_with_why(client, admin_headers, f):
    admin = _admin_id()
    applied = _posting(f, "applied", {0: 1.0})
    db.execute(
        "INSERT INTO applications (user_id, job_id, company_name, title) "
        "VALUES (%s, %s, 'Acme', 'Software Engineer')",
        (admin, applied),
    )
    best = _posting(f, "best", {0: 0.9, 1: 0.1})
    repost = _posting(f, "repost", {0: 1.0})
    middling = _posting(f, "middling", {0: 0.5, 1: 0.5})
    worst = _posting(f, "worst", {1: 1.0})
    _posting(f, "out of scope", {0: 1.0}, in_scope=False)
    _taste(admin, {0: 1.0})
    _rank(admin)

    first = client.get("/v1/user/recommendations?first=2", headers=admin_headers).json()
    # The best posting is a near copy of the repost ranked above it, so it is
    # folded into the repost's row.
    assert first["total_count"] == 3
    assert [n["job_id"] for n in first["nodes"]] == [repost, middling]
    repost_node = first["nodes"][0]
    assert [c["job_id"] for c in repost_node["copies"]] == [best]
    assert repost_node["applied_copy"] is True
    assert repost_node["like"]["job_id"] == applied
    assert repost_node["match"] == 1.0
    assert repost_node["filter"] is None
    assert first["page_info"]["has_next_page"] is True

    after = first["page_info"]["end_cursor"]
    rest = client.get(f"/v1/user/recommendations?after={after}", headers=admin_headers).json()
    assert [n["job_id"] for n in rest["nodes"]] == [worst]
    assert rest["page_info"]["has_next_page"] is False
    assert first["model"]["held_out_in_top_share"] == 0.8


def test_a_cursor_from_an_older_ranking_is_refused(client, admin_headers, f):
    admin = _admin_id()
    _posting(f, "a", {0: 1.0})
    _posting(f, "b", {1: 1.0})
    _taste(admin, {0: 1.0})
    _rank(admin, run_id=1)
    after = client.get("/v1/user/recommendations?first=1", headers=admin_headers).json()[
        "page_info"
    ]["end_cursor"]
    _rank(admin, run_id=2)
    response = client.get(f"/v1/user/recommendations?after={after}", headers=admin_headers)
    assert response.status_code == 400


def test_the_persons_filter_holds(client, admin_headers, f):
    admin = _admin_id()
    kept = _posting(f, "kept", {0: 1.0})
    rejected = _posting(f, "rejected", {0: 0.8, 1: 0.6})
    unchecked = _posting(f, "unchecked", {1: 1.0})
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash) "
        "VALUES (%s, 'pay', 'pay well', 'h1')",
        (admin,),
    )
    f.make_verdict("https://jobs.test/kept", "custom", "passed", prompt_hash="h1")
    f.make_verdict("https://jobs.test/rejected", "custom", "rejected", prompt_hash="h1")
    _taste(admin, {0: 1.0})
    _rank(admin)

    body = client.get("/v1/user/recommendations", headers=admin_headers).json()
    assert [(n["job_id"], n["filter"]) for n in body["nodes"]] == [
        (kept, "kept"),
        (unchecked, "unchecked"),
    ]
    assert rejected not in [n["job_id"] for n in body["nodes"]]
    only = client.get("/v1/user/recommendations?kept_only=true", headers=admin_headers).json()
    assert [n["job_id"] for n in only["nodes"]] == [kept]


def test_similar_postings_leave_out_near_copies(client, admin_headers, f):
    admin = _admin_id()
    source = _posting(f, "source", {0: 1.0})
    _posting(f, "copy", {0: 1.0})
    near = _posting(f, "near", {0: 0.8, 1: 0.6})
    far = _posting(f, "far", {1: 1.0})
    _taste(admin, {0: 1.0})
    _rank(admin)
    body = client.get(f"/v1/user/jobs/{source}/similar", headers=admin_headers).json()
    assert [n["job_id"] for n in body["nodes"]] == [near, far]
    assert body["nodes"][0]["similarity"] < recommendations.NEAR_COPY


def test_a_failed_fit_says_why(client, admin_headers):
    client.get("/v1/user/recommendations", headers=admin_headers)
    db.execute(
        "UPDATE tasks SET status = 'failed', error = 'ValueError: 0 applied' "
        "WHERE kind = 'train_taste'"
    )
    body = client.get("/v1/user/recommendations", headers=admin_headers).json()
    assert body["training"] is False
    assert body["fit_failed"] == "ValueError: 0 applied"
