"""Exclusions match places as well as words: "UK" hides "London" once London
is a classified row, a never-seen string hides nothing, and the word match
from before still works while a string waits for its row."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import db
from api.tasks import locations
from tests.test_api_jobs import _insert_job, _job_ids, _pass_closed, _subscribe, _uid


@pytest.fixture
def clean():
    db.execute("DELETE FROM locations")
    yield
    db.execute("DELETE FROM locations")


def _criteria(client, user_headers, excluded):
    r = client.put(
        "/v1/user/settings",
        json={"criteria": {"excluded_locations": excluded}},
        headers=user_headers,
    )
    assert r.status_code == 200, r.text


def _board(client, user_headers, source):
    return _job_ids(
        client.get("/v1/user/jobs", params={"source": source}, headers=user_headers).json()
    )


def test_a_country_criterion_hides_its_cities_and_an_unclassified_string_hides_nothing(
    client, user_headers, clean
):
    uid = _uid(user_headers)
    london = _insert_job("src-loc", "https://x.test/l1", locations=["London"])
    toronto = _insert_job("src-loc", "https://x.test/l2", locations=["Toronto, ON"])
    unknown = _insert_job("src-loc", "https://x.test/l3", locations=["Zorbulon Campus"])
    sf = _insert_job("src-loc", "https://x.test/l4", locations=["SF"])
    remote_uk = _insert_job("src-loc", "https://x.test/l5", locations=["Remote - United Kingdom"])
    for i in range(1, 6):
        _pass_closed(f"https://x.test/l{i}")
    _subscribe(uid, "src-loc")
    for text, country, region, city, remote in (
        ("London", "GB", "", "London", False),
        ("Toronto, ON", "CA", "ON", "Toronto", False),
        ("SF", "US", "CA", "San Francisco", False),
        ("Remote - United Kingdom", "GB", "", "", True),
        ("UK", "GB", "", "", False),
        ("Canada", "CA", "", "", False),
    ):
        locations.store(
            text,
            locations.LocationExtract(country=country, region=region, city=city, remote=remote),
            "test",
        )

    _criteria(client, user_headers, ["UK", "Canada"])
    visible = _board(client, user_headers, "src-loc")
    assert london not in visible and toronto not in visible and remote_uk not in visible
    assert unknown in visible and sf in visible

    # The word match still stands on its own: "Zorbulon" is not a classified
    # row, and the criterion is not either.
    _criteria(client, user_headers, ["Zorbulon"])
    visible = _board(client, user_headers, "src-loc")
    assert unknown not in visible and london in visible


def test_a_remote_criterion_hides_remote_postings_only(client, user_headers, clean):
    uid = _uid(user_headers)
    remote = _insert_job("src-rem", "https://x.test/r1", locations=["Remote in USA"])
    office = _insert_job("src-rem", "https://x.test/r2", locations=["Austin, TX"])
    _pass_closed("https://x.test/r1")
    _pass_closed("https://x.test/r2")
    _subscribe(uid, "src-rem")
    locations.store("Remote in USA", locations.LocationExtract(country="US", remote=True), "test")
    locations.store(
        "Austin, TX", locations.LocationExtract(country="US", region="TX", city="Austin"), "test"
    )
    locations.store("Remote", locations.LocationExtract(remote=True), "test")
    _criteria(client, user_headers, ["Remote"])
    visible = _board(client, user_headers, "src-rem")
    assert remote not in visible and office in visible


@pytest.mark.asyncio
async def test_the_sweep_classifies_every_unseen_string_once(
    clean, monkeypatch, client, user_headers
):
    _insert_job("src-sw", "https://x.test/s1", locations=["Bengaluru", "Hyderabad"])
    _insert_job("src-sw", "https://x.test/s2", locations=["Bengaluru"])
    _criteria(client, user_headers, ["India"])
    locations.store("Hyderabad", locations.LocationExtract(country="IN", city="Hyderabad"), "test")
    asked: list[str] = []

    async def fake_run_batched(task_id, shape, specs):
        asked.extend(s.input for s in specs)
        return (
            {
                s.custom_id: SimpleNamespace(
                    text='{"country": "IN", "region": "", "city": "Bengaluru", "remote": false}'
                    if s.input == "Bengaluru"
                    else '{"country": "IN", "region": "", "city": "", "remote": false}',
                    error=None,
                )
                for s in specs
            },
            SimpleNamespace(model="gpt-5-nano"),
        )

    monkeypatch.setattr(locations, "run_batched", fake_run_batched)
    task = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('classify_locations', '{}', 'running') RETURNING id"
    )
    await locations.handle_classify_locations(task["id"], {})
    assert sorted(asked) == ["Bengaluru", "India"]
    rows = {r["text"]: r for r in db.query("SELECT * FROM locations ORDER BY text")}
    assert rows["Bengaluru"]["city"] == "Bengaluru" and rows["Bengaluru"]["country"] == "IN"
    assert rows["India"]["country"] == "IN" and rows["India"]["city"] is None
    assert rows["Hyderabad"]["model"] == "test"


def test_a_place_without_a_country_is_stored_empty(clean):
    locations.store("EMEA", locations.LocationExtract(region="CA", city="Somewhere"), "test")
    row = db.query_one("SELECT * FROM locations WHERE text = 'EMEA'")
    assert row["country"] is None and row["region"] is None and row["city"] is None
