"""Object-level authorization: can user A reach user B's things?

Route-level gating (every route carries require_user/require_admin) was already
complete and tested. These are the checks that were missing: a signed-in user
naming an id that is not theirs. Job ids are sequential and there are ~49k of
them, so "you must be signed in" is not a boundary by itself.
"""

from __future__ import annotations

import pytest

from api import db


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


@pytest.fixture
def private_upload(user_headers):
    """A job user A uploaded privately - not in any shared source."""
    uid = _uid(user_headers)
    row = db.query_one(
        "INSERT INTO jobs (url, raw_url, company, title, source, active, uploaded_by) "
        "VALUES (%s, %s, %s, %s, %s, TRUE, %s) RETURNING id",
        (
            "https://private.test/secret-role",
            "https://private.test/secret-role",
            "SecretCo",
            "Staff Engineer",
            "upload",
            uid,
        ),
    )
    assert row is not None
    return row["id"]


def test_detail_of_another_users_private_upload_is_404(client, other_user_headers, private_upload):
    """The detail route returns the full cached page text. Leaking it exposes
    a posting the uploader chose not to share, plus its company and title."""
    resp = client.get(f"/v1/user/jobs/{private_upload}/detail", headers=other_user_headers)
    assert resp.status_code == 404, resp.text


def test_owner_can_still_read_their_own_upload(client, user_headers, private_upload):
    """The gate must not lock the owner out of their own job."""
    resp = client.get(f"/v1/user/jobs/{private_upload}/detail", headers=user_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["company"] == "SecretCo"


def test_cannot_pin_another_users_private_upload(client, other_user_headers, private_upload):
    """Patching creates a user_jobs row, and _VISIBILITY trusts that row
    unconditionally - so an unrestricted pin is a self-service permanent grant
    that launders around the detail gate."""
    resp = client.patch(
        f"/v1/user/jobs/{private_upload}",
        json={"notes": "mine now"},
        headers=other_user_headers,
    )
    assert resp.status_code == 404, resp.text
    assert (
        db.query_one(
            "SELECT 1 AS x FROM user_jobs WHERE job_id = %s AND user_id = %s",
            (private_upload, _uid(other_user_headers)),
        )
        is None
    )


def test_explain_on_an_invisible_job_is_404(client, other_user_headers, private_upload):
    """The severe one. explain writes into ai_queries, which has no user_id and
    resolves latest-row-per-(url, check_type) for EVERY user - so an ungated
    job_id here flips that job's closed status on every board at once."""
    resp = client.post(
        f"/v1/user/jobs/{private_upload}/explain",
        json={"check": "closed"},
        headers=other_user_headers,
    )
    assert resp.status_code == 404, resp.text
    assert (
        db.query_one(
            "SELECT 1 AS x FROM ai_queries WHERE url = %s",
            ("https://private.test/secret-role",),
        )
        is None
    ), "an unauthorized caller wrote a globally-visible verdict"


def test_pinning_a_catalog_job_still_works(client, other_user_headers):
    """The 'watching' case is deliberate and must survive the fix: any job in
    the shared catalog can be pinned to your own board."""
    row = db.query_one(
        "INSERT INTO jobs (url, raw_url, company, title, source, active) "
        "VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id",
        (
            "https://catalog.test/open-role",
            "https://catalog.test/open-role",
            "PublicCo",
            "Engineer",
            "src-catalog",
        ),
    )
    assert row is not None
    resp = client.patch(
        f"/v1/user/jobs/{row['id']}", json={"notes": "watching"}, headers=other_user_headers
    )
    assert resp.status_code == 200, resp.text


def test_task_of_another_user_is_404(client, user_headers, other_user_headers):
    """Task ids are sequential and `error` is str(exc) written verbatim by the
    worker, so an ungated lookup hands over other users' failures."""
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status, error) VALUES (%s, %s, %s, %s) RETURNING id",
        (
            "run_all_filters",
            db.jsonb({"user_id": _uid(user_headers)}),
            "failed",
            "boom: SQL detail",
        ),
    )
    assert row is not None
    assert client.get(f"/v1/tasks/{row['id']}", headers=other_user_headers).status_code == 404
    assert client.get(f"/v1/tasks/{row['id']}", headers=user_headers).status_code == 200


def test_system_task_is_not_readable_by_any_user(client, user_headers):
    """Fleet work carries no user_id in its payload and belongs to nobody."""
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES (%s, %s, %s) RETURNING id",
        ("ingest_source", db.jsonb({"source": "fulltime"}), "done"),
    )
    assert row is not None
    assert client.get(f"/v1/tasks/{row['id']}", headers=user_headers).status_code == 404
