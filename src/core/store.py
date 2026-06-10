from __future__ import annotations

import datetime
import os
import sqlite3
from typing import Any, Dict, List, Optional

from core.filters import compute_prompt_hash
from core.paths import DATA_DIR

DB_PATH = str(DATA_DIR / "ai_results.db")

_INSERT_COLUMNS = [
    "created_at",
    "config_name",
    "url",
    "check_type",
    "status",
    "reason",
    "model",
    "reasoning_effort",
    "filter_name",
    "prompt_hash",
    "company",
    "job_title",
    "instructions",
    "input_content",
    "parsed_json",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "duration_ms",
    "error",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                config_name TEXT,
                url TEXT,
                check_type TEXT,
                status TEXT,
                reason TEXT,
                model TEXT,
                reasoning_effort TEXT,
                filter_name TEXT,
                prompt_hash TEXT,
                company TEXT,
                job_title TEXT,
                instructions TEXT,
                input_content TEXT,
                parsed_json TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cached_tokens INTEGER,
                reasoning_tokens INTEGER,
                duration_ms INTEGER,
                error TEXT
            )
            """
        )
        _migrate(conn)
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_url ON ai_queries(url)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_url_check ON ai_queries(url, check_type)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_status ON ai_queries(status)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_check_type ON ai_queries(check_type)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_created_at ON ai_queries(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_ai_queries_prompt_hash ON ai_queries(check_type, prompt_hash)",
        ):
            conn.execute(stmt)


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_queries)")}
    if "filter_name" not in cols:
        conn.execute("ALTER TABLE ai_queries ADD COLUMN filter_name TEXT")
    if "prompt_hash" not in cols:
        conn.execute("ALTER TABLE ai_queries ADD COLUMN prompt_hash TEXT")
    # Backfill prompt_hash on legacy custom rows from their stored instructions,
    # so an unchanged filter's past results are reused (same hash).
    legacy = conn.execute(
        "SELECT id, instructions FROM ai_queries "
        "WHERE check_type = 'custom' AND prompt_hash IS NULL AND instructions IS NOT NULL"
    ).fetchall()
    for row in legacy:
        conn.execute(
            "UPDATE ai_queries SET prompt_hash = ?, "
            "filter_name = COALESCE(filter_name, 'default') WHERE id = ?",
            (compute_prompt_hash(row[1]), row[0]),
        )


def add_ai_result(
    url: str,
    status: str,
    reason: str = "",
    check_type: str = "",
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cached_tokens: Optional[int] = None,
    reasoning_tokens: Optional[int] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    filter_name: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    company: Optional[str] = None,
    job_title: Optional[str] = None,
    instructions: Optional[str] = None,
    input_content: Optional[str] = None,
    parsed_json: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    row = {
        "created_at": datetime.datetime.now().isoformat(),
        "config_name": os.environ.get("CONFIG_NAME"),
        "url": url,
        "check_type": check_type,
        "status": status,
        "reason": reason,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "filter_name": filter_name,
        "prompt_hash": prompt_hash,
        "company": company,
        "job_title": job_title,
        "instructions": instructions,
        "input_content": input_content,
        "parsed_json": parsed_json,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "duration_ms": duration_ms,
        "error": error,
    }
    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _INSERT_COLUMNS)
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO ai_queries ({columns}) VALUES ({placeholders})", row
        )


def get_ai_result(url: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM ai_queries WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_latest(url: str, check_type: str) -> Optional[Dict[str, Any]]:
    """Latest *decided* (passed/rejected) result for a url+check_type.

    Ignores 'failed' rows so failed checks are retried rather than cached.
    """
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM ai_queries WHERE url = ? AND check_type = ? "
            "AND status IN ('passed', 'rejected') ORDER BY id DESC LIMIT 1",
            (url, check_type),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_custom_result(url: str, prompt_hash: str) -> Optional[Dict[str, Any]]:
    """Latest decided custom result for a url under a specific filter (by hash)."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM ai_queries WHERE url = ? AND check_type = 'custom' "
            "AND prompt_hash = ? AND status IN ('passed', 'rejected') "
            "ORDER BY id DESC LIMIT 1",
            (url, prompt_hash),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_content(url: str) -> Optional[str]:
    """Most recent non-empty scraped content stored for a url."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT input_content FROM ai_queries WHERE url = ? "
            "AND input_content IS NOT NULL AND input_content != '' "
            "ORDER BY id DESC LIMIT 1",
            (url,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def get_prelim_passed_urls() -> List[str]:
    """URLs whose latest closed AND latest clearance checks both passed."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT url FROM ai_queries a
              WHERE check_type = 'closed' AND status = 'passed'
                AND id = (SELECT MAX(id) FROM ai_queries b
                          WHERE b.url = a.url AND b.check_type = 'closed')
            INTERSECT
            SELECT url FROM ai_queries a
              WHERE check_type = 'clearance' AND status = 'passed'
                AND id = (SELECT MAX(id) FROM ai_queries b
                          WHERE b.url = a.url AND b.check_type = 'clearance')
            """
        ).fetchall()
    return [r[0] for r in rows]


def is_prelim_rejected(url: str) -> bool:
    """Rejected by a prompt-independent check (closed or clearance)."""
    closed = get_latest(url, "closed")
    if closed and closed["status"] == "rejected":
        return True
    clearance = get_latest(url, "clearance")
    return bool(clearance and clearance["status"] == "rejected")


def is_url_rejected(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "rejected"


def is_url_passed(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "passed"


def is_url_failed(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "failed"


def _latest_per_url_where(condition: str, params: tuple) -> List[Dict[str, Any]]:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM ai_queries q WHERE id = "
            "(SELECT MAX(id) FROM ai_queries WHERE url = q.url) "
            f"AND {condition}",
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def get_all_failed_jobs() -> List[Dict[str, Any]]:
    return _latest_per_url_where("status = ?", ("failed",))


def get_all_custom_filter_jobs() -> List[Dict[str, Any]]:
    return _latest_per_url_where("check_type = ?", ("custom",))


init_db()
