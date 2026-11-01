from __future__ import annotations

from api import db
from tests.factories import user_id_for_subject


def test_presets_list_returns_seeded_presets(client, admin_headers, user_headers):
    created = client.post(
        "/v1/admin/filter-presets",
        json={"name": "Remote Only", "prompt": "must be fully remote"},
        headers=admin_headers,
    )
    assert created.status_code == 200
    preset = created.json()

    listed = client.get("/v1/filter-presets", headers=user_headers)
    assert listed.status_code == 200
    names = {p["name"] for p in listed.json()["presets"]}
    assert preset["name"] in names


def test_adopt_preset_creates_user_filter(client, admin_headers, user_headers):
    uid = user_id_for_subject("test-user")
    created = client.post(
        "/v1/admin/filter-presets",
        json={"name": "Junior Friendly", "prompt": "no more than 2 years experience required"},
        headers=admin_headers,
    )
    preset_id = created.json()["id"]

    adopted = client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers)
    assert adopted.status_code == 200, adopted.text
    body = adopted.json()
    assert body["name"] == "Junior Friendly"
    assert body["enabled"] is True

    row = db.query_one(
        "SELECT id FROM user_filters WHERE user_id = %s AND name = %s", (uid, "Junior Friendly")
    )
    assert row is not None

    # adopting the same preset twice is rejected
    again = client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ALREADY_ADOPTED"

    # Provenance is on the row, so a rename does not turn it back into "Add":
    # the list still says which preset it came from and a re-adopt is still
    # refused. A hand-written filter carries no preset.
    assert body["preset_id"] == preset_id
    renamed = client.patch(
        f"/v1/user/filters/{body['id']}", json={"name": "My junior filter"}, headers=user_headers
    )
    assert renamed.status_code == 200, renamed.text
    listed = {
        f["id"]: f for f in client.get("/v1/user/filters", headers=user_headers).json()["filters"]
    }
    assert listed[body["id"]]["preset_id"] == preset_id
    assert listed[body["id"]]["name"] == "My junior filter"
    assert (
        client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers).status_code
        == 409
    )
    # One filter runs at a time and the adopted one is on, so a hand-written
    # second one comes in disabled.
    own = client.post(
        "/v1/user/filters",
        json={"name": "hand written", "prompt": "no agencies", "enabled": False},
        headers=user_headers,
    )
    assert own.status_code == 200 and own.json()["preset_id"] is None


def _preset(name: str, prompt: str) -> int:
    from api import db

    row = db.query_one(
        "INSERT INTO filter_presets (name, description, prompt, on_ambiguous, active) "
        "VALUES (%s, 'd', %s, 'keep', TRUE) RETURNING id",
        (name, prompt),
    )
    assert row is not None
    return row["id"]


def test_preset_coverage_says_what_it_would_show_before_spending_anything(client, user_headers, f):
    """A preset's cached verdicts are reusable across users because
    prompt_hash is computed from the prompt and on_ambiguous alone. Nothing
    user-specific. That is what lets someone with no API key see a real board
    on day one instead of 7,397 undifferentiated postings."""
    from core.filters import build_custom_instructions, compute_prompt_hash

    preset_id = _preset("cached", "backend roles only")
    phash = compute_prompt_hash(build_custom_instructions("backend roles only", "keep"))
    _, shown = f.make_ready_job(source="s")
    _, hidden = f.make_ready_job(source="s")
    f.make_ready_job(source="s")  # eligible, never judged by this preset
    f.make_verdict(shown, "custom", "passed", prompt_hash=phash)
    f.make_verdict(hidden, "custom", "rejected", prompt_hash=phash)

    body = client.get("/v1/filter-presets", headers=user_headers).json()
    cov = next(p for p in body["presets"] if p["id"] == preset_id)["coverage"]
    assert body["eligible_postings"] == 3
    assert cov["would_show_now"] == 1
    assert cov["already_judged"] == 2
    assert cov["needs_ai"] == 1


def test_an_uncovered_preset_reports_zero_rather_than_looking_ready(client, user_headers, f):
    """Adopting a preset with no cached verdicts shows an EMPTY board until AI
    runs, which is worse than the wall it replaces. The numbers ship with the
    preset so the choice is not made blind."""
    preset_id = _preset("uncached", "aerospace only")
    f.make_ready_job(source="s")

    body = client.get("/v1/filter-presets", headers=user_headers).json()
    cov = next(p for p in body["presets"] if p["id"] == preset_id)["coverage"]
    assert cov["would_show_now"] == 0
    assert cov["already_judged"] == 0
    assert cov["needs_ai"] == body["eligible_postings"] == 1


def test_coverage_counts_only_postings_that_clear_both_gates(client, user_headers, f):
    """A posting the closed check rejected is not eligible for anyone, so it
    must not inflate what a preset promises."""
    from core.filters import build_custom_instructions, compute_prompt_hash

    preset_id = _preset("gated", "anything")
    phash = compute_prompt_hash(build_custom_instructions("anything", "keep"))
    _, live = f.make_ready_job(source="s", closed="passed", clearance="passed")
    _, dead = f.make_ready_job(source="s", closed="rejected", clearance="passed")
    for url in (live, dead):
        f.make_verdict(url, "custom", "passed", prompt_hash=phash)

    body = client.get("/v1/filter-presets", headers=user_headers).json()
    cov = next(p for p in body["presets"] if p["id"] == preset_id)["coverage"]
    assert body["eligible_postings"] == 1
    assert cov["would_show_now"] == 1


def test_coverage_reads_the_latest_custom_verdict(client, user_headers, f):
    from core.filters import build_custom_instructions, compute_prompt_hash

    preset_id = _preset("revised", "changed mind")
    phash = compute_prompt_hash(build_custom_instructions("changed mind", "keep"))
    _, url = f.make_ready_job(source="s")
    f.make_verdict(url, "custom", "passed", prompt_hash=phash)
    f.make_verdict(url, "custom", "rejected", prompt_hash=phash)

    body = client.get("/v1/filter-presets", headers=user_headers).json()
    cov = next(p for p in body["presets"] if p["id"] == preset_id)["coverage"]
    assert cov["already_judged"] == 1
    assert cov["would_show_now"] == 0
