from api import db, managed_board_runs, worker


def _board(f, slug: str, *, published: bool) -> int:
    user_id = f.make_user(sub=f"sponsor-{slug}")
    row = db.query_one(
        "INSERT INTO managed_boards "
        "(slug,name,sponsor_user_id,prompt,prompt_hash,requested_model,published,published_at,public_revision) "
        "VALUES (%s,%s,%s,'criterion','hash','gpt-5.6-luna',%s,CASE WHEN %s THEN now() END,CASE WHEN %s THEN 1 END) RETURNING id",
        (slug, slug, user_id, published, published, published),
    )
    return row["id"]


def test_scheduler_admits_every_published_board_and_isolates_refusals(f, monkeypatch):
    first = _board(f, "first", published=True)
    hidden = _board(f, "hidden", published=False)
    second = _board(f, "second", published=True)
    calls = []

    def admit(board_id, *, dedupe_key=None):
        calls.append((board_id, dedupe_key))
        if board_id == first:
            raise managed_board_runs.RunRefusal("IN_PROGRESS", "active")

    monkeypatch.setattr(managed_board_runs, "admit", admit)
    worker._managed_board_schedule_attempts.clear()
    worker.schedule_ingest_cycle()
    worker.schedule_ingest_cycle()

    assert [board_id for board_id, _ in calls] == [first, second]
    assert hidden not in [board_id for board_id, _ in calls]
    assert all(key and key.startswith(f"managed-board:{board_id}:") for board_id, key in calls)


def test_scheduler_with_no_published_boards_never_calls_admission(f, monkeypatch):
    _board(f, "draft", published=False)
    monkeypatch.setattr(
        managed_board_runs,
        "admit",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected admission")),
    )
    worker._managed_board_schedule_attempts.clear()
    worker.schedule_ingest_cycle()
