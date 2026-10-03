"""A decision's immutable content is stored once; its per-task row references it.

Rows exist in three shapes: fully inline (every row written before the
reference writer), copied (inline plus url_id and body_id), and
reference-only. Readers select from DECISIONS, which presents every shape with
the columns the inline table had, so a reader's result does not depend on
which shape stores a row.
"""

from __future__ import annotations

from typing import Any, Literal

from api import db, review_policy_storage
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


# A copied row's inline columns must equal what its references say. Readers
# that return whole decisions refuse a row where they differ.
STORED_MATCHES = (
    f"(d.body_id IS NULL OR d.stage IS NULL OR ({_content('d')}) IS NOT DISTINCT FROM "
    f"({_content('b')})) AND (d.url_id IS NULL OR d.url IS NULL OR d.url=u.url)"
)

DECISIONS = (
    "(SELECT d.id,d.task_id,d.job_id,d.user_id,d.filter_id,d.managed_board_id,d.revision,"
    "d.created_at,d.url AS inline_url,d.url_id,"
    "CASE WHEN d.url_id IS NULL THEN d.url ELSE u.url END AS url,"
    + ",".join(
        f"CASE WHEN d.body_id IS NULL THEN d.{column} ELSE b.{column} END AS {column}"
        for column in BODY
    )
    + f",d.policy AS inline_policy,{STORED_MATCHES} AS stored_matches "
    "FROM review_gate_decisions d LEFT JOIN review_gate_urls u ON u.id=d.url_id "
    "LEFT JOIN review_gate_decision_bodies b ON b.id=d.body_id) d"
)

# Both arms are indexed (url_created, url_id); a CASE over the two is not.
URL_MATCH = "(d.inline_url=%(url)s OR d.url_id=(SELECT id FROM review_gate_urls WHERE url=%(url)s))"

RESOLVED_COLUMNS = (
    "d.inline_policy,d.policy_id,p.id AS snapshot_id,p.policy AS snapshot_policy,"
    "(d.inline_policy::text IS NOT DISTINCT FROM p.policy::text) AS policy_matches,"
    "d.stored_matches"
)
RESOLVED_FROM = f"{DECISIONS} LEFT JOIN review_gate_policies p ON p.id=d.policy_id"


def resolve(row: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(row)
    if not resolved.pop("stored_matches"):
        raise PolicySnapshotUnavailable("Inline and referenced review decisions disagree")
    return review_policy_storage.resolve(resolved)


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


Mode = Literal["copy", "verify", "compact", "restore"]

INLINE = ("url", *BODY, "policy")
_HAS_INLINE = " OR ".join(f"d.{column} IS NOT NULL" for column in INLINE)


def migrate_chunk(
    *,
    after: int,
    through: int,
    limit: int,
    mode: Mode,
    backup_complete: bool = False,
    compatible_readers: bool = False,
) -> dict[str, int]:
    if after < 0 or through < after or limit <= 0:
        raise ValueError("Require 0 <= after <= through and a positive limit")
    if mode not in ("copy", "verify", "compact", "restore"):
        raise ValueError("Unknown review decision migration mode")
    if mode == "compact" and not (backup_complete and compatible_readers):
        raise ValueError("Compaction requires a completed backup and compatible readers")
    with db.transaction():
        if mode == "verify":
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows = db.query(
            "SELECT d.id FROM review_gate_decisions d WHERE d.id>%s AND d.id<=%s "
            "ORDER BY d.id LIMIT %s" + ("" if mode == "verify" else " FOR UPDATE OF d"),
            (after, through, limit),
        )
        counts = {
            "scanned": len(rows),
            "copied": 0,
            "verified": 0,
            "unreferenced": 0,
            "compacted": 0,
            "restored": 0,
            "inline_remaining": 0,
            "inline_bytes_removed": 0,
        }
        if rows:
            ids = [row["id"] for row in rows]
            if mode == "copy":
                counts["copied"] = _copy(ids)
            if mode != "verify":
                # Keep referenced values stable between verification and mutation.
                for table, column in (
                    ("review_gate_urls", "url_id"),
                    ("review_gate_decision_bodies", "body_id"),
                ):
                    db.query(
                        f"SELECT t.id FROM {table} t WHERE t.id IN (SELECT {column} FROM "
                        "review_gate_decisions WHERE id=ANY(%s)) ORDER BY t.id FOR SHARE OF t",
                        (ids,),
                    )
                db.query(
                    "SELECT p.id FROM review_gate_policies p WHERE p.id IN (SELECT "
                    "COALESCE(b.policy_id,d.policy_id) FROM review_gate_decisions d "
                    "LEFT JOIN review_gate_decision_bodies b ON b.id=d.body_id "
                    "WHERE d.id=ANY(%s)) ORDER BY p.id FOR SHARE OF p",
                    (ids,),
                )
            checked = _verify(ids, counts, compact=mode == "compact")
            if mode == "compact":
                counts["compacted"] = db.execute_count(
                    "UPDATE review_gate_decisions d SET "
                    + ",".join(f"{column}=NULL" for column in INLINE)
                    + f" WHERE d.id=ANY(%s) AND ({_HAS_INLINE})",
                    (ids,),
                )
                counts["inline_bytes_removed"] = sum(row["inline_bytes"] for row in checked)
                counts["inline_remaining"] = 0
            elif mode == "restore":
                # Rows written reference-only for policy also receive the exact
                # snapshot inline: every older reader then has what it needs.
                counts["restored"] = db.execute_count(
                    "UPDATE review_gate_decisions d SET url=u.url,"
                    + ",".join(f"{column}=b.{column}" for column in BODY)
                    + ",policy=p.policy FROM review_gate_urls u,review_gate_decision_bodies b,"
                    "review_gate_policies p WHERE d.id=ANY(%s) AND u.id=d.url_id "
                    "AND b.id=d.body_id AND p.id=b.policy_id "
                    "AND (d.url IS NULL OR d.stage IS NULL OR d.policy IS NULL)",
                    (ids,),
                )
                remaining = db.query_one(
                    f"SELECT count(*) AS n FROM review_gate_decisions d "
                    f"WHERE d.id=ANY(%s) AND ({_HAS_INLINE})",
                    (ids,),
                )
                assert remaining is not None
                counts["inline_remaining"] = remaining["n"]
        return {**counts, "after": rows[-1]["id"] if rows else through, "through": through}


def _copy(ids: list[int]) -> int:
    """References for every unreferenced row, in one UPDATE of each row.

    Rows holding only an inline policy get its snapshot here too, so policy
    and body normalization cost one new row version rather than two.
    """
    unreferenced = db.query(
        "SELECT d.id,d.url,d.policy_id,d.policy::text AS policy_text FROM review_gate_decisions d "
        "WHERE d.id=ANY(%s) AND d.body_id IS NULL ORDER BY d.id",
        (ids,),
    )
    if not unreferenced:
        return 0
    if any(row["policy_id"] is None and row["policy_text"] is None for row in unreferenced):
        raise PolicySnapshotUnavailable("Review decision has no policy snapshot")
    policies = {
        text: review_policy_storage.intern(text)
        for text in sorted({row["policy_text"] for row in unreferenced if row["policy_id"] is None})
    }
    policy_ids = {
        row["id"]: row["policy_id"]
        if row["policy_id"] is not None
        else policies[row["policy_text"]]
        for row in unreferenced
    }
    urls = intern_urls([row["url"] for row in unreferenced])
    keys = list(policy_ids)
    bodies = intern_bodies(
        "SELECT d.id AS key,"
        + ",".join("v.policy_id" if column == "policy_id" else f"d.{column}" for column in BODY)
        + " FROM review_gate_decisions d JOIN unnest(%(ids)s::bigint[],%(policies)s::bigint[]) "
        "v(id,policy_id) ON v.id=d.id",
        {"ids": keys, "policies": [policy_ids[key] for key in keys]},
    )
    return db.execute_count(
        "UPDATE review_gate_decisions d SET url_id=v.url_id,body_id=v.body_id,policy_id=v.policy_id "
        "FROM unnest(%s::bigint[],%s::bigint[],%s::bigint[],%s::bigint[]) v(id,url_id,body_id,policy_id) "
        "WHERE d.id=v.id AND d.body_id IS NULL",
        (
            keys,
            [urls[row["url"]] for row in unreferenced],
            [bodies[key] for key in keys],
            [policy_ids[key] for key in keys],
        ),
    )


def _verify(ids: list[int], counts: dict[str, int], *, compact: bool) -> list[dict[str, Any]]:
    checked = db.query(
        "SELECT d.id,d.url_id IS NOT NULL AND d.body_id IS NOT NULL AS referenced,"
        f"({_HAS_INLINE}) AS has_inline,{STORED_MATCHES} AS matches,"
        f"b.digest={body_digest('b')} AS body_valid,"
        "p.id IS NOT NULL AS has_policy,"
        "p.digest=sha256(convert_to(p.policy::text,'UTF8')) AS policy_valid,"
        "(d.policy IS NULL OR d.policy::text=p.policy::text) AS policy_exact,"
        + "+".join(f"COALESCE(pg_column_size(d.{column}),0)" for column in INLINE)
        + " AS inline_bytes FROM review_gate_decisions d "
        "LEFT JOIN review_gate_urls u ON u.id=d.url_id "
        "LEFT JOIN review_gate_decision_bodies b ON b.id=d.body_id "
        "LEFT JOIN review_gate_policies p ON p.id=COALESCE(b.policy_id,d.policy_id) "
        "WHERE d.id=ANY(%s) ORDER BY d.id",
        (ids,),
    )
    for row in checked:
        if not row["referenced"]:
            if compact:
                raise PolicySnapshotUnavailable("Compaction requires a verified decision reference")
            counts["unreferenced"] += 1
        elif not (
            row["matches"]
            and row["body_valid"]
            and row["has_policy"]
            and row["policy_valid"]
            and row["policy_exact"]
        ):
            raise PolicySnapshotUnavailable("Review decision reference verification failed")
        else:
            counts["verified"] += 1
        counts["inline_remaining"] += int(row["has_inline"])
    return checked
