"""An administrator opens any catalog posting's drawer, not another person's
private upload (api.board.visibility.ADMIN_CATALOG); everyone else is held to
their board (FAST)."""

from __future__ import annotations

from tests.factories import user_id_for_subject


def test_an_admin_opens_a_posting_that_is_on_no_board(client, admin_headers, user_headers, f):
    job_id, _ = f.make_ready_job(title="Backend Engineer")
    assert client.get(f"/v1/user/jobs/{job_id}/detail", headers=admin_headers).status_code == 200
    assert client.get(f"/v1/user/jobs/{job_id}/detail", headers=user_headers).status_code == 404


def test_an_admin_does_not_open_another_persons_private_upload(
    client, admin_headers, user_headers, f
):
    url = "https://private.test/mine"
    job_id = f.make_job(url=url, source="upload", uploaded_by=user_id_for_subject("test-user"))
    assert client.get(f"/v1/user/jobs/{job_id}/detail", headers=admin_headers).status_code == 404
