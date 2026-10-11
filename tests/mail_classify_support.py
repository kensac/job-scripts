from __future__ import annotations

import datetime

from api import db
from api.mail import store as mail_store
from core.mail.importer import ImportedMessage
from core.routing import resolve
from tasks.mail.classification import handler as mail_classify
from tests.factories import make_task


class _Res:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.usage = {}
        self.batch_id = "b1"


def _store(f, **kw) -> tuple[int, int]:
    uid = f.make_user()
    mail_store.store_messages(
        uid,
        [
            ImportedMessage(
                provider_message_id=kw.get("mid", "<c1@x>"),
                source="gmail",
                from_email=kw.get("from_email", "no-reply@greenhouse.io"),
                subject=kw.get("subject", "Your application"),
                sent_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
                body_text=kw.get("body", "Thanks for applying."),
            )
        ],
    )
    row = db.query_one("SELECT id FROM email_messages WHERE user_id = %s", (uid,))
    assert row is not None
    return uid, row["id"]


def _events(message_id: int):
    return db.query("SELECT * FROM email_events WHERE message_id = %s ORDER BY id", (message_id,))


async def _run(monkeypatch, payload, results):
    async def fake(task_id, shape, specs):
        from api.ai import batch_results
        from core.batch import BatchResult

        chosen = resolve(shape)
        batch_results.snapshot_specs(task_id, specs)
        batch_results.checkpoint(
            task_id,
            [
                BatchResult(
                    key,
                    text=res.text,
                    error=res.error,
                    usage=res.usage,
                    model=chosen.model,
                    batch_id=f"batch-{task_id}",
                )
                for key, res in results.items()
            ],
            [],
        )
        return batch_results.unconsumed(task_id), chosen

    monkeypatch.setattr(mail_classify, "run_batched", fake)
    monkeypatch.setattr(mail_classify, "set_progress", lambda *a, **k: None)
    await mail_classify.handle_classify_mail(make_task("classify_mail", payload), payload)
