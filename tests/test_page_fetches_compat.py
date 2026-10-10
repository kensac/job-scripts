"""Images older than migration 213749d456b4 keep working while the fleet rolls.

They write and delete fetches through page_fetch_rows and read
page_texts.on_verdict. Both go in the next release, and this file with them.
"""

from __future__ import annotations

from api import db


def test_an_old_image_writes_reads_and_deletes_through_the_old_names():
    row = db.query_one(
        "INSERT INTO page_fetch_rows (url, status, method, content, worker) "
        "VALUES ('https://x/old', 'passed', 'scraped', %s, 'old-image') RETURNING id",
        ("Posting text. " * 40,),
    )
    assert row is not None
    assert db.query("SELECT id, worker FROM page_fetches") == [
        {"id": row["id"], "worker": "old-image"}
    ]
    assert db.query("SELECT id, on_verdict FROM page_texts WHERE NOT on_verdict") == [
        {"id": row["id"], "on_verdict": False}
    ]
    db.execute("DELETE FROM page_fetch_rows WHERE id = %s", (row["id"],))
    assert db.query("SELECT id FROM page_fetches") == []
