"""A zero-field fill submitted beside the filled one (#1268) records its
submission on the filled fill, at submit time and by the repair."""

from __future__ import annotations

from tools import repair_empty_fills

from api import db
from api.apply import submissions
from api.mail import applications
from tests.job_board_support import _uid

URL = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1"
FIELDS = [
    {"key": "q", "label": "Office preference", "kind": "text"},
    {"key": "r", "label": "Pronouns", "kind": "text"},
]


def _fill(uid: int, url: str, fields: list, submitted: bool = False) -> int:
    row = db.query_one(
        "INSERT INTO application_fills (user_id, url, host, fields) "
        "VALUES (%s, %s, 'job-boards.greenhouse.io', %s) RETURNING id",
        (uid, url, db.jsonb(fields)),
    )
    assert row is not None
    if submitted:
        app = applications.from_form(uid, "acme", db.query_one("SELECT now() AS t")["t"])
        db.execute(
            "UPDATE application_fills SET submitted_at = now(), application_id = %s WHERE id = %s",
            (app, row["id"]),
        )
    return row["id"]


def _row(fill_id: int) -> dict:
    row = db.query_one(
        "SELECT submitted_at, application_id, superseded_by, fields "
        "FROM application_fills WHERE id = %s",
        (fill_id,),
    )
    assert row is not None
    return row


def test_an_empty_fill_submits_onto_the_filled_one(client, user_headers):
    uid = _uid(user_headers)
    filled = client.post(
        "/v1/user/apply/resolve", json={"url": URL, "fields": FIELDS}, headers=user_headers
    ).json()["fill_id"]
    empty = client.post(
        "/v1/user/apply/resolve", json={"url": URL, "fields": []}, headers=user_headers
    ).json()["fill_id"]
    url = f"/v1/user/apply/fills/{empty}/submitted"
    body = {"fields": [{"key": "q", "final": "Remote"}]}
    done = client.post(url, json=body, headers=user_headers)
    assert done.status_code == 200, done.text
    assert [f["key"] for f in done.json()["fields"]] == ["q", "r"]

    real, gone = _row(filled), _row(empty)
    assert real["submitted_at"] is not None and real["application_id"] is not None
    assert {f["key"]: f["final"] for f in real["fields"]}["q"] == "Remote"
    assert gone == {
        "submitted_at": None,
        "application_id": None,
        "superseded_by": filled,
        "fields": [],
    }
    report = client.get("/v1/user/apply/report", headers=user_headers).json()
    assert report["forms_submitted"] == 1 and report["fields"] == 2

    # A retry of the empty fill's submit is the filled fill's receipt.
    assert client.post(url, json=body, headers=user_headers).status_code == 200
    assert _row(filled)["submitted_at"] == real["submitted_at"]
    made = db.query_one("SELECT count(*) AS n FROM applications WHERE user_id = %s", (uid,))
    assert made == {"n": 1}
    listed = client.get("/v1/user/apply/fills", headers=user_headers).json()["fills"]
    assert {f["id"]: f["superseded_by"] for f in listed} == {filled: None, empty: filled}
    # A superseded fill takes no more fields.
    again = client.post(
        "/v1/user/apply/resolve",
        json={"url": URL, "fields": FIELDS, "fill_id": empty},
        headers=user_headers,
    ).json()["fill_id"]
    assert again not in (filled, empty)


def test_the_repair_moves_recorded_empty_submissions_once(client, user_headers):
    uid = _uid(user_headers)
    resolved = [
        {**FIELDS[0], "rung": "profile", "value": "Remote"},
        {**FIELDS[1], "rung": "", "value": None},
    ]
    real = _fill(uid, URL, resolved)
    empty = _fill(uid, URL, [], submitted=True)
    # Fill 79's shape: the filled fill beside it was submitted on its own,
    # so there is no open fill to move it to.
    other = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=2"
    finals = [{**f, "final": f["value"], "changed": False} for f in resolved]
    kept = _fill(uid, other, finals, submitted=True)
    lone = _fill(uid, other, [], submitted=True)
    app = _row(empty)["application_id"]
    before = client.get("/v1/user/apply/report", headers=user_headers).json()
    assert before["forms_submitted"] == 3 and before["fields"] == 2
    assert before["filled_unchanged"] == 1

    # A dry run rolls back.
    assert repair_empty_fills.main(["--dry-run"]) == 0
    assert _row(empty)["superseded_by"] is None

    assert submissions.repair() == [(empty, real)]
    assert _row(real)["application_id"] == app and _row(real)["submitted_at"] is not None
    assert _row(empty)["superseded_by"] == real and _row(empty)["submitted_at"] is None
    assert _row(lone)["superseded_by"] is None and _row(kept)["submitted_at"] is not None
    after = client.get("/v1/user/apply/report", headers=user_headers).json()
    assert after["forms_submitted"] == 3 and after["fields"] == 4
    # The empty submit read no finals, so the moved fields are not counted
    # as left unchanged.
    assert after["filled_unchanged"] == 1
    assert after["by_rung"] == {"profile": 2, "blank": 2}

    snapshot = db.query("SELECT * FROM application_fills WHERE user_id = %s ORDER BY id", (uid,))
    assert submissions.repair() == []
    assert repair_empty_fills.main([]) == 0
    assert (
        db.query("SELECT * FROM application_fills WHERE user_id = %s ORDER BY id", (uid,))
        == snapshot
    )
