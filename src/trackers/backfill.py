"""DEPRECATED: one-time sheet-era backfill tool.

Superseded by the multi-user API in src/api/. Nothing in the running product
imports this module - it is a manual CLI for the Google-Sheets era, kept so
local sheet runs still work. Importing it warns.
"""
from __future__ import annotations

import warnings

warnings.warn(
    "trackers.backfill is the deprecated Google-Sheets CLI; the API in src/api/ replaced it",
    DeprecationWarning,
    stacklevel=2,
)


import argparse
import datetime
import sys
from typing import Any, Dict, List, Optional

from api import db
from core.configs import load_configs
from core.filters import build_custom_instructions, compute_prompt_hash, load_filter_specs
from core.urls import normalize_url

PRESETS: List[Dict[str, Any]] = [
    {
        "name": "Early-career software roles",
        "description": "Keeps software engineering roles at tech companies for early-career candidates; filters non-engineering roles and non-tech employers.",
        "prompt": """Candidate: early-career software engineer looking for backend, infrastructure, full-stack, systems, or DevOps roles.

KEEP (do not filter):
- Tech or tech-adjacent companies: software, AI/ML, developer tools/infrastructure, fintech/quant, startups; especially well-known, high-growth, or well-funded ones with strong engineering reputations.
- Software/backend/frontend/full-stack/platform/infrastructure/systems/SRE/DevOps/cloud engineering roles at any level up to staff.

FILTER OUT only when clearly:
- Non-engineering roles: product manager, business analyst, sales or sales engineer, recruiter, marketing.
- Non-tech employers: marketing agencies, consulting firms, staffing/recruiting agencies.
- Requires (not merely prefers) a Master's or PhD, or more than 3 years of experience.

When uncertain, KEEP.""",
    },
    {
        "name": "New grad / entry level only",
        "description": "Keeps only new-grad, entry-level, or junior roles; filters anything mid-level and above or requiring experience.",
        "prompt": """Keep only roles that are clearly new-grad, entry-level, junior, or early-career.

FILTER OUT if ANY of these apply (each is sufficient on its own):
- Mid / senior / lead / staff / principal level.
- Requires (not merely prefers) a Master's or PhD.
- Requires more than 1 year of professional experience (internships do not count as professional experience).

When the level is genuinely unstated and the role reads as open to new grads, KEEP.""",
    },
    {
        "name": "US or remote only",
        "description": "Filters roles located outside the US that are not remote.",
        "prompt": """KEEP roles that are US-based or remote.

FILTER OUT roles clearly located outside the US with no remote option.

When the location is unclear, KEEP.""",
    },
    {
        "name": "Top-tier companies only",
        "description": "A deliberately high bar: only big tech, elite AI labs, top quant/trading firms, and elite-reputation startups survive.",
        "prompt": """Keep ONLY software/engineering roles at elite, extremely high-paying or prestigious employers. The bar is intentionally high: if a company is not clearly and recognizably in the tiers below, that is a violation of these criteria -- filter it out.

KEEP only if the company is clearly one of:
- Big tech: Google, Meta, Apple, Amazon, Microsoft, Netflix, Nvidia.
- Elite AI labs: OpenAI, Anthropic, Google DeepMind, xAI, Mistral, Scale AI, Cohere, Thinking Machines, SSI.
- Top quant / HFT / trading firms: Citadel, Citadel Securities, Jane Street, Two Sigma, Hudson River Trading, Jump Trading, Optiver, DRW, IMC, DE Shaw, Five Rings, SIG, Point72, Akuna, Old Mission.
- Top-tier high-comp startups / unicorns with elite engineering reputations: Stripe, Databricks, Snowflake, Ramp, Plaid, Figma, Notion, Rippling, Coinbase, Anduril, Palantir, Airbnb, Uber, Cloudflare, Datadog, HashiCorp, Confluent, Vercel, Roblox, Pinterest, Dropbox, Robinhood, Brex, Mercury.

Also acceptable: a company not on the list ONLY if the posting makes its top-tier status unmistakable (e.g. clearly states elite/FAANG-level compensation, or is a widely recognized top engineering brand).

FILTER OUT everything else, including solid-but-not-elite mid-size companies, banks/insurers, consultancies, agencies, enterprise/legacy software, government/defense contractors (except Palantir/Anduril), and any company you do not clearly recognize as top-tier. When unsure whether a company is truly elite, FILTER IT OUT.""",
    },
    {
        "name": "Pay: $200k+ total comp (new grad)",
        "description": "Keeps roles plausibly paying $200k+ first-year total comp to a new grad; judges unstated pay from the employer's reputation.",
        "fail_closed": True,
        "on_ambiguous": "filter",
        "prompt": """Target: first-year total compensation (base + annual equity + amortized signing) of at least $200k for a new grad.

Base salary is the proxy for TC: a base of $150k/year or more implies roughly $200k+ TC once equity and bonus are counted, so treat $150k base as clearing the bar.

READING STATED PAY:
- Judge on the TOP of a stated base range. $129k-$195k -> judge $195k -> KEEP. $95k-$140k -> judge $140k -> FILTER OUT.
- If total compensation is stated directly rather than base, use $200k as the bar instead of $150k.
- Annualize hourly, daily or weekly rates at 2080 hrs/year before judging ($40/hr = $83k -> FILTER OUT).

WHEN NO PAY IS STATED:
Absence of a stated range is NOT ambiguity -- decide from the employer and the role. Judge whether this specific company plausibly pays $200k+ first-year TC to a new grad in this role.
- KEEP when the employer is known to pay at that level: big tech, elite AI labs, quant/HFT and trading firms, top-tier high-comp startups and unicorns, and well-funded engineering-led companies. Equity-heavy employers frequently post base only, or nothing at all.
- FILTER OUT when the employer and role clearly do not reach it: utilities, retail, healthcare systems, staffing and consulting agencies, government and defense contractors, non-profits, universities, banks and insurers outside their quant desks, and any hourly, field, technician, operations or support role.
- If you genuinely cannot place the employer, FILTER OUT.

FILTER OUT if ANY of these apply (each is sufficient on its own):
- Mid / senior / lead / staff / principal level, or requires (not merely prefers) a Master's or PhD, or more than 1 year of experience.""",
    },
    {
        "name": "Pay: $150k+ stated (new grad)",
        "description": "Strict: keeps only roles that explicitly state pay reaching $150k/year; unlisted pay is filtered.",
        "fail_closed": True,
        "on_ambiguous": "filter",
        "prompt": """Keep only roles whose stated pay reaches $150k/year for a new grad. Judge on the TOP of a stated range (a range of $120k-$160k clears; $100k-$140k does not). Annualize hourly, daily or weekly rates at 2080 hrs/year before judging. If no pay is listed at all, FILTER IT OUT.

FILTER OUT if ANY of these apply (each is sufficient on its own):
- Mid / senior / lead / staff / principal level, or requires (not merely prefers) a Master's or PhD, or more than 1 year of experience.""",
    },
    {
        "name": "Backend & infrastructure focus",
        "description": "Keeps backend, distributed-systems, platform, and DevOps work; filters mobile-only, embedded, QA-only, and non-engineering roles.",
        "prompt": """KEEP roles whose primary work is:
- Backend / API / microservices development.
- Distributed systems, systems programming, performance, or real-time/concurrent work.
- Infrastructure / platform / SRE / DevOps / cloud engineering.
- Data or search infrastructure, pipelines, streaming.
- Full-stack roles with a meaningful backend component.

FILTER OUT roles that are primarily:
- Mobile-only (iOS/Android with no backend), embedded/firmware, hardware/FPGA/ASIC.
- QA/test-only, pure UI/UX design, research-only data science or ML research.
- Non-engineering roles entirely.

When uncertain, KEEP.""",
    },
    {
        "name": "Industrial engineering",
        "description": "Keeps industrial, manufacturing, process, and quality engineering roles; filters unrelated roles and hourly technician work.",
        "prompt": """For candidates targeting industrial engineering and adjacent fields.

KEEP roles whose primary work is:
- Industrial engineering, process engineering, manufacturing engineering, production engineering.
- Quality engineering, continuous improvement, lean / Six Sigma, operational excellence.
- Plant engineering, facilities engineering, packaging engineering, methods/standards work.
- Supply chain engineering or operations engineering with a clear engineering component.

FILTER OUT only when clearly:
- A different discipline entirely: pure software roles, sales, marketing, finance, HR.
- Hourly technician/operator/assembler production-floor labor rather than an engineering role.
- Non-engineering analyst roles with no process/manufacturing content.

When uncertain, KEEP.""",
    },
    {
        "name": "Aerospace engineering",
        "description": "Keeps aerospace and space-industry engineering roles across propulsion, structures, GNC, avionics, and systems.",
        "prompt": """For candidates targeting aerospace and space-industry engineering.

KEEP roles whose primary work is:
- Aerospace, aeronautical, or astronautical engineering.
- Propulsion, aerodynamics, thermal, structures/stress, loads, materials for flight hardware.
- GNC (guidance, navigation, control), avionics, flight software, flight test, integration and test.
- Spacecraft, satellite, launch vehicle, UAV/drone, or aircraft programs - including systems engineering and manufacturing engineering roles at aerospace companies.

FILTER OUT only when clearly:
- Unrelated disciplines at aerospace companies: sales, marketing, finance, HR, generic IT support.
- Hourly technician/assembler production labor rather than an engineering role.
- Roles with no aerospace content at non-aerospace companies.

Note: do NOT filter for citizenship or clearance requirements here - a separate check handles that.

When uncertain, KEEP.""",
    },
    {
        "name": "Supply chain",
        "description": "Keeps supply chain, logistics, procurement, and planning roles; filters warehouse floor labor and unrelated work.",
        "prompt": """For candidates targeting supply chain and operations.

KEEP roles whose primary work is:
- Supply chain analyst/engineer/planner, demand or supply planning, S&OP.
- Logistics, transportation, distribution, fulfillment, network optimization.
- Procurement, sourcing, purchasing, supplier/vendor management, commodity management.
- Inventory management, materials planning, operations analyst roles with supply chain content.

FILTER OUT only when clearly:
- Hourly floor labor: warehouse associate, picker/packer, forklift operator, driver, material handler.
- Retail store operations or shift-supervisor roles.
- Unrelated disciplines: pure software roles, sales, marketing, finance, HR.

When uncertain, KEEP.""",
    },
    {
        "name": "No staffing agencies or consultancies",
        "description": "Filters postings from staffing/recruiting agencies, outsourcing shops, and consulting firms hiring for client work.",
        "prompt": """FILTER OUT postings where the employer is a staffing agency, recruiting firm, outsourcing/offshoring shop, or a consultancy hiring engineers for unnamed client projects (signs: the company describes itself as a staffing/talent partner, the client is unnamed, "W2/C2C" language, bench/rotational client placement).

KEEP direct postings from the company the engineer would actually work for, including consultancies hiring for their own product teams.

When uncertain, KEEP.""",
    },
]


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


def backfill_presets(dry_run: bool) -> int:
    for p in PRESETS:
        if dry_run:
            continue
        db.execute(
            """
            INSERT INTO filter_presets (name, description, prompt, on_ambiguous, fail_closed)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            """,
            (
                p["name"],
                p["description"],
                p["prompt"].strip(),
                p.get("on_ambiguous", "keep"),
                p.get("fail_closed", False),
            ),
        )
    return len(PRESETS)


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
    parser.add_argument(
        "--presets", action="store_true",
        help="Seed the standard filter presets (user-independent; existing names untouched)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.sub and not args.email and not args.presets:
        parser.error("pass --sub and/or --email (or --presets for the preset seed alone)")

    db.init_schema()
    if args.presets:
        n = backfill_presets(args.dry_run)
        print(f"presets: {n} seeded (existing names left untouched)")
    if not args.sub and not args.email:
        return
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
