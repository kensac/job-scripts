from __future__ import annotations

import argparse
import datetime
import sys
from typing import Any, Dict, List, Optional

from api import db
from core.configs import load_configs
from core.filters import build_custom_instructions, compute_prompt_hash, load_filter_specs
from core.urls import normalize_url

COL = {
    "company": 1, "size": 2, "location": 3, "found": 4, "url": 5, "title": 6,
    "terms": 7, "recruiter": 8, "connection1": 9, "connection2": 10,
    "documents": 11, "date_applied": 12, "status": 13, "notes": 14,
}

DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y")


def _parse_date(value: str) -> Optional[datetime.date]:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _cell(row: List[str], key: str) -> str:
    idx = COL[key]
    return (row[idx] if idx < len(row) else "").strip()


def find_or_create_user(sub: Optional[str], email: Optional[str], dry_run: bool) -> Dict[str, Any]:
    user = None
    if sub:
        user = db.query_one("SELECT * FROM users WHERE sub = %s", (sub,))
    if user is None and email:
        user = db.query_one("SELECT * FROM users WHERE email = %s", (email,))
    if user:
        return user
    if not sub:
        sys.exit(
            "User not found by email and no --sub given. Either log in once first "
            "(bootstrap creates the row) and rerun with --email, or pass the "
            "Authentik subject id via --sub to pre-create it."
        )
    if dry_run:
        return {"id": -1, "sub": sub, "email": email}
    row = db.query_one(
        "INSERT INTO users (sub, email) VALUES (%s, %s) RETURNING *", (sub, email)
    )
    assert row is not None
    return row


def backfill_sources(user_id: int, dry_run: bool) -> int:
    configs = load_configs()
    if not dry_run:
        for name, cfg in configs.items():
            db.execute(
                "INSERT INTO sources (name, listings_url) VALUES (%s, %s) "
                "ON CONFLICT (name) DO UPDATE SET listings_url = EXCLUDED.listings_url",
                (name, cfg["JOB_LISTINGS_URL"]),
            )
            db.execute(
                "INSERT INTO user_sources (user_id, source) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (user_id, name),
            )
    return len(configs)


def backfill_filters(user_id: int, dry_run: bool) -> int:
    count = 0
    for name, spec in load_filter_specs().items():
        count += 1
        if dry_run:
            continue
        phash = compute_prompt_hash(
            build_custom_instructions(spec.prompt, spec.on_ambiguous)
        )
        db.execute(
            """
            INSERT INTO user_filters
                (user_id, name, prompt, on_ambiguous, fail_closed, enabled, prompt_hash)
            VALUES (%s, %s, %s, %s, %s, TRUE, %s)
            ON CONFLICT (user_id, name) DO NOTHING
            """,
            (user_id, name, spec.prompt, spec.on_ambiguous, spec.fail_closed, phash),
        )
    return count


def sheet_rows(sheet_id: str) -> List[List[str]]:
    from core.pittcsc_simplify import SHEET_NAME, authenticate_gspread

    client = authenticate_gspread()
    return client.open_by_key(sheet_id).worksheet(SHEET_NAME).get_all_values()


def backfill_sheet(user_id: int, sheet_id: str, dry_run: bool) -> Dict[str, int]:
    stats = {"rows": 0, "jobs": 0, "user_jobs": 0, "skipped": 0}
    for row in sheet_rows(sheet_id):
        url = _cell(row, "url")
        if not url.startswith(("http://", "https://")):
            stats["skipped"] += 1
            continue
        stats["rows"] += 1
        if dry_run:
            continue
        norm = normalize_url(url)
        job = db.query_one(
            """
            INSERT INTO jobs (url, raw_url, company, title, locations, terms, source,
                              uploaded_by, extraction_status)
            VALUES (%s, %s, %s, %s, %s, %s, 'sheet_import', %s, 'done')
            ON CONFLICT (url) DO UPDATE SET
                company = CASE WHEN jobs.company = '' THEN EXCLUDED.company ELSE jobs.company END,
                title = CASE WHEN jobs.title = '' THEN EXCLUDED.title ELSE jobs.title END
            RETURNING id
            """,
            (
                norm,
                url,
                _cell(row, "company"),
                _cell(row, "title"),
                [p.strip() for p in _cell(row, "location").split(",") if p.strip()],
                [p.strip() for p in _cell(row, "terms").split(",") if p.strip()],
                user_id,
            ),
        )
        assert job is not None
        stats["jobs"] += 1
        db.execute(
            """
            INSERT INTO user_jobs (user_id, job_id, status, date_applied, notes, size,
                                   recruiter, connection1, connection2, documents)
            VALUES (%(uid)s, %(jid)s, %(status)s, %(date_applied)s, %(notes)s, %(size)s,
                    %(recruiter)s, %(connection1)s, %(connection2)s, %(documents)s)
            ON CONFLICT (user_id, job_id) DO UPDATE SET
                status = COALESCE(user_jobs.status, EXCLUDED.status),
                date_applied = COALESCE(user_jobs.date_applied, EXCLUDED.date_applied),
                notes = COALESCE(user_jobs.notes, EXCLUDED.notes),
                size = COALESCE(user_jobs.size, EXCLUDED.size),
                recruiter = COALESCE(user_jobs.recruiter, EXCLUDED.recruiter),
                connection1 = COALESCE(user_jobs.connection1, EXCLUDED.connection1),
                connection2 = COALESCE(user_jobs.connection2, EXCLUDED.connection2),
                documents = COALESCE(user_jobs.documents, EXCLUDED.documents),
                updated_at = now()
            """,
            {
                "uid": user_id,
                "jid": job["id"],
                "status": _cell(row, "status") or None,
                "date_applied": _parse_date(_cell(row, "date_applied")),
                "notes": _cell(row, "notes") or None,
                "size": _cell(row, "size") or None,
                "recruiter": _cell(row, "recruiter") or None,
                "connection1": _cell(row, "connection1") or None,
                "connection2": _cell(row, "connection2") or None,
                "documents": _cell(row, "documents") or None,
            },
        )
        stats["user_jobs"] += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time backfill: sources + filters + sheet rows onto one user"
    )
    parser.add_argument("--sub", help="Authentik subject id (creates the user if missing)")
    parser.add_argument("--email", help="Find the user by email (after first login)")
    parser.add_argument(
        "--sheets", action="store_true", help="Also import sheet rows via gspread"
    )
    parser.add_argument(
        "--sheet-id", action="append", default=[],
        help="Sheet to import (repeatable; default: every sheet in configs.toml)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.sub and not args.email:
        parser.error("pass --sub and/or --email")

    db.init_schema()
    user = find_or_create_user(args.sub, args.email, args.dry_run)
    print(f"user: id={user['id']} sub={user['sub']} email={user.get('email')}")

    n_sources = backfill_sources(user["id"], args.dry_run)
    print(f"sources: {n_sources} upserted + subscribed")
    n_filters = backfill_filters(user["id"], args.dry_run)
    print(f"filters: {n_filters} imported (existing names left untouched)")

    if args.sheets:
        sheet_ids = args.sheet_id or sorted(
            {cfg["SHEET_ID"] for cfg in load_configs().values()}
        )
        for sheet_id in sheet_ids:
            stats = backfill_sheet(user["id"], sheet_id, args.dry_run)
            print(f"sheet {sheet_id}: {stats}")
    if args.dry_run:
        print("dry run: nothing was written")


if __name__ == "__main__":
    main()
