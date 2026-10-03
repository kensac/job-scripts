"""A decision's immutable content is stored once; its per-task row references it.

Readers select from DECISIONS, which resolves a row's url_id and body_id into
the columns the inline table had before 7ca95d34ef5f dropped them.
"""

from __future__ import annotations

from typing import Any

from api import db
from api.review_policy_storage import PolicySnapshotUnavailable

# Everything a decision says that is the same for every task re-admitting the
# same posting under the same prompt and policy. Per-task identity (task, job,
# user, filter, board, revision, created_at) and the URL stay on the row.
BODY = (
    "prompt_hash",
    "stage",
    "mode",
    "action",
    "reason",
    "profile_id",
    "title",
    "content_hash",
    "policy_id",
    "evidence",
)


def _content(alias: str) -> str:
    # JSONB text is the exact form: numeric scale survives, unlike a Python
    # round trip, and ::text equality is what "the same body" means.
    return ",".join(
        f"{alias}.evidence::text" if column == "evidence" else f"{alias}.{column}"
        for column in BODY
    )


def body_digest(alias: str) -> str:
    return (
        "sha256(convert_to(jsonb_build_array("
        + ",".join(f"{alias}.{column}" for column in BODY)
        + ")::text,'UTF8'))"
    )


# url_id and body_id are NOT NULL under validated foreign keys, so the inner
# joins cannot drop a decision.
DECISIONS = (
    "(SELECT d.id,d.task_id,d.job_id,d.user_id,d.filter_id,d.managed_board_id,d.revision,"
    "d.created_at,d.url_id,u.url,"
    + ",".join(f"b.{column}" for column in BODY)
    + " FROM review_gate_decisions d JOIN review_gate_urls u ON u.id=d.url_id "
    "JOIN review_gate_decision_bodies b ON b.id=d.body_id) d"
)

# idx_review_gate_decisions_url_id serves this; the resolved url has no index.
URL_MATCH = "(d.url_id=(SELECT id FROM review_gate_urls WHERE url=%(url)s))"

# A body's policy_id is NOT NULL under a validated foreign key, so the join
# cannot drop a decision.
RESOLVED_FROM = f"{DECISIONS} JOIN review_gate_policies p ON p.id=d.policy_id"


def intern_urls(urls: list[str]) -> dict[str, int]:
    distinct = sorted(set(urls))
    # Sorted inserts take unique-key waits in one order across writers.
    db.execute(
        "INSERT INTO review_gate_urls(url) SELECT u FROM unnest(%s::text[]) u ORDER BY u "
        "ON CONFLICT(url) DO NOTHING",
        (distinct,),
    )
    return {
        row["url"]: row["id"]
        for row in db.query("SELECT id,url FROM review_gate_urls WHERE url=ANY(%s)", (distinct,))
    }


def intern_bodies(source: str, parameters: dict[str, Any]) -> dict[Any, int]:
    """Body id for each `key` of a relation that has every BODY column.

    Must run inside the caller's transaction, so a failed decision write
    leaves no body behind and the decision row refers to a committed body.
    """
    db.execute(
        f"INSERT INTO review_gate_decision_bodies(digest,{','.join(BODY)}) "
        f"SELECT {body_digest('s')},{','.join(f's.{c}' for c in BODY)} FROM ({source}) s "
        "ORDER BY 1 ON CONFLICT(digest) DO NOTHING",
        parameters,
    )
    # A second statement sees a concurrent insert after its unique-key wait.
    # Never resolve a digest collision by substituting the other body.
    rows = db.query(
        f"SELECT s.key,b.id,({_content('s')}) IS NOT DISTINCT FROM ({_content('b')}) AS exact "
        f"FROM ({source}) s LEFT JOIN review_gate_decision_bodies b ON b.digest={body_digest('s')}",
        parameters,
    )
    if any(row["id"] is None or not row["exact"] for row in rows):
        raise PolicySnapshotUnavailable("Review decision digest does not identify its exact body")
    return {row["key"]: row["id"] for row in rows}
