from __future__ import annotations

from api import db
from tests.application_support import _available_owner_key as _available_owner_key
from tests.application_support import _job_for, _user_id
from tests.resume_support import create_text_resume


class TestOneDraftAtATime:
    def test_a_second_request_while_the_first_runs_is_refused_and_the_view_shows_it(
        self, client, user_headers, f
    ):
        """The page disabled the button only while it remembered its own
        task id, so a reload could queue the same drafts twice while the
        first parked on the provider's batch."""
        from api import events

        uid = _user_id()
        job_id = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/6")
        create_text_resume(client, json={"name": "master", "text": "Alice."}, headers=user_headers)
        r = client.post(f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers)
        assert r.status_code == 202
        task_id = r.json()["task_id"]
        r = client.post(f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {
            "code": "IN_PROGRESS",
            "message": "drafts for this job are already being written",
            "task_id": task_id,
        }
        view = client.get(f"/v1/user/jobs/{job_id}/application", headers=user_headers).json()
        assert (view["task"]["id"], view["task"]["status"]) == (task_id, "pending")
        # Parked on the batch is still in flight.
        db.execute("UPDATE tasks SET status = 'awaiting_batch' WHERE id = %s", (task_id,))
        assert (
            client.post(
                f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers
            ).status_code
            == 409
        )
        # Done: the view carries no task and the next request queues.
        db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task_id,))
        view = client.get(f"/v1/user/jobs/{job_id}/application", headers=user_headers).json()
        assert view["task"] is None
        assert (
            client.post(
                f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers
            ).status_code
            == 202
        )
        # The event a page matches on names the job, not only the task.
        sent = []
        monkeypatch_publish = lambda channel, data: sent.append((channel, data))
        original = events._publish
        events._publish = monkeypatch_publish
        events.CENTRIFUGO_API_URL, events.CENTRIFUGO_API_KEY = "http://c", "k"
        try:
            events.publish_task(task_id)
        finally:
            events._publish = original
            events.CENTRIFUGO_API_URL = events.CENTRIFUGO_API_KEY = ""
        channels = {c for c, _ in sent}
        assert channels == {"jobtracker:tasks", f"jobtracker:user.{uid}"}
        assert all(d["task"]["job_id"] == job_id and d["task"]["status"] == "done" for _, d in sent)
